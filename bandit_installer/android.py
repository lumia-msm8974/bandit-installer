"""The Android side of the install: ROM and TWRP files, lk1st fastboot, TWRP over adb, sideload.

Flow used by the Clean and Update modes and by the end of the Full mode, as in the
Lumia 950 XL installer:

  phone in Android (adb) or in lk1st (fastboot)
    -> adb reboot bootloader            lk1st fastboot
    -> fastboot boot <TWRP>             TWRP in RAM, never flashed
    -> adb in "recovery" state          (adb runs as root in this TWRP)
    -> Clean: twrp format data, twrp wipe cache, twrp wipe dalvik
    -> twrp sideload, adb sideload <ROM zip>
    -> /tmp/recovery.log must say "script succeeded" / "Updater process ended with RC=0"
    -> boot partition = boot.img of the zip
    -> adb reboot

Every ROM, TWRP and model check uses the profile of the phone in use (devices.py).

Facts measured on an RM-937 with twrp-3.7.0_9-1-bandit.img:
- adb sideload of the ROM stops around 47% with "Total xfer: 1.00x": normal for a
  block-based OTA, the installer reads the result from the TWRP log;
- the `twrp` command line client prints "Segmentation fault" after it has run the
  command: results are checked in the log, never through its exit code;
- TWRP's MTP is not used (a libmtp client on the PC made adb lose the phone).
"""
import glob
import hashlib
import os
import re
import time
import zipfile

from . import devices, fat, images
from .core import Abort, Cancelled, human_size, stamp
from .i18n import L

BYNAME = "/dev/block/platform/msm_sdcc.1/by-name"
DISK = "/dev/block/mmcblk0"
ANDROID_PARTS = ("boot", "recovery", "system", "userdata", "cache", "persist", "modemst1", "modemst2")
ROM_SDK = "30"
CHECKSUMS = "checksums.sha256"
PRODUCT_DAT = "Nokia/product.dat"

SUCCESS_LINES = ("script succeeded: result was [1.000000]", "Updater process ended with RC=0")
FAILURE_RE = re.compile(r"script aborted|Updater process ended with ERROR|Error installing zip|"
                        r"Updater process ended with signal|Installation aborted")
TWRP_ERROR_RE = re.compile(r"(?im)^.*(unable to (wipe|format|mount)|failed to (wipe|format)|"
                           r"format_data_err|wipe_err|does not appear to be running).*$")
SHOW_LOG_RE = re.compile(r"script succeeded|script aborted|Updater process ended|Install took|"
                         r"E[0-9]{4}:|Error installing zip|Formatting|Wiping|wipe|format", re.I)

CHUNK = 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# files: checksums, TWRP, ROM
# ---------------------------------------------------------------------------

def sha256_progress(ctx, path, label):
    size = os.path.getsize(path)
    h = hashlib.sha256()
    done, last = 0, 0
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            done += len(b)
            now = time.time()
            if now - last > 0.3 or done == size:
                last = now
                ctx.ui.progress(label, done, size)
    return h.hexdigest()


def pinned(root):
    """{file name: sha256} from checksums.sha256 next to the installer."""
    out = {}
    path = os.path.join(root, CHECKSUMS)
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^\s*([0-9a-fA-F]{64})\s+\*?(\S.*?)\s*$", line)
            if m and not line.lstrip().startswith("#"):
                out[m.group(2).replace("\\", "/").split("/")[-1]] = m.group(1).lower()
    return out


def twrp_hashes(root, device=None):
    """{sha256: name} of the pinned TWRP images; with a device, only those for that phone."""
    return {h: n for n, h in pinned(root).items()
            if n.lower().startswith("twrp") and (device is None or device.twrp_file(n))}


def _download(ctx, url, dest):
    import urllib.request
    part = dest + ".part"
    with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            ctx.ui.check_cancel()
            b = r.read(256 * 1024)
            if not b:
                break
            out.write(b)
            done += len(b)
            ctx.ui.progress(L("download", "download"), done, total or None)
    os.replace(part, dest)


