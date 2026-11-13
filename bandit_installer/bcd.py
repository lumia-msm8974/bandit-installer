"""The phone's boot configuration (BCD): read, plan, change, verify.

The change is the one the kit's bcd-bootshim.py makes: a "Android (lk1st)"
entry that starts BootShim, with the EFIESP device element copied from an
entry already on the same partition (resetphone or Mobile Startup), so no
BCD from another phone is ever used.

Three tools can write it: regfw.py (pure Python, the default on Linux, so no
extra package is needed), python3-hivex (the one first proven on the phone,
chosen with LUMIA_BCD_WRITER=hivex) and Microsoft's bcdedit on Windows. The
two Linux writers make the same calls in the same order. Whichever writes,
the result is read back with regf.py and checked against the same plan
before anything is copied to the phone. The volume-up action (flash mode, the way back) is
never changed.
"""
import os
import re
import shutil
import struct
import subprocess

from . import regfw
from .regf import Hive

BOOTMGR = "{9dea862c-5cdd-4e70-acc1-f32b344d4795}"
# Fixed id used by the Linux writer ("bandit" in hex in the last block).
ANDROID_FIXED = "{1520b007-5e1f-4a1b-9b8e-62616e646974}"
ANDROID = "<android>"  # placeholder in plans; replaced by the real id
BOOTSHIM_PATH = "\\Windows\\System32\\BOOT\\bootshim.efi"
ANDROID_DESC = "Android (lk1st)"
TEMPLATE_PATHS = ("resetphone.efi", "mobilestartup.efi")

E_DEVICE = "11000001"
E_PATH = "12000002"
E_DESC = "12000004"
E_NOINTEGRITY = "16000048"
E_TESTSIGN = "16000049"
E_DEFAULT = "23000003"
E_ORDER = "24000001"
E_TIMEOUT = "25000004"
E_CUSTOM_FIRST = "26000028"
E_CUSTOM_LIST = "27000030"
ANDROID_ELEMENTS = {E_DEVICE, E_PATH, E_DESC, E_NOINTEGRITY, E_TESTSIGN}

REG_SZ, REG_BINARY, REG_DWORD, REG_MULTI_SZ = 1, 3, 4, 7

KEYS = {"volup": 0x48000001, "voldown": 0x50000001}
KEY_NAMES = {v: k for k, v in KEYS.items()}


class BcdError(Exception):
    pass


# ---------------------------------------------------------------------------
# encoding helpers
# ---------------------------------------------------------------------------

def sz(s):
    return s.encode("utf-16le") + b"\0\0"


def multi_sz(items):
    return b"".join(sz(i) for i in items) + b"\0\0"


def parse_sz(b):
    return b.decode("utf-16le", "replace").split("\0", 1)[0]


def parse_multi_sz(b):
    return [x for x in b.decode("utf-16le", "replace").split("\0") if x]


def parse_actions(b):
    """CustomActionsList: 16-byte records (key, 0x00010000, element, 0)."""
    out = []
    for i in range(0, len(b) - len(b) % 16, 16):
        key, _flags, elem, _zero = struct.unpack_from("<IIII", b, i)
        out.append((key, elem))
    return out


def pack_actions(actions):
    return b"".join(struct.pack("<IIII", k, 0x00010000, e, 0) for k, e in actions)


# ---------------------------------------------------------------------------
# read-only view
# ---------------------------------------------------------------------------

