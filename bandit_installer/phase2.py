"""Phase 2: Android partitions on the Lumia 1520, the Lumia 950 XL way.

Port of kit/phase2/phase2.py (run on an RM-937 on 2026-11-01), which does what the
Android4Lumia950 installer does with partition.sh and provision.sh, driven from the
PC over adb with a recovery booted in RAM from lk1st (adb as root):

  1. the 11 provisioning partitions of the 1520 are copied to the PC (dump_provisioned)
     and checked with sha256sum on the phone and against the mass storage backup;
  2. the new GPT (layout.py: identical, entry by entry, to what partition.sh leaves) is
     written with dd (backup copy at the end first, then the primary), read back and
     checked with an independent parser; MainOS, Data and MMOS disappear: Windows is gone;
  3. the kernel re-reads the table (otherwise the phone goes back to lk1st and the
     recovery is started again);
  4. format: ext4 with the recovery's mke2fs, FAT16 aboot (with lk1st, COPIED from EFIESP,
     where BootShim keeps loading it) and modem built on the PC, `persistent` zeroed (it
     is the raw FRP block: an ext4 there makes system_server crash in a loop);
  5. provisioning: modemst1/2, fsc, fsg, ssd from MODEM_FS1/2, MODEM_FSC, MODEM_FSG, SSD;
     wlan_mac.bin, bdaddr.txt and WCNSS_qcom_wlan_nv.bin from DPP into persist.

Every dd that writes to the eMMC needs conv=notrunc: toybox dd otherwise ftruncate()s
the output, which fails on a block device (EINVAL).
"""
import hashlib
import io
import json
import os
import tempfile
import time

from . import bcd, fat, gpt, layout
from .android import prefix_sha256
from .core import Abort, Cancelled, stamp
from .i18n import L

DISK = "/dev/block/mmcblk0"
SECTOR = 512
PROVISIONED = ("DPP", "MODEM_FS1", "MODEM_FS2", "MODEM_FSC", "MODEM_FSG", "SSD", "DDR", "DBI",
               "UEFI_BS_NV", "UEFI_RT_NV", "BACKUP_BS_NV")
MODEM_COPY = (("MODEM_FS1", "modemst1"), ("MODEM_FS2", "modemst2"), ("MODEM_FSC", "fsc"),
              ("MODEM_FSG", "fsg"), ("SSD", "ssd"))
TOOLS = ("dd", "sha256sum", "blockdev", "mount", "umount", "sync")
PARTS_CMD = 'for d in /sys/block/mmcblk0/mmcblk0p*; do echo "${d##*p} $(grep PARTNAME $d/uevent)"; done'


class Phase2Error(Abort):
    pass


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Run:
    """adb helpers over ctx.phone, with a folder for everything phase 2 saves."""

    def __init__(self, ctx, folder):
        self.ctx = ctx
        self.dir = folder
        os.makedirs(folder, exist_ok=True)

    def sh(self, cmd, timeout=120, check=True):
        rc, out = self.ctx.phone.adb(["shell", cmd], timeout=timeout)
        if check and rc != 0:
            raise Phase2Error(f"adb shell {cmd[:80]}… failed ({rc}): {out.strip()[-400:]}")
        return out

    def read(self, cmd, timeout=300):
        """Binary stdout of a shell command (exec-out); stderr must be redirected in cmd."""
        rc, data = self.ctx.phone.adb_bytes(["exec-out", cmd], timeout=timeout)
        if rc != 0:
            raise Phase2Error(f"adb exec-out {cmd[:80]} failed ({rc})")
        return data

    def push(self, local, remote):
        rc, out = self.ctx.phone.adb(["push", local, remote], timeout=300)
        if rc != 0:
            raise Phase2Error(f"adb push {os.path.basename(local)} failed: {out.strip()[-300:]}")

    def save(self, name, data):
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path


# ---------------------------------------------------------------------------
# reading the phone
# ---------------------------------------------------------------------------