def resolve_twrp(ctx):
    """Finds the TWRP image and checks it against checksums.sha256. Sets ctx.twrp."""
    ui = ctx.ui
    dev = ctx.device
    ui.step(L("Controllo della TWRP", "Checking TWRP"))
    if not dev.twrp_name:
        raise Abort(L(f"Per il {dev.title} non c'è ancora una TWRP: questa modalità non è disponibile.",
                      f"There is no TWRP for the {dev.title} yet: this mode is not available."))
    known = twrp_hashes(ctx.root, dev)
    if not known:
        raise Abort(L(f"{CHECKSUMS} manca o non contiene la TWRP del {dev.title}: il pacchetto dell'installer è incompleto.",
                      f"{CHECKSUMS} is missing or lists no TWRP for the {dev.title}: the installer package is incomplete."))
    folder = os.path.join(ctx.root, "twrp")
    path = ctx.opts.get("twrp")
    if path:
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(path):
            raise Abort(L(f"TWRP non trovata: {path}", f"TWRP not found: {path}"))
    else:
        cands = [os.path.join(folder, dev.twrp_name)] + sorted(
            c for c in glob.glob(os.path.join(folder, "twrp*.img")) if dev.twrp_file(os.path.basename(c)))
        path = next((c for c in cands if os.path.isfile(c)), None)
        if not path:
            if not ui.confirm(L("Scaricare la TWRP?", "Download TWRP?"),
                              L(f"{dev.twrp_name} (circa 20 MB) non è nella cartella twrp. Scaricarla da\n{dev.twrp_url}?",
                                f"{dev.twrp_name} (about 20 MB) is not in the twrp folder. Download it from\n{dev.twrp_url}?"),
                              L("scarica", "download"), L("annulla", "cancel")):
                raise Abort(L(f"Metti {dev.twrp_name} nella cartella {folder} (pagina: {dev.twrp_page}).",
                              f"Put {dev.twrp_name} in the folder {folder} (page: {dev.twrp_page})."))
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, dev.twrp_name)
            _download(ctx, dev.twrp_url, path)
            ctx.give_back(folder)
    digest = sha256_progress(ctx, path, "TWRP SHA-256")
    if digest not in known:
        other = twrp_hashes(ctx.root).get(digest)
        if other:
            raise Abort(L(f"{path} è {other}, una TWRP per un altro telefono: per il {dev.title} serve {dev.twrp_name}.",
                          f"{path} is {other}, a TWRP for another phone: the {dev.title} needs {dev.twrp_name}."))
        raise Abort(L(f"{path} non è il file atteso (SHA-256 {digest}, nessuna corrispondenza in {CHECKSUMS}). "
                      f"Scarica di nuovo {dev.twrp_name} dalla pagina della release.",
                      f"{path} is not the expected file (SHA-256 {digest}, not listed in {CHECKSUMS}). "
                      f"Download {dev.twrp_name} again from the release page."))
    try:
        images.BootImage(path)
    except ValueError as e:
        raise Abort(f"TWRP: {e}")
    ctx.twrp = path
    ui.ok(L(f"{os.path.basename(path)}: SHA-256 verificato ({known[digest]})",
            f"{os.path.basename(path)}: SHA-256 verified ({known[digest]})"))
    return path


def find_rom_checksum(path):
    """(hash, source file) from a SHA256SUMS next to the zip, or (None, None)."""
    folder, name = os.path.split(path)
    cands = [path + ".sha256", path + ".sha256sum", os.path.join(folder, "SHA256SUMS"),
             os.path.join(folder, "SHA256SUMS.txt"), os.path.join(folder, "sha256sums.txt")]
    for c in cands:
        if not os.path.isfile(c):
            continue
        per_file = c.endswith((".sha256", ".sha256sum"))
        with open(c, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"^\s*([0-9a-fA-F]{64})(?:\s+\*?(.+?))?\s*$", line)
                if not m:
                    continue
                fn = (m.group(2) or "").replace("\\", "/").split("/")[-1]
                if fn.lower() == name.lower() or (per_file and not fn):
                    return m.group(1).lower(), c
    return None, None


def _pick_rom(ctx):
    folder = os.path.join(ctx.root, "rom")
    dev = ctx.device
    zips = sorted(glob.glob(os.path.join(folder, dev.rom_glob)))
    if len(zips) == 1:
        return zips[0]
    if len(zips) > 1:
        v = ctx.ui.choose(L("Quale ROM?", "Which ROM?"), L(f"Più zip in {folder}.", f"More than one zip in {folder}."),
                          [{"value": z, "title": os.path.basename(z), "sub": human_size(os.path.getsize(z))}
                           for z in reversed(zips)])
        if v is None:
            raise Cancelled()
        return v
    r = ctx.ui.form(L("ROM", "ROM"),
                    L(f"Nessuna ROM in {folder}. Scarica lineage-18.1-…-UNOFFICIAL-{dev.codename}.zip e il suo SHA256SUMS da\n"
                      f"{dev.rom_releases}\ne mettili in quella cartella, oppure scrivi qui il percorso dello zip.",
                      f"No ROM in {folder}. Download lineage-18.1-…-UNOFFICIAL-{dev.codename}.zip and its SHA256SUMS from\n"
                      f"{dev.rom_releases}\nand put them in that folder, or type the path of the zip here."),
                    [{"key": "path", "type": "text", "value": "", "label": L("Percorso dello zip", "Path of the zip")}],
                    L("avanti", "next"))
    if not r or not r["path"].strip():
        raise Abort(L(f"Nessuna ROM: mettila in {folder} con il suo SHA256SUMS.",
                      f"No ROM: put it in {folder} with its SHA256SUMS."))
    return r["path"].strip().strip('"')


