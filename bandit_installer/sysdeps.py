"""Everything that differs between Linux and Windows.

Linux: block devices, lsblk/sysfs, mount, sudo.
Windows: \\\\.\\PhysicalDriveN, PowerShell storage cmdlets, volume paths,
UAC elevation, bcdedit.

The phone's partitions are always found by reading its GPT directly
(gpt.py), so the partition names come from the phone, not from the OS.
"""
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

from . import gpt

IS_WINDOWS = os.name == "nt"
PLATFORM_TOOLS_URL = {
    "windows": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "linux": "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
}


class SysError(Exception):
    pass


class Disk:
    def __init__(self, ident, raw, size, model, bus, note=""):
        self.ident = ident      # Linux /dev/sdb, Windows disk number
        self.raw = raw          # path opened for raw reads
        self.size = size
        self.model = model
        self.bus = bus
        self.note = note
        self.gpt = None
        self.gpt_error = None

    def label(self):
        gb = self.size / 1e9
        return f"{self.ident}  {gb:.1f} GB  {self.model or '?'} ({self.bus or '?'})"


class UsbDevice:
    def __init__(self, vid, pid, name="", status="OK"):
        self.vid = vid.lower()
        self.pid = pid.lower()
        self.name = name
        self.status = status

    @property
    def id(self):
        return f"{self.vid}:{self.pid}"