def disk_sectors(r):
    out = r.sh("cat /sys/block/mmcblk0/size").strip()
    if not out.isdigit():
        raise Phase2Error(f"eMMC size not readable: {out!r}")
    return int(out)


class _TwoRegions(io.RawIOBase):
    def __init__(self, size, regions):
        self.size, self.regions, self.pos = size, regions, 0

    def readable(self):
        return True

    def seek(self, p, w=0):
        self.pos = p if w == 0 else self.pos + p if w == 1 else self.size + p
        return self.pos

    def read(self, n=-1):
        n = self.size - self.pos if n < 0 else n
        out = bytearray(n)
        for off, data in self.regions:
            a, b = max(off, self.pos), min(off + len(data), self.pos + n)
            if a < b:
                out[a - self.pos:b - self.pos] = data[a - off:b - off]
        self.pos += n
        return bytes(out)


def read_gpt(r, sectors):
    """(parsed GPT, RawGpt or None if it is not a phase-1 table, head bytes, tail bytes)."""
    head = r.read(f"dd if={DISK} bs=512 count=34 2>/dev/null")
    if len(head) != 34 * SECTOR:
        raise Phase2Error(f"short GPT read: {len(head)} bytes")
    tail = r.read(f"dd if={DISK} bs=512 skip={sectors - 33} count=33 2>/dev/null")
    if len(tail) != 33 * SECTOR:
        raise Phase2Error(f"short backup GPT read: {len(tail)} bytes")
    disk = _TwoRegions(sectors * SECTOR, [(0, head), ((sectors - 33) * SECTOR, tail)])
    parsed = gpt.read_gpt(disk)
    raw = layout.RawGpt.read(_TwoRegions(sectors * SECTOR, [(0, head), ((sectors - 33) * SECTOR, tail)]), sectors)
    return parsed, raw, head, tail


def partitions_by_name(r):
    """name -> partition number, as the kernel sees them now (uevent PARTNAME)."""
    m = {}
    for line in r.sh(PARTS_CMD).splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith("PARTNAME="):
            m[parts[1][9:]] = int(parts[0])
    return m


def has_android_layout(names):
    return all(n in names for n, _s, _f in layout.ANDROID_LAYOUT)


def check(r):
    """Read-only checks in the recovery: root, tools, nothing of the eMMC mounted."""
    ui = r.ctx.ui
    if r.sh("id -u").strip() != "0":
        raise Phase2Error(L("adb non è root nella recovery.", "adb is not root in the recovery."))
    out = r.sh("for t in " + " ".join(TOOLS + ("mke2fs", "make_ext4fs")) +
               "; do command -v $t >/dev/null 2>&1 || echo MISSING:$t; done; echo bandit-tools-checked")
    if "bandit-tools-checked" not in out:
        raise Phase2Error("tool check did not run")
    missing = {l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("MISSING:")}
    bad = [t for t in TOOLS if t in missing]
    if bad or {"mke2fs", "make_ext4fs"} <= missing:
        raise Phase2Error(L(f"Mancano nella recovery: {', '.join(bad or ['mke2fs'])}. Usa la recovery della ROM con --phase2-recovery.",
                            f"Missing in the recovery: {', '.join(bad or ['mke2fs'])}. Use the ROM's recovery with --phase2-recovery."))
    r.mkfs = "mke2fs -F -t ext4" if "mke2fs" not in missing else "make_ext4fs"
    fs = r.sh("cat /proc/filesystems")
    if "vfat" not in fs or "ext4" not in fs:
        raise Phase2Error(L("Il kernel della recovery non ha vfat o ext4.", "The recovery kernel lacks vfat or ext4."))
    mounts = r.sh("grep mmcblk0 /proc/mounts; echo bandit-mounts-checked", check=False)
    mounted = [l for l in mounts.splitlines() if "mmcblk0" in l and "bandit-mounts" not in l]
    if mounted:
        raise Phase2Error(L("Partizioni dell'eMMC montate nella recovery: " + "; ".join(mounted),
                            "eMMC partitions mounted in the recovery: " + "; ".join(mounted)))
    ui.ok(L("Recovery: adb root, strumenti presenti, eMMC non montata", "Recovery: adb root, tools present, eMMC not mounted"))


