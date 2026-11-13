"""A fake Lumia 1520 for the flow tests: what adb and fastboot see.

Nothing here talks to a real device. State lives in $FAKE_STATE:

  state         fastboot | booting | recovery | sideload | device | off
  disk.img      the eMMC (sparse image with a real GPT)
  kparts.json   the partition table as the kernel sees it (read at boot and on
                blockdev --rereadpt, like Linux)
  recovery.log  TWRP's /tmp/recovery.log
  calls.log     every adb/fastboot call
  remote/       files pushed to /tmp; persist/ files written into the mounted persist
  wiped, installed, booted-image, rebooted, formatted   what happened

Switches (environment): FAKE_INSTALLED ("sdk=30 lineage=18.1 device=bandit", "none"),
FAKE_SIDELOAD_FAIL, FAKE_SIDELOAD_LOST (first `twrp sideload` lost), FAKE_REREAD_BUSY
(blockdev --rereadpt fails), FAKE_BOOT_BAD (boot partition not written), FAKE_MOUNTED,
FAKE_NO_HEAD (toybox without head).

TWRP behaviours copied from the real phone: the `twrp` client prints "Segmentation
fault" and exits 139 after the command; adb sideload stops at 47% with
"Total xfer: 1.00x"; toybox dd without conv=notrunc fails on a block device.
"""
import hashlib
import json
import os
import re
import shlex
import struct
import sys
import time
import zipfile

S = os.environ.get("FAKE_STATE")
SERIAL = os.environ.get("FAKE_SERIAL", "bandit0001")
BYNAME = "/dev/block/platform/msm_sdcc.1/by-name"
DISK = "/dev/block/mmcblk0"
SECTOR = 512


def p(*a):
    return os.path.join(S, *a)


def rd(name, default=""):
    try:
        with open(p(name)) as f:
            return f.read().strip()
    except OSError:
        return default


def wr(name, text):
    with open(p(name), "w") as f:
        f.write(text)


def append(name, text):
    with open(p(name), "a") as f:
        f.write(text)


def count(name):
    n = int(rd("n-" + name, "0") or 0) + 1
    wr("n-" + name, str(n))
    return n


def out(text=""):
    sys.stdout.write(text)
    sys.stdout.flush()


def err(text):
    sys.stderr.write(text)
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# disk and kernel partition table
# ---------------------------------------------------------------------------

def disk_size():
    return os.path.getsize(p("disk.img"))


def read_gpt_parts():
    with open(p("disk.img"), "rb") as f:
        f.seek(SECTOR)
        h = f.read(SECTOR)
        if h[:8] != b"EFI PART":
            return []
        lba, n, esize = struct.unpack_from("<QII", h, 72)
        f.seek(lba * SECTOR)
        t = f.read(n * esize)
    parts = []
    for i in range(n):
        e = t[i * esize:(i + 1) * esize]
        if e[:16] == bytes(16):
            continue
        first, last = struct.unpack_from("<QQ", e, 32)
        name = e[56:128].decode("utf-16le").split("\0", 1)[0]
        parts.append([i + 1, name, first, last])
    return parts


def kernel_reread():
    wr("kparts.json", json.dumps(read_gpt_parts()))


def kparts():
    try:
        with open(p("kparts.json")) as f:
            return json.load(f)
    except OSError:
        return []


def device(path):
    """(offset, size) of a block device path, or None."""
    if path == DISK:
        return 0, disk_size()
    m = re.fullmatch(re.escape(DISK) + r"p(\d+)", path)
    for num, name, first, last in kparts():
        if (m and num == int(m.group(1))) or path == f"{BYNAME}/{name}":
            return first * SECTOR, (last - first + 1) * SECTOR
    return None


def dev_read(path, off=0, n=None):
    d = device(path)
    if d is None:
        raise FileNotFoundError(path)
    base, size = d
    n = size - off if n is None else max(0, min(n, size - off))
    with open(p("disk.img"), "rb") as f:
        f.seek(base + off)
        return f.read(n)


def dev_write(path, off, data):
    base, size = device(path)
    if off + len(data) > size:
        raise OSError("No space left on device")
    with open(p("disk.img"), "r+b") as f:
        f.seek(base + off)
        f.write(data)


def remote(path):
    """Local file for a phone path under /tmp."""
    if path.startswith("/tmp/persist/"):
        return p("persist", path[len("/tmp/persist/"):])
    return p("remote", path.lstrip("/").replace("/", "_"))


