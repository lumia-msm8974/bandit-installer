"""Minimal writer for Windows registry hives (regf), in pure Python.

Writes the phone's BCD on Linux without python3-hivex. It offers the few
hivex calls bcd.py needs (node_get_child, node_children, node_name,
node_add_child, node_delete_child, node_set_value, commit) and changes the
hive the way hivex does: cells already in the file stay where they are,
new cells go into a bin appended at the end, removed cells are marked free,
and the header gets equal sequence numbers and a new checksum. The result
is read back with regf.py and checked against the plan before it is copied
to the phone.

Format reference: the regf notes of the libyal project (Joachim Metz) and
hivex (Richard W.M. Jones, Red Hat), whose write logic this follows.
"""
import struct
import time

HBIN = 0x1000          # hive bins start after the 4 KiB base block
NONE = 0xFFFFFFFF
NK_COMP_NAME = 0x20    # key name stored as Latin-1
VK_COMP_NAME = 0x0001  # value name stored as Latin-1
MAX_INLINE = 16344     # larger values would need "db" big data records


class HiveError(Exception):
    pass


def _filetime():
    return int((time.time() + 11644473600) * 10_000_000)


def _lh_hash(name):
    h = 0
    for ch in name.upper():
        h = (h * 37 + ord(ch)) & 0xFFFFFFFF
    return h


def _name_bytes(name):
    try:
        return name.encode("latin-1"), True
    except UnicodeEncodeError:
        return name.encode("utf-16le"), False


