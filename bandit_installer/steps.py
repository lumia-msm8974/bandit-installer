"""The installer's tasks. Front-end independent: they talk to ctx.ui only.

Boot chain on the MSM8974 Lumias (1520, 930):
  UEFI Nokia -> bootmgr -> BCD entry "Android (lk1st)" -> BootShim -> lk1st (fastboot)
"""
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import time

from . import android, bcd, devices, gpt, images, phase2, sysdeps
from .core import Abort, Cancelled, human_size, stamp
from .i18n import L
from .phone import mode_text

SMALL_PART = 512 * 1024 * 1024
CHUNK = 4 * 1024 * 1024
# Partitions fastboot may write. Everything Windows/Qualcomm owns is refused.
FLASHABLE = ("boot", "recovery", "system", "vendor", "cache", "userdata", "misc", "persist")


def current_device(state, opts=None):
    """Codename in use: --device, else the one chosen last (saved in state.json), else bandit."""
    c = (opts or {}).get("device") or state.data.get("device") or devices.DEFAULT
    return c if c in devices.DEVICES else devices.DEFAULT


class Ctx:
    def __init__(self, root, work, system, phone, poller, state, kit, ui, opts=None):
        self.root = root
        self.opts = dict(opts or {})   # rom, rom_sha256, twrp, phase2_recovery
        self.rom = None
        self.twrp = None
        self.work = work
        self.system = system
        self.phone = phone
        self.poller = poller
        self.state = state
        self.device = devices.get(current_device(state, self.opts))
        self.kit = kit.for_device(self.device.codename)
        self.ui = ui
        self.disk = None
        for d in ("backups", "logs", "private", "boot"):
            os.makedirs(os.path.join(work, d), exist_ok=True)

    def path(self, *p):
        return os.path.join(self.work, *p)

    def give_back(self, path):
        self.system.give_back(path)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def ci_find(root, rel):
    """Case-insensitive lookup of rel ('a/b/c') under root (FAT keeps any case)."""
    cur = root
    for part in rel.split("/"):
        try:
            names = os.listdir(cur)
        except OSError:
            return None
        hit = next((n for n in names if n.lower() == part.lower()), None)
        if hit is None:
            return None
        cur = os.path.join(cur, hit)
    return cur


def read_range(ctx, f, offset, size, out, label, hashers=()):
    """Copies size bytes at offset from raw file f to out, with progress."""
    ui = ctx.ui
    f.seek(offset)
    done = 0
    t0 = time.time()
    last = 0
    while done < size:
        n = min(CHUNK, size - done)
        b = f.read(n)
        if not b:
            raise Abort(L(f"lettura interrotta a {done} di {size} byte", f"read stopped at {done} of {size} bytes"))
        out.write(b)
        for h in hashers:
            h.update(b)
        done += len(b)
        now = time.time()
        if now - last > 0.3 or done == size:
            last = now
            speed = done / max(now - t0, 0.001)
            eta = (size - done) / speed if speed else 0
            ui.progress(f"{label} — {human_size(speed)}/s, {int(eta // 60)}:{int(eta % 60):02d}", done, size)
    ui.progress(label, size, size)


def write_manifest(folder, name="SHA256SUMS"):
    lines = []
    for base, _dirs, files in os.walk(folder):
        for fn in sorted(files):
            p = os.path.join(base, fn)
            rel = os.path.relpath(p, folder).replace(os.sep, "/")
            if rel == name:
                continue
            lines.append(f"{images.sha256_file(p)}  ./{rel}")
    with open(os.path.join(folder, name), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(sorted(lines, key=lambda l: l[66:])) + "\n")