def read_efiesp(r, names, kit):
    """lk1st from EFIESP (bytes) and the BCD check result (True, False or None = unknown)."""
    ui = r.ctx.ui
    n = names.get("EFIESP")
    if n is None:
        raise Phase2Error(L("EFIESP non trovata.", "EFIESP not found."))
    img = fat.FatImage(r.read(f"dd if={DISK}p{n} bs=4096 2>/dev/null"))
    try:
        lk = img.read("emmc_appsboot.mbn")
    except fat.FatError:
        raise Phase2Error(L("lk1st (emmc_appsboot.mbn) non è su EFIESP: installa prima la catena di avvio.",
                            "lk1st (emmc_appsboot.mbn) is not on EFIESP: install the boot chain first."))
    digest = sha(lk)
    known = {e["sha256"]: e["id"] for e in kit.lk1st_entries()}
    for code, sec in kit.kit.meta.get("devices", {}).items():
        if code != kit.codename and any(e["sha256"] == digest for e in sec.get("lk1st", [])):
            raise Phase2Error(L(f"Su EFIESP c'è il lk1st di un altro telefono ({code}): reinstalla la catena di avvio con il telefono giusto.",
                                f"EFIESP holds the lk1st of another phone ({code}): install the boot chain again for the right phone."))
    if digest in known:
        ui.ok(L(f"lk1st su EFIESP = kit {known[digest]}", f"lk1st on EFIESP = kit {known[digest]}"))
    else:
        ui.warn(L(f"lk1st su EFIESP non è quello del kit (sha256 {digest[:16]}…): viene copiato così com'è.",
                  f"lk1st on EFIESP is not the kit's one (sha256 {digest[:16]}…): it is copied as it is."))
    r.save("emmc_appsboot.mbn", lk)
    android_default = None
    try:
        data = img.read("EFI/Microsoft/Boot/BCD")
        fd, tmp = tempfile.mkstemp(prefix="bcd-", dir=r.dir)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            view = bcd.BcdView(tmp)
            android_default = view.default() in [g.lower() for g in view.bootshim_entries()]
        finally:
            os.remove(tmp)
    except Exception as e:  # unreadable BCD: fall back to the saved options
        ui.detail(f"BCD: {e}")
    return lk, android_default


def plan_table(r, raw, sectors):
    ui = r.ctx.ui
    plan = layout.make_plan(raw)
    errors = layout.verify(plan)
    if errors:
        for e in errors:
            ui.error(e)
        raise Phase2Error(L("La tabella calcolata non supera la verifica: niente è stato scritto.",
                            "The computed table does not pass the check: nothing was written."))
    r.save("gpt-primary-new.bin", plan.after.primary())
    r.save("gpt-backup-new.bin", plan.after.backup())
    with open(os.path.join(r.dir, "plan.json"), "w", encoding="utf-8") as f:
        json.dump({"sectors": sectors, "backup_lba": plan.after.backup_lba_start(),
                   "removed": plan.removed, "created": plan.created}, f, indent=2)
    ui.show(L("Nuova tabella delle partizioni (verificata)", "New partition table (verified)"), plan.summary())
    return plan


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------

