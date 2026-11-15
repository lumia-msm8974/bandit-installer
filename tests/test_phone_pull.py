"""Windows binary reads: exec-out is replaced by a file in the recovery and adb pull."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from bandit_installer import phone, sysdeps  # noqa: E402


class FakeSystem:
    def find_tool(self, name):
        return name


class PullReadTest(unittest.TestCase):
    def setUp(self):
        self.saved = sysdeps.IS_WINDOWS
        sysdeps.IS_WINDOWS = True
        self.logs = []
        self.p = phone.Phone(FakeSystem(), self.logs.append, scan_usb=False)
        self.remote = {}
        self.calls = []
        self.payload = bytes(range(256)) * 68  # 17408 bytes with every byte value, LF included

        def adb(args, timeout=60, quiet=False, retry=True):
            self.calls.append(args)
            if args[0] == "shell" and phone.Phone.REMOTE_READ in args[1] and ">" in args[1]:
                self.remote["/tmp/.lumia-read"] = self.payload
                return 0, "lumia-read-rc=0 lumia-read-path=/tmp/.lumia-read\r\n"
            if args[0] == "pull":
                with open(args[2], "wb") as f:
                    f.write(self.remote[args[1]])
                return 0, "1 file pulled\n"
            if args[0] == "shell" and args[1].startswith("rm -f"):
                self.remote.pop(args[1].split()[-1], None)
                return 0, ""
            return 1, "unexpected"
        self.p.adb = adb

    def tearDown(self):
        sysdeps.IS_WINDOWS = self.saved

    def test_binary_read_is_exact(self):
        rc, data = self.p.adb_bytes(["exec-out", "dd if=/dev/block/mmcblk0 bs=512 count=34 2>/dev/null"])
        self.assertEqual(rc, 0)
        self.assertEqual(data, self.payload)
        self.assertEqual(self.calls[1][0], "pull")
        self.assertEqual(self.remote, {}, "the temporary file is removed")

    def test_failed_command_returns_error(self):
        self.p.adb = lambda args, **kw: (1, "error: closed")
        rc, data = self.p.adb_bytes(["exec-out", "dd if=/dev/null 2>/dev/null"])
        self.assertNotEqual(rc, 0)
        self.assertEqual(data, b"")


if __name__ == "__main__":
    unittest.main()