class BcdView:
    def __init__(self, path):
        hive = Hive.open(path)
        objs = hive.root.subkey("Objects")
        if objs is None:
            raise BcdError("no Objects key: not a BCD")
        self.objects = {}
        for k in objs.subkeys():
            desc = k.subkey("Description")
            t = desc.value("Type") if desc else None
            otype = struct.unpack("<I", t[1][:4])[0] if t and len(t[1]) >= 4 else None
            els = {}
            ek = k.subkey("Elements")
            if ek:
                for e in ek.subkeys():
                    v = e.value("Element")
                    if v is not None:
                        els[e.name.lower()] = v
            self.objects[k.name.lower()] = {"name": k.name, "type": otype, "elements": els}
        if BOOTMGR not in self.objects:
            raise BcdError("no Windows Boot Manager object in the BCD")

    def els(self, guid):
        o = self.objects.get(guid.lower())
        return o["elements"] if o else {}

    def string(self, guid, etype):
        v = self.els(guid).get(etype)
        return parse_sz(v[1]) if v else ""

    def path(self, guid):
        return self.string(guid, E_PATH)

    def desc(self, guid):
        return self.string(guid, E_DESC)

    def label(self, guid):
        if guid.lower() == BOOTMGR:
            return "bootmgr"
        p = self.path(guid)
        d = self.desc(guid)
        return f"{d or '?'} ({os.path.basename(p.replace(chr(92), '/')) or '-'})"

    def order(self):
        v = self.els(BOOTMGR).get(E_ORDER)
        return [g.lower() for g in parse_multi_sz(v[1])] if v else []

    def default(self):
        v = self.els(BOOTMGR).get(E_DEFAULT)
        return parse_sz(v[1]).lower() if v else ""

    def timeout(self):
        v = self.els(BOOTMGR).get(E_TIMEOUT)
        return struct.unpack("<Q", v[1][:8])[0] if v and len(v[1]) >= 8 else None

    def actions(self):
        """[(key, element, [target guids])]"""
        v = self.els(BOOTMGR).get(E_CUSTOM_LIST)
        out = []
        for key, elem in parse_actions(v[1]) if v else []:
            t = self.els(BOOTMGR).get(f"{elem:08x}")
            out.append((key, elem, [g.lower() for g in parse_multi_sz(t[1])] if t else []))
        return out

    def find_by_path(self, suffix):
        return [g for g in self.objects if self.path(g).lower().endswith(suffix.lower())]

    def template(self):
        for want in TEMPLATE_PATHS:
            for g in self.find_by_path(want):
                if E_DEVICE in self.els(g):
                    return g, want
        return None, None

    def windows(self):
        """The entry that starts Windows: the default the phone booted before (winload.efi on the
        1520's Windows Phone 8.1 BCD, Mobile Startup on Windows 10 Mobile), else Mobile Startup,
        else the Windows loader."""
        d = self.default()
        if d in self.objects and self.path(d).lower().endswith(("winload.efi", "mobilestartup.efi")):
            return d
        for want in ("mobilestartup.efi", "winload.efi"):
            hits = self.find_by_path(want)
            if hits:
                return hits[0]
        return None

    def bootshim_entries(self):
        return self.find_by_path("bootshim.efi")

    def flash_actions(self):
        """Actions that open flash mode (ffuloader.efi): never touched."""
        return [(k, e, t) for k, e, t in self.actions()
                if any(self.path(g).lower().endswith("ffuloader.efi") for g in t)]

    def summary(self):
        """Lines describing the menu and the key actions, for the UI."""
        lines = []
        d = self.default()
        shown = self.order()
        if d and d not in shown:
            shown = [d] + shown  # Lumia BCDs often boot the default with no menu
        for g in shown:
            mark = "*" if g == d else " "
            lines.append(f"{mark} {self.label(g)}")
        to = self.timeout()
        if to is not None:
            lines.append(f"  timeout {to}s")
        for key, _elem, targets in self.actions():
            name = KEY_NAMES.get(key, f"0x{key:08x}")
            lines.append(f"  [{name}] -> " + ", ".join(self.label(g) for g in targets))
        return lines


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

class Options:
    def __init__(self, default="android", timeout=0, windows_key="none", android_only=False):
        if default not in ("android", "windows"):
            raise ValueError(default)
        if windows_key not in ("none", "voldown"):
            raise ValueError(windows_key)
        self.default = default
        self.timeout = int(timeout)
        self.windows_key = windows_key
        self.android_only = bool(android_only)


class Plan:
    """What the BCD must look like after the change (ANDROID = new entry)."""

    def __init__(self, view, opts):
        self.opts = opts
        tmpl, kind = view.template()
        if not tmpl:
            raise BcdError("no resetphone/Mobile Startup entry to copy the EFIESP device from")
        self.template = tmpl
        self.template_kind = kind
        self.device = view.els(tmpl)[E_DEVICE]
        self.otype = view.objects[tmpl]["type"]
        self.windows = view.windows()
        self.remove = [g for g in view.bootshim_entries()]
        order = [g for g in view.order() if g not in self.remove]
        if opts.android_only:
            order = [ANDROID]
        elif opts.default == "android":
            order = [ANDROID] + order
        else:
            order = order + [ANDROID]
        self.order = order
        if opts.default == "android" or opts.android_only:
            self.default = ANDROID
        else:
            self.default = self.windows or order[0]
        self.timeout = opts.timeout

        actions = [(k, e) for k, e, _t in view.actions()]
        self.targets = {}  # element -> new target list (only changed ones)
        for k, e, t in view.actions():
            if any(g in self.remove for g in t):
                self.targets[e] = [ANDROID if g in self.remove else g for g in t]
        self.flash = view.flash_actions()
        if not self.flash:
            raise BcdError("no volume-up flash-mode action in the BCD: refusing to change it")
        if opts.android_only:
            # Only Android left: volume down must not start Reset My Phone
            # (resetphone.efi, a Windows tool) by mistake. It starts Android.
            key = KEYS["voldown"]
            hit = [e for k, e in actions if k == key]
            if hit and not any(e == hit[0] for _k, e, _t in self.flash):
                self.targets[hit[0]] = [ANDROID]
        elif opts.windows_key != "none":
            other = self.windows if self.default == ANDROID else ANDROID
            key = KEYS[opts.windows_key]
            hit = [e for k, e in actions if k == key]
            if hit:
                if any(e == hit[0] for _k, e, _t in self.flash):
                    raise BcdError("that key opens flash mode: not reassigning it")
                elem = hit[0]
            else:
                used = {e for _k, e in actions}
                elem = next(e for e in range(0x54000001, 0x54000010) if e not in used)
                actions.append((key, elem))
            if other:
                self.targets[elem] = [other]
        self.actions = actions

    def resolve(self, android_guid, items):
        return [android_guid if g == ANDROID else g for g in items]