def resolve_rom(ctx):
    """Finds the ROM zip, checks its SHA-256 and its contents. Sets ctx.rom."""
    ui = ctx.ui
    dev = ctx.device
    ui.step(L("Controllo della ROM", "Checking the ROM"))
    path = ctx.opts.get("rom") or _pick_rom(ctx)
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise Abort(L(f"ROM non trovata: {path}", f"ROM not found: {path}"))
    name = os.path.basename(path)
    want = (ctx.opts.get("rom_sha256") or "").strip().lower()
    source = "--rom-sha256"
    if not want:
        want, source = find_rom_checksum(path)
        if not want:
            raise Abort(L(f"Nessun SHA-256 per {name}. Metti accanto allo zip il file SHA256SUMS della pagina di download, "
                          f"oppure usa --rom-sha256 <hash>.",
                          f"No SHA-256 found for {name}. Put the SHA256SUMS file from the download page next to the zip, "
                          f"or pass --rom-sha256 <hash>."))
    if not re.fullmatch(r"[0-9a-f]{64}", want):
        raise Abort(L(f"SHA-256 atteso non valido: {want}", f"The expected SHA-256 is not a valid hash: {want}"))
    ui.info(L(f"{name}: calcolo dello SHA-256 ({human_size(os.path.getsize(path))})…",
              f"{name}: computing SHA-256 ({human_size(os.path.getsize(path))})…"))
    digest = sha256_progress(ctx, path, "ROM SHA-256")
    if digest != want:
        raise Abort(L(f"Lo zip della ROM non corrisponde al suo checksum (zip {digest}, atteso {want} da {source}). "
                      "Il download è danneggiato o è un altro file: scaricalo di nuovo.",
                      f"The ROM zip does not match its checksum (zip {digest}, expected {want} from {source}). "
                      "The download is damaged or it is another file: download it again."))
    ui.ok(L(f"SHA-256 corrisponde ({os.path.basename(source)})", f"SHA-256 matches ({os.path.basename(source)})"))
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise Abort(L(f"{name} non è uno zip valido: {e}", f"{name} is not a valid zip file: {e}"))
    with z:
        names = set(z.namelist())
        if "META-INF/com/google/android/update-binary" not in names or "boot.img" not in names:
            raise Abort(L(f"{name} non è uno zip di ROM installabile (manca boot.img o update-binary).",
                          f"{name} is not a flashable ROM zip (no boot.img or update-binary)."))
        meta = {}
        if "META-INF/com/android/metadata" in names:
            for line in z.read("META-INF/com/android/metadata").decode("utf-8", "replace").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    meta[k.strip()] = v.strip()
        devices = [d.strip() for d in meta.get("pre-device", "").split(",") if d.strip()]
        if not devices:
            raise Abort(L(f"{name} non dice per quale telefono è (manca pre-device nei metadati OTA): non lo installo.",
                          f"{name} does not say which phone it is for (no pre-device in its OTA metadata): not installing it."))
        if not set(devices) & set(dev.rom_devices):
            raise Abort(L(f"{name} è per «{', '.join(devices)}», non per il {dev.title}.",
                          f"{name} is for '{', '.join(devices)}', not for the {dev.title}."))
        sdk = meta.get("post-sdk-level", "")
        if sdk and sdk != ROM_SDK:
            ui.warn(L(f"La ROM è Android SDK {sdk}: installer e TWRP sono pensati per LineageOS 18.1 (SDK {ROM_SDK}).",
                      f"The ROM is Android SDK {sdk}: installer and TWRP are meant for LineageOS 18.1 (SDK {ROM_SDK})."))
        info = z.getinfo("boot.img")
        bh = hashlib.sha256()
        with z.open(info) as f:
            for b in iter(lambda: f.read(CHUNK), b""):
                bh.update(b)
    ctx.rom = {"path": path, "name": name, "sha256": digest, "boot_size": info.file_size,
               "boot_sha": bh.hexdigest(), "build": meta.get("post-build", "")}
    ui.ok(L(f"Zip installabile per {dev.codename}; boot.img {info.file_size} byte",
            f"Flashable zip for {dev.codename}; boot.img {info.file_size} bytes"))
    return ctx.rom


# ---------------------------------------------------------------------------
# phone: fastboot (lk1st) and TWRP
# ---------------------------------------------------------------------------

FASTBOOT_HELP_IT = [
    "Collega il telefono e portalo in lk1st (fastboot):",
    " • da Android con il debug USB attivo: l'installer lo riavvia da solo (adb reboot bootloader);",
    " • dalla TWRP: Riavvia → Bootloader;",
    " • se Android non è installato o non si avvia, accendendo il telefono lk1st resta in fastboot",
    "   (schermo fermo dopo il testo di BootShim); con il menu di avvio attivo scegli «Android (lk1st)».",
    "Volume su all'accensione è la modalità flash di Windows Phone (WPinternals), non fastboot.",
]
FASTBOOT_HELP_EN = [
    "Connect the phone and bring it to lk1st (fastboot):",
    " • from Android with USB debugging on: the installer restarts it by itself (adb reboot bootloader);",
    " • from TWRP: Reboot → Bootloader;",
    " • if Android is not installed or does not start, powering the phone on leaves lk1st in fastboot",
    "   (still screen after the BootShim text); with the boot menu on, pick \"Android (lk1st)\".",
    "Volume up at power-on is Windows Phone's flash mode (WPinternals), not fastboot.",
]


