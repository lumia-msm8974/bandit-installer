"""Flow tests: the Full, Clean and Update modes against a fake phone (no real device).

    python3 -m unittest discover -s tests

The installer runs in --text mode with --adb/--fastboot pointing at tests/fake/adb and
tests/fake/fastboot, a simulated Lumia 1520 whose eMMC is a sparse disk image with the
real 1520 partition table (as in test_layout.py). Every adb/fastboot call goes to the
fake; USB scanning is switched off (BANDIT_NO_USB_SCAN). The installer is copied to a
temporary folder, with a checksums.sha256 that pins the fake TWRP image.

Needs sfdisk, mkfs.fat and mtools (mmd, mcopy) to build the fake disk.
"""
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

from bandit_installer import android, devices, fat, gpt, layout  # noqa: E402
from test_layout import DISK_SECTORS, LUMIA1520, sfdisk_script  # noqa: E402

FAKE = os.path.join(HERE, "fake")
TOOLS = ("sfdisk", "mkfs.fat", "mmd", "mcopy")
ROWS = {row.split()[3]: (int(row.split()[0]), int(row.split()[1])) for row in LUMIA1520.splitlines()}
WLAN_MACS = ["A0B1C2D3E4F0", "A0B1C2D3E4F1", "A0B1C2D3E4F2", "A0B1C2D3E4F3"]
BT_MAC = "0C1D2E3F4A5B"


def sha(b):
    return hashlib.sha256(b).hexdigest()


def readb(path):
    with open(path, "rb") as f:
        return f.read()