def run(cmd, timeout=None, check=False, input=None):
    """Runs a command; returns (rc, stdout+stderr as text)."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, input=input)
    except FileNotFoundError:
        if check:
            raise SysError(f"command not found: {cmd[0]}")
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired as e:
        out = (e.output or b"").decode("utf-8", "replace")
        if check:
            raise SysError(f"timeout: {' '.join(cmd)}")
        return 124, out
    enc = "utf-8"
    out = p.stdout.decode(enc, "replace")
    if check and p.returncode != 0:
        raise SysError(f"{' '.join(cmd)} failed ({p.returncode}):\n{out.strip()}")
    return p.returncode, out


class System:
    name = "?"

    def __init__(self, root_dir, test_mode=False):
        self.root_dir = root_dir
        self.test_mode = test_mode

    # tools -----------------------------------------------------------------
    def tool_dir(self):
        return os.path.join(self.root_dir, "tools", "platform-tools")

    def find_tool(self, name):
        exe = name + (".exe" if IS_WINDOWS else "")
        local = os.path.join(self.tool_dir(), exe)
        if os.path.isfile(local):
            return local
        return shutil.which(name)

    def platform_tools_url(self):
        return PLATFORM_TOOLS_URL["windows" if IS_WINDOWS else "linux"]

    def install_platform_tools(self, zip_path):
        dest = os.path.join(self.root_dir, "tools")
        os.makedirs(dest, exist_ok=True)
        with zipfile.ZipFile(zip_path) as z:
            for m in z.infolist():
                target = os.path.realpath(os.path.join(dest, m.filename))
                if not target.startswith(os.path.realpath(dest) + os.sep):
                    raise SysError(f"unsafe path in zip: {m.filename}")
            z.extractall(dest)
        if not IS_WINDOWS:
            for n in ("adb", "fastboot"):
                p = os.path.join(self.tool_dir(), n)
                if os.path.exists(p):
                    os.chmod(p, 0o755)

    # files written as root on Linux go back to the user who ran sudo
    def give_back(self, path):
        pass

    def free_space(self, path):
        return shutil.disk_usage(path).free


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------

class LinuxSystem(System):
    name = "linux"

    def is_admin(self):
        return os.geteuid() == 0

    def elevate(self, argv):
        """Re-runs the installer with sudo (replaces this process)."""
        sudo = shutil.which("sudo")
        if not sudo:
            raise SysError("sudo not found: run the installer as root")
        keep = [f"{k}={os.environ[k]}" for k in ("LANG", "LC_ALL", "TERM", "BANDIT_LANG", "DISPLAY", "WAYLAND_DISPLAY",
                                                          "XDG_RUNTIME_DIR", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS")
                if k in os.environ]
        os.execv(sudo, [sudo, "env"] + keep + [sys.executable, "-B"] + argv)

    def give_back(self, path):
        uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
        if os.geteuid() != 0 or not uid or not os.path.exists(path):
            return
        uid, gid = int(uid), int(gid or uid)
        for base, dirs, files in os.walk(path):
            for n in [base] + [os.path.join(base, x) for x in dirs + files]:
                with contextlib.suppress(OSError):
                    os.lchown(n, uid, gid)

    def list_disks(self):
        rc, out = run(["lsblk", "-J", "-b", "-d", "-o", "NAME,PATH,SIZE,TYPE,TRAN,MODEL,VENDOR"])
        if rc != 0:
            raise SysError("lsblk failed: " + out)
        disks = []
        for d in json.loads(out).get("blockdevices", []):
            if d.get("type") == "loop" and self.test_mode:
                pass
            elif d.get("type") != "disk" or d.get("tran") != "usb":
                continue
            model = " ".join(x.strip() for x in (d.get("vendor") or "", d.get("model") or "") if x and x.strip())
            disks.append(Disk(d["path"], d["path"], int(d.get("size") or 0), model, d.get("tran") or d.get("type")))
        return disks

    def open_raw(self, disk, write=False):
        return open(disk.raw, "r+b" if write else "rb", buffering=0)

    def partition_node(self, disk, part):
        name = os.path.basename(disk.raw)
        sysdir = f"/sys/block/{name}"
        if os.path.isdir(sysdir):
            for e in os.listdir(sysdir):
                f = os.path.join(sysdir, e, "partition")
                if os.path.isfile(f):
                    with open(f) as fh:
                        if int(fh.read().strip()) == part.index:
                            return "/dev/" + e
        raise SysError(f"no device node for partition {part.index} ({part.name}) of {disk.raw}")

    def _mounts_of(self, node):
        real = os.path.realpath(node)
        out = []
        with open("/proc/self/mounts") as f:
            for line in f:
                src, tgt, fs, opts = line.split()[:4]
                if os.path.realpath(src) == real:
                    out.append((tgt.replace("\\040", " "), fs, opts.split(",")))
        return out

    def partition_mounted(self, disk, part):
        return bool(self._mounts_of(self.partition_node(disk, part)))

    @contextlib.contextmanager
    def mounted(self, disk, part, fstype, write=False):
        """Yields a folder with the partition's files (mounting it if needed)."""
        node = self.partition_node(disk, part)
        for tgt, _fs, opts in self._mounts_of(node):
            if write and "ro" in opts:
                continue
            yield tgt
            return
        mnt = tempfile.mkdtemp(prefix="bandit-")
        try:
            if fstype == "ntfs":
                rc, out = run(["mount", "-t", "ntfs3", "-o", "ro", node, mnt])
                if rc != 0:
                    rc, out = run(["mount", "-t", "ntfs-3g", "-o", "ro", node, mnt])
            else:
                rc, out = run(["mount", "-t", "vfat", "-o", "rw" if write else "ro", node, mnt])
            if rc != 0:
                raise SysError(f"cannot mount {node}: {out.strip()}")
            try:
                yield mnt
            finally:
                os.sync()
                run(["umount", mnt])
        finally:
            with contextlib.suppress(OSError):
                os.rmdir(mnt)

    def unmount_all(self, disk):
        """Unmounts every partition of the phone (before raw backups/restores)."""
        busy = []
        for p in disk.gpt.partitions if disk.gpt else []:
            with contextlib.suppress(SysError):
                for tgt, _fs, _o in self._mounts_of(self.partition_node(disk, p)):
                    rc, _ = run(["umount", tgt])
                    if rc != 0:
                        busy.append(tgt)
        return busy

    def usb_devices(self):
        out = []
        base = "/sys/bus/usb/devices"
        if not os.path.isdir(base):
            return out
        for d in os.listdir(base):
            p = os.path.join(base, d)
            try:
                with open(os.path.join(p, "idVendor")) as f:
                    vid = f.read().strip()
                with open(os.path.join(p, "idProduct")) as f:
                    pid = f.read().strip()
            except OSError:
                continue
            name = ""
            with contextlib.suppress(OSError), open(os.path.join(p, "product")) as f:
                name = f.read().strip()
            out.append(UsbDevice(vid, pid, name))
        return out

    def bcd_backend(self):
        from . import bcd
        return bcd.linux_writer()

    def missing_requirements(self):
        miss = []
        if not shutil.which("lsblk"):
            miss.append(("lsblk", "util-linux"))
        return miss


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def powershell(script, timeout=60):
    pre = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;$ErrorActionPreference='Stop';"
    rc, out = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-Command", pre + script], timeout=timeout)
    return rc, out