def _one_device(devs, what):
    if len(devs) > 1:
        raise Abort(L(f"Più di un dispositivo in {what}: {', '.join(devs)}. Scollega gli altri o usa --serial.",
                      f"More than one device in {what}: {', '.join(devs)}. Disconnect the others or use --serial."))


def enter_fastboot(ctx, timeout=600):
    """Gets the phone to lk1st fastboot, from Android, TWRP or fastboot itself."""
    ui = ctx.ui
    ph = ctx.phone
    if not ph.fastboot_exe or not ph.adb_exe:
        raise Abort(L("Servono adb e fastboot: usa «Platform Tools» negli strumenti avanzati.",
                      "adb and fastboot are needed: use \"Platform Tools\" in the advanced tools."))
    ph.start_adb_server()
    fb = ph.fastboot_serials()
    _one_device(fb, "fastboot")
    if fb:
        ui.ok(L("Telefono in lk1st (fastboot)", "Phone in lk1st (fastboot)"))
        return
    devs = ph.adb_devices()
    _one_device([s for s, _ in devs], "adb")
    asked = False
    if devs and devs[0][1] == "unauthorized":
        ui.info(L("Autorizza il debug USB sullo schermo del telefono («Consenti sempre»).",
                  "Allow USB debugging on the phone screen (tick \"Always allow\")."))
        asked = True
        r = ui.wait(lambda: (ph.adb_devices() or [(None, None)])[0][1] in ("device", "recovery") or None, 180, 2)
        devs = ph.adb_devices() if r else devs
    rebooted = False
    if devs and devs[0][1] in ("device", "recovery", "sideload"):
        ui.info(L(f"Telefono trovato ({devs[0][1]}): riavvio in lk1st.", f"Phone found ({devs[0][1]}): restarting it into lk1st."))
        ph.adb(["reboot", "bootloader"], timeout=30)
        rebooted = True
    elif not asked:
        ui.show(L("Telefono in fastboot", "Phone in fastboot"), L(FASTBOOT_HELP_IT, FASTBOOT_HELP_EN))

    def poll():
        nonlocal rebooted
        fb = ph.fastboot_serials()
        _one_device(fb, "fastboot")
        if fb:
            return fb[0]
        if not rebooted:
            d = ph.adb_devices()
            if len(d) == 1 and d[0][1] in ("device", "recovery"):
                ui.info(L(f"Telefono in adb ({d[0][1]}): riavvio in lk1st.", f"Phone on adb ({d[0][1]}): restarting it into lk1st."))
                ph.adb(["reboot", "bootloader"], timeout=30)
                rebooted = True
        ui.status(L("Attesa di lk1st (fastboot)…", "Waiting for lk1st (fastboot)…"))
        return None

    if ui.wait(poll, timeout, 1.5) is None:
        raise Abort(L("Il telefono non è comparso in fastboot. Controlla il cavo e, su Windows, il driver USB "
                      "(Google USB Driver, «Android Bootloader Interface»).",
                      "The phone did not appear in fastboot. Check the cable and, on Windows, the USB driver "
                      "(Google USB Driver, \"Android Bootloader Interface\")."))
    ui.ok(L("Telefono in lk1st (fastboot)", "Phone in lk1st (fastboot)"))


def wait_adb_state(ctx, states, timeout, hint=None):
    ui = ctx.ui
    ph = ctx.phone
    next_hint = time.time() + 45

    def poll():
        nonlocal next_hint
        d = ph.adb_devices()
        _one_device([s for s, _ in d], "adb")
        if d and d[0][1] in states:
            return d[0][1]
        if hint and time.time() > next_hint:
            ui.info(hint)
            next_hint = time.time() + 45
        ui.status(L("Attesa del telefono su adb… ora: ", "Waiting for the phone on adb… now: ") + (d[0][1] if d else "-"))
        return None

    return ui.wait(poll, timeout, 1)


def boot_recovery(ctx, image, label="TWRP"):
    """lk1st -> fastboot boot <image> -> adb in recovery with a working root shell."""
    ui = ctx.ui
    enter_fastboot(ctx)
    ui.info(L(f"Avvio di {label} in RAM (fastboot boot {os.path.basename(image)}): non viene scritta sul telefono.",
              f"Starting {label} in RAM (fastboot boot {os.path.basename(image)}): it is not written to the phone."))
    rc, out = ctx.phone.fastboot(["boot", image], timeout=180)
    if rc != 0:
        raise Abort(L("fastboot boot non riuscito:\n", "fastboot boot failed:\n") + out.strip()[-600:])
    wait_recovery(ctx, label)