def file_read(path):
    if path == "/tmp/recovery.log":
        return open(p("recovery.log"), "rb").read() if os.path.exists(p("recovery.log")) else b""
    if device(path) is not None:
        return dev_read(path)
    with open(remote(path), "rb") as f:
        return f.read()


def log_line(text):
    append("recovery.log", text + "\n")


# ---------------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------------

def dd(args):
    """Returns bytes for stdout (reads to stdout) or None; raises on errors."""
    opts = dict(a.split("=", 1) for a in args if "=" in a)
    bs = int(opts.get("bs", "512"))
    skip, seek = int(opts.get("skip", 0)), int(opts.get("seek", 0))
    cnt = int(opts["count"]) if "count" in opts else None
    src = opts["if"]
    if src == "/dev/zero":
        data = bytes(bs * cnt)
    elif device(src) is not None:
        data = dev_read(src, skip * bs, None if cnt is None else cnt * bs)
    else:
        whole = file_read(src)
        data = whole[skip * bs:] if cnt is None else whole[skip * bs:(skip + cnt) * bs]
    dst = opts.get("of")
    if not dst:
        return data
    if device(dst) is not None:
        if "notrunc" not in opts.get("conv", ""):
            raise OSError(f"dd: {dst}: ftruncate: Invalid argument")
        dev_write(dst, seek * bs, data)
        return None
    with open(remote(dst), "wb") as f:
        f.write(data)
    return None


def ext4_marked(path):
    return dev_read(path, 0x438, 2) == b"\x53\xef"


def run_segment(seg):
    """One simple command (no ; or &&). Returns (rc, stdout bytes)."""
    seg = seg.strip()
    if not seg:
        return 0, b""
    seg = re.sub(r"\s*2>(/dev/null|&1)", "", seg)
    if "|" in seg:
        left, right = [x.strip() for x in seg.split("|", 1)]
        rc, data = run_segment(left)
        if rc != 0:  # the error went to stderr: the next command reads nothing
            sys.stderr.write(data.decode("utf-8", "replace"))
            data = b""
        if right == "sha256sum":   # a pipeline's status is the last command's
            return 0, (hashlib.sha256(data).hexdigest() + "  -\n").encode()
        if right.startswith("tail"):
            return 0, b"\n".join(data.splitlines()[-2:]) + b"\n"
        return 1, f"fake shell: pipe {right}\n".encode()
    a = shlex.split(seg)
    c = a[0]
    if c in ("sync", "true"):
        return 0, b""
    if c == "echo":
        return 0, (" ".join(a[1:]) + "\n").encode()
    if c == "id" and a[1:] == ["-u"]:
        return 0, b"0\n"
    if c == "sleep":
        return 0, b""
    if c == "mkdir":
        return 0, b""
    if c == "rm":
        for x in a[1:]:
            if not x.startswith("-") and os.path.exists(remote(x)):
                os.remove(remote(x))
        return 0, b""
    if c == "dd":
        try:
            data = dd(a[1:])
        except (OSError, KeyError) as e:
            return 1, f"{e}\n".encode()
        return 0, data or b""
    if c == "head" and os.environ.get("FAKE_NO_HEAD"):
        return 127, b"head: not found\n"
    if c == "head" and a[1] == "-c":
        if device(a[3]) is not None:
            return 0, dev_read(a[3], 0, int(a[2]))
        return 0, file_read(a[3])[:int(a[2])]
    if c == "cat":
        if a[1] == "/sys/block/mmcblk0/size":
            return 0, f"{disk_size() // SECTOR}\n".encode()
        m = re.fullmatch(r"/sys/block/mmcblk0/mmcblk0p(\d+)/size", a[1])
        if m:
            d = device(f"{DISK}p{m.group(1)}")
            return (0, f"{d[1] // SECTOR}\n".encode()) if d else (1, b"No such file\n")
        if a[1] == "/proc/filesystems":
            return 0, b"nodev\tsysfs\n\text4\n\tvfat\n"
        if a[1] == "/sys/class/power_supply/battery/capacity":
            return 0, b"80\n"
        try:
            return 0, file_read(a[1])
        except OSError:
            return 1, f"cat: {a[1]}: No such file\n".encode()
    if c == "sha256sum":
        lines = []
        for x in a[1:]:
            lines.append(f"{hashlib.sha256(file_read(x)).hexdigest()}  {x}")
        return 0, ("\n".join(lines) + "\n").encode()
    if c == "ls":
        if a[-1] == BYNAME:
            return 0, ("\n".join(sorted(n for _i, n, _f, _l in kparts())) + "\n").encode()
        return 1, b""
    if c in ("mke2fs", "make_ext4fs"):
        dev = a[-1]
        dev_write(dev, 0x438, b"\x53\xef")
        append("formatted", dev + "\n")
        return 0, b"Creating journal (8192 blocks): done\nWriting superblocks and filesystem accounting information: done\n"
    if c == "mount":
        dev, mnt = a[-2], a[-1]
        if mnt == "/tmp/persist":
            if not ext4_marked(dev):
                return 1, b"mount: Invalid argument\n"
            os.makedirs(p("persist"), exist_ok=True)
            wr("persist-mounted", "1")
            return 0, b""
        if mnt == "/tmp/bandit-system":
            return (1, b"mount: Invalid argument\n") if os.environ.get("FAKE_INSTALLED") == "none" else (0, b"")
        return 1, b"mount: unsupported\n"
    if c == "umount":
        if a[-1] == "/tmp/persist" and os.path.exists(p("persist-mounted")):
            os.remove(p("persist-mounted"))
        return 0, b""
    if c == "blockdev" and a[1] == "--rereadpt":
        if os.environ.get("FAKE_REREAD_BUSY"):
            return 1, b"blockdev: ioctl BLKRRPART: Device or resource busy\n"
        kernel_reread()
        return 0, b""
    if c == "pkill":
        return 0, b""
    if c == "grep":
        return 1, b""
    return 127, f"fake shell: unsupported: {seg}\n".encode()