def readt(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def boot_image(tag, size=12288):
    """A small valid Android boot image (header v0, page 2048, 1-byte kernel)."""
    h = bytearray(2048)
    h[:8] = b"ANDROID!"
    struct.pack_into("<10I", h, 8, 1, 0x8000, 0, 0, 0, 0, 0x100, 2048, 0, 0)
    h[48:48 + len(tag)] = tag.encode()
    return bytes(h) + os.urandom(size - 2048)


def fat_image(path, kib, dirs=(), files=()):
    subprocess.run(["mkfs.fat", "-C", path, str(kib)], check=True, capture_output=True)
    env = dict(os.environ, MTOOLS_SKIP_CHECK="1")
    for d in dirs:
        subprocess.run(["mmd", "-i", path, "::" + d], check=True, capture_output=True, env=env)
    for local, dest in files:
        subprocess.run(["mcopy", "-i", path, local, "::" + dest], check=True, capture_output=True, env=env)
    with open(path, "rb") as f:
        return f.read()


def write_at(img, sector, data):
    with open(img, "r+b") as f:
        f.seek(sector * gpt.SECTOR)
        f.write(data)


def read_gpt(img):
    with open(img, "rb") as f:
        return gpt.read_gpt(f)


def read_part(img, name, n=None):
    g = read_gpt(img)
    p = g.by_name(name)
    with open(img, "rb") as f:
        f.seek(p.offset)
        return f.read(p.size if n is None else n)


@unittest.skipUnless(all(shutil.which(t) for t in TOOLS), "sfdisk, mkfs.fat and mtools needed")
class FlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bandit-flows-")
        t = cls.tmp
        # installer copy with a checksums.sha256 pinning the fake TWRP
        cls.inst = os.path.join(t, "installer")
        os.makedirs(cls.inst)
        shutil.copytree(os.path.join(REPO, "bandit_installer"), os.path.join(cls.inst, "bandit_installer"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(REPO, "payload"), os.path.join(cls.inst, "payload"))
        shutil.copy(os.path.join(REPO, "installer.py"), cls.inst)
        cls.twrp = os.path.join(t, devices.BANDIT.twrp_name)
        with open(cls.twrp, "wb") as f:
            f.write(boot_image("twrp"))
        cls.twrp_sha = sha(readb(cls.twrp))
        with open(os.path.join(cls.inst, android.CHECKSUMS), "w") as f:
            f.write(f"# test\n{cls.twrp_sha}  {devices.BANDIT.twrp_name}\n")
        cls.other_twrp = os.path.join(t, "twrp-other.img")
        with open(cls.other_twrp, "wb") as f:
            f.write(boot_image("other"))
        # ROM zips
        cls.rom = cls.make_rom("lineage-18.1-20261102-UNOFFICIAL-bandit.zip", "bandit,RM-937,RM-938,RM-939,RM-940")
        cls.rom_sha = sha(readb(cls.rom))
        with zipfile.ZipFile(cls.rom) as z:
            cls.boot = z.read("boot.img")
        cls.other_rom = cls.make_rom("lineage-18.1-20261102-UNOFFICIAL-talkman.zip", "talkman", folder="other")
        # disks
        cls.lk = readb(os.path.join(REPO, "payload", "bandit", "lk1st", "base-0ff", "emmc_appsboot.mbn"))
        cls.win_disk = cls.make_windows_disk(os.path.join(t, "windows.img"))
        cls.android_disk = os.path.join(t, "android.img")
        subprocess.run(["cp", "--sparse=always", cls.win_disk, cls.android_disk], check=True)
        with open(cls.android_disk, "rb") as f:
            plan = layout.make_plan(layout.RawGpt.read(f, DISK_SECTORS))
        write_at(cls.android_disk, 0, plan.after.primary())
        write_at(cls.android_disk, plan.after.backup_lba_start(), plan.after.backup())
        cls.guid = read_gpt(cls.win_disk).disk_guid
        # the same Android layout, but the DPP of a Lumia 930: a phone that is not the chosen one
        cls.android930_disk = os.path.join(t, "android930.img")
        subprocess.run(["cp", "--sparse=always", cls.android_disk, cls.android930_disk], check=True)
        w = os.path.join(t, "dpp930")
        os.makedirs(w)
        with open(os.path.join(w, "product.dat"), "w", newline="\r\n") as f:
            f.write("TYPE:RM-1045\nLPSN:TEST0002\n")
        write_at(cls.android930_disk, ROWS["DPP"][0],
                 fat_image(os.path.join(w, "dpp.img"), ROWS["DPP"][1] // 2, ["Nokia"],
                           [(os.path.join(w, "product.dat"), "Nokia/product.dat")]))
        # a TWRP pinned for another phone, and a ROM without OTA metadata
        cls.martini_twrp = os.path.join(t, "twrp-3.7.0_9-0-martini.img")
        with open(cls.martini_twrp, "wb") as f:
            f.write(boot_image("twrp-martini"))
        with open(os.path.join(cls.inst, android.CHECKSUMS), "a") as f:
            f.write(f"{sha(readb(cls.martini_twrp))}  twrp-3.7.0_9-0-martini.img\n")
        cls.nometa_rom = cls.make_rom("lineage-18.1-20261102-UNOFFICIAL-bandit.zip", None, folder="nometa")

    @classmethod
    def make_rom(cls, name, devices, folder="rom"):
        d = os.path.join(cls.tmp, folder)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with zipfile.ZipFile(path, "w") as z:
            if devices:
                z.writestr("META-INF/com/android/metadata",
                           f"ota-type=BLOCK\npost-sdk-level=30\npre-device={devices}\n")
            z.writestr("META-INF/com/google/android/update-binary", "fake")
            z.writestr("boot.img", boot_image("rom", 3 * 4096))
            z.writestr("system.new.dat.br", os.urandom(4096))
        with open(os.path.join(d, "SHA256SUMS"), "a") as f:
            f.write(f"{sha(readb(path))}  {name}\n")
        return path

    @classmethod
    def make_windows_disk(cls, path, model="RM-937"):
        with open(path, "wb") as f:
            f.truncate(DISK_SECTORS * gpt.SECTOR)
        subprocess.run(["sfdisk", "-q", "--no-reread", "--no-tell-kernel", path], input=sfdisk_script(),
                       text=True, check=True, env=dict(os.environ, LC_ALL="C"))
        w = os.path.join(cls.tmp, "fat-" + os.path.basename(path))
        os.makedirs(w)
        prov = {"WLAN.PROVISION": b"\x01\x02\x03" + bytes.fromhex("".join(WLAN_MACS)),
                "BT.PROVISION": b"\x01\x06" + bytes.fromhex(BT_MAC),
                "WLAN_CLPC.PROVISION": os.urandom(29816)}
        for n, data in prov.items():
            with open(os.path.join(w, n), "wb") as f:
                f.write(data)
        cls.clpc = prov["WLAN_CLPC.PROVISION"]
        with open(os.path.join(w, "product.dat"), "w", newline="\r\n") as f:
            f.write(f"TYPE:{model}\nLPSN:TEST0001\nHWID:5301\n")
        dpp = fat_image(os.path.join(w, "dpp.img"), ROWS["DPP"][1] // 2, ["QCOM", "Nokia"],
                        [(os.path.join(w, n), "QCOM/" + n) for n in prov] + [(os.path.join(w, "product.dat"), "Nokia/product.dat")])
        write_at(path, ROWS["DPP"][0], dpp)
        with open(os.path.join(w, "emmc_appsboot.mbn"), "wb") as f:
            f.write(cls.lk)
        with open(os.path.join(w, "BCD"), "wb") as f:
            f.write(b"not a real hive")
        efiesp = fat_image(os.path.join(w, "efiesp.img"), ROWS["EFIESP"][1] // 2,
                           ["EFI", "EFI/Microsoft", "EFI/Microsoft/Boot"],
                           [(os.path.join(w, "emmc_appsboot.mbn"), "emmc_appsboot.mbn"),
                            (os.path.join(w, "BCD"), "EFI/Microsoft/Boot/BCD")])
        write_at(path, ROWS["EFIESP"][0], efiesp)
        cls.modem = {}
        for n in ("MODEM_FS1", "MODEM_FS2", "MODEM_FSC", "MODEM_FSG", "SSD"):
            data = os.urandom(ROWS[n][1] * gpt.SECTOR)
            cls.modem[n] = data
            write_at(path, ROWS[n][0], data)
        return path

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    # ------------------------------------------------------------------ helpers
    def run_installer(self, name, mode, stdin="", disk="android", phone="device", env=None, args=(), steps=None):
        s = os.path.join(self.tmp, "cases", name)
        os.makedirs(s)
        src = {"android": self.android_disk, "android930": self.android930_disk}.get(disk, self.win_disk)
        subprocess.run(["cp", "--sparse=always", src, os.path.join(s, "disk.img")], check=True)
        with open(os.path.join(s, "state"), "w") as f:
            f.write(phone)
        work = os.path.join(s, "work")
        os.makedirs(work)
        if steps is not None:
            with open(os.path.join(work, "state.json"), "w") as f:
                json.dump({"phones": {self.guid: {"steps": steps}}, "last": self.guid}, f)
        e = dict(os.environ, FAKE_STATE=s, BANDIT_NO_USB_SCAN="1", PATH=FAKE + os.pathsep + os.environ.get("PATH", ""))
        for k in ("ANDROID_SERIAL", "BANDIT_LANG"):
            e.pop(k, None)
        e.update(env or {})
        a = list(args)
        if "--twrp" not in a:
            a += ["--twrp", self.twrp]
        if "--rom" not in a:
            a += ["--rom", self.rom]
        cmd = [sys.executable, "-I", os.path.join(self.inst, "installer.py"), "--text", "--no-elevate", "--lang", "en",
               "--work", work, "--mode", mode, "--adb", os.path.join(FAKE, "adb"),
               "--fastboot", os.path.join(FAKE, "fastboot")] + a
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=e, timeout=600)
        out = p.stdout + p.stderr
        with open(os.path.join(s, "out.txt"), "w") as f:
            f.write(out)
        self.case = s
        return p.returncode, out

    def file(self, name, default=""):
        p = os.path.join(self.case, name)
        return readt(p) if os.path.exists(p) else default

    def assertInstalled(self):
        self.assertEqual(self.file("installed").split(), ["sideload", self.rom_sha])
        self.assertEqual(read_part(os.path.join(self.case, "disk.img"), "boot", len(self.boot)), self.boot)
        self.assertEqual(self.file("rebooted").split()[-1], "system")
        self.assertEqual(self.file("state"), "off")

    def assertUntouched(self):
        calls = self.file("calls.log")
        self.assertNotIn("fastboot boot", calls)
        self.assertNotIn("reboot", calls)

    def full_steps(self, default="android"):
        t = "2026-11-02T10:00:00"
        return {"backup": {"time": t, "path": os.path.join(self.tmp, "no-backup"), "full": True},
                "firmware": {"time": t}, "chain": {"time": t, "options": {"default": default, "android_only": default == "android"}},
                "lk1st": {"time": t, "product": "lk1st-msm8974"}}

    # ------------------------------------------------------------------ Update
    def test_update_from_android(self):
        rc, out = self.run_installer("update", "update")
        self.assertEqual(rc, 0, out)
        self.assertIn("adb reboot bootloader", self.file("calls.log"))
        self.assertEqual(self.file("booted-image"), self.twrp_sha)
        self.assertEqual(self.file("wiped"), "")
        self.assertInstalled()
        st = json.loads(readt(os.path.join(self.case, "work", "state.json")))
        self.assertIn("rom", st["phones"][self.guid]["steps"])

    def test_update_from_fastboot(self):
        rc, out = self.run_installer("update-fb", "update", phone="fastboot")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("adb reboot bootloader", self.file("calls.log"))
        self.assertInstalled()

    def test_update_other_system_refused(self):
        rc, out = self.run_installer("update-other", "update", "no\n", env={"FAKE_INSTALLED": "sdk=33 lineage=20.0 device=cityman"})
        self.assertEqual(rc, 1, out)
        self.assertIn("not LineageOS 18.1 for bandit", out)
        self.assertEqual(self.file("installed"), "")

    def test_update_unreadable_system_confirmed(self):
        rc, out = self.run_installer("update-none", "update", "UPDATE\n", env={"FAKE_INSTALLED": "none"})
        self.assertEqual(rc, 0, out)
        self.assertInstalled()

    def test_update_without_head_c(self):
        rc, out = self.run_installer("update-nohead", "update", env={"FAKE_NO_HEAD": "1"})
        self.assertEqual(rc, 0, out)
        self.assertInstalled()

    def test_update_sideload_retry(self):
        rc, out = self.run_installer("update-retry", "update", env={"FAKE_SIDELOAD_LOST": "1"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.file("n-twrp-sideload").strip(), "2")
        self.assertInstalled()

    def test_update_install_failed(self):
        rc, out = self.run_installer("update-fail", "update", env={"FAKE_SIDELOAD_FAIL": "1"})
        self.assertEqual(rc, 1, out)
        self.assertIn("ROM installation failed", out)
        self.assertEqual(self.file("state"), "recovery")

    def test_update_boot_mismatch(self):
        rc, out = self.run_installer("update-boot", "update", env={"FAKE_BOOT_BAD": "1"})
        self.assertEqual(rc, 1, out)
        self.assertIn("does not match boot.img", out)
        self.assertEqual(self.file("state"), "recovery")

    def test_bad_rom_checksum(self):
        rc, out = self.run_installer("bad-rom", "update", args=["--rom-sha256", "0" * 64])
        self.assertEqual(rc, 1, out)
        self.assertIn("does not match its checksum", out)
        self.assertUntouched()

    def test_rom_for_other_device(self):
        rc, out = self.run_installer("other-dev", "update", args=["--rom", self.other_rom])
        self.assertEqual(rc, 1, out)
        self.assertIn("not for the Lumia 1520 (bandit)", out)
        self.assertUntouched()

    def test_rom_without_metadata(self):
        rc, out = self.run_installer("no-meta", "update", args=["--rom", self.nometa_rom])
        self.assertEqual(rc, 1, out)
        self.assertIn("does not say which phone it is for", out)
        self.assertUntouched()

    def test_twrp_of_other_phone(self):
        rc, out = self.run_installer("twrp-930", "update", args=["--twrp", self.martini_twrp])
        self.assertEqual(rc, 1, out)
        self.assertIn("a TWRP for another phone", out)
        self.assertUntouched()

    def test_update_on_wrong_phone(self):
        rc, out = self.run_installer("wrong-phone", "update", disk="android930")
        self.assertEqual(rc, 1, out)
        self.assertIn("The phone is an RM-1045 (Lumia 930 (martini))", out)
        self.assertEqual(self.file("installed"), "")
        self.assertNotIn("sideload", self.file("calls.log"))

    def test_clean_on_wrong_phone(self):
        rc, out = self.run_installer("wrong-phone-clean", "clean", "ERASE\n", disk="android930")
        self.assertEqual(rc, 1, out)
        self.assertIn("RM-1045", out)
        self.assertEqual(self.file("wiped"), "")

    def test_update_model_checked(self):
        rc, out = self.run_installer("model-ok", "update")
        self.assertEqual(rc, 0, out)
        self.assertIn("Phone RM-937: Lumia 1520 (bandit)", out)

    def test_twrp_not_pinned(self):
        rc, out = self.run_installer("bad-twrp", "update", args=["--twrp", self.other_twrp])
        self.assertEqual(rc, 1, out)
        self.assertIn("is not the expected file", out)
        self.assertUntouched()

    # ------------------------------------------------------------------ Clean
    def test_clean(self):
        rc, out = self.run_installer("clean", "clean", "ERASE\n")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.file("wiped").splitlines(), ["format data", "wipe cache", "wipe dalvik"])
        self.assertInstalled()

    def test_clean_not_confirmed(self):
        rc, out = self.run_installer("clean-stop", "clean", "nope\n")
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.file("wiped"), "")
        self.assertEqual(self.file("installed"), "")

    def test_clean_on_windows_layout(self):
        rc, out = self.run_installer("clean-win", "clean", "ERASE\n", disk="windows", phone="fastboot")
        self.assertEqual(rc, 1, out)
        self.assertIn("use the Full mode", out)
        self.assertEqual(self.file("wiped"), "")

    # ------------------------------------------------------------------ Full (phase 2)
    def check_phase2_disk(self):
        img = os.path.join(self.case, "disk.img")
        g = read_gpt(img)
        names = g.names()
        for n in ("MainOS", "Data", "MMOS"):
            self.assertNotIn(n, names)
        with open(self.win_disk, "rb") as f:
            want = layout.make_plan(layout.RawGpt.read(f, DISK_SECTORS))
        got = [(p.index, p.name, p.first, p.last) for p in g.partitions if p.name in dict((n, 1) for n, _s, _f in layout.ANDROID_LAYOUT)]
        self.assertEqual(sorted(got), sorted((s, n, a, b) for s, n, a, b, _fs in want.created))
        self.assertEqual(fat.FatImage(read_part(img, "aboot")).read("emmc_appsboot.mbn"), self.lk)
        for src, dst in (("MODEM_FS1", "modemst1"), ("MODEM_FS2", "modemst2"), ("MODEM_FSC", "fsc"),
                         ("MODEM_FSG", "fsg"), ("SSD", "ssd")):
            self.assertEqual(read_part(img, dst, len(self.modem[src])), self.modem[src], dst)
        self.assertEqual(read_part(img, "persistent", 4096), bytes(4096))
        persist = os.path.join(self.case, "persist")
        self.assertEqual(readt(os.path.join(persist, "wlan_mac.bin")),
                         "".join(f"Intf{i}MacAddress={m}\n" for i, m in enumerate(WLAN_MACS)))
        self.assertEqual(readt(os.path.join(persist, "bdaddr.txt")), "0C:1D:2E:3F:4A:5B\n")
        self.assertEqual(readb(os.path.join(persist, "WCNSS_qcom_wlan_nv.bin")), self.clpc)
        formatted = self.file("formatted").split()
        self.assertEqual(len(formatted), sum(1 for _n, _s, fs in layout.ANDROID_LAYOUT if fs == "ext4") - 1)  # not persistent

    def provisioned(self):
        base = os.path.join(self.case, "work", "backups")
        dirs = [d for d in os.listdir(base) if d.startswith("phase2-")]
        found = []
        for d in dirs:
            pdir = os.path.join(base, d, "provisioned")
            if os.path.isdir(pdir):
                found = sorted(os.listdir(pdir))
        return found

    def test_full_from_lk1st(self):
        rc, out = self.run_installer("full", "full", "ERASE WINDOWS\n", disk="windows", phone="fastboot",
                                     steps=self.full_steps())
        self.assertEqual(rc, 0, out)
        self.check_phase2_disk()
        self.assertEqual(len(self.provisioned()), 12)          # 11 partitions + SHA256SUMS
        self.assertEqual(self.file("calls.log").count("fastboot boot"), 2)   # phase 2, then the ROM
        self.assertEqual(self.file("wiped").splitlines(), ["format data", "wipe cache", "wipe dalvik"])
        self.assertInstalled()
        st = json.loads(readt(os.path.join(self.case, "work", "state.json")))["phones"][self.guid]["steps"]
        for k in ("p2-provisioned", "p2-gpt", "phase2", "rom"):
            self.assertIn(k, st)

    def test_full_not_confirmed(self):
        rc, out = self.run_installer("full-stop", "full", "no\n", disk="windows", phone="fastboot", steps=self.full_steps())
        self.assertEqual(rc, 1, out)
        self.assertIn("MainOS", read_gpt(os.path.join(self.case, "disk.img")).names())
        self.assertEqual(len(self.provisioned()), 12)          # the backup is made before asking
        self.assertEqual(self.file("wiped"), "")

    def test_full_refuses_mounted_emmc(self):
        rc, out = self.run_installer("full-mounted", "full", "ERASE WINDOWS\n", disk="windows", phone="fastboot",
                                     steps=self.full_steps(), env={"FAKE_MOUNTED": "1"})
        self.assertEqual(rc, 1, out)
        self.assertIn("eMMC partitions mounted", out)
        self.assertIn("MainOS", read_gpt(os.path.join(self.case, "disk.img")).names())

    def test_full_refuses_windows_default(self):
        rc, out = self.run_installer("full-windefault", "full", "ERASE WINDOWS\n", disk="windows", phone="fastboot",
                                     steps=self.full_steps("windows"))
        self.assertEqual(rc, 1, out)
        self.assertIn("not the default entry", out)
        self.assertIn("MainOS", read_gpt(os.path.join(self.case, "disk.img")).names())

    def test_full_reread_busy(self):
        rc, out = self.run_installer("full-busy", "full", "ERASE WINDOWS\n", disk="windows", phone="fastboot",
                                     steps=self.full_steps(), env={"FAKE_REREAD_BUSY": "1", "FAKE_NO_HEAD": "1"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.file("calls.log").count("fastboot boot"), 3)   # phase 2, again after the table, ROM
        self.check_phase2_disk()
        self.assertInstalled()

    def test_full_resume_after_table(self):
        steps = self.full_steps()
        rc, out = self.run_installer("full-resume", "full", "", disk="android", phone="fastboot", steps=steps)
        self.assertEqual(rc, 0, out)
        self.assertIn("phase 2 was interrupted", out)
        self.assertEqual(read_part(os.path.join(self.case, "disk.img"), "modemst1", len(self.modem["MODEM_FS1"])),
                         self.modem["MODEM_FS1"])
        self.assertInstalled()


class ChecksumsTest(unittest.TestCase):
    def test_twrp_pinned(self):
        self.assertEqual(android.twrp_hashes(REPO).get("c53687b54a394c553c5e8e6dacbc367d7ce91b4f536c59c5b8328dbe1ddabd2d"),
                         devices.BANDIT.twrp_name)

    def test_twrp_per_device(self):
        self.assertEqual(set(android.twrp_hashes(REPO, devices.BANDIT).values()), {devices.BANDIT.twrp_name})
        self.assertEqual(set(android.twrp_hashes(REPO, devices.MARTINI).values()), {devices.MARTINI.twrp_name})
        self.assertTrue(devices.MARTINI.twrp_file("twrp-3.7.0_9-0-martini.img"))
        self.assertFalse(devices.MARTINI.twrp_file("twrp-3.7.0_9-1-bandit.img"))
        self.assertFalse(devices.BANDIT.twrp_file("twrp-banditx.img"))


class DevicesTest(unittest.TestCase):
    def test_models(self):
        self.assertIs(devices.by_model("rm-937"), devices.BANDIT)
        self.assertIs(devices.by_model("RM1045"), devices.MARTINI)
        self.assertIs(devices.by_model("RM-1087"), devices.MARTINI)
        self.assertIsNone(devices.by_model("RM-927"))   # Lumia Icon: other firmware set
        self.assertIsNone(devices.by_model("RM-914"))   # Lumia 520

    def test_product_dat(self):
        d = devices.parse_product_dat("TYPE:RM-939\r\nLPSN:X\r\nHWID:5301\r\n")
        self.assertEqual(d["TYPE"], "RM-939")

    def test_payload_sections(self):
        with open(os.path.join(REPO, "payload", "payload.json")) as f:
            meta = json.load(f)
        self.assertEqual(set(meta["devices"]), set(devices.DEVICES))
        for code, sec in meta["devices"].items():
            for e in sec["lk1st"] + sec["boot"]:
                path = os.path.join(REPO, "payload", *e["path"].split("/"))
                self.assertTrue(e["path"].startswith(code + "/"), e["path"])
                self.assertEqual(sha(readb(path)), e["sha256"], e["path"])
            for e in sec["lk1st"]:
                from bandit_installer import images
                elf = images.check_bootshim_elf(os.path.join(REPO, "payload", *e["path"].split("/")))
                self.assertTrue(elf.ok, elf.errors)
                self.assertEqual(elf.entry, int(e["entry"], 16), e["id"])


if __name__ == "__main__":
    unittest.main()
