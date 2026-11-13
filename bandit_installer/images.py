"""Checks on the files the installer writes or boots.

- lk1st ELF: the checks BootShim elf-loader-pre3 makes (src/EFIApp.c).
- Android boot image: header v0-v2, so the user sees what is booted.
- Qualcomm PIL firmware: .mbn split into .mdt + .bNN, same output as
  KorelisLabs' tools/pil-split.py.
"""
import hashlib
import os
import struct


def sha256_file(path, progress=None):
    h = hashlib.sha256()
    done = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(4 << 20)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress:
                progress(done)
    return h.hexdigest()


class ElfInfo:
    def __init__(self):
        self.ok = True
        self.errors = []
        self.entry = None
        self.load = None  # (paddr, filesz, memsz)


def check_bootshim_elf(path):
    info = ElfInfo()

    def fail(msg):
        info.ok = False
        info.errors.append(msg)

    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"\x7fELF":
        fail("no ELF magic (flat MBN?)")
        return info
    if data[4] != 1:
        fail("not ELF32")
    e_type, e_machine = struct.unpack_from("<HH", data, 16)
    e_entry, e_phoff = struct.unpack_from("<II", data, 24)
    e_phentsize, e_phnum = struct.unpack_from("<HH", data, 42)
    info.entry = e_entry
    if e_machine != 40:
        fail(f"e_machine {e_machine} is not ARM")
    if e_type != 2:
        fail(f"e_type {e_type} is not ET_EXEC")
    if e_entry == 0:
        fail("entry point 0")
    if e_phnum < 1:
        fail("no program header")
    for i in range(e_phnum):
        p_type, p_off, p_vaddr, p_paddr, p_filesz, p_memsz, _fl, _al = \
            struct.unpack_from("<8I", data, e_phoff + i * e_phentsize)
        if p_type == 1 and p_paddr == p_vaddr == e_entry:
            if p_off + p_filesz > len(data):
                fail("segment runs past the end of the file")
            info.load = (p_paddr, p_filesz, p_memsz)
            break
    if info.load is None:
        fail("no PT_LOAD with paddr == vaddr == entry")
    return info


class BootImage:
    def __init__(self, path):
        with open(path, "rb") as f:
            h = f.read(1660)
        if h[:8] != b"ANDROID!":
            raise ValueError("not an Android boot image (no ANDROID! magic)")
        (self.kernel_size, self.kernel_addr, self.ramdisk_size, self.ramdisk_addr,
         self.second_size, self.second_addr, self.tags_addr, self.page_size,
         self.header_version, self.os_version) = struct.unpack_from("<10I", h, 8)
        self.name = h[48:64].split(b"\0", 1)[0].decode("ascii", "replace")
        cmd = h[64:64 + 512] + h[608:608 + 1024]
        self.cmdline = cmd.split(b"\0", 1)[0].decode("ascii", "replace")
        self.size = os.path.getsize(path)
        if self.page_size not in (2048, 4096, 8192, 16384):
            raise ValueError(f"unusual page size {self.page_size}")
        if self.kernel_size == 0:
            raise ValueError("boot image with an empty kernel")

    def lines(self):
        return [
            f"kernel   {self.kernel_size:>10} B  @0x{self.kernel_addr:08x}",
            f"ramdisk  {self.ramdisk_size:>10} B  @0x{self.ramdisk_addr:08x}",
            f"tags     @0x{self.tags_addr:08x}   page {self.page_size}   "
            + (f"header v{self.header_version}" if self.header_version <= 4
               else f"qcom dt {self.header_version} B"),
            f"cmdline  {self.cmdline}",
        ]


def pil_split(path, outdir, base):
    """Writes base.mdt (b00 + b01) and base.bNN; returns the list of files.

    An image with a single segment and no hash segment (the modem's MBA,
    qcdsp1v28974.mbn) gives only base.b00, that segment: the vendor's
    extract-local.sh rebuilds base.mdt from the ELF headers, as the kernel
    loads it.
    """
    with open(path, "rb") as f:
        d = f.read()
    if d[:4] != b"\x7fELF":
        raise ValueError(f"{os.path.basename(path)}: not an ELF")
    e_phoff = struct.unpack_from("<I", d, 0x1C)[0]
    e_phentsize, e_phnum = struct.unpack_from("<HH", d, 0x2A)
    if e_phnum == 0:
        raise ValueError("no program headers")
    os.makedirs(outdir, exist_ok=True)
    if e_phnum == 1:
        _t, off, _v, _p, filesz = struct.unpack_from("<5I", d, e_phoff)
        fn = os.path.join(outdir, f"{base}.b00")
        with open(fn, "wb") as f:
            f.write(d[off:off + filesz])
        return [fn]
    parts = []
    for i in range(e_phnum):
        o = e_phoff + i * e_phentsize
        _t, off, _v, _p, filesz, _m, _f, _a = struct.unpack_from("<8I", d, o)
        parts.append(d[off:off + filesz] if filesz else b"")
    files = []
    for i, blob in enumerate(parts):
        fn = os.path.join(outdir, f"{base}.b{i:02d}")
        with open(fn, "wb") as f:
            f.write(blob)
        files.append(fn)
    mdt = os.path.join(outdir, f"{base}.mdt")
    with open(mdt, "wb") as f:
        f.write(parts[0] + parts[1])
    return [mdt] + files


# Windows firmware name -> Android PIL name on msm8974 (to be confirmed with the kernel)
PIL_NAMES = [
    ("qcadsp8974", "adsp"),
    ("qcwcnss8974", "wcnss"),
    ("qcvss8974", "venus"),
    ("qcdsp1v28974", "mba"),
    ("qcdsp28974", "modem"),
]