def ps_json(script, timeout=60):
    rc, out = powershell(script + " | ConvertTo-Json -Depth 4 -Compress", timeout)
    if rc != 0:
        raise SysError("PowerShell: " + out.strip())
    out = out.strip()
    if not out:
        return []
    data = json.loads(out)
    return data if isinstance(data, list) else [data]


class WindowsSystem(System):
    name = "windows"

    def is_admin(self):
        import ctypes
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def elevate(self, argv):
        """Starts an elevated copy in a new console window (UAC prompt)."""
        import ctypes
        params = subprocess.list2cmdline(["-B"] + argv + ["--pause-at-end"])
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        if rc <= 32:
            raise SysError("administrator rights refused")
        sys.exit(0)

    def list_disks(self):
        rows = ps_json("Get-Disk | Select-Object Number,FriendlyName,Size,BusType,PartitionStyle,IsOffline")
        disks = []
        for r in rows:
            if str(r.get("BusType")) not in ("USB", "7"):
                continue
            note = "offline" if r.get("IsOffline") else ""
            n = int(r["Number"])
            disks.append(Disk(n, rf"\\.\PhysicalDrive{n}", int(r.get("Size") or 0),
                              r.get("FriendlyName") or "", "USB", note))
        return disks

    def open_raw(self, disk, write=False):
        return open(disk.raw, "r+b" if write else "rb", buffering=0)

    def _partitions(self, disk):
        return ps_json(f"Get-Partition -DiskNumber {int(disk.ident)} | Select-Object PartitionNumber,Offset,Size,"
                       "@{n='DriveLetter';e={[string]$_.DriveLetter}},AccessPaths")

    def _access_path(self, disk, part):
        for r in self._partitions(disk):
            if int(r.get("Offset") or -1) != part.offset:
                continue
            letter = (r.get("DriveLetter") or "").strip("\0 ")
            if letter:
                return f"{letter}:\\", r
            paths = r.get("AccessPaths") or []
            if isinstance(paths, str):
                paths = [paths]
            folders = [p for p in paths if not p.startswith("\\\\?\\")]
            vols = [p for p in paths if p.startswith("\\\\?\\Volume")]
            if folders:
                return folders[0], r
            if vols:
                return vols[0], r
            return None, r
        return None, None

    def partition_mounted(self, disk, part):
        path, _ = self._access_path(disk, part)
        return path is not None

    @contextlib.contextmanager
    def mounted(self, disk, part, fstype, write=False):
        path, row = self._access_path(disk, part)
        if row is None:
            raise SysError(f"Windows does not list the partition {part.name} at offset {part.offset}")
        if path is None:
            rc, out = powershell(f"Add-PartitionAccessPath -DiskNumber {int(disk.ident)} "
                                 f"-PartitionNumber {int(row['PartitionNumber'])} -AssignDriveLetter")
            if rc != 0:
                raise SysError(f"cannot give {part.name} a drive letter: {out.strip()}")
            path, row = self._access_path(disk, part)
            if path is None:
                raise SysError(f"{part.name} has no drive letter or volume path")
        if not os.path.isdir(path):
            raise SysError(f"{path} ({part.name}) is not readable: is the file system recognised?")
        yield path

    def unmount_all(self, disk):
        return []  # raw reads work with the volumes mounted

    def usb_devices(self):
        try:
            rows = ps_json("Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\\VID_*' } | "
                           "Select-Object InstanceId,FriendlyName,Status", timeout=30)
        except Exception:
            return []
        seen = {}
        for r in rows:
            m = re.search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", r.get("InstanceId") or "")
            if not m:
                continue
            dev = UsbDevice(m.group(1), m.group(2), r.get("FriendlyName") or "", r.get("Status") or "")
            # keep the entry with a problem, if any interface has one
            if dev.id not in seen or dev.status != "OK":
                seen[dev.id] = dev
        return list(seen.values())

    def bcd_backend(self):
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "bcdedit.exe")
        return "bcdedit" if os.path.isfile(exe) else None

    def missing_requirements(self):
        miss = []
        if not self.bcd_backend():
            miss.append(("bcdedit", "Windows\\System32\\bcdedit.exe"))
        if not shutil.which("powershell.exe"):
            miss.append(("powershell", "Windows PowerShell 5.1"))
        return miss


# ---------------------------------------------------------------------------
# test: a disk image as a phone in mass storage mode
# ---------------------------------------------------------------------------

