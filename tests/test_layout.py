"""Phase 2 layout tests, on disk images only (no phone).

    python3 -m unittest discover -s tests

The fake disk is created by sfdisk with the Lumia 1520 (RM-937, 32 GB) partition
layout and random GUIDs. When parted is installed, the Android4Lumia950
partition.sh sequence is run with it on a copy, and the table computed by
bandit_installer.layout must match it entry by entry.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from bandit_installer import gpt, layout  # noqa: E402

DISK_SECTORS = 61071360
# start, size, type, name, attrs (from the GPT of an RM-937, without its GUIDs)
LUMIA1520 = """\
4096 16384 EBD0A0A2-B9E5-4433-87C0-68B6B72699C7 DPP 63
20480 3072 638FF8E2-22C9-E33B-8F5D-0E81686A68CB MODEM_FSG 63
24576 3072 EBBEADAF-22C9-E33B-8F5D-0E81686A68CB MODEM_FS1 63
28672 3072 0A288B1F-22C9-E33B-8F5D-0E81686A68CB MODEM_FS2 63
32768 32 57B90A16-22C9-E33B-8F5D-0E81686A68CB MODEM_FSC 63
36864 32 20A0C19C-286A-42FA-9CE7-F64C3226A794 DDR 63
40960 32 2C86E742-745E-4FDD-BFD8-B6A7AC638772 SSD 63
45056 512 ABAA8F43-1E0D-4271-AAEF-7E6955009553 BACKUP_BS_NV 63
49152 512 6BB94537-7D1C-44D0-9DFE-6D77C011DBFC UEFI_RT_NV 63
53248 1024 A3381699-350C-465E-BD5D-FA3AB901A39A BACKUP_SBL1 63
57344 64 D4E0D938-B7FA-48C1-9D21-BC5ED5C4B203 DBI 63
61440 4096 A3381699-350C-465E-BD5D-FA3AB901A39A BACKUP_UEFI 63
65536 1000 A3381699-350C-465E-BD5D-FA3AB901A39A BACKUP_RPM 63
69632 1000 A3381699-350C-465E-BD5D-FA3AB901A39A BACKUP_TZ 63
73728 1024 A3381699-350C-465E-BD5D-FA3AB901A39A BACKUP_WINSECAPP 63
77824 32768 A3381699-350C-465E-BD5D-FA3AB901A39A BACKUP_TZAPPS 63
110592 1024 DEA0BA2C-CBDD-4805-B4F9-F428251C3E98 SBL1 63
114688 64 A3381699-350C-465E-BD5D-FA3AB901A39A BACKUP_DBI 63
118784 4096 400FFDCD-22E0-47E7-9A23-F16ED9382388 UEFI 63
122880 1000 098DF793-D712-413D-9D4E-89D711772228 RPM 63
126976 1000 A053AA7F-40B8-4B1C-BA08-2F68AC71A4F4 TZ 63
131072 1024 69B4201F-A5AD-45EB-9F49-45B38CCDAEF5 WINSECAPP 63
135168 32768 14D11C40-2A3D-4F97-882D-103A1EC09333 TZAPPS 63
167936 16384 543C031A-4CB6-4897-BFFE-4B485768A8AD PLAT 63
184320 65536 EBD0A0A2-B9E5-4433-87C0-68B6B72699C7 EFIESP 63
249856 8512 EBD0A0A2-B9E5-4433-87C0-68B6B72699C7 MMOS 63
262144 5155616 EBD0A0A2-B9E5-4433-87C0-68B6B72699C7 MainOS -
5423104 55648223 EBD0A0A2-B9E5-4433-87C0-68B6B72699C7 Data 63
45568 512 F0B4F48B-AEBA-4ECF-9142-5DC30CDC3E77 UEFI_BS_NV 63
64 1 1F9FEBA9-D568-4516-B6E3-1B29135A12E1 IS_UNLOCKED -
"""

# Lumia 930 (martini): no GPT of a real 930 has been read yet. Same Spec B set as the
# 1520; MainOS is shorter because Data starts at LBA 5308416 on the 32 GB 930 that
# Markynose audited (lumia930-linux, alpine/README.md). Replace with the real table
# once a 930 is backed up.
LUMIA930 = LUMIA1520.replace("262144 5155616 EBD0A0A2-B9E5-4433-87C0-68B6B72699C7 MainOS -",
                             "262144 5046272 EBD0A0A2-B9E5-4433-87C0-68B6B72699C7 MainOS -").replace(
    "5423104 55648223 EBD0A0A2-B9E5-4433-87C0-68B6B72699C7 Data 63",
    "5308416 55762911 EBD0A0A2-B9E5-4433-87C0-68B6B72699C7 Data 63")
TABLES = {"bandit": LUMIA1520, "martini": LUMIA930}

PARTITION_SH = r"""
export LC_ALL=C
D="$1"
sub() { parted "$D" print | grep -Ev '^(Model:|Disk|Sector size|Partition Table|Number)' | grep "$1" | awk '{print $1}'; }
for p in Data MainOS MMOS; do n=$(sub "$p"); [ -n "$n" ] && parted "$D" --script rm "$n"; done
parts=(aboot 1MiB boot 34MiB recovery 34MiB misc 16MiB modem 70MiB cache 150MiB persist 68MiB persistent 68MiB
       fsc 1MiB fsg 2MiB metadata 2MiB ssd 2MiB modemst1 2MiB modemst2 2MiB vendor 260MiB system 3072MiB userdata 100%)