def verify(before, after, plan):
    """Checks the written BCD against the plan. Returns (android guid, errors)."""
    errors = []
    cands = [g for g in after.bootshim_entries()]
    if len(cands) != 1:
        return None, [f"expected one BootShim entry, found {len(cands)}"]
    g = cands[0]
    els = after.els(g)
    if after.path(g) != BOOTSHIM_PATH:
        errors.append(f"BootShim entry path is {after.path(g)!r}")
    if after.desc(g) != ANDROID_DESC:
        errors.append(f"BootShim entry description is {after.desc(g)!r}")
    if els.get(E_DEVICE, (None, None))[1] != plan.device[1]:
        errors.append("BootShim entry device differs from the EFIESP template entry")
    for e in (E_NOINTEGRITY, E_TESTSIGN):
        v = els.get(e)
        if not v or not v[1] or v[1][0] != 1:
            errors.append(f"element {e} is not enabled")
    extra = set(els) - ANDROID_ELEMENTS
    if extra:
        errors.append("unexpected elements on the BootShim entry: " + ", ".join(sorted(extra)))
    if after.objects[g]["type"] != plan.otype:
        errors.append("BootShim entry type differs from the template")
    if after.order() != plan.resolve(g, plan.order):
        errors.append(f"display order {after.order()} != {plan.resolve(g, plan.order)}")
    want_default = g if plan.default == ANDROID else plan.default
    if after.default() != want_default:
        errors.append(f"default {after.default()} != {want_default}")
    if after.timeout() != plan.timeout:
        errors.append(f"timeout {after.timeout()} != {plan.timeout}")
    # every object except bootmgr and the replaced/new entry must be untouched
    for o, data in before.objects.items():
        if o == BOOTMGR or o in plan.remove:
            continue
        if o not in after.objects:
            errors.append(f"object {o} disappeared")
        elif after.objects[o]["elements"] != data["elements"] or after.objects[o]["type"] != data["type"]:
            errors.append(f"object {o} ({before.label(o)}) was changed")
    for o in plan.remove:
        if o != g and o in after.objects:
            errors.append(f"old BootShim entry {o} still present")
    new_objs = set(after.objects) - set(before.objects) - {g}
    if new_objs:
        errors.append("unexpected new objects: " + ", ".join(sorted(new_objs)))
    # key actions
    after_actions = {(k, e): t for k, e, t in after.actions()}
    for k, e, t in plan.flash:
        if after_actions.get((k, e)) != t:
            errors.append("the flash-mode (volume up) action changed")
    if [(k, e) for k, e, _t in after.actions()] != plan.actions:
        errors.append("key action list differs from the plan")
    for e, t in plan.targets.items():
        got = [t2 for (_k, e2), t2 in after_actions.items() if e2 == e]
        if not got or got[0] != plan.resolve(g, t):
            errors.append(f"key action 0x{e:08x} does not point to {plan.resolve(g, t)}")
    return g, errors


# ---------------------------------------------------------------------------
# writers
# ---------------------------------------------------------------------------

def hivex_available():
    try:
        import hivex  # noqa: F401
        return True
    except Exception:
        return False


def linux_writer():
    """The Linux BCD writer: "python" (regfw.py), or "hivex" when asked for and installed."""
    if os.environ.get("LUMIA_BCD_WRITER", "").lower() == "hivex" and hivex_available():
        return "hivex"
    return "python"


def write_hivex(src, dst, plan):
    """python3-hivex writer, same steps as kit/scripts/bcd-bootshim.py."""
    write_hive(src, dst, plan, engine="hivex")


def write_python(src, dst, plan):
    """Pure Python writer (regfw.py), the same calls as the hivex one."""
    write_hive(src, dst, plan, engine="python")


