"""Mass storage tests: backup, firmware, boot chain and EFIESP restore on a disk image.

    python3 -m unittest discover -s tests

The installer runs with --test and LUMIA_TEST_DISK pointing at a sparse image with the
phone's partition table (sysdeps.ImageSystem): FAT partitions are read and written
with mtools, MainOS (NTFS) is a folder given in LUMIA_TEST_DIRS. Nothing needs root.

The boot chain test needs a real BCD hive. No BCD is shipped with
the tests (it comes from a phone): set LUMIA_TEST_BCD to the EFI/Microsoft/Boot/BCD of
a phone's EFIESP backup to run it.

Needs sfdisk, mkfs.fat and mtools (mmd, mcopy, mdir).
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

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, HERE)

from bandit_installer import bcd, gpt  # noqa: E402
from test_layout import DISK_SECTORS, TABLES, sfdisk_script  # noqa: E402

TOOLS = ("sfdisk", "mkfs.fat", "mmd", "mcopy", "mdir")
MTOOLS_ENV = dict(os.environ, MTOOLS_SKIP_CHECK="1")


def sha(b):
    return hashlib.sha256(b).hexdigest()


def readb(path):
    with open(path, "rb") as f:
        return f.read()


def rows(table):
    return {r.split()[3]: (int(r.split()[0]), int(r.split()[1])) for r in table.splitlines()}


def fat_image(path, kib, dirs=(), files=()):
    subprocess.run(["mkfs.fat", "-C", path, str(kib)], check=True, capture_output=True)
    for d in dirs:
        subprocess.run(["mmd", "-i", path, "::" + d], check=True, capture_output=True, env=MTOOLS_ENV)
    for local, dest in files:
        subprocess.run(["mcopy", "-i", path, local, "::" + dest], check=True, capture_output=True, env=MTOOLS_ENV)
    return readb(path)


def write_at(img, sector, data):
    with open(img, "r+b") as f:
        f.seek(sector * gpt.SECTOR)
        f.write(data)


def read_part(img, name):
    with open(img, "rb") as f:
        p = gpt.read_gpt(f).by_name(name)
        f.seek(p.offset)
        return f.read(p.size)


def mtype(img, offset, path):
    p = subprocess.run(["mtype", "-i", f"{img}@@{offset}", "::" + path], capture_output=True, env=MTOOLS_ENV)
    return p.stdout if p.returncode == 0 else None


def fake_pil(path, segments=3):
    """A small ELF with program headers, enough for images.pil_split."""
    ph_off, ph_size = 52, 32
    data_off = ph_off + ph_size * segments
    blobs = [os.urandom(64 + i) for i in range(segments)]
    hdr = bytearray(b"\x7fELF\x01\x01\x01" + bytes(9))
    hdr += struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0, ph_off, 0, 0, 52, ph_size, segments, 0, 0, 0)
    phs, body, off = b"", b"", data_off
    for b in blobs:
        phs += struct.pack("<8I", 1, off, 0, 0, len(b), len(b), 4, 0x1000)
        body += b
        off += len(b)
    with open(path, "wb") as f:
        f.write(bytes(hdr) + phs + body)
    return blobs


@unittest.skipUnless(all(shutil.which(t) for t in TOOLS), "sfdisk, mkfs.fat and mtools needed")
class MassStorageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lumia-mass-")
        cls.inst = os.path.join(cls.tmp, "installer")
        os.makedirs(cls.inst)
        for d in ("bandit_installer", "payload"):
            shutil.copytree(os.path.join(REPO, d), os.path.join(cls.inst, d), ignore=shutil.ignore_patterns("__pycache__"))
        for f in ("installer.py", "checksums.sha256"):
            shutil.copy(os.path.join(REPO, f), cls.inst)
        cls.disks = {}
        for name, device, model in (("bandit", "bandit", "RM-937"), ("martini", "martini", "RM-1045"),
                                    ("icon", "martini", "RM-927"), ("nodpp", "bandit", None)):
            cls.disks[name] = cls.make_disk(name, TABLES[device], model)
        # a phone not unlocked yet: WPinternals adds IS_UNLOCKED when it unlocks
        locked = "\n".join(r for r in TABLES["bandit"].splitlines() if "IS_UNLOCKED" not in r) + "\n"
        cls.disks["locked"] = cls.make_disk("locked", locked, "RM-937")
        cls.mainos = os.path.join(cls.tmp, "MainOS")
        drv = os.path.join(cls.mainos, "Windows", "System32", "DriverStore", "FileRepository", "qcadsp.inf_arm_x")
        os.makedirs(drv)
        cls.adsp = fake_pil(os.path.join(drv, "QCADSP8974.MBN"))
        # the modem MBA has a single segment and no hash segment, as on a real 930
        cls.mba = fake_pil(os.path.join(drv, "qcdsp1v28974.mbn"), segments=1)
        with open(os.path.join(drv, "qcwlan8974cfg.dat"), "wb") as f:
            f.write(b"wlan cfg")
        cfg = os.path.join(cls.mainos, "Windows", "System32", "config")
        os.makedirs(cfg)
        for hive in ("SYSTEM", "SOFTWARE"):
            with open(os.path.join(cfg, hive), "wb") as f:
                f.write(b"regf" + hive.encode())

    @classmethod
    def make_disk(cls, name, table, model):
        path = os.path.join(cls.tmp, name + ".img")
        with open(path, "wb") as f:
            f.truncate(DISK_SECTORS * gpt.SECTOR)
        subprocess.run(["sfdisk", "-q", "--no-reread", "--no-tell-kernel", path], input=sfdisk_script(table),
                       text=True, check=True, env=dict(os.environ, LC_ALL="C"))
        r = rows(table)
        w = os.path.join(cls.tmp, "fat-" + name)
        os.makedirs(w)
        files, dirs = [], ["QCOM"]
        with open(os.path.join(w, "BT.PROVISION"), "wb") as f:
            f.write(b"\x01\x06\x0c\x1d\x2e\x3f\x4a\x5b")
        files.append((os.path.join(w, "BT.PROVISION"), "QCOM/BT.PROVISION"))
        if model:
            dirs.append("Nokia")
            with open(os.path.join(w, "product.dat"), "w", newline="\r\n") as f:
                f.write(f"TYPE:{model}\nLPSN:TEST0001\nHWID:5301\n")
            files.append((os.path.join(w, "product.dat"), "Nokia/product.dat"))
        write_at(path, r["DPP"][0], fat_image(os.path.join(w, "dpp.img"), r["DPP"][1] // 2, dirs, files))
        with open(os.path.join(w, "dsdt.aml"), "wb") as f:
            f.write(b"DSDT" + name.encode())
        write_at(path, r["PLAT"][0], fat_image(os.path.join(w, "plat.img"), r["PLAT"][1] // 2, ["ACPI"],
                                               [(os.path.join(w, "dsdt.aml"), "ACPI/dsdt.aml")]))
        bcd_src = os.environ.get("LUMIA_TEST_BCD")
        bcd_file = os.path.join(w, "BCD")
        if bcd_src and os.path.isfile(bcd_src):
            shutil.copyfile(bcd_src, bcd_file)
        else:
            with open(bcd_file, "wb") as f:
                f.write(b"not a real hive")
        with open(os.path.join(w, "bootarm.efi"), "wb") as f:
            f.write(b"MZ bootarm")
        write_at(path, r["EFIESP"][0], fat_image(
            os.path.join(w, "efiesp.img"), r["EFIESP"][1] // 2,
            ["EFI", "EFI/Microsoft", "EFI/Microsoft/Boot", "EFI/Boot", "Windows", "Windows/System32", "Windows/System32/Boot"],
            [(bcd_file, "EFI/Microsoft/Boot/BCD"), (os.path.join(w, "bootarm.efi"), "EFI/Boot/bootarm.efi")]))
        for n in ("MODEM_FS1", "MODEM_FSG", "SSD"):
            write_at(path, r[n][0], os.urandom(r[n][1] * gpt.SECTOR))
        return path

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def run_installer(self, name, action, disk, stdin="", device=None, extra_env=None):
        s = os.path.join(self.tmp, "cases", name)
        os.makedirs(s)
        img = os.path.join(s, "disk.img")
        subprocess.run(["cp", "--sparse=always", self.disks[disk], img], check=True)
        work = os.path.join(s, "work")
        env = dict(os.environ, LUMIA_TEST_DISK=img, LUMIA_TEST_DIRS=f"MainOS={self.mainos}", BANDIT_NO_USB_SCAN="1",
                   MTOOLS_SKIP_CHECK="1")
        env.pop("ANDROID_SERIAL", None)
        env.update(extra_env or {})
        cmd = [sys.executable, "-I", os.path.join(self.inst, "installer.py"), "--test", "--text", "--no-elevate",
               "--lang", "en", "--work", work, "--action", action, "--adb", "/bin/false", "--fastboot", "/bin/false"]
        if device:
            cmd += ["--device", device]
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=env, timeout=600)
        out = p.stdout + p.stderr
        with open(os.path.join(s, "out.txt"), "w") as f:
            f.write(out)
        self.case, self.img, self.work = s, img, work
        return p.returncode, out

    def state(self):
        with open(os.path.join(self.work, "state.json")) as f:
            return json.load(f)

    def only_dir(self, sub, prefix):
        base = os.path.join(self.work, sub)
        hits = [os.path.join(base, d) for d in os.listdir(base) if d.startswith(prefix)]
        self.assertEqual(len(hits), 1, hits)
        return hits[0]

    # ------------------------------------------------------------------ backup
    def test_backup_bandit(self):
        rc, out = self.run_installer("backup-bandit", "backup", "bandit", stdin="n\n", device="bandit")
        self.assertEqual(rc, 0, out)
        self.assertIn("Phone RM-937: Lumia 1520 (bandit)", out)
        bk = self.only_dir("backups", "telefono-")
        for name in ("DPP", "MODEM_FS1", "SSD", "EFIESP", "PLAT", "IS_UNLOCKED"):
            self.assertEqual(readb(os.path.join(bk, "partizioni", f"{name}.img")), read_part(self.img, name), name)
        self.assertEqual(readb(os.path.join(bk, "gpt-primaria.bin")), readb(self.img)[:34 * 512])
        with open(os.path.join(bk, "gpt.json")) as f:
            info = json.load(f)
        self.assertNotIn("MainOS", [p["name"] for p in info["partitions"] if os.path.exists(
            os.path.join(bk, "partizioni", p["name"] + ".img"))])
        self.assertFalse([f for f in os.listdir(bk) if f.endswith("-full.img") or "-full.img." in f])
        st = self.state()
        phone = st["phones"][st["last"]]
        self.assertEqual((phone["device"], phone["rm"]), ("bandit", "RM-937"))
        self.assertIn("backup", phone["steps"])
        with open(os.path.join(bk, "SHA256SUMS")) as f:
            manifest = f.read()
        self.assertIn(sha(read_part(self.img, "DPP")), manifest)

    def test_backup_martini(self):
        rc, out = self.run_installer("backup-martini", "backup", "martini", stdin="n\n", device="martini")
        self.assertEqual(rc, 0, out)
        self.assertIn("Phone RM-1045: Lumia 930 (martini)", out)
        bk = self.only_dir("backups", "telefono-")
        self.assertEqual(readb(os.path.join(bk, "partizioni", "DPP.img")), read_part(self.img, "DPP"))

    def test_wrong_profile_refused(self):
        rc, out = self.run_installer("backup-wrong", "backup", "bandit", stdin="n\n", device="martini")
        self.assertEqual(rc, 1, out)
        self.assertIn("RM-937 (Lumia 1520 (bandit))", out)
        self.assertIn("--device bandit", out)
        self.assertFalse(os.path.isdir(os.path.join(self.work, "backups")) and
                         [d for d in os.listdir(os.path.join(self.work, "backups")) if d.startswith("telefono-")])

    def test_lumia_icon_refused(self):
        rc, out = self.run_installer("backup-icon", "backup", "icon", stdin="n\n", device="martini")
        self.assertEqual(rc, 1, out)
        self.assertIn("RM-927: not a supported model", out)

    def test_unreadable_model_warns(self):
        rc, out = self.run_installer("backup-nodpp", "backup", "nodpp", stdin="n\n", device="bandit")
        self.assertEqual(rc, 0, out)
        self.assertIn("Could not read the phone model (DPP)", out)

    def test_backup_full_image(self):
        rc, out = self.run_installer("backup-full", "backup", "martini", stdin="y\n", device="martini")
        self.assertEqual(rc, 0, out)
        bk = self.only_dir("backups", "telefono-")
        full = [f for f in os.listdir(bk) if f.startswith("lumia-martini-full.img")]
        self.assertEqual(len(full), 1, os.listdir(bk))

    def test_full_refuses_locked_phone_first(self):
        rc, out = self.run_installer("full-locked", "full", "locked", device="bandit")
        self.assertEqual(rc, 1, out)
        self.assertIn("The phone is not unlocked (no IS_UNLOCKED)", out)
        self.assertNotIn("Checking TWRP", out)       # nothing else was asked or checked
        self.assertNotIn("Phone backup", out)
        self.assertFalse(os.path.isdir(os.path.join(self.work, "backups")) and
                         [d for d in os.listdir(os.path.join(self.work, "backups")) if d.startswith("telefono-")])

    def test_full_accepts_prepared_phone(self):
        rc, out = self.run_installer("full-prepared", "full", "bandit", device="bandit")
        self.assertIn("Preparation done: phone unlocked and in mass storage", out)
        self.assertIn("Checking TWRP", out)          # then goes on (stops later: no TWRP in this test)

    # ------------------------------------------------------------------ firmware
    def test_firmware(self):
        rc, out = self.run_installer("firmware", "firmware", "martini", device="martini")
        self.assertEqual(rc, 0, out)
        fw = self.only_dir("private", "firmware-")
        self.assertEqual(readb(os.path.join(fw, "acpi", "ACPI", "dsdt.aml")), b"DSDTmartini")
        self.assertEqual(readb(os.path.join(fw, "registro", "SYSTEM")), b"regfSYSTEM")
        split = os.path.join(fw, "split")
        for i, blob in enumerate(self.adsp):
            self.assertEqual(readb(os.path.join(split, f"adsp.b{i:02d}")), blob)
        self.assertEqual(readb(os.path.join(split, "adsp.mdt")), self.adsp[0] + self.adsp[1])
        self.assertEqual(readb(os.path.join(split, "mba.b00")), self.mba[0])
        self.assertFalse(os.path.exists(os.path.join(split, "mba.mdt")))
        self.assertIn("qcdsp1v28974.mbn -> mba.b00", out)
        self.assertIn("qcwcnss8974.mbn not found", out)
        self.assertTrue(any(f.lower() == "qcwlan8974cfg.dat" for _b, _d, fs in os.walk(os.path.join(fw, "stock")) for f in fs))

    # ------------------------------------------------------------------ modes
    def test_full_mode_locked_for_martini(self):
        rc, out = self.run_installer("full-martini", "full", "martini", device="martini")
        self.assertEqual(rc, 1, out)
        self.assertIn("The Full mode is not available yet for the Lumia 930 (martini)", out)

    def test_clean_mode_locked_for_martini(self):
        rc, out = self.run_installer("clean-martini", "clean", "martini", device="martini")
        self.assertEqual(rc, 1, out)
        self.assertIn("not available yet for the Lumia 930", out)

    def test_martini_phase2_needs_the_twrp_file(self):
        rc, out = self.run_installer("phase2-martini", "phase2", "martini", device="martini")
        self.assertEqual(rc, 1, out)
        self.assertIn("Put twrp-3.7.0_9-0-martini.img in the folder", out)
        self.assertIn("no release to download it from", out)

    # ------------------------------------------------------------------ boot chain
    def test_chain_refuses_fake_bcd(self):
        if os.environ.get("LUMIA_TEST_BCD"):
            self.skipTest("a real BCD is in use")
        before = read_part(self.disks["martini"], "EFIESP")
        rc, out = self.run_installer("chain-fake-bcd", "chain", "martini", device="martini")
        self.assertEqual(rc, 1, out)
        self.assertEqual(read_part(self.img, "EFIESP"), before)

    @unittest.skipUnless(os.environ.get("LUMIA_TEST_BCD"), "set LUMIA_TEST_BCD to a phone's BCD")
    def test_chain_and_restore_martini(self):
        before = read_part(self.disks["martini"], "EFIESP")
        efiesp = rows(TABLES["martini"])["EFIESP"][0] * 512
        # form: lk1st variant 1 (0x7CC00000), then four fields left at their defaults
        # (Android at power-on, volume down -> Windows, not Android only, no menu); INSTALL; no restart now
        rc, out = self.run_installer("chain", "chain", "martini", device="martini",
                                     stdin="1\n\n\n\n\nINSTALL\nn\n")
        self.assertEqual(rc, 0, out)
        self.assertIn("entry 0x7cc00000", out)
        self.assertIn("has never been tried on a Lumia 930", out)
        payload = os.path.join(REPO, "payload")
        self.assertEqual(mtype(self.img, efiesp, "/emmc_appsboot.mbn"),
                         readb(os.path.join(payload, "martini", "lk1st", "base-7cc", "emmc_appsboot.mbn")))
        self.assertEqual(mtype(self.img, efiesp, "/Windows/System32/Boot/bootshim.efi"),
                         readb(os.path.join(payload, "bootshim", "BootShim.efi")))
        new_bcd = os.path.join(self.case, "BCD.after")
        with open(new_bcd, "wb") as f:
            f.write(mtype(self.img, efiesp, "/EFI/Microsoft/Boot/BCD"))
        view = bcd.BcdView(new_bcd)
        self.assertTrue(view.bootshim_entries())
        self.assertEqual(view.default(), view.bootshim_entries()[0])
        keys = {bcd.KEY_NAMES.get(k): [view.path(g).lower().rsplit("\\", 1)[-1] for g in t] for k, _e, t in view.actions()}
        self.assertEqual(keys["volup"], ["ffuloader.efi"])
        before_bcd = os.path.join(self.case, "BCD.before")
        with open(before_bcd, "wb") as f:
            f.write(readb(os.environ["LUMIA_TEST_BCD"]))
        old_default = bcd.BcdView(before_bcd).default()
        self.assertEqual([g for _k, _e, t in view.actions() if _k == bcd.KEYS["voldown"] for g in t], [old_default])
        bk = self.only_dir("backups", "efiesp-")
        self.assertEqual(readb(os.path.join(bk, "efiesp-partition.img")), before)
        # restore the whole partition image: EFIESP byte for byte as before
        env_case = self.case
        shutil.copyfile(self.img, os.path.join(self.tmp, "after-chain.img"))
        rc, out = self.run_restore(env_case, bk)
        self.assertEqual(rc, 0, out)
        self.assertEqual(read_part(self.img, "EFIESP"), before)

    def run_restore(self, case, backup):
        """Restore in the same work folder, on the disk the chain step wrote."""
        img = os.path.join(case, "disk.img")
        env = dict(os.environ, LUMIA_TEST_DISK=img, BANDIT_NO_USB_SCAN="1", MTOOLS_SKIP_CHECK="1")
        cmd = [sys.executable, "-I", os.path.join(self.inst, "installer.py"), "--test", "--text", "--no-elevate",
               "--lang", "en", "--work", os.path.join(case, "work"), "--action", "restore", "--device", "martini",
               "--adb", "/bin/false", "--fastboot", "/bin/false"]
        p = subprocess.run(cmd, input="1\n2\nRESTORE\n", capture_output=True, text=True, env=env, timeout=600)
        self.img = img
        return p.returncode, p.stdout + p.stderr


if __name__ == "__main__":
    unittest.main()
