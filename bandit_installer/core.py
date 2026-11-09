"""Shared pieces: the UI contract the steps talk to, saved progress, the kit."""
import datetime
import json
import os
import re
import threading
import time

from . import images
from .i18n import L, get_lang


class Abort(Exception):
    """Stops the current task with a message for the user."""


class Cancelled(Exception):
    """The user cancelled the current task."""


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def human_size(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ---------------------------------------------------------------------------
# UI contract
# ---------------------------------------------------------------------------

class UI:
    """What the steps can ask of a front end (web or text).

    Every method may be called from the worker thread that runs a step.
    """

    def __init__(self, logfile=None):
        self.logfile = logfile
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()

    # log ---------------------------------------------------------------
    def write_log(self, text):
        if not self.logfile:
            return
        with self._lock, open(self.logfile, "a", encoding="utf-8") as f:
            f.write(datetime.datetime.now().strftime("%H:%M:%S ") + ANSI_RE.sub("", text) + "\n")

    def emit(self, kind, text):
        raise NotImplementedError

    def step(self, text):
        self.write_log("==> " + text)
        self.emit("step", text)

    def info(self, text):
        self.write_log("    " + text)
        self.emit("info", text)

    def ok(self, text):
        self.write_log("    OK: " + text)
        self.emit("ok", text)

    def warn(self, text):
        self.write_log("    WARNING: " + text)
        self.emit("warn", text)

    def error(self, text):
        self.write_log("    ERROR: " + text)
        self.emit("error", text)

    def detail(self, text):
        """Command output: kept in the log file, shown collapsed."""
        self.write_log(text)
        self.emit("detail", text)

    def show(self, title, lines):
        self.write_log(f"[{title}]\n" + "\n".join("    " + l for l in lines))
        self.emit_block(title, lines)

    def emit_block(self, title, lines):
        raise NotImplementedError

    # questions -----------------------------------------------------------
    def confirm(self, title, body, yes, no=None, word=None, danger=False):
        raise NotImplementedError

    def choose(self, title, body, options):
        """options: [{"value", "title", "sub", "disabled"}]; returns a value or None."""
        raise NotImplementedError

    def form(self, title, body, fields, submit):
        """fields: [{"key", "type": toggle|select|number|text, "label", "sub", "value", "options"}]."""
        raise NotImplementedError

    # progress ------------------------------------------------------------
    def progress(self, label, done, total):
        raise NotImplementedError

    def status(self, text):
        raise NotImplementedError

    def check_cancel(self):
        if self.cancel_event.is_set():
            raise Cancelled()

    def wait(self, poll, timeout, interval=1.0):
        """Calls poll() every interval s until it returns a non-None value.

        poll() may call ui.status() to show what it sees. Raises Cancelled.
        """
        end = time.time() + timeout if timeout else None
        while True:
            self.check_cancel()
            r = poll()
            if r is not None:
                return r
            if end and time.time() > end:
                return None
            for _ in range(int(interval * 10)):
                self.check_cancel()
                time.sleep(0.1)


# ---------------------------------------------------------------------------
# saved progress
# ---------------------------------------------------------------------------

class State:
    def __init__(self, path):
        self.path = path
        self.data = {"phones": {}, "last": None}
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except (OSError, ValueError):
                pass

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

    def phone(self, guid=None):
        guid = guid or self.data.get("last")
        if not guid:
            return {}
        return self.data["phones"].setdefault(guid, {"steps": {}})

    def set_phone(self, guid, info):
        p = self.data["phones"].setdefault(guid, {"steps": {}})
        p.update(info)
        self.data["last"] = guid
        self.save()

    def mark(self, step, **info):
        p = self.phone()
        if p is None or not self.data.get("last"):
            return
        info["time"] = datetime.datetime.now().isoformat(timespec="seconds")
        p.setdefault("steps", {})[step] = info
        self.save()

    def done(self, step):
        return self.phone().get("steps", {}).get(step)


# ---------------------------------------------------------------------------
# kit (payload)
# ---------------------------------------------------------------------------

class Kit:
    """payload/payload.json: BootShim (shared) and, per phone, lk1st builds and test kernels."""

    def __init__(self, payload_dir):
        self.dir = payload_dir
        with open(os.path.join(payload_dir, "payload.json"), encoding="utf-8") as f:
            self.meta = json.load(f)
        self._checked = {}

    def path(self, rel):
        return os.path.join(self.dir, *rel.split("/"))

    def check(self, entry):
        """True when the file exists and has the pinned SHA-256 (cached)."""
        p = self.path(entry["path"])
        key = (p, entry["sha256"])
        if key not in self._checked:
            self._checked[key] = os.path.isfile(p) and images.sha256_file(p) == entry["sha256"]
        return self._checked[key]

    def title(self, entry):
        return entry.get("title_" + get_lang()) or entry.get("title_en") or entry["id"]

    def note(self, entry):
        return entry.get("note_" + get_lang()) or entry.get("note_en") or ""

    def bootshim(self):
        return self.meta["bootshim"]

    def for_device(self, codename):
        return DeviceKit(self, codename)


class DeviceKit:
    """The kit as one phone sees it: the shared BootShim and that phone's lk1st and kernels."""

    def __init__(self, kit, codename):
        self.kit = kit
        self.codename = codename
        self.section = kit.meta.get("devices", {}).get(codename, {})
        self.path, self.check, self.title, self.note, self.bootshim = kit.path, kit.check, kit.title, kit.note, kit.bootshim

    def lk1st_entries(self):
        return list(self.section.get("lk1st", []))

    def lk1st(self, ident=None):
        for e in self.lk1st_entries():
            if ident in (None, e["id"]):
                return e
        return None

    def boot_images(self):
        return list(self.section.get("boot", []))

    def status_lines(self):
        out = [("BootShim", self.check(self.bootshim()))]
        for e in self.lk1st_entries():
            out.append((f"lk1st {e['id']}", self.check(e)))
        for e in self.boot_images():
            out.append((e["id"].upper(), self.check(e)))
        return out