def wait_recovery(ctx, label="TWRP"):
    ui = ctx.ui
    ui.info(L(f"Attesa di {label} su adb (fino a 5 minuti)…", f"Waiting for {label} on adb (up to 5 minutes)…"))
    st = wait_adb_state(ctx, ("recovery",), 300,
                        L(f"Ancora in attesa di {label}. Se il telefono la mostra ma qui non succede niente, controlla il driver USB.",
                          f"Still waiting for {label}. If the phone shows it but nothing happens here, check the USB driver."))
    if not st:
        raise Abort(L(f"{label} non è comparsa su adb entro 5 minuti. Controlla cavo e driver USB, poi riavvia l'installer.",
                      f"{label} did not appear on adb within 5 minutes. Check the cable and the USB driver, then run the installer again."))
    end = time.time() + 120
    while time.time() < end:
        ui.check_cancel()
        rc, out = ctx.phone.adb(["shell", "echo bandit-shell-ok"], timeout=15, quiet=True)
        if "bandit-shell-ok" in out:
            break
        time.sleep(2)
    else:
        raise Abort(L(f"{label} è su adb ma la shell non risponde. Riavvia il telefono in lk1st e riprova.",
                      f"{label} is on adb but its shell does not answer. Restart the phone into lk1st and try again."))
    rc, out = ctx.phone.adb(["shell", "id -u"], timeout=15, quiet=True)
    if out.strip() != "0":
        raise Abort(L(f"adb in {label} non è root (uid {out.strip()}).", f"adb in {label} is not root (uid {out.strip()})."))
    ui.ok(L(f"{label} pronta, adb root", f"{label} ready, adb root"))
    check_model_in_recovery(ctx)


def model_from_dpp(data):
    """TYPE of DPP Nokia/product.dat (for example 'RM-937') from the raw DPP partition, or None."""
    try:
        text = fat.FatImage(data).read(PRODUCT_DAT).decode("ascii", "replace")
    except (fat.FatError, ValueError):
        return None
    return devices.normalize_model(devices.parse_product_dat(text).get("TYPE", "")) or None


def check_model(ctx, model, where):
    """Stops when the phone is not the one the profile in use is for."""
    dev = ctx.device
    if not model:
        ctx.ui.warn(L(f"Modello del telefono non leggibile ({where}): controlla tu che sia un {dev.title}.",
                      f"Could not read the phone model ({where}): make sure it is a {dev.title}."))
        return None
    if dev.matches_model(model):
        extra = (L(" (codice non ancora confermato)", " (code not confirmed yet)")
                 if model in dev.unverified_models else "")
        ctx.ui.ok(L(f"Telefono {model}: {dev.title}{extra}", f"Phone {model}: {dev.title}{extra}"))
        return model
    other = devices.by_model(model)
    if other:
        raise Abort(L(f"Il telefono è un {model} ({other.title}), ma l'installer è impostato sul {dev.title}. "
                      f"Scegli il {other.name} all'avvio (--device {other.codename}).",
                      f"The phone is an {model} ({other.title}), but the installer is set to the {dev.title}. "
                      f"Pick the {other.name} at start (--device {other.codename})."))
    raise Abort(L(f"Il telefono è un {model}: non è un modello supportato ({dev.title} = {', '.join(dev.models)}).",
                  f"The phone is an {model}: not a supported model ({dev.title} = {', '.join(dev.models)})."))


def check_model_in_recovery(ctx):
    rc, data = ctx.phone.adb_bytes(["exec-out", f"dd if={BYNAME}/DPP bs=4096 2>/dev/null"], timeout=60)
    return check_model(ctx, model_from_dpp(bytes(data)) if rc == 0 else None, "DPP")


def wait_twrp_commands(ctx):
    """Waits until the `twrp` command line client reaches the TWRP interface."""
    ui = ctx.ui
    end = time.time() + 180
    while time.time() < end:
        ui.check_cancel()
        rc, out = ctx.phone.adb(["shell", "twrp print bandit-twrp-ready"], timeout=20)
        if rc != 124 and "does not appear to be running" not in out:
            return
        if rc == 124:
            ctx.phone.adb(["shell", "pkill -f 'twrp [p]rint'"], timeout=15, quiet=True)
        ui.status(L("Attesa dell'interfaccia della TWRP…", "Waiting for the TWRP interface…"))
        time.sleep(5)
    raise Abort(L("L'interfaccia a comandi della TWRP non è partita entro 3 minuti. Riavvia il telefono in lk1st e riprova.",
                  "The TWRP command interface did not start within 3 minutes. Restart the phone into lk1st and try again."))