class Hive:
    """A hive opened for writing. Node handles are cell offsets, as in hivex."""

    def __init__(self, path, write=True):
        with open(path, "rb") as f:
            d = bytearray(f.read())
        if len(d) < HBIN + 0x20 or d[:4] != b"regf":
            raise HiveError("not a registry hive")
        seq1, seq2 = struct.unpack_from("<II", d, 4)
        if seq1 != seq2:
            raise HiveError("the hive is dirty (sequence numbers differ): it needs its log files")
        size = struct.unpack_from("<I", d, 0x28)[0]
        if size % 0x1000 or HBIN + size > len(d):
            raise HiveError("bad hive bins size")
        self.d = d[:HBIN + size]
        self.minor = struct.unpack_from("<I", d, 0x18)[0]
        self._root = struct.unpack_from("<I", d, 0x24)[0]
        self._free = None       # (cell offset, size) of the free tail of our new bin
        self.list_sig = self._pick_list_sig()

    # raw access ------------------------------------------------------------
    def _pos(self, off):
        if off in (0, NONE) or HBIN + off + 4 > len(self.d):
            raise HiveError(f"bad cell offset 0x{off:x}")
        return HBIN + off

    def _size(self, off):
        size = struct.unpack_from("<i", self.d, self._pos(off))[0]
        if size >= 0:
            raise HiveError(f"cell 0x{off:x} is free")
        return -size

    def _body(self, off):
        p = self._pos(off)
        return memoryview(self.d)[p + 4:p + self._size(off)]

    def _get(self, fmt, off, at):
        return struct.unpack_from(fmt, self.d, self._pos(off) + 4 + at)[0]

    def _put(self, fmt, off, at, value):
        struct.pack_into(fmt, self.d, self._pos(off) + 4 + at, value)

    # cell allocation -------------------------------------------------------
    def _new_bin(self, need):
        size = (need + 0x20 + 0xFFF) & ~0xFFF
        start = len(self.d)
        hdr = bytearray(0x20)
        hdr[0:4] = b"hbin"
        struct.pack_into("<III", hdr, 4, start - HBIN, size, 0)
        struct.pack_into("<Q", hdr, 0x14, _filetime())
        self.d += hdr + bytes(size - 0x20)
        off = start + 0x20 - HBIN
        struct.pack_into("<i", self.d, HBIN + off, size - 0x20)
        self._free = (off, size - 0x20)

    def _alloc(self, body):
        need = (len(body) + 4 + 7) & ~7
        if self._free is None or self._free[1] < need:
            self._new_bin(need)
        off, avail = self._free
        if avail - need < 8:
            need = avail
        p = HBIN + off
        struct.pack_into("<i", self.d, p, -need)
        self.d[p + 4:p + need] = bytes(body) + bytes(need - 4 - len(body))
        if avail > need:
            struct.pack_into("<i", self.d, p + need, avail - need)
            self._free = (off + need, avail - need)
        else:
            self._free = None
        return off

    def _release(self, off):
        if off in (0, NONE):
            return
        p = self._pos(off)
        size = struct.unpack_from("<i", self.d, p)[0]
        if size < 0:
            struct.pack_into("<i", self.d, p, -size)

    # keys ------------------------------------------------------------------
    def _check_nk(self, off):
        if bytes(self._body(off)[:2]) != b"nk":
            raise HiveError(f"expected nk at 0x{off:x}")

    def root(self):
        return self._root

    def node_name(self, node):
        self._check_nk(node)
        b = self._body(node)
        flags, nlen = struct.unpack_from("<H", b, 2)[0], struct.unpack_from("<H", b, 72)[0]
        raw = bytes(b[76:76 + nlen])
        return raw.decode("latin-1") if flags & NK_COMP_NAME else raw.decode("utf-16le")

    def _list(self, off, depth=0):
        if depth > 4:
            raise HiveError("subkey index too deep")
        b = self._body(off)
        sig, n = bytes(b[:2]), struct.unpack_from("<H", b, 2)[0]
        if sig in (b"lf", b"lh"):
            return [struct.unpack_from("<I", b, 4 + 8 * i)[0] for i in range(n)], [off]
        if sig == b"li":
            return [struct.unpack_from("<I", b, 4 + 4 * i)[0] for i in range(n)], [off]
        if sig == b"ri":
            items, cells = [], [off]
            for i in range(n):
                sub_items, sub_cells = self._list(struct.unpack_from("<I", b, 4 + 4 * i)[0], depth + 1)
                items += sub_items
                cells += sub_cells
            return items, cells
        raise HiveError(f"unknown subkey list {sig!r}")

    def node_children(self, node):
        self._check_nk(node)
        n, lst = self._get("<I", node, 20), self._get("<I", node, 28)
        if n == 0 or lst in (0, NONE):
            return []
        return self._list(lst)[0]

    def node_get_child(self, node, name):
        low = name.lower()
        for c in self.node_children(node):
            if self.node_name(c).lower() == low:
                return c
        return None

    def _pick_list_sig(self):
        lst = self._get("<I", self._root, 28)
        if self._get("<I", self._root, 20) and lst not in (0, NONE):
            sig = bytes(self._body(lst)[:2])
            if sig in (b"lf", b"lh"):
                return sig
        return b"lh" if self.minor >= 5 else b"lf"

    def _write_list(self, node, children):
        """New subkey list for node, sorted as Windows expects; frees the old one."""
        old = self._get("<I", node, 28)
        if self._get("<I", node, 20) and old not in (0, NONE):
            for cell in self._list(old)[1]:
                self._release(cell)
        named = sorted(((self.node_name(c), c) for c in children), key=lambda t: t[0].upper())
        if not named:
            self._put("<I", node, 20, 0)
            self._put("<I", node, 28, NONE)
            return
        body = bytearray(self.list_sig + struct.pack("<H", len(named)))
        for name, c in named:
            if self.list_sig == b"lh":
                hint = struct.pack("<I", _lh_hash(name))
            else:
                hint = (name.encode("latin-1", "replace") + b"\0\0\0\0")[:4]
            body += struct.pack("<I", c) + hint
        self._put("<I", node, 20, len(named))
        self._put("<I", node, 28, self._alloc(body))

    def node_add_child(self, parent, name):
        self._check_nk(parent)
        if self.node_get_child(parent, name) is not None:
            raise HiveError(f"key {name} already exists")
        raw, comp = _name_bytes(name)
        sk = self._get("<I", parent, 44)
        body = (b"nk" + struct.pack("<H", NK_COMP_NAME if comp else 0) + struct.pack("<Q", _filetime())
                + struct.pack("<IIIIIIIIII", 0, parent, 0, 0, NONE, NONE, 0, NONE, sk, NONE)
                + struct.pack("<IIIII", 0, 0, 0, 0, 0) + struct.pack("<HH", len(raw), 0) + raw)
        child = self._alloc(body)
        if sk not in (0, NONE):
            self._put("<I", sk, 12, self._get("<I", sk, 12) + 1)
        self._write_list(parent, self.node_children(parent) + [child])
        nlen = len(name) * 2
        if self._get("<I", parent, 52) < nlen:
            self._put("<I", parent, 52, nlen)
        self._put("<Q", parent, 4, _filetime())
        return child

    def _free_values(self, node):
        n, lst = self._get("<I", node, 36), self._get("<I", node, 40)
        if n and lst not in (0, NONE):
            for i in range(n):
                self._free_vk(self._get("<I", lst, 4 * i))
            self._release(lst)

    def _free_vk(self, vk):
        size, doff = self._get("<I", vk, 4), self._get("<I", vk, 8)
        if not size & 0x80000000 and size:
            if size > MAX_INLINE and bytes(self._body(doff)[:2]) == b"db":
                nseg, segs = struct.unpack_from("<HI", self._body(doff), 2)
                for i in range(nseg):
                    self._release(self._get("<I", segs, 4 * i))
                self._release(segs)
            self._release(doff)
        self._release(vk)

    def _unref_sk(self, sk):
        if sk in (0, NONE):
            return
        refs = self._get("<I", sk, 12) - 1
        self._put("<I", sk, 12, max(refs, 0))
        if refs <= 0:
            flink, blink = self._get("<I", sk, 4), self._get("<I", sk, 8)
            if flink != sk:
                self._put("<I", blink, 4, flink)
                self._put("<I", flink, 8, blink)
                self._release(sk)

    def _delete_tree(self, node):
        for c in self.node_children(node):
            self._delete_tree(c)
        old = self._get("<I", node, 28)
        if self._get("<I", node, 20) and old not in (0, NONE):
            for cell in self._list(old)[1]:
                self._release(cell)
        self._free_values(node)
        self._unref_sk(self._get("<I", node, 44))
        if self._get("<H", node, 74):
            self._release(self._get("<I", node, 48))
        self._release(node)

    def node_delete_child(self, node):
        """Deletes node and everything under it (hivex semantics)."""
        self._check_nk(node)
        if node == self._root:
            raise HiveError("cannot delete the root key")
        parent = self._get("<I", node, 16)
        siblings = [c for c in self.node_children(parent) if c != node]
        self._delete_tree(node)
        self._write_list(parent, siblings)
        self._put("<Q", parent, 4, _filetime())

    # values ----------------------------------------------------------------
    def _vk_name(self, vk):
        b = self._body(vk)
        if bytes(b[:2]) != b"vk":
            raise HiveError(f"expected vk at 0x{vk:x}")
        nlen, flags = struct.unpack_from("<H", b, 2)[0], struct.unpack_from("<H", b, 16)[0]
        raw = bytes(b[20:20 + nlen])
        return raw.decode("latin-1") if flags & VK_COMP_NAME else raw.decode("utf-16le")

    def node_set_value(self, node, val):
        """val = {"key": name, "t": type, "value": bytes}; replaces a value of that name."""
        self._check_nk(node)
        name, vtype, data = val["key"], val["t"], bytes(val["value"])
        if len(data) > MAX_INLINE:
            raise HiveError("value too large for this writer")
        raw, comp = _name_bytes(name)
        if len(data) <= 4:
            size, doff = len(data) | 0x80000000, struct.unpack("<I", data + bytes(4 - len(data)))[0]
        else:
            size, doff = len(data), self._alloc(data)
        vk = self._alloc(b"vk" + struct.pack("<HIIIHH", len(raw), size, doff, vtype,
                                             VK_COMP_NAME if comp else 0, 0) + raw)
        n, lst = self._get("<I", node, 36), self._get("<I", node, 40)
        items = [self._get("<I", lst, 4 * i) for i in range(n)] if n and lst not in (0, NONE) else []
        out, replaced = [], False
        for v in items:
            if not replaced and self._vk_name(v).lower() == name.lower():
                self._free_vk(v)
                out.append(vk)
                replaced = True
            else:
                out.append(v)
        if not replaced:
            out.append(vk)
        if items:
            self._release(lst)
        self._put("<I", node, 36, len(out))
        self._put("<I", node, 40, self._alloc(b"".join(struct.pack("<I", v) for v in out)))
        if self._get("<I", node, 60) < len(name) * 2:
            self._put("<I", node, 60, len(name) * 2)
        if self._get("<I", node, 64) < len(data):
            self._put("<I", node, 64, len(data))
        self._put("<Q", node, 4, _filetime())

    # save ------------------------------------------------------------------
    def commit(self, path):
        d = self.d
        seq = struct.unpack_from("<I", d, 4)[0] + 1
        struct.pack_into("<IIQ", d, 4, seq, seq, _filetime())
        struct.pack_into("<I", d, 0x28, len(d) - HBIN)
        csum = 0
        for i in range(0, 0x1FC, 4):
            csum ^= struct.unpack_from("<I", d, i)[0]
        if csum == 0:
            csum = 1
        elif csum == 0xFFFFFFFF:
            csum = 0xFFFFFFFE
        struct.pack_into("<I", d, 0x1FC, csum)
        with open(path, "wb") as f:
            f.write(d)


