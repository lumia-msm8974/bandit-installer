"""What mode the phone is in, and fastboot/adb.

The mode comes from the USB ids (cheap, works without drivers) and, for
fastboot and adb, from the tools themselves.
"""
import os
import queue
import re
import subprocess
import threading
import time

from . import sysdeps
from .i18n import L

# (vid, pid) -> mode
USB_MODES = {
    ("045e", "9006"): "mass",       # WPinternals mass storage ("Phone MMC Stor")
    ("0421", "0714"): "bootmgr",    # Windows Phone boot manager starting
    ("0421", "066e"): "flash",      # UEFI flash mode (volume up)
    ("0421", "0661"): "windows",    # Windows Phone running
    ("0421", "06fc"): "windows",    # Windows Phone running (Lumia 930 RM-1045, MTP)
    ("045e", "0a00"): "windows",    # Windows 10 Mobile running (Microsoft Lumia, MTP)
    ("18d1", "d00d"): "fastboot",   # lk1st / any fastboot
    ("18d1", "d00e"): "k1",         # K1 test kernel (CDC-ACM serial)
    ("05c6", "9008"): "edl",        # Qualcomm emergency download
}


def mode_text(mode):
    return {
        "none": L("non collegato", "not connected"),
        "mass": L("mass storage (WPinternals)", "mass storage (WPinternals)"),
        "bootmgr": L("boot manager di Windows Phone", "Windows Phone boot manager"),
        "flash": L("modalità flash (volume su)", "flash mode (volume up)"),
        "windows": L("Windows Phone avviato", "Windows Phone running"),
        "fastboot": L("fastboot", "fastboot"),
        "k1": L("kernel di prova K1 (seriale USB)", "K1 test kernel (USB serial)"),
        "edl": L("Qualcomm 9008 (emergenza)", "Qualcomm 9008 (emergency)"),
        "adb": L("Android (adb)", "Android (adb)"),
        "recovery": L("recovery (adb)", "recovery (adb)"),
        "sideload": L("recovery, sideload", "recovery, sideload"),
        "unauthorized": L("Android, adb non autorizzato", "Android, adb not authorised"),
        "android": L("Android (senza adb)", "Android (no adb)"),
    }.get(mode, mode)


class Snapshot:
    def __init__(self, mode="none", detail="", usb=None, problem="", when=None):
        self.mode = mode
        self.detail = detail
        self.usb = usb or []
        self.problem = problem      # e.g. Windows driver missing
        self.when = when or time.time()

    def text(self):
        t = mode_text(self.mode)
        if self.detail:
            t += f" — {self.detail}"
        return t