def recovery_log(ctx):
    rc, data = ctx.phone.adb_bytes(["exec-out", "cat /tmp/recovery.log"], timeout=60)
    if rc != 0:
        rc, text = ctx.phone.adb(["shell", "cat /tmp/recovery.log"], timeout=60, quiet=True)
        return text if rc == 0 else ""
    return data.decode("utf-8", "replace")


def twrp_command(ctx, command, timeout):
    """Runs `twrp <command>`; the client may crash afterwards, so the log decides."""
    ui = ctx.ui
    before = len(recovery_log(ctx))
    ui.info(f"twrp {command}")
    rc, out = ctx.phone.adb(["shell", f"twrp {command}"], timeout=timeout)
    if rc == 124:
        raise Abort(L(f"La TWRP non ha finito «{command}» entro {timeout} s.", f"TWRP did not finish '{command}' within {timeout} s."))
    new = recovery_log(ctx)[before:]
    for line in new.splitlines():
        if SHOW_LOG_RE.search(line):
            ui.detail("  " + line.strip())
    m = TWRP_ERROR_RE.search(out + "\n" + new)
    if m:
        raise Abort(L(f"La TWRP ha segnalato un errore durante «{command}»: {m.group(0).strip()}",
                      f"TWRP reported an error during '{command}': {m.group(0).strip()}"))
    if "Segmentation fault" in out:
        ui.detail(L("  (il client twrp si è chiuso con «Segmentation fault» dopo il comando: è normale con questa TWRP)",
                    "  (the twrp client ended with \"Segmentation fault\" after the command: normal with this TWRP)"))


def android_partitions(ctx):
    rc, out = ctx.phone.adb(["shell", f"ls {BYNAME}"], timeout=20, quiet=True)
    return set(out.split())


def check_android_layout(ctx):
    names = android_partitions(ctx)
    missing = [p for p in ANDROID_PARTS if p not in names]
    if missing:
        if {"MainOS", "Data"} & names:
            raise Abort(L("Il telefono ha ancora le partizioni di Windows (MainOS, Data) e non quelle Android: "
                          "usa la modalità Completa.",
                          "The phone still has the Windows partitions (MainOS, Data) and not the Android ones: "
                          "use the Full mode."))
        raise Abort(L(f"Partizioni Android mancanti: {', '.join(missing)}. Usa la modalità Completa.",
                      f"Android partitions missing: {', '.join(missing)}. Use the Full mode."))
    ctx.ui.ok(L("Partizioni Android presenti", "Android partitions present"))


def disk_guid(ctx):
    """Disk GUID of the eMMC (key of the saved progress), or None."""
    import struct
    import uuid
    rc, hdr = ctx.phone.adb_bytes(["exec-out", f"dd if={DISK} bs=512 skip=1 count=1 2>/dev/null"], timeout=30)
    if rc != 0 or len(hdr) < 92 or hdr[:8] != b"EFI PART":
        return None
    return str(uuid.UUID(bytes_le=bytes(hdr[56:72]))).upper() if struct.unpack_from("<I", hdr, 12)[0] >= 92 else None


def remember_phone(ctx):
    g = disk_guid(ctx)
    if g:
        ctx.state.set_phone(g, {})
    return g


def battery_check(ctx):
    rc, out = ctx.phone.adb(["shell", "cat /sys/class/power_supply/battery/capacity"], timeout=15, quiet=True)
    if out.strip().isdigit():
        level = int(out.strip())
        if level < 30:
            ctx.ui.warn(L(f"Batteria al {level}%: meglio sopra il 50% (in TWRP con il cavo del PC si carica poco).",
                          f"Battery at {level}%: better above 50% (in TWRP the PC cable charges it slowly)."))
        else:
            ctx.ui.info(L(f"Batteria al {level}%", f"Battery at {level}%"))


def installed_system(ctx):
    """{'sdk', 'lineage', 'device'} read from the system partition, or None."""
    mnt = "/tmp/bandit-system"
    cmd = (f"mkdir -p {mnt}; umount {mnt} 2>/dev/null; mount -t ext4 -o ro {BYNAME}/system {mnt} && "
           f"grep -h -E '^ro\\.(build\\.version\\.sdk|lineage\\.build\\.version|lineage\\.device|product\\.device)=' "
           f"{mnt}/system/build.prop {mnt}/build.prop 2>/dev/null; umount {mnt} 2>/dev/null; echo bandit-props-done")
    rc, out = ctx.phone.adb(["shell", cmd], timeout=60)
    if "bandit-props-done" not in out:
        return None
    props = {}
    for line in out.splitlines():
        if "=" in line and line.startswith("ro."):
            k, v = line.strip().split("=", 1)
            props.setdefault(k, v)
    if not props:
        return None
    return {"sdk": props.get("ro.build.version.sdk", ""), "lineage": props.get("ro.lineage.build.version", ""),
            "device": props.get("ro.lineage.device") or props.get("ro.product.device", "")}