def run_shell(cmd):
    """A tiny shell: ; and && between simple commands, plus the installer's compound checks."""
    if cmd.startswith("for d in /sys/block/mmcblk0/mmcblk0p*"):
        return 0, "".join(f"{n} PARTNAME={name}\n" for n, name, _f, _l in kparts()).encode()
    if "getprop sys.usb.config" in cmd:   # android.settle_usb: uptime, USB config and state
        return 0, b"30.00 mtp,adb mtp,adb\n"
    if "bandit-tools-checked" in cmd:
        return 0, b"bandit-tools-checked\n"
    if "bandit-mounts-checked" in cmd:
        return 0, (b"/dev/block/mmcblk0p27 /efiesp vfat rw 0 0\n" if os.environ.get("FAKE_MOUNTED") else b"") \
            + b"bandit-mounts-checked\n"
    if "bandit-props-done" in cmd:
        inst = os.environ.get("FAKE_INSTALLED", "sdk=30 lineage=18.1 device=bandit")
        if inst == "none":
            return 0, b"bandit-props-done\n"
        kv = dict(x.split("=", 1) for x in inst.split())
        return 0, (f"ro.build.version.sdk={kv['sdk']}\nro.lineage.build.version={kv['lineage']}\n"
                   f"ro.lineage.device={kv['device']}\nbandit-props-done\n").encode()
    if "bandit-data-checked" in cmd:
        return 0, b"bandit-data-checked\n"
    if "bandit-reread-done" in cmd:
        rc, o = run_segment(cmd.split(";")[0])
        return 0, o + b"bandit-reread-done\n"
    if cmd.startswith("twrp "):
        return twrp(cmd[5:].strip())
    output, rc = b"", 0
    for part in re.split(r"\s*;\s*", cmd):
        for i, seg in enumerate(re.split(r"\s*&&\s*", part)):
            rc, o = run_segment(seg)
            output += o
            if rc != 0:
                break
    return rc, output


def twrp(command):
    if command.startswith("print"):
        if count("ors") == 1:
            return 0, b"TWRP does not appear to be running. Waiting for TWRP to start . . .\n"
        return 139, b"Segmentation fault\n"
    if command == "sideload":
        if os.environ.get("FAKE_SIDELOAD_LOST") and count("twrp-sideload") == 1:
            time.sleep(60)
            return 0, b""
        wr("state", "sideload")
        log_line("Starting ADB sideload feature...")
        time.sleep(60)  # the installer kills this process once it sees the sideload state
        return 0, b""
    if command in ("format data", "wipe cache", "wipe dalvik"):
        append("wiped", command + "\n")
        log_line({"format data": "Formatting Data using mke2fs...", "wipe cache": "Formatting Cache using mke2fs...",
                  "wipe dalvik": "-- Dalvik Cache Directories Wipe Complete!"}[command])
        log_line("Done.")
        return 139, b"Segmentation fault\n"
    return 1, f"fake twrp: {command}\n".encode()