class ImageSystem(LinuxSystem):
    """A phone in mass storage mode simulated by a disk image (only with --test).

    LUMIA_TEST_DISK  the eMMC image (raw, may be sparse)
    LUMIA_TEST_DIRS  "MainOS=/folder;…": folders that stand for partitions whose file
                     system mtools cannot read (NTFS)

    FAT partitions are read and written inside the image with mtools, so no root, loop
    device or mount is needed. Used by the tests and to rehearse a whole install on a
    copy of a real phone's full backup.
    """
    name = "linux-test-image"

    def __init__(self, root_dir, image, dirs=None):
        super().__init__(root_dir, test_mode=True)
        self.image = os.path.abspath(image)
        self.dirs = dict(dirs or {})

    def is_admin(self):
        return True

    def elevate(self, argv):
        raise SysError("not needed with a test disk image")

    def give_back(self, path):
        pass

    def list_disks(self):
        if not os.path.isfile(self.image):
            return []
        return [Disk(self.image, self.image, os.path.getsize(self.image), "Test image", "usb",
                     os.path.basename(self.image))]

    def partition_node(self, disk, part):
        return f"{self.image}@@{part.offset}"

    def partition_mounted(self, disk, part):
        return False

    def unmount_all(self, disk):
        return []

    def usb_devices(self):
        return []

    @staticmethod
    def _mtools(args):
        env = dict(os.environ, MTOOLS_SKIP_CHECK="1", MTOOLS_NO_VFAT="0")
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        if p.returncode != 0:
            raise SysError(" ".join(args[:1]) + " failed: " + p.stdout.decode("utf-8", "replace").strip())
        return p.stdout.decode("utf-8", "replace")

    @staticmethod
    def _tree(root):
        out = {}
        for base, dirs, files in os.walk(root):
            for d in dirs:
                out[os.path.relpath(os.path.join(base, d), root)] = None
            for f in files:
                p = os.path.join(base, f)
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, root)] = hashlib_sha256(fh.read())
        return out

    @contextlib.contextmanager
    def mounted(self, disk, part, fstype, write=False):
        if part.name in self.dirs:
            yield self.dirs[part.name]
            return
        if fstype != "vfat":
            raise SysError(f"{part.name}: {fstype} is not readable from a test image (use LUMIA_TEST_DIRS)")
        img = self.partition_node(disk, part)
        mnt = tempfile.mkdtemp(prefix="lumia-img-")
        try:
            listing = self._mtools(["mdir", "-b", "-i", img, "::/"])
            if listing.strip():
                self._mtools(["mcopy", "-s", "-n", "-p", "-m", "-i", img] + [
                    "::/" + os.path.basename(x.strip().rstrip("/")) for x in listing.splitlines() if x.strip()] + [mnt])
            before = self._tree(mnt)
            yield mnt
            if not write:
                return
            after = self._tree(mnt)
            for rel in sorted(set(before) - set(after), key=len, reverse=True):
                self._mtools(["mrd" if before[rel] is None else "mdel", "-i", img, "::/" + rel.replace(os.sep, "/")])
            for rel in sorted(after, key=len):
                dst = "::/" + rel.replace(os.sep, "/")
                if after[rel] is None and rel not in before:
                    self._mtools(["mmd", "-i", img, dst])
                elif after[rel] is not None and before.get(rel) != after[rel]:
                    self._mtools(["mcopy", "-o", "-i", img, os.path.join(mnt, rel), dst])
        finally:
            shutil.rmtree(mnt, ignore_errors=True)

    def bcd_backend(self):
        from . import bcd
        return bcd.linux_writer()

    def missing_requirements(self):
        return [] if shutil.which("mcopy") else [("mtools", "mcopy, mdir, mmd, mdel, mrd")]


def hashlib_sha256(b):
    import hashlib
    return hashlib.sha256(b).hexdigest()


def get_system(root_dir, test_mode=False):
    image = os.environ.get("LUMIA_TEST_DISK") if test_mode else None
    if image:
        dirs = {}
        for item in (os.environ.get("LUMIA_TEST_DIRS") or "").split(";"):
            if "=" in item:
                k, v = item.split("=", 1)
                dirs[k.strip()] = v.strip()
        return ImageSystem(root_dir, image, dirs)
    return (WindowsSystem if IS_WINDOWS else LinuxSystem)(root_dir, test_mode)


def read_disk_gpt(system, disk):
    try:
        with system.open_raw(disk) as f:
            disk.gpt = gpt.read_gpt(f)
    except (OSError, gpt.GptError) as e:
        disk.gpt = None
        disk.gpt_error = str(e)
    return disk