class Phone:
    """fastboot and adb. With a serial (--serial or ANDROID_SERIAL) only that device is used."""

    def __init__(self, system, log, adb=None, fastboot=None, serial=None, scan_usb=True):
        self.system = system
        self.log = log
        self.adb_override = adb
        self.fastboot_override = fastboot
        self.serial = serial or os.environ.get("ANDROID_SERIAL") or None
        self.scan_usb = scan_usb and not os.environ.get("BANDIT_NO_USB_SCAN")
        self.refresh_tools()

    def refresh_tools(self):
        self.fastboot_exe = self.fastboot_override or self.system.find_tool("fastboot")
        self.adb_exe = self.adb_override or self.system.find_tool("adb")

    def _sel(self, args):
        return (["-s", self.serial] if self.serial and args[:1] != ["devices"] else []) + list(args)

    # tools -----------------------------------------------------------------
    def fastboot(self, args, timeout=60, quiet=False):
        if not self.fastboot_exe:
            return 127, "fastboot not found"
        # empty stdin: never a terminal, so nothing on the phone side waits for a key
        rc, out = sysdeps.run([self.fastboot_exe] + self._sel(args), timeout=timeout, input=b"")
        if not quiet:
            self.log(f"$ fastboot {' '.join(args)}  (rc={rc})\n{out.rstrip()}")
        return rc, out

    def adb(self, args, timeout=60, quiet=False):
        if not self.adb_exe:
            return 127, "adb not found"
        rc, out = sysdeps.run([self.adb_exe] + self._sel(args), timeout=timeout, input=b"")
        if not quiet:
            self.log(f"$ adb {' '.join(args)}  (rc={rc})\n{out.rstrip()}")
        return rc, out

    def adb_bytes(self, args, timeout=120):
        """Runs adb and returns (rc, stdout bytes): for exec-out reads of partitions."""
        if not self.adb_exe:
            return 127, b""
        try:
            p = subprocess.run([self.adb_exe] + self._sel(args), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, b""
        if p.returncode:
            self.log(f"$ adb {' '.join(args)}  (rc={p.returncode})\n{p.stderr.decode('utf-8', 'replace').rstrip()}")
        return p.returncode, p.stdout

    def adb_background(self, args):
        """Starts adb without waiting (twrp sideload blocks until adbd restarts)."""
        self.log(f"$ adb {' '.join(args)}  (background)")
        return subprocess.Popen([self.adb_exe] + self._sel(args), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def adb_stream(self, args, on_text, timeout):
        """Runs adb, calling on_text(piece) for every line or \\r-separated piece of its output.

        Returns (rc, whole output). rc 124 on timeout (the process is killed).
        """
        if not self.adb_exe:
            return 127, "adb not found"
        p = subprocess.Popen([self.adb_exe] + self._sel(args), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        q = queue.Queue()

        def reader():
            while True:
                b = p.stdout.read1(4096) if hasattr(p.stdout, "read1") else p.stdout.read(1)
                if not b:
                    break
                q.put(b)
            q.put(None)

        threading.Thread(target=reader, daemon=True).start()
        deadline = time.time() + timeout
        buf, out = "", []
        while True:
            try:
                b = q.get(timeout=0.5)
            except queue.Empty:
                if time.time() > deadline:
                    p.kill()
                    p.wait()
                    self.log(f"$ adb {' '.join(args)}  (timeout)\n{''.join(out).rstrip()}")
                    return 124, "".join(out)
                continue
            if b is None:
                break
            text = b.decode("utf-8", "replace")
            out.append(text)
            buf += text
            *pieces, buf = re.split(r"[\r\n]", buf)
            for piece in pieces:
                if piece.strip():
                    on_text(piece.strip())
        if buf.strip():
            on_text(buf.strip())
        rc = p.wait()
        self.log(f"$ adb {' '.join(args)}  (rc={rc})\n{''.join(out).rstrip()}")
        return rc, "".join(out)

    def start_adb_server(self):
        """Starts the adb server outside our pipes, so it never holds them open (no-op if running)."""
        if not self.adb_exe:
            return
        try:
            subprocess.Popen([self.adb_exe, "start-server"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).wait(60)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def fastboot_serials(self):
        rc, out = self.fastboot(["devices"], timeout=10, quiet=True)
        serials = [l.split()[0] for l in out.splitlines() if l.strip().endswith("fastboot")]
        return [s for s in serials if not self.serial or s == self.serial]

    def adb_devices(self):
        """[(serial, state)] as adb lists them (device, recovery, sideload, unauthorized…)."""
        rc, out = self.adb(["devices"], timeout=10, quiet=True)
        found = []
        for l in out.splitlines()[1:]:
            parts = l.split(None, 1)
            if len(parts) == 2 and not l.startswith("*"):
                found.append((parts[0], parts[1].strip()))
        return [d for d in found if not self.serial or d[0] == self.serial]

    def adb_states(self):
        return [st for _s, st in self.adb_devices()]

    def getvar(self, name):
        rc, out = self.fastboot(["getvar", name], timeout=15, quiet=True)
        m = re.search(rf"^{re.escape(name)}:\s*(.*)$", out, re.M)
        return m.group(1).strip() if m else None

    # mode ------------------------------------------------------------------
    def snapshot(self, use_tools=True):
        usb = self.system.usb_devices() if self.scan_usb else []
        modes = []
        problem = ""
        for d in usb:
            m = USB_MODES.get((d.vid, d.pid))
            if m:
                modes.append((m, d))
                if d.status and d.status not in ("OK", "Unknown") and sysdeps.IS_WINDOWS:
                    problem = L(f"Windows segnala {d.status} per {d.id}: driver mancante?",
                                f"Windows reports {d.status} for {d.id}: missing driver?")
        names = [m for m, _ in modes]
        if use_tools and ("fastboot" in names or not names):
            serials = self.fastboot_serials() if self.fastboot_exe else []
            if serials:
                product = self.getvar("product") or "?"
                return Snapshot("fastboot", product, usb, problem)
        if use_tools and self.adb_exe and not set(names) & {"mass", "fastboot", "edl", "bootmgr", "flash"}:
            st = self.adb_states()
            for s in st:
                if s in ("device",):
                    return Snapshot("adb", "", usb, problem)
                if s in ("recovery", "sideload", "unauthorized"):
                    return Snapshot(s, "", usb, problem)
        for pref in ("edl", "mass", "fastboot", "k1", "flash", "bootmgr", "windows"):
            if pref in names:
                return Snapshot(pref, "", usb, problem)
        if any(d.vid == "18d1" for d in usb):
            return Snapshot("android", "", usb, problem)
        return Snapshot("none", "", usb, problem)


class Poller:
    """Refreshes the phone mode in the background while menus wait for keys.

    Paused during operations: a background `fastboot devices` must never run
    while a transfer is in progress.
    """

    def __init__(self, phone, interval):
        self.phone = phone
        self.interval = interval
        self.snap = Snapshot()
        self.version = 0
        self._active = False
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            self._wake.wait(self.interval)
            self._wake.clear()
            with self._lock:
                if not self._active:
                    continue
                try:
                    s = self.phone.snapshot()
                except Exception as e:  # never kill the UI thread
                    s = Snapshot("none", f"error: {e}")
            if s.mode != self.snap.mode or s.detail != self.snap.detail or s.problem != self.snap.problem:
                self.version += 1
            self.snap = s

    def start(self):
        self._active = True
        self._wake.set()

    def stop(self):
        """Stops polling and waits for a running poll to finish."""
        self._active = False
        with self._lock:
            pass

    def now(self):
        """Fresh snapshot taken in the foreground."""
        self.stop()
        self.snap = self.phone.snapshot()
        self.version += 1
        return self.snap