def check_manifest(folder, manifest):
    """Returns the list of files that differ from a sha256sum manifest."""
    bad = []
    with open(manifest, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if len(line) < 67:
                continue
            h, rel = line[:64], line[66:].lstrip("*")
            rel = rel[2:] if rel.startswith("./") else rel
            p = ci_find(folder, rel)
            if not p or images.sha256_file(p) != h:
                bad.append(rel)
    return bad


def fsync_file(path):
    try:
        with open(path, "rb+") as f:
            os.fsync(f.fileno())
    except OSError:
        pass


def flush_all():
    if hasattr(os, "sync"):
        os.sync()


class Compressed:
    """Write-only compressed file: zstd (Python 3.14 or the zstd tool) or gzip."""

    def __init__(self, base):
        self.proc = None
        try:
            from compression import zstd  # Python 3.14+
            self.path = base + ".zst"
            self.f = zstd.open(self.path, "wb", level=3)
            self.kind = "zstd"
            return
        except ImportError:
            pass
        exe = shutil.which("zstd")
        if exe:
            self.path = base + ".zst"
            self.proc = subprocess.Popen([exe, "-T0", "-3", "-q", "-f", "-o", self.path], stdin=subprocess.PIPE)
            self.f = self.proc.stdin
            self.kind = "zstd"
            return
        self.path = base + ".gz"
        self.f = gzip.open(self.path, "wb", compresslevel=1)
        self.kind = "gzip"

    def write(self, b):
        self.f.write(b)

    def close(self):
        self.f.close()
        if self.proc and self.proc.wait() != 0:
            raise Abort("zstd failed")


# ---------------------------------------------------------------------------
# requirements and tools
# ---------------------------------------------------------------------------

def requirements(ctx):
    """[(name, ok, hint)] for the home page."""
    out = []
    ctx.phone.refresh_tools()
    out.append(("fastboot", bool(ctx.phone.fastboot_exe), ctx.phone.fastboot_exe or L(
        "Android SDK Platform Tools: scaricabili dall'installer", "Android SDK Platform Tools: the installer can download them")))
    out.append(("adb", bool(ctx.phone.adb_exe), ctx.phone.adb_exe or ""))
    for name, hint in ctx.system.missing_requirements():
        out.append((name, False, hint))
    if ctx.system.bcd_backend():
        out.append(("BCD", True, ctx.system.bcd_backend()))
    dev = ctx.device
    if dev.twrp_name:
        twrp = ctx.opts.get("twrp") or os.path.join(ctx.root, "twrp", dev.twrp_name)
        out.append(("TWRP", os.path.isfile(twrp), twrp if os.path.isfile(twrp) else L(
            f"{dev.twrp_name}: l'installer la scarica quando serve", f"{dev.twrp_name}: the installer downloads it when needed")))
    else:
        out.append(("TWRP", False, L(f"non ancora rilasciata per il {dev.title}", f"not released yet for the {dev.title}")))
    import glob as _glob
    roms = [ctx.opts["rom"]] if ctx.opts.get("rom") else sorted(_glob.glob(os.path.join(ctx.root, "rom", dev.rom_glob)))
    out.append(("ROM", bool(roms), ", ".join(os.path.basename(r) for r in roms) if roms else L(
        "metti lo zip e SHA256SUMS nella cartella rom", "put the zip and SHA256SUMS in the rom folder")))
    out.append((L("amministratore", "administrator"), ctx.system.is_admin(),
                "" if ctx.system.is_admin() else L("serve per leggere e scrivere il telefono", "needed to read and write the phone")))
    return out


def act_tools(ctx):
    ui = ctx.ui
    ui.step(L("Strumenti", "Tools"))
    for name, ok, hint in requirements(ctx):
        (ui.ok if ok else ui.warn)(f"{name}: {hint or 'OK'}")
    if ctx.phone.fastboot_exe and ctx.phone.adb_exe:
        return
    url = ctx.system.platform_tools_url()
    if not ui.confirm(L("Scaricare Platform Tools?", "Download Platform Tools?"),
                      L(f"fastboot e adb di Google (circa 7 MB) da\n{url}\nin {ctx.system.tool_dir()}.",
                        f"Google's fastboot and adb (about 7 MB) from\n{url}\ninto {ctx.system.tool_dir()}."),
                      L("scarica", "download"), L("annulla", "cancel")):
        return
    import urllib.request
    dest = ctx.path("platform-tools.zip")
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            b = r.read(256 * 1024)
            if not b:
                break
            out.write(b)
            done += len(b)
            ui.progress(L("download", "download"), done, total or None)
    ctx.system.install_platform_tools(dest)
    os.remove(dest)
    ctx.give_back(os.path.join(ctx.root, "tools"))
    ctx.phone.refresh_tools()
    if not ctx.phone.fastboot_exe:
        raise Abort(L("fastboot non trovato dopo l'estrazione", "fastboot not found after extraction"))
    ui.ok(L(f"Platform Tools in {ctx.system.tool_dir()}", f"Platform Tools in {ctx.system.tool_dir()}"))


def need_admin(ctx):
    if not ctx.system.is_admin():
        raise Abort(L("Serve l'esecuzione come amministratore (root su Linux). Riavvia l'installer senza --no-elevate.",
                      "Administrator (root on Linux) rights are needed. Start the installer without --no-elevate."))


# ---------------------------------------------------------------------------
# phone disk (mass storage)
# ---------------------------------------------------------------------------

# The unlock and the switch to mass storage are the preparation, done with WPinternals
# on Windows before the installer starts (see the guide): the installer never drives
# WPinternals, it only checks the result.
MASS_HELP_IT = [
    "Collega il telefono già preparato: sbloccato e in mass storage (simbolo USB sullo schermo).",
    "La preparazione si fa prima, con WPinternals su un PC Windows (guida, «Preparazione»);",
    "poi puoi spostare il cavo su qualsiasi PC, anche Linux: il telefono resta in mass storage",
    "fino al riavvio.",
]
MASS_HELP_EN = [
    "Connect the prepared phone: unlocked and in mass storage mode (USB symbol on the screen).",
    "The preparation is done first, with WPinternals on a Windows PC (guide, \"Preparation\");",
    "then you may move the cable to any PC, Linux too: the phone stays in mass storage until",
    "it restarts.",
]
NOT_UNLOCKED_IT = ("Il telefono non è sbloccato (manca IS_UNLOCKED): fai prima la preparazione con WPinternals su Windows "
                   "(sblocco del bootloader, poi «Switch to Mass Storage Mode»), poi ricollegalo qui. "
                   "Nessuna scrittura è stata fatta.")
NOT_UNLOCKED_EN = ("The phone is not unlocked (no IS_UNLOCKED): do the preparation with WPinternals on Windows first "
                   "(bootloader unlock, then \"Switch to Mass Storage Mode\"), then connect it here again. "
                   "Nothing has been written.")


def find_lumia_disks(ctx):
    disks = []
    for d in ctx.system.list_disks():
        sysdeps.read_disk_gpt(ctx.system, d)
        if d.gpt and d.gpt.looks_like_lumia():
            disks.append(d)
    return disks


def wait_mass_storage(ctx):
    ui = ctx.ui
    if find_lumia_disks(ctx):
        return
    ui.show(L("Telefono in mass storage", "Phone in mass storage mode"), L(MASS_HELP_IT, MASS_HELP_EN))

    def poll():
        disks = find_lumia_disks(ctx)
        if disks:
            return disks
        s = ctx.phone.snapshot(use_tools=False)
        ui.status(L("In attesa del telefono in mass storage… ora: ", "Waiting for the phone in mass storage… now: ")
                  + mode_text(s.mode))
        return None

    ui.wait(poll, timeout=None, interval=2)


def select_disk(ctx, need_unlocked=False):
    ui = ctx.ui
    need_admin(ctx)
    wait_mass_storage(ctx)
    disks = find_lumia_disks(ctx)
    if len(disks) == 1:
        d = disks[0]
    else:
        v = ui.choose(L("Quale telefono?", "Which phone?"), L("Più dischi Lumia collegati.", "More than one Lumia disk."),
                      [{"value": i, "title": str(x.ident), "sub": x.label()} for i, x in enumerate(disks)])
        if v is None:
            raise Cancelled()
        d = disks[v]
    g = d.gpt
    unlocked = g.by_name("IS_UNLOCKED") is not None
    model = disk_model(ctx, d)
    lines = [d.label(),
             L(f"modello {model or 'non leggibile'} (DPP)", f"model {model or 'unreadable'} (DPP)"),
             L(f"GPT {g.disk_guid}, {len(g.partitions)} partizioni", f"GPT {g.disk_guid}, {len(g.partitions)} partitions"),
             (L("sbloccato con WPinternals (IS_UNLOCKED presente)", "unlocked with WPinternals (IS_UNLOCKED present)")
              if unlocked else L("NON sbloccato: manca IS_UNLOCKED", "NOT unlocked: IS_UNLOCKED missing")),
             L("partizioni Android presenti", "Android partitions present") if g.has_android()
             else L("nessuna partizione Android (Windows intatto)", "no Android partitions (Windows intact)")]
    ui.show(L("Telefono trovato", "Phone found"), lines)
    android.check_model(ctx, model, "DPP")
    if (need_unlocked or getattr(ctx, "require_unlocked", False)) and not unlocked:
        raise Abort(L(NOT_UNLOCKED_IT, NOT_UNLOCKED_EN))
    ctx.disk = d
    ctx.state.set_phone(g.disk_guid, {"size": d.size, "model": d.model, "unlocked": unlocked,
                                      "device": ctx.device.codename, "rm": model})
    return d


def disk_model(ctx, d):
    """RM code from DPP Nokia/product.dat, read raw from the phone in mass storage, or None."""
    p = d.gpt.by_name("DPP") if d.gpt else None
    if p is None or p.size > 64 * 1024 * 1024:
        return None
    try:
        with ctx.system.open_raw(d) as raw:
            raw.seek(p.offset)
            data = raw.read(p.size)
    except OSError:
        return None
    return android.model_from_dpp(data)


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------

def act_backup(ctx, full=None):
    ui = ctx.ui
    ui.step(L("Backup del telefono", "Phone backup"))
    d = select_disk(ctx)
    free = ctx.system.free_space(ctx.work)
    if full is None:
        r = ui.form(L("Backup", "Backup"),
                    L(f"Spazio libero in {ctx.work}: {human_size(free)}. Le partizioni piccole (GPT, DPP, modem, UEFI, EFIESP…) si salvano sempre.",
                      f"Free space in {ctx.work}: {human_size(free)}. Small partitions (GPT, DPP, modem, UEFI, EFIESP…) are always saved."),
                    [{"key": "full", "type": "toggle", "value": True,
                      "label": L("Immagine completa compressa", "Full compressed image"),
                      "sub": L(f"{human_size(d.size)} letti, 20-60 minuti; serve per tornare a Windows esattamente com'era",
                               f"reads {human_size(d.size)}, 20-60 minutes; needed to get Windows back exactly as it was")}],
                    L("inizia", "start"))
        if r is None:
            raise Cancelled()
        full = bool(r["full"])
    if full and free < d.size // 3:
        ui.warn(L("Lo spazio libero potrebbe non bastare per l'immagine completa.", "Free space may not be enough for the full image."))
    busy = ctx.system.unmount_all(d)
    if busy:
        raise Abort(L(f"Partizioni ancora montate: {', '.join(busy)}. Chiudi le finestre che le usano.",
                      f"Partitions still mounted: {', '.join(busy)}. Close the windows using them."))
    out = ctx.path("backups", f"telefono-{stamp()}")
    os.makedirs(os.path.join(out, "partizioni"))
    g = d.gpt
    info = {"disk": str(d.ident), "size": d.size, "model": d.model, "disk_guid": g.disk_guid,
            "partitions": [{"index": p.index, "name": p.name, "first": p.first, "last": p.last,
                            "type": p.type_guid, "guid": p.part_guid, "attrs": p.attrs} for p in g.partitions]}
    with open(os.path.join(out, "gpt.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)
    with ctx.system.open_raw(d) as raw:
        with open(os.path.join(out, "gpt-primaria.bin"), "wb") as o:
            read_range(ctx, raw, 0, 34 * gpt.SECTOR, o, "GPT")
        with open(os.path.join(out, "gpt-secondaria.bin"), "wb") as o:
            read_range(ctx, raw, d.size - 33 * gpt.SECTOR, 33 * gpt.SECTOR, o, "GPT backup")
        small = [p for p in g.partitions if p.size < SMALL_PART]
        hashes = {}
        for i, p in enumerate(small):
            ui.check_cancel()
            h = hashlib.sha256()
            with open(os.path.join(out, "partizioni", f"{p.name}.img"), "wb") as o:
                read_range(ctx, raw, p.offset, p.size, o, f"{i + 1}/{len(small)} {p.name}", (h,))
            hashes[p.name] = h.hexdigest()
        ui.ok(L(f"{len(small)} partizioni piccole salvate", f"{len(small)} small partitions saved"))
        # read them again: a USB glitch must not go unnoticed
        ui.info(L("Rilettura di controllo delle partizioni piccole…", "Reading the small partitions again to check them…"))
        for p in small:
            h = hashlib.sha256()
            read_range(ctx, raw, p.offset, p.size, _Null(), L(f"controllo {p.name}", f"check {p.name}"), (h,))
            if h.hexdigest() != hashes[p.name]:
                raise Abort(L(f"{p.name}: due letture diverse. Cavo o porta USB instabili.",
                              f"{p.name}: two different reads. Unstable USB cable or port."))
        ui.ok(L("letture identiche", "reads match"))
        if full:
            c = Compressed(os.path.join(out, f"lumia-{ctx.device.codename}-full.img"))
            ui.info(L(f"Immagine completa ({c.kind})…", f"Full image ({c.kind})…"))
            try:
                read_range(ctx, raw, 0, d.size, c, L("immagine completa", "full image"))
            finally:
                c.close()
            ui.ok(os.path.basename(c.path) + " " + human_size(os.path.getsize(c.path)))
    write_manifest(out)
    ctx.give_back(out)
    ctx.state.mark("backup", path=out, full=bool(full))
    ui.ok(L(f"Backup in {out}", f"Backup in {out}"))
    ui.warn(L("Contiene dati personali e del modem (DPP, MODEM_FS*): non pubblicarlo.",
              "It contains personal and modem data (DPP, MODEM_FS*): do not publish it."))


class _Null:
    def write(self, b):
        pass


# ---------------------------------------------------------------------------
# firmware
# ---------------------------------------------------------------------------

FW_SUFFIXES = (".mbn", ".acdb", ".mdt")


def _is_fw(name):
    n = name.lower()
    if n.endswith(FW_SUFFIXES) or "wlan" in n or "wcnss" in n:
        return True
    return len(n) > 4 and n[-4:-2] == ".b" and n[-2:].isdigit()


def act_firmware(ctx):
    ui = ctx.ui
    ui.step(L("Estrazione firmware da Windows", "Firmware extraction from Windows"))
    d = select_disk(ctx)
    g = d.gpt
    out = ctx.path("private", f"firmware-{stamp()}")
    for sub in ("stock", "split", "acpi", "registro"):
        os.makedirs(os.path.join(out, sub), exist_ok=True)
    with ctx.system.mounted(d, g.by_name("PLAT"), "vfat") as plat:
        ui.info(L(f"PLAT (ACPI) da {plat}", f"PLAT (ACPI) from {plat}"))
        shutil.copytree(plat, os.path.join(out, "acpi"), dirs_exist_ok=True)
    with ctx.system.mounted(d, g.by_name("MainOS"), "ntfs") as mainos:
        win = ci_find(mainos, "Windows")
        if not win:
            raise Abort(L("Cartella Windows assente in MainOS", "No Windows folder in MainOS"))
        ui.info(L(f"Ricerca firmware in {win}…", f"Looking for firmware in {win}…"))
        n = 0
        for base, dirs, files in os.walk(win):
            ui.check_cancel()
            dirs[:] = [x for x in dirs if x.lower() != "winsxs"]
            for fn in files:
                if _is_fw(fn):
                    src = os.path.join(base, fn)
                    dst = os.path.join(out, "stock", os.path.relpath(src, mainos))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    n += 1
                    ui.status(os.path.relpath(src, mainos))
        ui.ok(L(f"{n} file di firmware e calibrazione", f"{n} firmware and calibration files"))
        for hive in ("SYSTEM", "SOFTWARE"):
            p = ci_find(win, f"System32/config/{hive}")
            if p:
                shutil.copy2(p, os.path.join(out, "registro", hive))
            else:
                ui.warn(L(f"registro {hive} non trovato", f"registry {hive} not found"))
    stock = os.path.join(out, "stock")
    for win_name, pil in images.PIL_NAMES:
        hit = None
        for base, _d, files in os.walk(stock):
            for fn in files:
                if fn.lower() == win_name + ".mbn":
                    hit = os.path.join(base, fn)
        if not hit:
            ui.warn(f"{win_name}.mbn " + L("non trovato", "not found"))
            continue
        try:
            files = images.pil_split(hit, os.path.join(out, "split"), pil)
            if len(files) == 1:
                ui.ok(f"{win_name}.mbn -> {pil}.b00")
            else:
                ui.ok(f"{win_name}.mbn -> {pil}.mdt + {len(files) - 1} .bNN")
        except ValueError as e:
            ui.warn(f"{win_name}.mbn: {e}")
    write_manifest(out)
    ctx.give_back(out)
    ctx.state.mark("firmware", path=out, files=n)
    ui.ok(L(f"Firmware in {out}", f"Firmware in {out}"))
    ui.warn(L("File Nokia/Microsoft e dati del telefono: non vanno nei repository pubblici.",
              "Nokia/Microsoft files and phone data: keep them out of public repositories."))


# ---------------------------------------------------------------------------
# boot chain
# ---------------------------------------------------------------------------

def _efiesp_layout(root):
    lay = {"bcd": ci_find(root, "EFI/Microsoft/Boot/BCD"),
           "bootdir": ci_find(root, "Windows/System32/Boot"),
           "bootarm": ci_find(root, "EFI/Boot/bootarm.efi")}
    missing = [k for k, v in lay.items() if not v]
    if missing:
        raise Abort(L(f"EFIESP inattesa, mancano: {', '.join(missing)}", f"Unexpected EFIESP, missing: {', '.join(missing)}"))
    return lay


def chain_options(ctx, view, for_full=False):
    """Boot options. In the Full mode Windows is erased afterwards: Android only, no menu."""
    ui = ctx.ui
    variants = [{"value": e["id"], "title": ctx.kit.title(e),
                 "sub": ctx.kit.note(e) + ("" if ctx.kit.check(e) else L(" — FILE MANCANTE O DIVERSO", " — FILE MISSING OR CHANGED"))}
                for e in ctx.kit.lk1st_entries()]
    has_win = view.windows() is not None and not for_full
    note = L("Il tasto volume su all'accensione apre sempre la modalità flash: è la via di recupero e non viene mai cambiato.",
             "Holding volume up at power-on always opens flash mode: it is the way back and is never changed.")
    if for_full:
        note += "\n" + L("Installazione completa: Windows verrà cancellato, quindi Android parte all'accensione ed è l'unico sistema.",
                         "Full install: Windows will be erased, so Android starts at power-on and is the only system.")
    r = ui.form(
        L("Opzioni di avvio", "Boot options"), note,
        [
            {"key": "variant", "type": "select", "label": "lk1st", "value": variants[0]["value"], "options": variants},
            {"key": "android_default", "type": "toggle", "value": True, "disabled": for_full,
             "label": L("Avvia Android all'accensione", "Start Android at power-on"),
             "sub": L("altrimenti parte Windows e Android resta nel menu", "otherwise Windows starts and Android stays in the menu")},
            {"key": "voldown", "type": "toggle", "value": has_win, "disabled": not has_win,
             "label": L("Volume giù → l'altro sistema", "Volume down → the other system"),
             "sub": L("al posto di «Reset My Phone»", "instead of \"Reset My Phone\"")},
            {"key": "android_only", "type": "toggle", "value": not has_win, "disabled": for_full,
             "label": L("Solo Android", "Android only"),
             "sub": L("quando Windows è già stato rimosso", "when Windows has already been removed")},
            {"key": "timeout", "type": "number", "value": 0, "label": L("Attesa del menu (s)", "Menu timeout (s)"),
             "sub": L("0 = nessun menu", "0 = no menu")},
        ],
        L("anteprima", "preview"))
    if r is None:
        raise Cancelled()
    opts = bcd.Options(default="android" if r["android_default"] else "windows",
                       timeout=max(0, min(30, int(r.get("timeout") or 0))),
                       windows_key="voldown" if r["voldown"] and not r["android_only"] else "none",
                       android_only=bool(r["android_only"]))
    return r["variant"], opts


def act_chain(ctx, for_full=False):
    ui = ctx.ui
    ui.step(L("Catena di avvio: BootShim + lk1st", "Boot chain: BootShim + lk1st"))
    backend = ctx.system.bcd_backend()
    if not backend:
        raise Abort(L("Manca lo strumento per il BCD: ", "No BCD tool: ")
                    + "; ".join(f"{n} ({h})" for n, h in ctx.system.missing_requirements()))
    shim = ctx.kit.bootshim()
    if not ctx.kit.check(shim):
        raise Abort(L("BootShim del kit mancante o diverso dall'originale elf-loader-pre3.",
                      "The kit's BootShim is missing or not the original elf-loader-pre3."))
    d = select_disk(ctx, need_unlocked=True)
    part = d.gpt.by_name("EFIESP")
    with ctx.system.mounted(d, part, "vfat", write=True) as root:
        lay = _efiesp_layout(root)
        before = bcd.BcdView(lay["bcd"])
        ui.show(L("BCD attuale", "Current BCD"), before.summary())
        variant, opts = chain_options(ctx, before, for_full)
        lk = ctx.kit.lk1st(variant)
        if not ctx.kit.check(lk):
            raise Abort(L(f"lk1st {variant}: file mancante o diverso", f"lk1st {variant}: file missing or changed"))
        lk_path = ctx.kit.path(lk["path"])
        elf = images.check_bootshim_elf(lk_path)
        if not elf.ok:
            raise Abort("lk1st: " + "; ".join(elf.errors))
        if lk.get("entry") and elf.entry != int(lk["entry"], 16):
            raise Abort(L(f"lk1st {variant}: ingresso 0x{elf.entry:08x}, il kit dice {lk['entry']}",
                          f"lk1st {variant}: entry 0x{elf.entry:08x}, the kit says {lk['entry']}"))
        ui.ok(L(f"lk1st {variant}: ELF valido per BootShim, ingresso 0x{elf.entry:08x}",
                f"lk1st {variant}: ELF valid for BootShim, entry 0x{elf.entry:08x}"))
        if lk.get("status") != "verified":
            ui.warn(L(f"lk1st {variant} non è mai stato provato su un {ctx.device.name}. Se il telefono non arriva a fastboot: "
                      "volume su all'accensione apre la modalità flash; da lì WPinternals porta il telefono in mass storage e "
                      "«Ripristino EFIESP» rimette il backup.",
                      f"lk1st {variant} has never been tried on a {ctx.device.name}. If the phone does not reach fastboot: "
                      "volume up at power-on opens flash mode; from there WPinternals switches the phone to mass storage and "
                      "\"Restore EFIESP\" puts the backup back."))

        # backup: files, raw partition image, manifest, info
        bk = ctx.path("backups", f"efiesp-{stamp()}")
        ui.info(L(f"Backup di EFIESP in {bk}", f"EFIESP backup in {bk}"))
        shutil.copytree(root, os.path.join(bk, "EFIESP"))
        _manifest_to(os.path.join(bk, "EFIESP"), os.path.join(bk, "EFIESP.sha256"))
        flush_all()
        with ctx.system.open_raw(d) as raw, open(os.path.join(bk, "efiesp-partition.img"), "wb") as o:
            read_range(ctx, raw, part.offset, part.size, o, L("immagine EFIESP", "EFIESP image"))
        rel_bcd = os.path.relpath(lay["bcd"], root).replace(os.sep, "/")
        rel_boot = os.path.relpath(lay["bootdir"], root).replace(os.sep, "/")
        old_lk = ci_find(root, "emmc_appsboot.mbn")
        info = {"date": stamp(), "disk_guid": d.gpt.disk_guid, "partition_offset": part.offset,
                "partition_size": part.size, "variant": variant, "bcd": rel_bcd, "bootdir": rel_boot,
                "had_bootshim": bool(ci_find(lay["bootdir"], "bootshim.efi")), "had_lk": bool(old_lk),
                "options": vars(opts), "backend": backend}
        with open(os.path.join(bk, "info.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)
        with open(os.path.join(bk, "BCD-originale.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(before.summary()) + "\n")

        # new BCD, written next to the backup and checked before it goes to the phone
        plan = bcd.Plan(before, opts)
        new_bcd = os.path.join(bk, "BCD.new")
        if backend in ("python", "hivex"):
            bcd.write_hive(lay["bcd"], new_bcd, plan, engine=backend)
        else:
            bcd.write_bcdedit(lay["bcd"], new_bcd, plan, log=ui.detail)
        after = bcd.BcdView(new_bcd)
        guid, errors = bcd.verify(before, after, plan)
        if errors:
            for e in errors:
                ui.error(e)
            raise Abort(L("Il nuovo BCD non corrisponde al piano: niente è stato scritto sul telefono.",
                          "The new BCD does not match the plan: nothing was written to the phone."))
        with open(os.path.join(bk, "BCD-nuovo.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(after.summary()) + "\n")
        ui.show(L("Nuovo BCD (verificato)", "New BCD (verified)"), after.summary() + [
            L(f"voce {guid}, device copiato da {plan.template_kind}", f"entry {guid}, device copied from {plan.template_kind}")])
        ctx.give_back(bk)
        if not ui.confirm(L("Scrivere su EFIESP?", "Write to EFIESP?"),
                          L("Verranno scritti BootShim, lk1st e il nuovo BCD. Windows per ora resta intatto; il backup è pronto per il ripristino.",
                            "BootShim, lk1st and the new BCD will be written. Windows stays intact for now; the backup is ready for a restore."),
                          L("installa", "install"), L("annulla", "cancel"),
                          word=L("INSTALLA", "INSTALL"), danger=True):
            raise Cancelled()
        targets = [(ctx.kit.path(shim["path"]), os.path.join(lay["bootdir"], "bootshim.efi")),
                   (lk_path, old_lk or os.path.join(root, "emmc_appsboot.mbn")),
                   (new_bcd, lay["bcd"])]
        for src, dst in targets:
            shutil.copyfile(src, dst)
            fsync_file(dst)
            ui.info(L(f"scritto {os.path.relpath(dst, root)}", f"wrote {os.path.relpath(dst, root)}"))
        flush_all()
        for src, dst in targets:
            if images.sha256_file(src) != images.sha256_file(dst):
                raise Abort(L(f"Verifica fallita su {dst}. Ripristina EFIESP dal backup {bk}.",
                              f"Check failed on {dst}. Restore EFIESP from the backup {bk}."))
        ui.ok(L("File su EFIESP identici al kit", "Files on EFIESP match the kit"))
    ctx.state.mark("chain", backup=bk, variant=variant, options=vars(opts), entry=guid)
    ui.ok(L("Catena di avvio installata.", "Boot chain installed."))
    if ui.confirm(L("Riavviare e verificare?", "Restart and check?"),
                  L("Ora il telefono va riavviato: l'installer aspetta lk1st in fastboot.",
                    "The phone now needs a restart: the installer waits for lk1st in fastboot."),
                  L("continua", "continue"), L("più tardi", "later")):
        act_verify_lk1st(ctx)


def _manifest_to(folder, manifest_path):
    lines = []
    for base, _dirs, files in os.walk(folder):
        for fn in files:
            p = os.path.join(base, fn)
            lines.append((os.path.relpath(p, folder).replace(os.sep, "/"), images.sha256_file(p)))
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as f:
        for rel, h in sorted(lines):
            f.write(f"{h}  ./{rel}\n")


# ---------------------------------------------------------------------------
# restart and lk1st
# ---------------------------------------------------------------------------

REBOOT_HELP_IT = [
    "Riavvia il telefono:",
    " • da WPinternals esci dalla modalità mass storage, oppure",
    " • tieni premuti accensione + volume giù per circa 10 s, finché vibra.",
    "Compare il testo di BootShim, poi lo schermo resta fermo: è lk1st in fastboot.",
    "Recupero: volume su tenuto all'accensione = modalità flash (WPinternals).",
]
REBOOT_HELP_EN = [
    "Restart the phone:",
    " • leave mass storage mode in WPinternals, or",
    " • hold power + volume down for about 10 s, until it vibrates.",
    "BootShim text appears, then the screen stays still: that is lk1st in fastboot.",
    "Recovery: hold volume up at power-on = flash mode (WPinternals).",
]


def wait_fastboot(ctx, timeout=300, help_lines=None):
    """Waits for a fastboot device; explains the modes seen on the way."""
    ui = ctx.ui
    if not ctx.phone.fastboot_exe:
        raise Abort(L("fastboot non trovato: usa «Strumenti».", "fastboot not found: use \"Tools\"."))
    if ctx.phone.fastboot_serials():
        return ctx.phone.getvar("product") or "?"
    if help_lines:
        ui.show(L("Riavvio", "Restart"), help_lines)
    seen = []

    def poll():
        s = ctx.phone.snapshot()
        if s.mode not in seen:
            seen.append(s.mode)
            ui.info(L("visto: ", "seen: ") + mode_text(s.mode) + (f" ({s.detail})" if s.detail else ""))
            if s.problem:
                ui.warn(s.problem)
        if s.mode == "fastboot":
            return s.detail or "?"
        if s.mode == "edl":
            raise Abort(L("Il telefono è finito in Qualcomm 9008: la catena non è partita. Tieni premuto accensione + volume giù 10 s per riavviarlo, "
                          "poi volume su all'accensione (modalità flash) e ripristina EFIESP dal backup.",
                          "The phone ended in Qualcomm 9008: the chain did not start. Hold power + volume down for 10 s to restart it, "
                          "then volume up at power-on (flash mode) and restore EFIESP from the backup."))
        ui.status(L("Attesa di fastboot… ora: ", "Waiting for fastboot… now: ") + mode_text(s.mode))
        return None

    r = ui.wait(poll, timeout, interval=1.5)
    if r is None:
        raise Abort(L("fastboot non è comparso. Se lo schermo mostra un punto esclamativo o «Cannot load this LK image», annota il messaggio "
                      "e ripristina EFIESP; su Windows controlla anche il driver USB (Google USB Driver).",
                      "fastboot did not appear. If the screen shows an exclamation mark or \"Cannot load this LK image\", note the message "
                      "and restore EFIESP; on Windows also check the USB driver (Google USB Driver)."))
    return r


def act_verify_lk1st(ctx, timeout=300):
    ui = ctx.ui
    ui.step(L("Verifica di lk1st", "lk1st check"))
    product = wait_fastboot(ctx, timeout, L(REBOOT_HELP_IT, REBOOT_HELP_EN))
    out = ctx.path("logs", f"lk1st-{stamp()}")
    os.makedirs(out)
    rc, txt = ctx.phone.fastboot(["getvar", "all"], timeout=30)
    with open(os.path.join(out, "getvar-all.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    rc, _ = ctx.phone.fastboot(["oem", "log"], timeout=30)
    if rc == 0:
        ctx.phone.fastboot(["get_staged", os.path.join(out, "lk1st.log")], timeout=30)
    with open(os.path.join(out, "usb.txt"), "w", encoding="utf-8") as f:
        for u in ctx.system.usb_devices():
            f.write(f"{u.id} {u.name} {u.status}\n")
    keys = ("product", "version-bootloader", "version", "unlocked", "hw-revision", "serialno")
    shown = [l.split("(bootloader)")[-1].strip() for l in txt.splitlines()
             if any(l.replace("(bootloader)", "").strip().startswith(k + ":") for k in keys)]
    ui.show("fastboot getvar", shown or [product])
    ctx.give_back(out)
    if not product.startswith(("lk1st", "lk2nd")):
        ui.warn(L(f"product={product}: non sembra lk1st", f"product={product}: does not look like lk1st"))
    ctx.state.mark("lk1st", product=product, log=out)
    ui.ok(L(f"lk1st risponde ({product}). Log in {out}", f"lk1st answers ({product}). Logs in {out}"))


# ---------------------------------------------------------------------------
# boot an image in RAM
# ---------------------------------------------------------------------------

def _boot_choices(ctx):
    opts = []
    for e in ctx.kit.boot_images():
        ok = ctx.kit.check(e)
        opts.append({"value": ("kit", e["id"]), "title": ctx.kit.title(e),
                     "sub": ctx.kit.note(e) + ("" if ok else L(" — file mancante o diverso", " — file missing or changed")),
                     "disabled": not ok,
                     "badge": {"verified": L("provato", "tested"), "untested": L("non provato", "untested")}.get(e.get("status"), "")})
    folder = ctx.path("boot")
    for fn in sorted(os.listdir(folder)):
        if fn.lower().endswith(".img"):
            opts.append({"value": ("file", os.path.join(folder, fn)), "title": fn,
                         "sub": L("dalla cartella work/boot", "from the work/boot folder")})
    opts.append({"value": ("ask", ""), "title": L("Altro file…", "Other file…"),
                 "sub": L("percorso di un boot.img (per esempio quello della build)", "path to a boot.img (for example from the build)")})
    return opts


def act_boot(ctx, choice=None):
    ui = ctx.ui
    ui.step(L("Avvio in RAM (fastboot boot)", "Boot in RAM (fastboot boot)"))
    kind, val = choice or ui.choose(L("Cosa avviare?", "What to boot?"),
                                    L("Niente viene scritto sul telefono: al prossimo riavvio torna lk1st.",
                                      "Nothing is written to the phone: the next restart goes back to lk1st."),
                                    _boot_choices(ctx)) or (None, None)
    if kind is None:
        raise Cancelled()
    entry = None
    if kind == "kit":
        entry = next(e for e in ctx.kit.boot_images() if e["id"] == val)
        path = ctx.kit.path(entry["path"])
    elif kind == "ask":
        r = ui.form(L("File da avviare", "File to boot"), "", [
            {"key": "path", "type": "text", "value": "", "label": L("Percorso del boot.img", "Path of the boot.img")}],
            L("avanti", "next"))
        if not r or not r["path"].strip():
            raise Cancelled()
        path = os.path.expanduser(r["path"].strip().strip('"'))
    else:
        path = val
    if not os.path.isfile(path):
        raise Abort(L(f"File non trovato: {path}", f"File not found: {path}"))
    try:
        bi = images.BootImage(path)
    except ValueError as e:
        raise Abort(str(e))
    digest = images.sha256_file(path)
    ui.show(os.path.basename(path), bi.lines() + [f"sha256   {digest}"])
    product = wait_fastboot(ctx, 300, L(["Porta il telefono in lk1st (fastboot): basta accenderlo con la catena installata."],
                                        ["Bring the phone to lk1st (fastboot): just power it on with the chain installed."]))
    ui.info(f"fastboot: {product}")
    if not ui.confirm(L("Avviare questa immagine?", "Boot this image?"),
                      L("Il telefono la carica in RAM e la avvia. Se si blocca: accensione tenuta 12 s (la RAM resta, i log si recuperano da lk1st).",
                        "The phone loads it into RAM and starts it. If it hangs: hold power for 12 s (RAM is kept, logs can be read from lk1st)."),
                      L("avvia", "boot"), L("annulla", "cancel")):
        raise Cancelled()
    rc, out = ctx.phone.fastboot(["boot", path], timeout=180)
    if rc != 0:
        raise Abort(L("fastboot boot non riuscito:\n", "fastboot boot failed:\n") + out.strip()[-600:])
    ui.ok(L("Immagine inviata e avviata", "Image sent and started"))
    log_dir = ctx.path("logs", f"boot-{stamp()}")
    os.makedirs(log_dir)
    with open(os.path.join(log_dir, "image.txt"), "w", encoding="utf-8") as f:
        f.write(f"{path}\nsha256 {digest}\n" + "\n".join(bi.lines()) + "\n")
    result = watch_after_boot(ctx, log_dir, entry)
    ctx.give_back(log_dir)
    ctx.state.mark("boot", image=os.path.basename(path), sha256=digest, result=result, log=log_dir)


def watch_after_boot(ctx, log_dir, entry):
    """Watches the USB side for 3 minutes; captures the K1 serial log on Linux."""
    ui = ctx.ui
    serial = (entry or {}).get("serial")
    seen = []
    ui.info(L("Osservo il telefono (3 minuti, «stop» per finire prima)…", "Watching the phone (3 minutes, \"stop\" to end early)…"))

    def poll():
        s = ctx.phone.snapshot()
        if s.mode not in seen:
            seen.append(s.mode)
            ui.info(L("visto: ", "seen: ") + mode_text(s.mode))
        if serial and any(u.id == serial for u in s.usb):
            return "serial"
        if s.mode in ("adb", "recovery", "unauthorized"):
            return s.mode
        if s.mode == "edl":
            return "edl"
        if s.mode == "fastboot" and "none" in seen:
            return "fastboot-again"
        ui.status(L("ora: ", "now: ") + mode_text(s.mode))
        return None

    try:
        r = ui.wait(poll, 180, interval=1.5)
    except Cancelled:
        return "stopped:" + ",".join(seen)
    if r == "serial":
        ui.ok(L("Il kernel si è presentato sulla seriale USB: è vivo.", "The kernel showed up on USB serial: it is alive."))
        capture_serial(ctx, log_dir)
    elif r in ("adb", "recovery"):
        ui.ok(L("Android risponde su adb.", "Android answers on adb."))
        rc, out = ctx.phone.adb(["shell", "dmesg"], timeout=30)
        with open(os.path.join(log_dir, "dmesg.txt"), "w", encoding="utf-8") as f:
            f.write(out)
        ui.info(L(f"dmesg salvato in {log_dir}", f"dmesg saved in {log_dir}"))
    elif r == "fastboot-again":
        ui.warn(L("Il telefono è tornato in lk1st: il kernel si è riavviato. Usa «Log del boot precedente».",
                  "The phone went back to lk1st: the kernel restarted. Use \"Previous boot log\"."))
    elif r == "edl":
        ui.error(L("Qualcomm 9008 dopo l'avvio: accensione + volume giù 10 s per uscirne.",
                   "Qualcomm 9008 after booting: power + volume down for 10 s to leave it."))
    else:
        ui.warn(L("Nessun segno dal telefono via USB. Guarda lo schermo; se è bloccato, accensione 12 s e poi «Log del boot precedente».",
                  "No sign from the phone over USB. Look at the screen; if it is stuck, hold power 12 s, then \"Previous boot log\"."))
    return r or "no-usb"


def capture_serial(ctx, log_dir):
    ui = ctx.ui
    if sysdeps.IS_WINDOWS:
        ui.info(L("Su Windows apri la porta COM del telefono (Gestione dispositivi) con PuTTY o simili per leggere il log.",
                  "On Windows open the phone's COM port (Device Manager) with PuTTY or similar to read the log."))
        return
    import glob
    import select
    deadline = time.time() + 15
    port = None
    while time.time() < deadline and not port:
        ports = sorted(glob.glob("/dev/ttyACM*"))
        port = ports[-1] if ports else None
        if not port:
            time.sleep(0.5)
    if not port:
        ui.warn(L("Nessuna /dev/ttyACM*", "No /dev/ttyACM*"))
        return
    out = os.path.join(log_dir, "serial.log")
    ui.info(L(f"Cattura di {port} in {out} — «stop» per finire", f"Capturing {port} into {out} — \"stop\" to end"))
    lines = 0
    try:
        fd = os.open(port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as e:
        ui.warn(str(e))
        return
    buf = b""
    try:
        with open(out, "ab") as f:
            while True:
                ui.check_cancel()
                r, _, _ = select.select([fd], [], [], 0.3)
                if not r:
                    continue
                try:
                    b = os.read(fd, 4096)
                except OSError:
                    break
                if not b:
                    break
                f.write(b)
                f.flush()
                buf += b
                *done, buf = buf.split(b"\n")
                for l in done:
                    lines += 1
                    ui.detail(l.decode("utf-8", "replace").rstrip("\r"))
    except Cancelled:
        pass
    finally:
        os.close(fd)
    ui.ok(L(f"{lines} righe salvate in {out}", f"{lines} lines saved in {out}"))


def act_ramoops(ctx):
    ui = ctx.ui
    ui.step(L("Log del boot precedente", "Previous boot log"))
    wait_fastboot(ctx, 120, L(["Porta il telefono in lk1st (fastboot)."], ["Bring the phone to lk1st (fastboot)."]))
    out = ctx.path("logs", f"ramoops-{stamp()}")
    os.makedirs(out)
    got = []
    for name, cmd in (("ramoops-console.txt", ["oem", "ramoops", "console"]), ("lk1st.log", ["oem", "log"])):
        rc, _ = ctx.phone.fastboot(cmd, timeout=30)
        if rc == 0:
            rc, _ = ctx.phone.fastboot(["get_staged", os.path.join(out, name)], timeout=30)
            if rc == 0 and os.path.isfile(os.path.join(out, name)):
                got.append(name)
    ctx.give_back(out)
    if not got:
        raise Abort(L("lk1st non ha restituito log.", "lk1st returned no logs."))
    p = os.path.join(out, got[0])
    with open(p, encoding="utf-8", errors="replace") as f:
        tail = f.read().splitlines()[-25:]
    ui.show(got[0], tail)
    ui.ok(L(f"Log in {out}", f"Logs in {out}"))


# ---------------------------------------------------------------------------
# write a partition (fastboot flash)
# ---------------------------------------------------------------------------

def act_flash(ctx):
    ui = ctx.ui
    ui.step(L("Scrittura di una partizione Android", "Write an Android partition"))
    wait_fastboot(ctx, 120, L(["Porta il telefono in lk1st (fastboot)."], ["Bring the phone to lk1st (fastboot)."]))
    parts = []
    for name in FLASHABLE:
        size = ctx.phone.getvar(f"partition-size:{name}")
        if size and size.lower().startswith("0x"):
            parts.append((name, int(size, 16)))
    if not parts:
        ui.show(L("Partizioni Android", "Android partitions"),
                L(["Il telefono non ha ancora partizioni Android (boot, system, …).",
                   "Le crea l'installazione completa (fase 2). Intanto puoi usare «Avvio in RAM»."],
                  ["The phone has no Android partitions yet (boot, system, …).",
                   "The Full install creates them (phase 2). Meanwhile you can use \"Boot in RAM\"."]))
        return
    name = ui.choose(L("Quale partizione?", "Which partition?"), "",
                     [{"value": n, "title": n, "sub": human_size(s)} for n, s in parts])
    if not name:
        raise Cancelled()
    r = ui.form(L(f"Immagine per {name}", f"Image for {name}"), "", [
        {"key": "path", "type": "text", "value": "", "label": L("Percorso del file .img", "Path of the .img file")}],
        L("avanti", "next"))
    if not r or not r["path"].strip():
        raise Cancelled()
    path = os.path.expanduser(r["path"].strip().strip('"'))
    if not os.path.isfile(path):
        raise Abort(L(f"File non trovato: {path}", f"File not found: {path}"))
    size = dict(parts)[name]
    fsize = os.path.getsize(path)
    with open(path, "rb") as f:
        sparse = f.read(4) == b"\x3a\xff\x26\xed"
    if fsize > size and not sparse:
        raise Abort(L(f"Il file ({human_size(fsize)}) è più grande della partizione ({human_size(size)}).",
                      f"The file ({human_size(fsize)}) is larger than the partition ({human_size(size)})."))
    if name == "boot":
        try:
            images.BootImage(path)
        except ValueError as e:
            raise Abort(str(e))
    ui.show(os.path.basename(path), [f"{human_size(fsize)}{' (sparse)' if sparse else ''} -> {name} ({human_size(size)})",
                                     f"sha256 {images.sha256_file(path)}"])
    if not ui.confirm(L(f"Scrivere {name}?", f"Write {name}?"),
                      L("Il contenuto attuale della partizione viene sostituito.", "The partition's current content is replaced."),
                      L("scrivi", "write"), L("annulla", "cancel"), word=L("SCRIVI", "WRITE"), danger=True):
        raise Cancelled()
    rc, out = ctx.phone.fastboot(["flash", name, path], timeout=900)
    if rc != 0:
        raise Abort(L("fastboot flash non riuscito:\n", "fastboot flash failed:\n") + out.strip()[-600:])
    ctx.state.mark("flash-" + name, image=os.path.basename(path))
    ui.ok(L(f"{name} scritta", f"{name} written"))


# ---------------------------------------------------------------------------
# restore EFIESP
# ---------------------------------------------------------------------------

def _read_info(folder):
    p = os.path.join(folder, "info.json")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    p = os.path.join(folder, "INFO")  # backups made by kit/scripts/efiesp-prep.sh
    if os.path.isfile(p):
        info = {}
        with open(p, encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    info[k] = v
        for k in ("had_bootshim", "had_lk"):
            info[k] = info.get(k) == "1"
        info.setdefault("date", info.get("data", ""))
        return info
    return None


def efiesp_backups(ctx, extra_dirs=()):
    found = []
    for base in [ctx.path("backups")] + list(extra_dirs):
        if not os.path.isdir(base):
            continue
        for n in sorted(os.listdir(base), reverse=True):
            folder = os.path.join(base, n)
            if n.startswith("efiesp-") and os.path.isdir(os.path.join(folder, "EFIESP")):
                info = _read_info(folder)
                if info:
                    found.append((folder, info))
    return found


def act_restore(ctx):
    ui = ctx.ui
    ui.step(L("Ripristino di EFIESP", "EFIESP restore"))
    proj_backups = os.path.join(os.path.dirname(ctx.root), "backups")
    backups = efiesp_backups(ctx, [proj_backups])
    if not backups:
        raise Abort(L("Nessun backup di EFIESP trovato.", "No EFIESP backup found."))
    choice = ui.choose(L("Quale backup?", "Which backup?"),
                       L("Ogni backup è lo stato di EFIESP prima di un'installazione.", "Each backup is EFIESP as it was before an install."),
                       [{"value": i, "title": os.path.basename(f),
                         "sub": f"{info.get('variant', '?')} · " + (L("con immagine", "with image") if os.path.isfile(os.path.join(f, "efiesp-partition.img")) else L("solo file", "files only"))}
                        for i, (f, info) in enumerate(backups)])
    if choice is None:
        raise Cancelled()
    folder, info = backups[choice]
    d = select_disk(ctx)
    if info.get("disk_guid") and info["disk_guid"].upper() != d.gpt.disk_guid.upper():
        raise Abort(L(f"Il backup {os.path.basename(folder)} è di un altro telefono (disco {info['disk_guid']}, "
                      f"collegato {d.gpt.disk_guid}): il suo BCD non vale per questo.",
                      f"The backup {os.path.basename(folder)} is from another phone (disk {info['disk_guid']}, "
                      f"connected {d.gpt.disk_guid}): its BCD does not fit this one."))
    part = d.gpt.by_name("EFIESP")
    has_img = os.path.isfile(os.path.join(folder, "efiesp-partition.img"))
    modes = [{"value": "files", "title": L("File (BCD, BootShim, lk1st)", "Files (BCD, BootShim, lk1st)"),
              "sub": L("rimette i file cambiati dall'installer e li confronta col backup", "puts back the files the installer changed and checks them")}]
    modes.append({"value": "image", "title": L("Immagine intera della partizione", "Whole partition image"),
                  "sub": L("riscrive EFIESP byte per byte", "rewrites EFIESP byte for byte") if not sysdeps.IS_WINDOWS
                  else L("su Windows usa Win32DiskImager con efiesp-partition.img", "on Windows use Win32DiskImager with efiesp-partition.img"),
                  "disabled": not has_img or sysdeps.IS_WINDOWS})
    mode = ui.choose(L("Come ripristinare?", "How to restore?"), folder, modes)
    if mode is None:
        raise Cancelled()
    if not ui.confirm(L("Ripristinare EFIESP?", "Restore EFIESP?"),
                      L(f"EFIESP del telefono torna come in {os.path.basename(folder)}.",
                        f"The phone's EFIESP goes back to {os.path.basename(folder)}."),
                      L("ripristina", "restore"), L("annulla", "cancel"), word=L("RIPRISTINA", "RESTORE"), danger=True):
        raise Cancelled()
    if mode == "image":
        img = os.path.join(folder, "efiesp-partition.img")
        if os.path.getsize(img) != part.size:
            raise Abort(L("La dimensione dell'immagine non coincide con EFIESP.", "The image size does not match EFIESP."))
        sha = os.path.join(folder, "efiesp-partition.img.sha256")
        if os.path.isfile(sha):
            with open(sha) as f:
                want = f.read().split()[0]
            if images.sha256_file(img) != want:
                raise Abort(L("Hash dell'immagine errato.", "Image hash mismatch."))
        busy = ctx.system.unmount_all(d)
        if busy:
            raise Abort(L(f"Partizioni ancora montate: {', '.join(busy)}", f"Partitions still mounted: {', '.join(busy)}"))
        # written through the whole disk at the partition offset, the same bytes the
        # backup read (so it also works where the OS has no node for the partition)
        with open(img, "rb") as src, ctx.system.open_raw(d, write=True) as dst:
            dst.seek(part.offset)
            done = 0
            while True:
                b = src.read(CHUNK)
                if not b:
                    break
                dst.write(b)
                done += len(b)
                ui.progress("EFIESP", done, part.size)
            os.fsync(dst.fileno())
        with ctx.system.open_raw(d) as raw:
            h = hashlib.sha256()
            read_range(ctx, raw, part.offset, part.size, _Null(), L("controllo EFIESP", "EFIESP check"), (h,))
        if h.hexdigest() != images.sha256_file(img):
            raise Abort(L("EFIESP riletta non corrisponde all'immagine: riprova il ripristino.",
                          "EFIESP read back does not match the image: run the restore again."))
        ui.ok(L(f"Immagine riscritta su EFIESP di {d.ident} e riletta", f"Image written to EFIESP of {d.ident} and read back"))
    else:
        with ctx.system.mounted(d, part, "vfat", write=True) as root:
            src_root = os.path.join(folder, "EFIESP")
            bcd_rel = info.get("bcd", "EFI/Microsoft/Boot/BCD")
            boot_rel = info.get("bootdir", "Windows/System32/Boot")
            dst_bcd = ci_find(root, bcd_rel)
            shutil.copyfile(ci_find(src_root, bcd_rel), dst_bcd)
            fsync_file(dst_bcd)
            shim_dst = ci_find(root, boot_rel + "/bootshim.efi")
            if info.get("had_bootshim"):
                dst = shim_dst or os.path.join(ci_find(root, boot_rel), "bootshim.efi")
                shutil.copyfile(ci_find(src_root, boot_rel + "/bootshim.efi"), dst)
                fsync_file(dst)
            elif shim_dst:
                os.remove(shim_dst)
            lk_dst = ci_find(root, "emmc_appsboot.mbn")
            if info.get("had_lk"):
                dst = lk_dst or os.path.join(root, "emmc_appsboot.mbn")
                shutil.copyfile(ci_find(src_root, "emmc_appsboot.mbn"), dst)
                fsync_file(dst)
            elif lk_dst:
                os.remove(lk_dst)
            stage2 = ci_find(root, "Stage2.efi")
            if stage2 and not ci_find(src_root, "Stage2.efi"):
                os.remove(stage2)
            flush_all()
            manifest = os.path.join(folder, "EFIESP.sha256")
            if os.path.isfile(manifest):
                bad = check_manifest(root, manifest)
                if bad:
                    ui.warn(L("File diversi dal backup: ", "Files different from the backup: ") + ", ".join(bad[:10]))
                else:
                    ui.ok(L("EFIESP identica al backup", "EFIESP matches the backup"))
    ctx.state.mark("restore", backup=folder, mode=mode)
    ui.ok(L("Ripristino completato. Riavvia il telefono.", "Restore done. Restart the phone."))


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------

def act_diag(ctx):
    ui = ctx.ui
    ui.step(L("Diagnostica", "Diagnostics"))
    s = ctx.phone.snapshot()
    lines = [L("stato: ", "mode: ") + s.text()]
    for u in s.usb:
        if u.vid not in ("045e", "0421", "18d1", "05c6"):
            continue  # only phone-related devices (Microsoft, Nokia, Google, Qualcomm)
        lines.append(f"USB {u.id}  {u.name}  {u.status if sysdeps.IS_WINDOWS else ''}".rstrip())
    ui.show(L("Collegamenti", "Connections"), lines)
    if s.mode == "fastboot":
        rc, out = ctx.phone.fastboot(["getvar", "all"], timeout=30)
        rows = [l.replace("(bootloader)", "").strip() for l in out.splitlines()]
        keep = ("product:", "version-bootloader:", "version:", "unlocked:", "hw-revision:", "secure:", "max-download-size:")
        parts = sorted({r.split(":")[1] for r in rows if r.startswith("partition-size:")})
        android = [p for p in parts if p in FLASHABLE]
        ui.show("fastboot", [r for r in rows if r.startswith(keep)] + [
            L(f"{len(parts)} partizioni; Android: {', '.join(android) or 'nessuna'}",
              f"{len(parts)} partitions; Android: {', '.join(android) or 'none'}")])
    if s.mode == "mass" and ctx.system.is_admin():
        for d in find_lumia_disks(ctx):
            ui.show(str(d.ident), [f"{p.index:>2} {p.name:<18} {human_size(p.size)}" for p in d.gpt.partitions])
    for name, ok, hint in requirements(ctx):
        (ui.ok if ok else ui.warn)(f"{name}: {hint or 'OK'}")


# ---------------------------------------------------------------------------
# install modes (as in the Lumia 950 XL installer: Full, Clean, Update)
# ---------------------------------------------------------------------------

# Phase 2 (Android partitions) and the ROM install are available.
PHASE2_READY = True

MODES = {
    "full": {
        "title": ("Completa", "Full"),
        "before": ("Windows 10 Mobile o 8.1, già sbloccato e in mass storage (preparazione con WPinternals, fuori dall'installer)",
                   "Windows 10 Mobile or 8.1, already unlocked and in mass storage mode (preparation with WPinternals, outside the installer)"),
        "does": ("backup, firmware da Windows, BootShim + lk1st su EFIESP, verifica di lk1st, partizioni Android "
                 "(Windows cancellato), TWRP in RAM, formattazione dei dati, ROM",
                 "backup, firmware from Windows, BootShim + lk1st on EFIESP, lk1st check, Android partitions "
                 "(Windows erased), TWRP in RAM, format data, ROM"),
        "data": ("Windows e tutti i suoi dati cancellati (prima un backup sul PC)",
                 "Windows and all its data erased (after a backup on the PC)"),
        "ready": PHASE2_READY,
    },
    "clean": {
        "title": ("Pulita", "Clean"),
        "before": ("lk1st e partizioni Android già presenti", "lk1st and Android partitions already in place"),
        "does": ("TWRP in RAM, formattazione dei dati, ROM", "TWRP in RAM, format data, ROM"),
        "data": ("tutti i dati Android cancellati, memoria interna compresa", "all Android data erased, internal storage included"),
        "ready": PHASE2_READY,
    },
    "update": {
        "title": ("Aggiornamento", "Update"),
        "before": ("questa ROM già installata", "this ROM already installed"),
        "does": ("TWRP in RAM, ROM", "TWRP in RAM, ROM"),
        "data": ("conservati", "kept"),
        "ready": PHASE2_READY,
    },
}


def mode_ready(key, device):
    return bool(MODES[key]["ready"] and device.ready.get(key))


def mode_options(device):
    """Menu entries for the three modes, for this phone."""
    out = []
    for key, m in MODES.items():
        sub = L(f"prima: {m['before'][0]}. Dati: {m['data'][0]}.", f"before: {m['before'][1]}. Data: {m['data'][1]}.")
        if not mode_ready(key, device):
            sub += L(f" Non ancora disponibile per il {device.name}.", f" Not available yet for the {device.name}.")
        out.append({"value": key, "title": L(*m["title"]), "sub": sub, "disabled": not mode_ready(key, device)})
    return out


def _need_mode(ctx, key):
    """Modes not ready for this phone stop before touching anything."""
    if not mode_ready(key, ctx.device):
        n = L(*ctx.device.notes) if any(ctx.device.notes) else ""
        raise Abort(L(f"La modalità {L(*MODES[key]['title'])} non è ancora disponibile per il {ctx.device.title}. ",
                      f"The {L(*MODES[key]['title'])} mode is not available yet for the {ctx.device.title}. ") + n
                    + L(" I singoli passi sono negli strumenti avanzati.", " The single steps are in the advanced tools."))


def _intro(ctx, key, extra=()):
    m = MODES[key]
    ctx.ui.show(L("Cosa fa", "What it does"), [
        L("Telefono: ", "Phone: ") + L(*m["before"]),
        L("Passi: ", "Steps: ") + L(*m["does"]),
        L("Dati: ", "Data: ") + L(*m["data"]),
    ] + list(extra))


def _need_tools(ctx):
    if not (ctx.phone.fastboot_exe and ctx.phone.adb_exe):
        act_tools(ctx)
    if not (ctx.phone.fastboot_exe and ctx.phone.adb_exe):
        raise Abort(L("Servono adb e fastboot.", "adb and fastboot are needed."))


def _resolve_files(ctx):
    """TWRP and ROM are checked before anything touches the phone."""
    android.resolve_twrp(ctx)
    android.resolve_rom(ctx)


def phase2_recovery(ctx):
    """(image, label) of the recovery used for phase 2: the pinned TWRP unless --phase2-recovery."""
    path = ctx.opts.get("phase2_recovery")
    if not path:
        return ctx.twrp, "TWRP"
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise Abort(L(f"Recovery per la fase 2 non trovata: {path}", f"Phase 2 recovery not found: {path}"))
    try:
        images.BootImage(path)
    except ValueError as e:
        raise Abort(f"{path}: {e}")
    ctx.ui.warn(L(f"Fase 2 con una recovery non verificata dall'installer: {path} (sha256 {images.sha256_file(path)})",
                  f"Phase 2 with a recovery the installer does not pin: {path} (sha256 {images.sha256_file(path)})"))
    return path, "recovery"


def act_phase2(ctx):
    """Phase 2 alone (advanced tools): Android partitions, Windows erased. No ROM."""
    ui = ctx.ui
    ui.step(L("Partizioni Android (fase 2)", "Android partitions (phase 2)"))
    _need_tools(ctx)
    if not ctx.twrp:
        android.resolve_twrp(ctx)
    image, label = phase2_recovery(ctx)
    phase2.run_phase2(ctx, android.boot_recovery, image, label)


def _full_mass_storage_part(ctx):
    """Backup, firmware and boot chain in mass storage, then the lk1st check."""
    ui = ctx.ui
    st = ctx.state
    plan = [("backup", L("Backup", "Backup"), act_backup),
            ("firmware", L("Firmware da Windows", "Firmware from Windows"), act_firmware),
            ("chain", L("Catena di avvio", "Boot chain"), lambda c: act_chain(c, for_full=True))]
    for key, title, fn in plan:
        if ctx.disk is None:
            select_disk(ctx)
        done = st.done(key)
        if done:
            v = ui.choose(title, L(f"Già fatto il {done['time'].replace('T', ' ')}.", f"Already done on {done['time'].replace('T', ' ')}."),
                          [{"value": "skip", "title": L("Salta", "Skip"), "sub": L("usa quello esistente", "use the existing one")},
                           {"value": "redo", "title": L("Rifai", "Do it again"), "sub": ""}])
            if v is None:
                raise Cancelled()
            if v == "skip":
                continue
        fn(ctx)
    if not st.done("lk1st"):
        act_verify_lk1st(ctx)


def act_full(ctx):
    ui = ctx.ui
    st = ctx.state
    ui.step(L("Installazione completa", "Full install"))
    _need_mode(ctx, "full")
    _intro(ctx, "full", [
        L("Riprende da dove si era fermata: i passi fatti sono salvati in work/state.json.",
          "It resumes where it stopped: the steps done are saved in work/state.json."),
        L("Windows viene cancellato solo dopo il backup del provisioning e la parola di conferma.",
          "Windows is erased only after the provisioning backup and the confirmation words."),
    ])
    _need_tools(ctx)
    snap = ctx.phone.snapshot()
    lk_seen = snap.mode == "fastboot" and snap.detail.startswith(("lk1st", "lk2nd"))
    chain_ready = st.done("chain") or st.done("lk1st") or st.done("phase2") or lk_seen
    need_mass = snap.mode == "mass" or not chain_ready
    if need_mass:
        # preparation first: the phone must arrive unlocked (WPinternals, outside the
        # installer); checked before anything else is asked or written
        ctx.require_unlocked = True
        select_disk(ctx)
        ui.ok(L("Preparazione fatta: telefono sbloccato e in mass storage", "Preparation done: phone unlocked and in mass storage"))
    _resolve_files(ctx)
    if need_mass:
        _full_mass_storage_part(ctx)
    elif not (st.done("lk1st") or st.done("phase2")):
        if snap.mode == "fastboot":
            act_verify_lk1st(ctx)
        else:
            ui.info(L("Catena di avvio già installata.", "Boot chain already installed."))
    else:
        ui.ok(L("Catena di avvio installata e lk1st verificato.", "Boot chain installed and lk1st checked."))
    if not st.done("phase2"):
        image, label = phase2_recovery(ctx)
        phase2.run_phase2(ctx, android.boot_recovery, image, label)
    else:
        ui.ok(L(f"Fase 2 già fatta il {st.done('phase2')['time'].replace('T', ' ')}.",
                f"Phase 2 already done on {st.done('phase2')['time'].replace('T', ' ')}."))
        if st.done("rom"):
            v = ui.choose(L("Installazione già completata", "Install already finished"),
                          L(f"ROM installata il {st.done('rom')['time'].replace('T', ' ')}.",
                            f"ROM installed on {st.done('rom')['time'].replace('T', ' ')}."),
                          [{"value": "again", "title": L("Reinstalla la ROM (pulita)", "Install the ROM again (clean)"),
                            "sub": L("formatta i dati", "formats data")}])
            if v is None:
                raise Cancelled()
    # TWRP is (re)started so that it sees the new partitions (data, cache)
    ui.step(L("ROM", "ROM"))
    android.install_rom(ctx, wipe_data=True)
    ui.ok(L("Installazione completa terminata.", "Full install finished."))


def _confirm_clean(ctx):
    if not ctx.ui.confirm(L("Installazione pulita: TUTTI i dati del telefono vengono cancellati, memoria interna compresa.",
                            "Clean install: ALL data on the phone is erased, internal storage included."),
                          L("Foto, app e file su Android spariscono. Niente è stato ancora scritto: il telefono è nella TWRP.",
                            "Photos, apps and files in Android disappear. Nothing has been written yet: the phone is in TWRP."),
                          L("cancella", "erase"), L("annulla", "cancel"), word=L("CANCELLA", "ERASE"), danger=True):
        raise Cancelled()


def act_clean(ctx):
    ctx.ui.step(L("Installazione pulita", "Clean install"))
    _need_mode(ctx, "clean")
    _intro(ctx, "clean")
    _need_tools(ctx)
    _resolve_files(ctx)
    android.install_rom(ctx, wipe_data=True, before_write=_confirm_clean)


def act_update(ctx):
    ctx.ui.step(L("Aggiornamento", "Update"))
    _need_mode(ctx, "update")
    _intro(ctx, "update")
    _need_tools(ctx)
    _resolve_files(ctx)
    android.install_rom(ctx, wipe_data=False, before_write=android.check_update_target)


ACTIONS = {
    "full": act_full,
    "clean": act_clean,
    "update": act_update,
    "wizard": act_full,  # old name of the full mode
    "backup": act_backup,
    "firmware": act_firmware,
    "chain": act_chain,
    "verify": act_verify_lk1st,
    "phase2": act_phase2,
    "boot": act_boot,
    "ramoops": act_ramoops,
    "flash": act_flash,
    "restore": act_restore,
    "diag": act_diag,
    "tools": act_tools,
}