def check_update_target(ctx):
    ui = ctx.ui
    info = installed_system(ctx)
    if info:
        ui.info(L(f"Installato: LineageOS «{info['lineage']}», Android SDK «{info['sdk']}», dispositivo «{info['device']}»",
                  f"Installed: LineageOS '{info['lineage']}', Android SDK '{info['sdk']}', device '{info['device']}'"))
        dev = ctx.device
        if info["sdk"] == ROM_SDK and info["lineage"].startswith("18.1") and info["device"] in dev.rom_devices:
            ui.ok(L(f"Sul telefono c'è LineageOS 18.1 per {dev.codename}: i dati restano.",
                    f"The phone runs LineageOS 18.1 for {dev.codename}: data is kept."))
            return
        ui.warn(L(f"Non è LineageOS 18.1 per {dev.codename}. Aggiornare un altro sistema senza formattare i dati di solito finisce "
                  "in un riavvio continuo: per quello usa la modalità Pulita.",
                  f"This is not LineageOS 18.1 for {dev.codename}. Updating another system without formatting data usually ends "
                  "in a boot loop: use the Clean mode for that."))
    else:
        ui.warn(L("Non riesco a leggere il sistema installato (partizione system vuota o illeggibile).",
                  "Could not read the installed system (system partition empty or unreadable)."))
    if not ui.confirm(L("Aggiornare comunque, conservando i dati?", "Update anyway, keeping data?"),
                      L("Niente è stato scritto: il telefono è nella TWRP.", "Nothing has been written: the phone is in TWRP."),
                      L("aggiorna", "update"), L("annulla", "cancel"), word=L("AGGIORNA", "UPDATE"), danger=True):
        raise Cancelled()


def wipe(ctx):
    ui = ctx.ui
    ui.step(L("Formattazione dei dati, pulizia di cache e Dalvik", "Format data, wipe cache and Dalvik"))
    wait_twrp_commands(ctx)
    twrp_command(ctx, "format data", 1800)
    twrp_command(ctx, "wipe cache", 600)
    twrp_command(ctx, "wipe dalvik", 600)
    rc, out = ctx.phone.adb(["shell", "ls -d /data/system 2>/dev/null; echo bandit-data-checked"], timeout=30)
    if "/data/system" in out:
        raise Abort(L("Dopo la formattazione /data contiene ancora i dati di Android.", "/data still contains Android data after the format."))
    ui.ok(L("Dati formattati, cache e Dalvik puliti", "Data formatted, cache and Dalvik wiped"))


def enter_sideload(ctx):
    ui = ctx.ui
    for attempt in range(1, 7):
        if attempt > 1:
            ui.info(L(f"La TWRP non è ancora in sideload, nuovo tentativo ({attempt}/6)…",
                      f"TWRP is not in sideload mode yet, retrying ({attempt}/6)…"))
            time.sleep(5)
        p = ctx.phone.adb_background(["shell", "twrp sideload"])
        try:
            end = time.time() + 25
            while time.time() < end:
                ui.check_cancel()
                time.sleep(1)
                d = ctx.phone.adb_devices()
                if len(d) == 1 and d[0][1] == "sideload":
                    return
        finally:
            if p.poll() is None:
                p.kill()
            p.wait()
    raise Abort(L("La TWRP non è entrata in ADB sideload. Sul telefono scegli Avanzate → ADB Sideload, scorri, "
                  f"poi: adb sideload \"{ctx.rom['path']}\"",
                  "TWRP did not enter ADB sideload mode. On the phone choose Advanced → ADB Sideload and swipe, "
                  f"then run: adb sideload \"{ctx.rom['path']}\""))