def dump_provisioned(r, names, mass_backup=None):
    ui = r.ctx.ui
    ui.info(L("Copia sul PC delle partizioni di provisioning…", "Copying the provisioning partitions to the PC…"))
    out = os.path.join(r.dir, "provisioned")
    os.makedirs(out, exist_ok=True)
    sums = []
    for i, name in enumerate(PROVISIONED, 1):
        ui.check_cancel()
        n = names.get(name)
        if n is None:
            ui.detail(f"{name}: not on this phone, skipped")
            continue
        data = r.read(f"dd if={DISK}p{n} bs=4096 2>/dev/null")
        on_phone = (r.sh(f"sha256sum {DISK}p{n}").split() or [""])[0]
        if sha(data) != on_phone:
            raise Phase2Error(L(f"{name}: la copia differisce dal telefono. Cavo o porta USB instabili.",
                                f"{name}: the copy differs from the phone. Unstable USB cable or port."))
        r.save(os.path.join("provisioned", name + ".bin"), data)
        note = ""
        mb = os.path.join(mass_backup, name + ".img") if mass_backup else None
        if mb and os.path.isfile(mb):
            with open(mb, "rb") as f:
                note = L(" = backup in mass storage", " = mass storage backup") if sha(f.read()) == on_phone \
                    else L(" (diversa dal backup in mass storage)", " (differs from the mass storage backup)")
        ui.progress(L("provisioning", "provisioning"), i, len(PROVISIONED))
        ui.detail(f"{name}: {len(data)} bytes, sha256 {on_phone[:16]}…{note}")
        sums.append(f"{on_phone}  {name}.bin")
    with open(os.path.join(out, "SHA256SUMS"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(sums) + "\n")
    if not any(s.endswith("  DPP.bin") for s in sums):
        raise Phase2Error(L("DPP non copiata: senza DPP non si ricavano MAC e calibrazione.",
                            "DPP not copied: without it the MAC addresses and calibration cannot be made."))
    ui.ok(L(f"{len(sums)} partizioni di provisioning copiate e verificate in {out}",
            f"{len(sums)} provisioning partitions copied and checked in {out}"))
    return out


def write_gpt(r, plan):
    ui = r.ctx.ui
    ui.info(L("Scrittura della nuova GPT…", "Writing the new GPT…"))
    prim, back = plan.after.primary(), plan.after.backup()
    pp, bp = r.save("gpt-primary-new.bin", prim), r.save("gpt-backup-new.bin", back)
    r.push(pp, "/tmp/gpt-primary.bin")
    r.push(bp, "/tmp/gpt-backup.bin")
    if r.sh("sha256sum /tmp/gpt-primary.bin /tmp/gpt-backup.bin").split()[0::2] != [sha(prim), sha(back)]:
        raise Phase2Error(L("I file della GPT copiati sul telefono sono diversi.", "The GPT files pushed to the phone differ."))
    start = plan.after.backup_lba_start()
    r.sh(f"dd if=/tmp/gpt-backup.bin of={DISK} bs=512 seek={start} conv=notrunc,fsync && "
         f"dd if=/tmp/gpt-primary.bin of={DISK} bs=512 seek=0 conv=notrunc,fsync && sync")
    got_p = r.read(f"dd if={DISK} bs=512 count={len(prim) // SECTOR} 2>/dev/null")
    got_b = r.read(f"dd if={DISK} bs=512 skip={start} count={len(back) // SECTOR} 2>/dev/null")
    if got_p != prim or got_b != back:
        raise Phase2Error(L("La GPT riletta è diversa da quella scritta. Non riavviare: copia i log e chiedi aiuto.",
                            "The GPT read back differs from what was written. Do not restart: keep the logs and ask for help."))
    errors = layout.verify(plan, got_p, got_b)
    if errors:
        raise Phase2Error("GPT read back fails the check: " + "; ".join(errors))
    ui.ok(L("GPT scritta, riletta identica e verificata", "GPT written, read back identical and checked"))


def reread(r, want):
    """Asks the kernel to re-read the table; returns the new name map or None."""
    out = r.sh(f"blockdev --rereadpt {DISK} 2>&1; echo bandit-reread-done", check=False)
    r.ctx.ui.detail("blockdev --rereadpt: " + out.replace("bandit-reread-done", "").strip())
    for _ in range(20):
        names = partitions_by_name(r)
        if set(want) <= set(names) and not ({"Data", "MainOS", "MMOS"} & set(names)):
            return names
        time.sleep(1)
    return None


def format_all(r, names, lk):
    ui = r.ctx.ui
    ui.info(L("Formattazione delle partizioni Android…", "Formatting the Android partitions…"))
    for i, (name, _size, fs) in enumerate(layout.ANDROID_LAYOUT, 1):
        ui.check_cancel()
        n = names[name]
        size = int(r.sh(f"cat /sys/block/mmcblk0/mmcblk0p{n}/size").strip()) * SECTOR
        if name == "persistent":
            # ro.frp.pst: the persistent data block must be raw zeros, not ext4
            r.sh(f"dd if=/dev/zero of={DISK}p{n} bs=1048576 count={size >> 20} conv=notrunc,fsync && sync", timeout=300)
            ui.detail(f"{name} (p{n}) zeroed (FRP block, not ext4)")
        elif fs == "ext4":
            out = r.sh(f"{r.mkfs} {DISK}p{n} 2>&1 | tail -2", timeout=1800)
            ui.detail(f"{name} (p{n}) ext4: {out.strip().splitlines()[-1] if out.strip() else 'done'}")
        elif fs == "fat16":
            files = {"emmc_appsboot.mbn": lk} if name == "aboot" else {}
            img = fat.make_fat(size, files, label=name.upper()[:11])
            path = r.save(f"{name}.img", img)
            r.push(path, f"/tmp/{name}.img")
            r.sh(f"dd if=/tmp/{name}.img of={DISK}p{n} bs=4096 conv=notrunc,fsync && sync && rm /tmp/{name}.img")
            back = r.read(f"dd if={DISK}p{n} bs=4096 count={len(img) // 4096} 2>/dev/null")
            if back != img:
                raise Phase2Error(f"{name}: FAT image read back differs")
            ui.detail(f"{name} (p{n}) FAT16 written" + (", lk1st inside" if files else ""))
        ui.progress(L("formattazione", "format"), i, len(layout.ANDROID_LAYOUT))
    ui.ok(L("Partizioni formattate; lk1st copiato in aboot (resta anche su EFIESP)",
            "Partitions formatted; lk1st copied to aboot (it stays on EFIESP too)"))


def provision(r, names, dpp_path):
    ui = r.ctx.ui
    ui.info(L("Provisioning: modem e indirizzi MAC…", "Provisioning: modem and MAC addresses…"))
    for src, dst in MODEM_COPY:
        r.sh(f"dd if={DISK}p{names[src]} of={DISK}p{names[dst]} bs=4096 conv=notrunc,fsync && sync")
        a = (r.sh(f"sha256sum {DISK}p{names[src]}").split() or [""])[0]
        size = int(r.sh(f"cat /sys/block/mmcblk0/mmcblk0p{names[src]}/size").strip()) * SECTOR
        b = prefix_sha256(r.ctx.phone, f"{DISK}p{names[dst]}", size)
        if not a or a != b:
            raise Phase2Error(f"{src} -> {dst}: copy differs")
        ui.detail(f"{src} -> {dst}: {size} bytes copied and checked")
    with open(dpp_path, "rb") as f:
        dpp = fat.FatImage(f.read())
    files = {}
    try:
        wlan = dpp.read("QCOM/WLAN.PROVISION")
        macs = [wlan[3 + 6 * i:9 + 6 * i].hex().upper() for i in range(4)]   # extract_wlan_mac
        files["wlan_mac.bin"] = "".join(f"Intf{i}MacAddress={m}\n" for i, m in enumerate(macs)).encode()
    except fat.FatError as e:
        ui.warn(L(f"MAC Wi-Fi non trovato nel DPP ({e}): Android userà un indirizzo casuale.",
                  f"Wi-Fi MAC not found in DPP ({e}): Android will use a random address."))
    try:
        bt = dpp.read("QCOM/BT.PROVISION")
        bd = bt[2:8].hex().upper()                                           # write_bdaddr
        files["bdaddr.txt"] = (":".join(bd[i:i + 2] for i in range(0, 12, 2)) + "\n").encode()
    except fat.FatError as e:
        ui.warn(L(f"Indirizzo Bluetooth non trovato nel DPP ({e}).", f"Bluetooth address not found in DPP ({e})."))
    try:
        # the phone's calibrated WCNSS NV in the Android format (init.bandit.wifi.sh uses it)
        files["WCNSS_qcom_wlan_nv.bin"] = dpp.read("QCOM/WLAN_CLPC.PROVISION")
    except fat.FatError as e:
        ui.warn(L(f"Calibrazione Wi-Fi non trovata nel DPP ({e}).", f"Wi-Fi calibration not found in DPP ({e})."))
    r.sh(f"mkdir -p /tmp/persist && mount -t ext4 {DISK}p{names['persist']} /tmp/persist")
    try:
        for fn, data in files.items():
            r.push(r.save(fn, data), f"/tmp/persist/{fn}")
        r.sh("sync")
    finally:
        r.sh("umount /tmp/persist", check=False)
    ui.ok(L(f"modemst1/2, fsc, fsg, ssd copiati; in persist: {', '.join(files) or 'niente'}",
            f"modemst1/2, fsc, fsg, ssd copied; in persist: {', '.join(files) or 'nothing'}"))


# ---------------------------------------------------------------------------
# the whole phase, resumable
# ---------------------------------------------------------------------------

ERASE_IT = [
    "Windows 10 Mobile viene CANCELLATO per sempre: le partizioni MainOS, Data e MMOS spariscono",
    "con tutte le app, le foto e i file del telefono. Al loro posto vanno le partizioni Android.",
    "Restano: catena di avvio, firmware, calibrazioni, modem (DPP, MODEM_FS*, UEFI…) e la modalità",
    "flash (volume su all'accensione).",
    "Per tornare a Windows serve un FFU con WPinternals (o Windows Device Recovery Tool).",
]
ERASE_EN = [
    "Windows 10 Mobile is ERASED for good: the MainOS, Data and MMOS partitions disappear with",
    "all apps, photos and files on the phone. The Android partitions take their place.",
    "Kept: boot chain, firmware, calibration and modem data (DPP, MODEM_FS*, UEFI…) and flash",
    "mode (volume up at power-on).",
    "Going back to Windows needs an FFU with WPinternals (or Windows Device Recovery Tool).",
]


def run_phase2(ctx, boot_recovery, recovery_image, label):
    """Runs phase 2 with the recovery `recovery_image`. Resumes after an interruption:
    with the Android table already written it formats and provisions again."""
    try:
        _run_phase2(ctx, boot_recovery, recovery_image, label)
    except (layout.LayoutError, gpt.GptError, fat.FatError, bcd.BcdError) as e:
        raise Phase2Error(L(f"Fase 2 fermata: {e}", f"Phase 2 stopped: {e}"))


def _run_phase2(ctx, boot_recovery, recovery_image, label):
    ui = ctx.ui
    ui.step(L("Fase 2: partizioni Android", "Phase 2: Android partitions"))
    folder = ctx.path("backups", f"phase2-{stamp()}")
    r = Run(ctx, folder)
    boot_recovery(ctx, recovery_image, label)
    check(r)
    sectors = disk_sectors(r)
    parsed, raw, head, tail = read_gpt(r, sectors)
    r.save("gpt-primary-before.bin", head)
    r.save("gpt-backup-before.bin", tail)
    ctx.state.set_phone(parsed.disk_guid, {})
    names = partitions_by_name(r)
    on_disk = parsed.names()

    if has_android_layout(on_disk):
        ui.info(L("La tabella Android è già scritta (fase 2 interrotta): riprendo da formattazione e provisioning.",
                  "The Android table is already written (phase 2 was interrupted): resuming with format and provisioning."))
        prov = ctx.state.done("p2-provisioned")
        dpp_path = os.path.join(prov["path"], "DPP.bin") if prov else None
        if not dpp_path or not os.path.isfile(dpp_path):
            dpp_path = None
    else:
        plan = plan_table(r, raw, sectors)
        lk, android_default = read_efiesp(r, names, ctx.kit)
        chain = ctx.state.done("chain") or {}
        if android_default is None and chain.get("options"):
            o = chain["options"]
            android_default = o.get("default") == "android" or bool(o.get("android_only"))
        if android_default is False:
            raise Abort(L("Nel BCD Android (lk1st) non è il sistema predefinito: senza Windows il telefono non partirebbe. "
                          "Rifai «Catena di avvio» in mass storage con «Avvia Android all'accensione». Niente è stato scritto.",
                          "Android (lk1st) is not the default entry in the BCD: without Windows the phone would not start. "
                          "Run \"Boot chain\" again in mass storage with \"Start Android at power-on\". Nothing was written."))
        if android_default is None:
            ui.warn(L("Non riesco a verificare che lk1st sia il predefinito nel BCD.",
                      "Could not check that lk1st is the default boot entry in the BCD."))
        backup = ctx.state.done("backup")
        mass = os.path.join(backup["path"], "partizioni") if backup and backup.get("path") else None
        prov_dir = dump_provisioned(r, names, mass)
        ctx.state.mark("p2-provisioned", path=prov_dir)
        dpp_path = os.path.join(prov_dir, "DPP.bin")
        lines = list(L(ERASE_IT, ERASE_EN))
        if backup and backup.get("full"):
            lines.append(L(f"Immagine completa del telefono: {backup['path']}", f"Full image of the phone: {backup['path']}"))
        else:
            lines.append(L("ATTENZIONE: non c'è un backup completo del telefono fatto da questo installer.",
                           "WARNING: there is no full backup of the phone made by this installer."))
        lines.append(L(f"Copia del provisioning: {prov_dir}", f"Provisioning copy: {prov_dir}"))
        ui.show(L("Prima di cancellare Windows", "Before erasing Windows"), lines)
        if not ui.confirm(L("Cancellare Windows e creare le partizioni Android?", "Erase Windows and create the Android partitions?"),
                          L("Non si torna indietro senza un FFU. Scrivi la parola per confermare.",
                            "There is no way back without an FFU. Type the words to confirm."),
                          L("cancella Windows", "erase Windows"), L("annulla", "cancel"),
                          word=L("CANCELLA WINDOWS", "ERASE WINDOWS"), danger=True):
            raise Cancelled()
        write_gpt(r, plan)
        ctx.state.mark("p2-gpt", folder=folder)
        r.lk = lk
        names = None

    want = [n for n, _s, _f in layout.ANDROID_LAYOUT]
    if names is None or not set(want) <= set(names):
        names = reread(r, want)
        if names is None:
            ui.info(L("Il kernel non vede ancora le nuove partizioni: riavvio in lk1st e nuovo avvio della recovery.",
                      "The kernel does not see the new partitions yet: restarting into lk1st and starting the recovery again."))
            ctx.phone.adb(["reboot", "bootloader"], timeout=30)
            boot_recovery(ctx, recovery_image, label)
            check(r)
            names = partitions_by_name(r)
            if not set(want) <= set(names):
                raise Phase2Error(L("Anche dopo il riavvio il kernel non vede le partizioni Android.",
                                    "Even after the restart the kernel does not see the Android partitions."))
    ui.ok(L(f"Il kernel vede le {len(want)} partizioni Android", f"The kernel sees the {len(want)} Android partitions"))

    lk = getattr(r, "lk", None)
    if lk is None:
        lk = read_efiesp(r, names, ctx.kit)[0]
    if dpp_path is None:
        ui.info(L("Nessuna copia del DPP in questo PC: la rileggo dal telefono.", "No copy of DPP on this PC: reading it from the phone."))
        dpp_path = os.path.join(dump_provisioned(r, names), "DPP.bin")
    format_all(r, names, lk)
    provision(r, names, dpp_path)
    ctx.give_back(folder)
    ctx.state.mark("phase2", folder=folder)
    ui.ok(L(f"Fase 2 completata. File in {folder} (dati del telefono: non pubblicarli).",
            f"Phase 2 done. Files in {folder} (phone data: do not publish them)."))
