#!/usr/bin/env python3
"""Android installer for the MSM8974 Nokia Lumias: 1520 (bandit) and 930 (martini). Windows and Linux."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bandit_installer.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(__file__, sys.argv[1:]))