def check(path):
    """Structural check of a hive: header, bins, cell chain. Returns a list of problems."""
    with open(path, "rb") as f:
        d = f.read()
    errs = []
    if d[:4] != b"regf":
        return ["no regf signature"]
    seq1, seq2 = struct.unpack_from("<II", d, 4)
    if seq1 != seq2:
        errs.append("sequence numbers differ")
    csum = 0
    for i in range(0, 0x1FC, 4):
        csum ^= struct.unpack_from("<I", d, i)[0]
    want = struct.unpack_from("<I", d, 0x1FC)[0]
    if csum not in (want, 0 if want == 1 else -1, 0xFFFFFFFF if want == 0xFFFFFFFE else -1):
        errs.append("bad header checksum")
    size = struct.unpack_from("<I", d, 0x28)[0]
    if HBIN + size != len(d):
        errs.append(f"bins size {size} does not match the file ({len(d) - HBIN})")
    pos = HBIN
    while pos < HBIN + size and not errs:
        if d[pos:pos + 4] != b"hbin":
            errs.append(f"no hbin at 0x{pos:x}")
            break
        rel, bsize = struct.unpack_from("<II", d, pos + 4)
        if rel != pos - HBIN or bsize % 0x1000 or bsize == 0:
            errs.append(f"bad hbin header at 0x{pos:x}")
            break
        c = pos + 0x20
        while c < pos + bsize:
            cs = abs(struct.unpack_from("<i", d, c)[0])
            if cs < 8 or cs % 8 or c + cs > pos + bsize:
                errs.append(f"bad cell at 0x{c - HBIN:x} in hbin 0x{pos:x}")
                break
            c += cs
        pos += bsize
    return errs