for ((i=0; i<${#parts[@]}; i+=2)); do
    end_size=$(parted "$D" unit MiB print | tail -n 2 | awk '{print $3}')
    if [ "${parts[i+1]}" != "100%" ]; then total=$(( ${parts[i+1]//[^0-9]/} + ${end_size//[^0-9]/} )); else total="100%"; fi
    parted "$D" unit MiB --script mkpart "${parts[i]}" "$end_size" "$total"
done
"""


def sfdisk_script(table=LUMIA1520):
    lines = ["label: gpt", "first-lba: 34", f"last-lba: {DISK_SECTORS - 34}", ""]
    for row in table.splitlines():
        start, size, typ, name, attrs = row.split()
        a = ', attrs="GUID:63"' if attrs == "63" else ""
        lines.append(f'start={start}, size={size}, type={typ}, name="{name}"{a}')
    return "\n".join(lines) + "\n"


def entries(path):
    """[(slot, name, type, first, last, attrs)] read with the installer's parser."""
    with open(path, "rb") as f:
        g = gpt.read_gpt(f)
    return sorted((p.index, p.name, p.type_guid, p.first, p.last, p.attrs) for p in g.partitions)


@unittest.skipUnless(shutil.which("sfdisk"), "sfdisk needed to build the fake disk")
class LayoutTest(unittest.TestCase):
    TABLE = LUMIA1520

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lumia-layout-")
        cls.disk = cls.make_disk("lumia.img")

    @classmethod
    def make_disk(cls, name):
        """A sparse image with the phone's table (copies would not stay sparse)."""
        path = os.path.join(cls.tmp, name)
        with open(path, "wb") as f:
            f.truncate(DISK_SECTORS * gpt.SECTOR)
        subprocess.run(["sfdisk", "-q", "--no-reread", "--no-tell-kernel", path],
                       input=sfdisk_script(cls.TABLE), text=True, check=True, env=dict(os.environ, LC_ALL="C"))
        return path

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def plan(self):
        with open(self.disk, "rb") as f:
            return layout.make_plan(layout.RawGpt.read(f, DISK_SECTORS))

    def write_copy(self, plan, name):
        path = self.make_disk(name)
        with open(path, "r+b") as f:
            f.write(plan.after.primary())
            f.seek(plan.after.backup_lba_start() * gpt.SECTOR)
            f.write(plan.after.backup())
        return path

    def test_plan_verifies(self):
        plan = self.plan()
        self.assertEqual(layout.verify(plan), [])
        self.assertEqual([n for _s, n, _sz in plan.removed], ["MMOS", "MainOS", "Data"])
        names = [c[1] for c in plan.created]
        self.assertEqual(names, [n for n, _s, _f in layout.ANDROID_LAYOUT])
        self.assertEqual(plan.created[0][2], 249856)        # right after EFIESP (122 MiB)
        self.assertEqual(plan.created[-1][3], 61069311)     # last whole MiB

    def test_sfdisk_accepts_written_table(self):
        path = self.write_copy(self.plan(), "ours.img")
        r = subprocess.run(["sfdisk", "--verify", path], capture_output=True, text=True, env=dict(os.environ, LC_ALL="C"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("problem", (r.stdout + r.stderr).lower())

    @unittest.skipUnless(shutil.which("parted") and shutil.which("bash"), "parted needed for the reference")
    def test_matches_partition_sh(self):
        ours = self.write_copy(self.plan(), "ours-cmp.img")
        ref = self.make_disk("parted.img")
        subprocess.run(["bash", "-c", PARTITION_SH, "partition.sh", ref], check=True, capture_output=True)
        self.assertEqual(entries(ours), entries(ref))

    def test_refuses_second_run(self):
        plan = self.plan()
        with self.assertRaises(layout.LayoutError):
            layout.make_plan(plan.after)

    def test_detects_changed_entry(self):
        plan = self.plan()
        primary = bytearray(plan.after.primary())
        dpp = 2 * gpt.SECTOR  # entry 1 (DPP): move its first LBA, then fix the CRCs as a buggy writer would
        primary[dpp + 32] ^= 0x10
        bad = layout.RawGpt(plan.after.mbr, plan.after.header,
                            [bytes(primary[dpp + i * 128: dpp + (i + 1) * 128]) for i in range(plan.after.n_entries)],
                            DISK_SECTORS)
        errors = layout.verify(plan, bad.primary(), bad.backup())
        self.assertTrue(any("entry 1 changed" in e for e in errors), errors)
        self.assertTrue(any("DPP" in e for e in errors), errors)

    def test_detects_corrupted_backup(self):
        plan = self.plan()
        backup = bytearray(plan.after.backup())
        backup[100] ^= 0xFF
        self.assertTrue(layout.verify(plan, backup=bytes(backup)))


class MartiniLayoutTest(LayoutTest):
    """Same checks on the Lumia 930 table (synthetic until a real 930 GPT is read)."""
    TABLE = LUMIA930

    def test_userdata_does_not_depend_on_mainos(self):
        plan = self.plan()
        first, last = plan.created[-1][2], plan.created[-1][3]
        self.assertEqual(plan.created[-1][1], "userdata")
        self.assertEqual(last - first + 1, 25913 * 2048)   # same layout as the 1520 from EFIESP on


if __name__ == "__main__":
    unittest.main()