def write_hive(src, dst, plan, engine="python"):
    """Linux writers: the steps of kit/scripts/bcd-bootshim.py on a copy of the BCD."""
    shutil.copyfile(src, dst)
    if engine == "hivex":
        import hivex
        h = hivex.Hivex(dst, write=True)
    else:
        h = regfw.Hive(dst)
    objects = h.node_get_child(h.root(), "Objects")

    def obj(g):
        for n in h.node_children(objects):
            if h.node_name(n).lower() == g.lower():
                return n
        return None

    def set_el(g, etype, t, value):
        o = obj(g)
        el = h.node_get_child(o, "Elements") or h.node_add_child(o, "Elements")
        n = h.node_get_child(el, etype) or h.node_add_child(el, etype)
        h.node_set_value(n, {"key": "Element", "t": t, "value": value})

    for g in plan.remove + [ANDROID_FIXED]:
        o = obj(g)
        if o:
            h.node_delete_child(o)
    o = h.node_add_child(objects, ANDROID_FIXED)
    d = h.node_add_child(o, "Description")
    h.node_set_value(d, {"key": "Type", "t": REG_DWORD, "value": struct.pack("<I", plan.otype)})
    h.node_add_child(o, "Elements")
    g = ANDROID_FIXED
    set_el(g, E_DEVICE, plan.device[0], plan.device[1])
    set_el(g, E_PATH, REG_SZ, sz(BOOTSHIM_PATH))
    set_el(g, E_DESC, REG_SZ, sz(ANDROID_DESC))
    set_el(g, E_NOINTEGRITY, REG_BINARY, b"\x01")
    set_el(g, E_TESTSIGN, REG_BINARY, b"\x01")
    set_el(BOOTMGR, E_ORDER, REG_MULTI_SZ, multi_sz(plan.resolve(g, plan.order)))
    default = g if plan.default == ANDROID else plan.default
    set_el(BOOTMGR, E_DEFAULT, REG_SZ, sz(default))
    set_el(BOOTMGR, E_TIMEOUT, REG_BINARY, struct.pack("<Q", plan.timeout))
    if plan.targets:
        for e, t in plan.targets.items():
            set_el(BOOTMGR, f"{e:08x}", REG_MULTI_SZ, multi_sz(plan.resolve(g, t)))
        set_el(BOOTMGR, E_CUSTOM_LIST, REG_BINARY, pack_actions(plan.actions))
        set_el(BOOTMGR, E_CUSTOM_FIRST, REG_BINARY, b"\x01")
    h.commit(dst)


GUID_RE = re.compile(r"\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}")


def bcdedit_commands(plan, android_guid):
    """bcdedit arguments after /copy, for an entry already copied to android_guid."""
    g = android_guid
    cmds = [
        ["/set", g, "path", BOOTSHIM_PATH],
        ["/set", g, "description", ANDROID_DESC],
        ["/set", g, "nointegritychecks", "on"],
        ["/set", g, "testsigning", "on"],
        ["/displayorder"] + plan.resolve(g, plan.order),
        ["/default", g if plan.default == ANDROID else plan.default],
        ["/timeout", str(plan.timeout)],
    ]
    if plan.targets:
        ints = []
        for k, e in plan.actions:
            ints += [f"0x{(0x00010000 << 32) | k:x}", f"0x{e:x}"]
        cmds.append(["/set", "{bootmgr}", "customactions"] + ints)
        for e, t in plan.targets.items():
            cmds.append(["/set", "{bootmgr}", f"custom:{e:08x}"] + plan.resolve(g, t))
        cmds.append(["/set", "{bootmgr}", "processcustomactionsfirst", "yes"])
    return cmds


def write_bcdedit(src, dst, plan, run=None, log=None):
    """Windows writer: Microsoft's bcdedit on a local copy of the phone's BCD."""
    shutil.copyfile(src, dst)
    run = run or _run_bcdedit

    def bcd(args):
        rc, out = run(["/store", dst] + args)
        if log:
            log("bcdedit " + " ".join(args) + "\n" + out)
        if rc != 0:
            raise BcdError("bcdedit " + " ".join(args) + " failed:\n" + out.strip())
        return out

    for g in plan.remove:
        bcd(["/delete", g, "/cleanup"])
    out = bcd(["/copy", plan.template, "/d", ANDROID_DESC])
    found = [m.lower() for m in GUID_RE.findall(out) if m.lower() != plan.template.lower()]
    if not found:
        raise BcdError("bcdedit /copy did not print the new entry id:\n" + out)
    g = found[-1]
    # drop elements the template has and the BootShim entry must not carry
    view = BcdView(dst)
    for e in sorted(set(view.els(g)) - ANDROID_ELEMENTS):
        bcd(["/deletevalue", g, f"custom:{e}"])
    for c in bcdedit_commands(plan, g):
        bcd(c)
    return g


def _run_bcdedit(args):
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "bcdedit.exe")
    p = subprocess.run([exe] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("mbcs" if os.name == "nt" else "utf-8", "replace")
