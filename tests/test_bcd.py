"""BCD tests on a real phone BCD: plan, hivex writer, independent check, bcdedit commands.

    LUMIA_TEST_BCD=<EFI/Microsoft/Boot/BCD of a phone backup> python3 -m unittest tests.test_bcd

No BCD is shipped (it comes from a phone). The writer tests also need python3-hivex
(tests/docker/run-tests.sh has it).
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from bandit_installer import bcd  # noqa: E402

BCD = os.environ.get("LUMIA_TEST_BCD")
OPTIONS = {
    "android-only": bcd.Options(default="android", android_only=True),
    "android-default-voldown-windows": bcd.Options(default="android", windows_key="voldown"),
    "windows-default-voldown-android": bcd.Options(default="windows", windows_key="voldown", timeout=5),
    "android-default-no-key": bcd.Options(default="android"),
}


@unittest.skipUnless(BCD and os.path.isfile(BCD), "set LUMIA_TEST_BCD to a phone's BCD")
class BcdTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lumia-bcd-")
        cls.src = os.path.join(cls.tmp, "BCD")
        shutil.copyfile(BCD, cls.src)
        cls.view = bcd.BcdView(cls.src)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_reads_phone_bcd(self):
        self.assertTrue(self.view.flash_actions(), "volume up must open flash mode")
        self.assertIsNotNone(self.view.template()[0])
        self.assertIsNotNone(self.view.windows())

    def test_windows_is_the_previous_default(self):
        d = self.view.default()
        if self.view.path(d).lower().endswith(("winload.efi", "mobilestartup.efi")):
            self.assertEqual(self.view.windows(), d)

    def test_plans(self):
        for name, opts in OPTIONS.items():
            with self.subTest(name):
                plan = bcd.Plan(self.view, opts)
                self.assertEqual(plan.order[0] if opts.default == "android" else plan.order[-1], bcd.ANDROID)
                for k, e, _t in plan.flash:
                    self.assertNotIn(e, plan.targets, "flash mode target must not change")

    @unittest.skipUnless(bcd.hivex_available(), "python3-hivex needed")
    def test_hivex_writer_verifies(self):
        for name, opts in OPTIONS.items():
            with self.subTest(name):
                plan = bcd.Plan(self.view, opts)
                out = os.path.join(self.tmp, f"BCD.{name}")
                bcd.write_hivex(self.src, out, plan)
                after = bcd.BcdView(out)
                guid, errors = bcd.verify(self.view, after, plan)
                self.assertEqual(errors, [], name)
                self.assertEqual(guid, bcd.ANDROID_FIXED.lower())
                self.assertEqual(after.flash_actions(), self.view.flash_actions())
                # a second run replaces the entry instead of adding another one
                again = os.path.join(self.tmp, f"BCD.{name}.again")
                bcd.write_hivex(out, again, bcd.Plan(after, opts))
                self.assertEqual(len(bcd.BcdView(again).bootshim_entries()), 1)

    def test_bcdedit_commands(self):
        plan = bcd.Plan(self.view, OPTIONS["android-default-voldown-windows"])
        g = "{11111111-2222-3333-4444-555555555555}"
        cmds = bcd.bcdedit_commands(plan, g)
        flat = [" ".join(c) for c in cmds]
        self.assertIn(f"/set {g} path {bcd.BOOTSHIM_PATH}", flat)
        self.assertIn(f"/default {g}", flat)
        self.assertTrue(any(c[:3] == ["/set", "{bootmgr}", "customactions"] for c in cmds))
        for _k, e, _t in plan.flash:
            self.assertFalse(any(x.startswith(f"/set {{bootmgr}} custom:{e:08x}") for x in flat),
                             "the flash mode target is never rewritten")


if __name__ == "__main__":
    unittest.main()