# ---------------------------------------------------------------------------
# adb and fastboot
# ---------------------------------------------------------------------------

def adb(args):
    append("calls.log", "adb " + " ".join(args) + "\n")
    if args[:2] == ["-s", SERIAL]:
        args = args[2:]
    elif args[:1] == ["-s"]:
        err(f"adb: device '{args[1]}' not found\n")
        return 1
    state = rd("state")
    if not args:
        return 1
    c = args[0]
    if c in ("start-server", "version"):
        return 0
    if c == "devices":
        if state == "booting":
            n = int(rd("countdown", "0")) - 1
            wr("countdown", str(n))
            if n <= 0:
                wr("state", "recovery")
                state = "recovery"
        out("List of devices attached\n")
        if state in ("recovery", "sideload", "device"):
            out(f"{SERIAL}\t{state}\n")
        out("\n")
        return 0
    if c == "reboot":
        wr("state", "fastboot" if args[1:] == ["bootloader"] else "off")
        append("rebooted", (args[1] if len(args) > 1 else "system") + "\n")
        return 0
    if c == "sideload":
        if state != "sideload":
            err("adb: sideload connection failed: no devices/emulators found\n")
            return 1
        zpath = args[1]
        with open(zpath, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        append("installed", f"sideload {digest}\n")
        for pct in (0, 12, 31, 47):
            out(f"serving: '{zpath}'  (~{pct}%)    \r")
        out("\nTotal xfer: 1.00x\n")
        if os.environ.get("FAKE_SIDELOAD_FAIL"):
            log_line("E1001: Failed to update system image.")
            log_line("Updater process ended with ERROR: 1")
            log_line("Error installing zip file '/sideload/package.zip'")
        else:
            if not os.environ.get("FAKE_BOOT_BAD"):
                with zipfile.ZipFile(zpath) as z:
                    dev_write(f"{BYNAME}/boot", 0, z.read("boot.img"))
            log_line("Patching system image unconditionally...")
            log_line("script succeeded: result was [1.000000]")
            log_line("Updater process ended with RC=0")
            log_line("Install took 212 second(s).")
        wr("state", "recovery")
        return 0
    if state not in ("recovery", "device"):
        err(f"adb: no devices/emulators found ({state})\n")
        return 1
    if c == "push":
        if state != "recovery":
            return 1
        if args[2].startswith("/tmp/persist/") and not os.path.exists(p("persist-mounted")):
            err("adb: error: failed to copy: remote No such file or directory\n")
            return 1
        os.makedirs(p("remote"), exist_ok=True)
        with open(args[1], "rb") as src, open(remote(args[2]), "wb") as dst:
            dst.write(src.read())
        out(f"{args[1]}: 1 file pushed, 0 skipped.\n")
        return 0
    if c in ("shell", "exec-out"):
        if state != "recovery":
            err("fake: only TWRP has a shell here\n")
            return 1
        rc, data = run_shell(" ".join(args[1:]))
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
        return rc
    err("fake adb: unsupported " + " ".join(args) + "\n")
    return 1


def fastboot(args):
    append("calls.log", "fastboot " + " ".join(args) + "\n")
    if args[:2] == ["-s", SERIAL]:
        args = args[2:]
    state = rd("state")
    c = args[0] if args else ""
    if c == "devices":
        if state == "fastboot":
            out(f"{SERIAL}\tfastboot\n")
        return 0
    if state != "fastboot":
        err("< waiting for any device >\n")
        return 1
    if c == "getvar":
        err({"product": "product: lk1st-msm8974\n"}.get(args[1], f"{args[1]}: \n") + "Finished. Total time: 0.001s\n")
        return 0
    if c == "boot":
        with open(args[1], "rb") as f:
            wr("booted-image", hashlib.sha256(f.read()).hexdigest())
        kernel_reread()
        if os.path.exists(p("persist-mounted")):
            os.remove(p("persist-mounted"))
        wr("state", "booting")
        wr("countdown", "2")
        err("Sending 'boot.img' (20016 KB)  OKAY\nBooting  OKAY\n")
        return 0
    err("fake fastboot: unsupported " + " ".join(args) + "\n")
    return 1


def main(tool):
    if not S:
        sys.exit("FAKE_STATE not set: this is the test fake, not the real tool")
    args = sys.argv[1:]
    sys.exit(adb(args) if tool == "adb" else fastboot(args))