def sideload(ctx, log_dir):
    """adb sideload of the ROM; the TWRP log decides the result."""
    ui = ctx.ui
    ui.step(L("Installazione della ROM (adb sideload)", "Installing the ROM (adb sideload)"))
    wait_twrp_commands(ctx)
    before = len(recovery_log(ctx))
    enter_sideload(ctx)
    ui.info(L("Invio della ROM: la TWRP la installa mentre arriva (alcuni minuti). Il trasferimento si ferma intorno "
              "al 47% con «Total xfer: 1.00x»: è normale, la ROM è a blocchi.",
              "Sending the ROM: TWRP installs it while it arrives (a few minutes). The transfer stops around 47% "
              "with \"Total xfer: 1.00x\": that is normal, the ROM is block based."))
    last = [0]

    def on_text(piece):
        m = re.search(r"\(~\s*(\d+)%\)", piece)
        if m:
            pct = int(m.group(1))
            if pct != last[0]:
                last[0] = pct
                ui.progress("adb sideload", pct, 100)
        elif piece:
            ui.detail("  " + piece)

    rc, out = ctx.phone.adb_stream(["sideload", ctx.rom["path"]], on_text, timeout=3600)
    if rc == 124:
        raise Abort(L("adb sideload non è finito entro un'ora. Guarda lo schermo del telefono.",
                      "adb sideload did not finish within an hour. Look at the phone screen."))
    if rc != 0:
        ui.info(L(f"adb sideload è terminato con codice {rc}: la TWRP spesso segnala un errore di trasferimento alla fine "
                  "anche quando l'installazione è riuscita. Controllo il log della TWRP.",
                  f"adb sideload ended with code {rc}. TWRP often reports a transfer error at the end even when the "
                  "install worked: checking the TWRP log."))
    if not wait_adb_state(ctx, ("recovery",), 900, L("La TWRP sta finendo l'installazione…", "TWRP is finishing the installation…")):
        raise Abort(L("La TWRP non è tornata dopo il sideload. Guarda lo schermo; poi riavvia l'installer in modalità "
                      "Aggiornamento o Pulita.",
                      "TWRP did not come back after the sideload. Look at the phone screen; then run the installer "
                      "again in Update or Clean mode."))
    wait_recovery(ctx)
    log = recovery_log(ctx)
    with open(os.path.join(log_dir, "recovery.log"), "w", encoding="utf-8") as f:
        f.write(log)
    new = log[before:] if len(log) >= before else log
    shown = [l.strip() for l in new.splitlines() if SHOW_LOG_RE.search(l)][-10:]
    if shown:
        ui.show(L("Log della TWRP", "TWRP log"), shown)
    if FAILURE_RE.search(new):
        raise Abort(L(f"La TWRP segnala che l'installazione della ROM non è riuscita. Log in {log_dir}.",
                      f"TWRP reports that the ROM installation failed. Log in {log_dir}."))
    if not any(s in new for s in SUCCESS_LINES):
        raise Abort(L(f"Il log della TWRP non conferma l'installazione della ROM. Log in {log_dir}.",
                      f"The TWRP log does not confirm the ROM installation. Log in {log_dir}."))
    ui.ok(L("La TWRP conferma l'installazione (script succeeded)", "TWRP confirms the installation (script succeeded)"))


EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def prefix_sha256(phone, path, size, timeout=300):
    """SHA-256 of the first `size` bytes of a file on the phone ("" if it cannot be read).

    A shell without `head -c` still runs sha256sum on empty input: that hash means "failed".
    """
    def hashed(cmd):
        rc, out = phone.adb(["shell", cmd], timeout=timeout)
        got = (out.split() or [""])[0].lower()
        ok = re.fullmatch(r"[0-9a-f]{64}", got) and (got != EMPTY_SHA256 or size == 0)
        return got if ok else ""

    got = hashed(f"head -c {size} {path} | sha256sum")
    if not got and size % 4096 == 0:  # older toybox without head -c
        got = hashed(f"dd if={path} bs=4096 count={size // 4096} 2>/dev/null | sha256sum")
    return got


def check_boot(ctx):
    ui = ctx.ui
    size = ctx.rom["boot_size"]
    got = prefix_sha256(ctx.phone, f"{BYNAME}/boot", size)
    if got != ctx.rom["boot_sha"]:
        raise Abort(L(f"La partizione boot non corrisponde a boot.img dello zip (telefono {got}, zip {ctx.rom['boot_sha']}). "
                      "L'installazione è incompleta: riavvia l'installer.",
                      f"The boot partition does not match boot.img from the zip (phone {got}, zip {ctx.rom['boot_sha']}). "
                      "The installation is incomplete: run the installer again."))
    ui.ok(L("Partizione boot = boot.img dello zip", "boot partition = boot.img of the zip"))


def finish(ctx, clean):
    ui = ctx.ui
    ui.step(L("Riavvio in Android", "Restarting into Android"))
    ctx.phone.adb(["reboot"], timeout=30)
    ui.ok(L("Fatto. Il primo avvio richiede diversi minuti" + (", di più dopo un'installazione pulita." if clean else "."),
            "Done. The first start takes several minutes" + (", longer after a clean install." if clean else ".")))


def install_rom(ctx, wipe_data, before_write=None, twrp_running=False):
    """TWRP in RAM, optional wipe, sideload, checks, reboot. Expects resolve_twrp/resolve_rom done.

    before_write(ctx) runs in TWRP before anything is written (questions, checks).
    """
    ui = ctx.ui
    log_dir = ctx.path("logs", f"rom-{stamp()}")
    os.makedirs(log_dir, exist_ok=True)
    if not twrp_running:
        boot_recovery(ctx, ctx.twrp)
    remember_phone(ctx)
    check_android_layout(ctx)
    battery_check(ctx)
    if before_write:
        before_write(ctx)
    if wipe_data:
        wipe(ctx)
    sideload(ctx, log_dir)
    check_boot(ctx)
    ctx.state.mark("rom", rom=ctx.rom["name"], sha256=ctx.rom["sha256"], wiped=bool(wipe_data), log=log_dir)
    ctx.give_back(log_dir)
    finish(ctx, wipe_data)
