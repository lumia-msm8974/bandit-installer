"""adb/fastboot too old for TWRP (adb 1.0.32 from a 2015 C:\\adb) are detected and replaced."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from bandit_installer import phone  # noqa: E402


class FakeSystem:
    def find_tool(self, name):
        return "C:\\adb\\" + name + ".exe"


def make(adb_text, fastboot_text, **kw):
    p = phone.Phone(FakeSystem(), lambda t: None, scan_usb=False, **kw)
    texts = {"version": adb_text, "--version": fastboot_text}
    p._version_text = lambda exe, arg: texts[arg]
    return p


NEW_ADB = "Android Debug Bridge version 1.0.41\nVersion 35.0.2-12147458\n"
NEW_FB = "fastboot version 35.0.2-12147458\n"


class OutdatedTest(unittest.TestCase):
    def test_current_tools(self):
        self.assertEqual(make(NEW_ADB, NEW_FB).outdated_tools(), [])

    def test_old_c_adb(self):
        p = make("Android Debug Bridge version 1.0.32\nRevision 09a0d98bebce-android\n", "")
        self.assertEqual(p.adb_version(), (1, 0, 32))
        self.assertEqual(p.outdated_tools(), ["adb", "fastboot"])

    def test_unreadable_version_is_old(self):
        self.assertEqual(make("", NEW_FB).outdated_tools(), ["adb"])

    def test_minimum_accepted(self):
        self.assertEqual(make("Android Debug Bridge version 1.0.39\n", NEW_FB).outdated_tools(), [])

    def test_override_trusted(self):
        p = make("", "", adb="/opt/adb", fastboot="/opt/fastboot")
        self.assertEqual(p.outdated_tools(), [])


if __name__ == "__main__":
    unittest.main()
