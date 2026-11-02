#!/usr/bin/env python3
"""
Python reimplementation of QNX dumpifs (clean-room, from openQNX sources).

Behavior mirrors trunk/utils/m/mkxfs/dumpifs/dumpifs.c:

  * Finds the startup header (signature 0x00FF7EEB scanned from offset 0,
    with the "zero" field validation at spos+52..+64), or, failing that,
    accepts a raw imagefs at file offset 0 (spos = -1).
  * If the startup header's compression field (flags1 & 0x0C) is non-zero,
    the image is transparently decompressed into a virtual buffer:
        raw[0 : spos+startup_size]  +  decompressed(image stream)
    All subsequent offsets (ipos, file offsets, trailers) index into it.
  * Walks the image header (signature "imagefs", 88-byte fixed part plus
    4-aligned mountpoint) and the image directory (dirents), displaying
    them in dumpifs' exact output formats, extracting files, computing
    MD5 sums and printing both checksums.
  * Startup trailer checksum: -sum(u32 words of [spos, spos+startup_size-4))
    Image trailer checksum (inside the decompressed stream):
    -sum(u32 words of [ipos, ipos+image_size-4))
    File-end trailer (compressed images only):
    -sum(u32 words of compressed stream, incl. pad to 4)

Intentional deviation from the reference tool: when extracting, this
implementation creates missing parent directories (the reference dumpifs
does not, and simply fails with "Unable to open ...").

Usage mirrors the reference getopt string "f:d:mvxbu:z":

  dumpifs [options] image [basename-filter ...]
    -f path   Extract only this file (repeatable; implies extraction)
    -d dir    Extract into dir (created if necessary)
    -m        Display MD5 checksums of files
    -v        Verbose (repeat: -vv also shows script/ELF details)
    -x        Extract files
    -b        Extract using basename only
    -u file   Write the uncompressed image to file
    -z        Disable the zero check while searching for the startup header
"""

import hashlib
import os
import struct
import sys
import zlib

# ---------------------------------------------------------------------------
# Constants (sys/startup.h, sys/image.h as documented + pinned from mkifs)
# ---------------------------------------------------------------------------

STARTUP_SIG = 0x00FF7EEB
IMAGE_SIG = b'imagefs'
STARTUP_HDR_SIZE = 256          # sizeof(struct startup_header) == header_size
IMAGE_HDR_FIXED = 88            # offsetof(struct image_header, mountpoint)

# startup_header.flags1 bits (pinned from parse_file_attr.c:523 /
# mk_image_fsys.c:2183: compressed=3 (UCL) -> flags1 |= 3 << 2 == 0x0C)
STARTUP_HDR_FLAGS1_VIRTUAL = 0x01
STARTUP_HDR_FLAGS1_BIGENDIAN = 0x02
STARTUP_HDR_FLAGS1_COMPRESS_SHIFT = 2
STARTUP_HDR_FLAGS1_COMPRESS_MASK = 0x0C
COMPRESS_NONE = 0x00
COMPRESS_ZLIB = 0x04
COMPRESS_LZO = 0x08
COMPRESS_UCL = 0x0C

# image_header.flags
IMAGE_FLAGS_BIGENDIAN = 0x01
IMAGE_FLAGS_READONLY = 0x02
IMAGE_FLAGS_INO_BITS = 0x04

# File mode bits (QNX/SVR4 values, same numeric values as Linux)
S_IFMT = 0o170000
S_IFIFO = 0o010000
S_IFCHR = 0o020000
S_IFDIR = 0o040000
S_IFBLK = 0o060000
S_IFREG = 0o100000
S_IFLNK = 0o120000
S_IFNAM = 0o140000

# script command types/flags (sys/image.h)
SCRIPT_FLAGS_EXTSCHED = 0x01
SCRIPT_FLAGS_SESSION = 0x02
SCRIPT_FLAGS_SCHED_SET = 0x04
SCRIPT_FLAGS_CPU_SET = 0x08
SCRIPT_FLAGS_BACKGROUND = 0x20
SCRIPT_FLAGS_KDEBUG = 0x40

SCRIPT_POLICY_NOCHANGE = 0
SCRIPT_POLICY_FIFO = 1
SCRIPT_POLICY_RR = 2
SCRIPT_POLICY_OTHER = 3

SCRIPT_TYPE_EXTERNAL = 0
SCRIPT_TYPE_WAITFOR = 1
SCRIPT_TYPE_REOPEN = 2
SCRIPT_TYPE_DISPLAY_MSG = 3
SCRIPT_TYPE_PROCMGR_SYMLINK = 4
SCRIPT_TYPE_EXTSCHED_APS = 5

SCRIPT_SCHED_EXT_NONE = 0
SCRIPT_SCHED_EXT_APS = 1

SCRIPT_APS_SYSTEM_PARTITION_ID = 0
SCRIPT_APS_SYSTEM_PARTITION_NAME = "System"

FLAG_EXTRACT = 0x00000001
FLAG_DISPLAY = 0x00000002
FLAG_BASENAME = 0x00000004
FLAG_MD5 = 0x00000008

# Host endianness: CROSSENDIAN(big) on a little-endian host == big.
HOST_LE = sys.byteorder == 'little'


def crossendian(flag: bool) -> bool:
    return flag if HOST_LE else not flag


def endian_ret16(x: int) -> int:
    return ((x >> 8) & 0xFF) | ((x & 0xFF) << 8)


def endian_ret32(x: int) -> int:
    return (((x >> 24) & 0xFF) | ((x >> 8) & 0xFF00) |
            ((x & 0xFF00) << 8) | ((x & 0xFF) << 24))


# ---------------------------------------------------------------------------
# Formatting helpers matching C printf semantics
# ---------------------------------------------------------------------------

def hexhash(v: int) -> str:
    """%#x / %#lx: '0' for zero, else 0x..."""
    return '0' if v == 0 else format(v, '#x')


def octalhash(v: int) -> str:
    """%#lo: '0' for zero, else 0NNN."""
    return '0' if v == 0 else format(v, '#o')


def c_basename(path: str) -> str:
    """POSIX basename() as used by dumpifs' check()."""
    p = path.rstrip('/')
    if not p:
        return '/'
    i = p.rfind('/')
    return p[i + 1:]


# ---------------------------------------------------------------------------
# Error reporting (mirrors dumpifs error(): level 0 exits)
# ---------------------------------------------------------------------------

PROGNAME = 'dumpifs'


def error(level: int, msg: str) -> None:
    sys.stderr.write(f"{PROGNAME}: {msg}\n")
    sys.stderr.flush()
    if level == 0:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

class StartupHeader:
    """struct startup_header (256 bytes), QNX 8.0 docs + empirical layout."""

    __slots__ = ('offset', 'signature', 'version', 'flags1', 'flags2',
                 'header_size', 'machine', 'startup_vaddr', 'paddr_bias',
                 'image_paddr', 'ram_paddr', 'ram_size', 'startup_size',
                 'stored_size', 'imagefs_paddr', 'imagefs_size',
                 'preboot_size')

    @classmethod
    def parse(cls, buf: bytes, spos: int, be: bool = False) -> 'StartupHeader':
        u32 = lambda o: struct.unpack_from('>I' if be else '<I', buf, spos + o)[0]
        u16 = lambda o: struct.unpack_from('>H' if be else '<H', buf, spos + o)[0]
        self = cls()
        self.offset = spos
        self.signature = u32(0)
        self.version = u16(4)
        self.flags1 = buf[spos + 6]
        self.flags2 = buf[spos + 7]
        self.header_size = u16(8)
        self.machine = u16(10)
        self.startup_vaddr = u32(12)
        self.paddr_bias = u32(16)
        self.image_paddr = u32(20)
        self.ram_paddr = u32(24)
        self.ram_size = u32(28)
        self.startup_size = u32(32)
        self.stored_size = u32(36)
        self.imagefs_paddr = u32(40)
        self.imagefs_size = u32(44)
        self.preboot_size = u16(48)
        return self

    @property
    def compression(self) -> int:
        return self.flags1 & STARTUP_HDR_FLAGS1_COMPRESS_MASK


def zero_ok(buf: bytes, spos: int) -> bool:
    """int zero_ok(): zero[0..2] (u32) at spos+52 must all be zero."""
    return (struct.unpack_from('<I', buf, spos + 52)[0] == 0 and
            struct.unpack_from('<I', buf, spos + 56)[0] == 0 and
            struct.unpack_from('<I', buf, spos + 60)[0] == 0)


class ImageHeader:
    """struct image_header: 88-byte fixed part + 4-aligned mountpoint."""

    __slots__ = ('offset', 'signature', 'flags', 'image_size', 'hdr_dir_size',
                 'dir_offset', 'boot_ino', 'script_ino', 'chain_paddr',
                 'spare', 'mountflags', 'mountpoint')

    @classmethod
    def parse(cls, buf: bytes, ipos: int, be: bool = False) -> 'ImageHeader':
        u32 = lambda o: struct.unpack_from('>I' if be else '<I', buf, ipos + o)[0]
        self = cls()
        self.offset = ipos
        self.signature = buf[ipos:ipos + 7]
        self.flags = buf[ipos + 7]
        self.image_size = u32(8)
        self.hdr_dir_size = u32(12)
        self.dir_offset = u32(16)
        self.boot_ino = [u32(20 + 4 * i) for i in range(4)]
        self.script_ino = u32(36)
        self.chain_paddr = u32(40)
        self.spare = [u32(44 + 4 * i) for i in range(10)]
        self.mountflags = u32(84)
        # mountpoint occupies [88, dir_offset), NUL terminated
        raw = buf[ipos + 88:ipos + self.dir_offset]
        end = raw.find(b'\x00')
        if end != -1:
            raw = raw[:end]
        self.mountpoint = raw.decode('latin-1')
        return self


class DirEntry:
    """union image_dirent (attr + variant fields + path)."""

    __slots__ = ('dpos', 'size', 'extattr_offset', 'ino', 'mode', 'gid',
                 'uid', 'mtime', 'offset', 'filesize', 'dev', 'rdev',
                 'sym_offset', 'sym_size', 'path', 'target')

    @classmethod
    def parse(cls, buf: bytes, dpos: int, be: bool = False) -> 'DirEntry':
        u16 = lambda o: struct.unpack_from('>H' if be else '<H', buf, dpos + o)[0]
        u32 = lambda o: struct.unpack_from('>I' if be else '<I', buf, dpos + o)[0]
        self = cls()
        self.dpos = dpos
        self.size = u16(0)
        self.extattr_offset = u16(2)
        self.ino = u32(4)
        self.mode = u32(8)
        self.gid = u32(12)
        self.uid = u32(16)
        self.mtime = u32(20)
        self.offset = self.filesize = self.dev = self.rdev = 0
        self.sym_offset = self.sym_size = 0
        self.target = ''
        typ = self.mode & S_IFMT
        if typ == S_IFREG:
            self.offset = u32(24)
            self.filesize = u32(28)
            pstart = dpos + 32
        elif typ == S_IFLNK:
            self.sym_offset = u16(24)
            self.sym_size = u16(26)
            pstart = dpos + 28
        elif typ in (S_IFCHR, S_IFBLK, S_IFIFO, S_IFNAM):
            self.dev = u32(24)
            self.rdev = u32(28)
            pstart = dpos + 32
        else:
            pstart = dpos + 24
        end = buf.find(b'\x00', pstart, dpos + self.size)
        if end == -1:
            end = dpos + self.size
        self.path = buf[pstart:end].decode('latin-1')
        if typ == S_IFLNK:
            tstart = pstart + self.sym_offset
            tend = buf.find(b'\x00', tstart, dpos + self.size)
            if tend == -1:
                tend = dpos + self.size
            self.target = buf[tstart:tend].decode('latin-1')
        return self


# ---------------------------------------------------------------------------
# Decompression (dispatch on startup header flags1 & COMPRESS_MASK)
# ---------------------------------------------------------------------------

def _ucl_block_stream(data: bytes, start: int, end: int) -> bytes:
    from ucl_nrv2b import nrv2b_decompress_8
    out = bytearray()
    pos = start
    while pos + 2 <= end:
        blen = (data[pos] << 8) | data[pos + 1]
        pos += 2
        if blen == 0:
            break
        if pos + blen > end:
            raise ValueError("truncated UCL block stream")
        out += nrv2b_decompress_8(data[pos:pos + blen])
        pos += blen
    return bytes(out)


def _lzo_block_stream(data: bytes, start: int, end: int) -> bytes:
    from lzo1x import lzo1x_decompress
    out = bytearray()
    pos = start
    while pos + 2 <= end:
        blen = (data[pos] << 8) | data[pos + 1]
        pos += 2
        if blen == 0:
            break
        if pos + blen > end:
            raise ValueError("truncated LZO block stream")
        out += lzo1x_decompress(data[pos:pos + blen])
        pos += blen
    return bytes(out)


def _zlib_stream(data: bytes, start: int) -> bytes:
    # gzopen accepts both gzip and zlib wrapped streams.
    d = zlib.decompressobj(47)
    return d.decompress(data[start:]) + d.flush()


def decompress_image(raw: bytes, spos: int, startup_size: int,
                     method: int) -> bytes:
    """Build the virtual image buffer for a compressed image.

    Non-compressed part is raw[0, spos+startup_size); the image stream
    that follows is decompressed per the startup header compression field.
    """
    head = raw[:spos + startup_size]
    tail = raw[spos + startup_size:]
    if method == COMPRESS_ZLIB:
        body = _zlib_stream(tail, 0)
    elif method == COMPRESS_LZO:
        body = _lzo_block_stream(tail, 0, len(tail))
    elif method == COMPRESS_UCL:
        body = _ucl_block_stream(tail, 0, len(tail))
    else:
        raise ValueError(f"Unsupported compression type {method >> 2}.")
    return head + body


# ---------------------------------------------------------------------------
# Startup header / image header search (mirrors process())
# ---------------------------------------------------------------------------

def find_startup(raw: bytes, zero_check: bool, verbose: int):
    """Scan for a valid startup header; returns (spos, shdr) or (-1, None).

    Mirrors the reference loop: find() signature EB 7E FF 00 (LE), read the
    256-byte header, validate zero[0..2]; on failure resume the scan from
    spos+256 (the reference consumed that much via fread).  If the LE
    signature is absent the byte-swapped signature is searched as well.
    """
    sig_le = struct.pack('<I', STARTUP_SIG)
    sig_be = struct.pack('>I', STARTUP_SIG)
    swapped = False
    pos = 0
    while True:
        sig = sig_be if swapped else sig_le
        spos = raw.find(sig, pos)
        if spos == -1:
            if not swapped:
                swapped = True
                pos = 0
                continue
            error(1, "Unable to find startup header")
            return -1, None
        if not zero_ok(raw, spos):
            if not zero_check:
                if verbose:
                    print("Warning: Non zero data in zero fields ignored")
                break
            pos = spos + STARTUP_HDR_SIZE
            continue
        break
    shdr = StartupHeader.parse(raw, spos, be=crossendian(bool(
        raw[spos + 6] & STARTUP_HDR_FLAGS1_BIGENDIAN)))
    return spos, shdr


def process(image: str, raw: bytes, flags: int, verbose: int,
            check_files, extract_files, ucompress_file: str,
            zero_check: bool) -> None:
    # -- locate the image header -------------------------------------------
    if raw[:7] == IMAGE_SIG:
        # Raw uncompressed imagefs at file offset 0: no startup header.
        ipos = 0
        spos = -1
        shdr = None
        buf = raw
    else:
        spos, shdr = find_startup(raw, zero_check, verbose)
        if spos == -1:
            return
        if shdr.compression != 0:
            try:
                buf = decompress_image(raw, spos, shdr.startup_size,
                                       shdr.compression)
            except Exception as exc:  # decompression failure (level 1)
                error(1, f"decompression failure ({exc})")
                return
        else:
            buf = raw
        if ucompress_file:
            try:
                with open(ucompress_file, 'wb') as f:
                    f.write(buf)
            except OSError as exc:
                error(1, f"Unable to create a file to uncompress image ({exc})")
                return
        ipos = buf.find(IMAGE_SIG, spos + shdr.startup_size)
        if ipos == -1:
            error(1, f"Unable to find image header in {image}")
            return

    ihdr_be = False
    ihdr = ImageHeader.parse(buf, ipos, be=ihdr_be)
    if ihdr.flags & IMAGE_FLAGS_BIGENDIAN:
        if crossendian(True):
            ihdr_be = True
            ihdr = ImageHeader.parse(buf, ipos, be=True)
        if ihdr.signature != IMAGE_SIG:
            error(1, f"Unable to read image {image}")
            return
    if ihdr.signature != IMAGE_SIG:
        error(1, f"Unable to read image {image}")
        return

    dpos = ipos + ihdr.dir_offset
    check = make_check(check_files)
    out = sys.stdout

    # -- header block -------------------------------------------------------
    if flags & FLAG_DISPLAY:
        out.write("   Offset     Size  Name\n")
        if spos != -1:
            if spos:
                if check("*.boot") == 0:
                    out.write(" %8x %8x  %s\n" % (0, spos, "*.boot"))
            display_shdr(shdr, spos, check, verbose, out)
            if check("startup.*") == 0:
                out.write(" %8x %8lx  %s\n" %
                          (spos + STARTUP_HDR_SIZE,
                           shdr.startup_size - STARTUP_HDR_SIZE, "startup.*"))
        display_ihdr(buf, ihdr, ipos, check, verbose, out)
        if check("Image-directory") == 0:
            out.write(" %8x %8lx  %s\n" %
                      (dpos, ihdr.hdr_dir_size - ihdr.dir_offset,
                       "Image-directory"))

    # -- dirent walk --------------------------------------------------------
    processing_done = False
    files_left = len(extract_files) if extract_files else 0
    assoc = {}      # associate() table for sched_aps names (0 -> "System")
    ext_sched = [SCRIPT_SCHED_EXT_NONE]

    while not processing_done:
        if dpos + 24 > len(buf):
            error(1, "Early end reading directory")
            break
        ent = DirEntry.parse(buf, dpos, be=ihdr_be)
        if ent.size < 24:
            if ent.size != 0:
                error(1, "Invalid dir entry")
            break
        dpos += ent.size
        typ = ent.mode & S_IFMT

        if typ == S_IFREG:
            # extract_file() begins with the check() filter, and display_file()
            # applies it again itself (reference behaviour).
            if (flags & FLAG_EXTRACT) and check(ent.path) == 0:
                name = (c_basename(ent.path) if flags & FLAG_BASENAME
                        else ent.path)
                if extract_files is None:
                    _extract_file(buf, ipos, ent, name, verbose)
                elif name in extract_files:
                    files_left -= 1
                    if files_left == 0:
                        processing_done = True
                    _extract_file(buf, ipos, ent, name, verbose)
            if flags & (FLAG_MD5 | FLAG_DISPLAY):
                display_file(buf, ipos, ent, flags, check, verbose, out)
            if ent.ino == ihdr.script_ino and verbose > 1:
                display_script(buf, ipos + ent.offset, ent.filesize, out,
                               assoc, ext_sched)
        elif typ == S_IFDIR:
            if flags & FLAG_DISPLAY:
                display_dir(ent, check, verbose, out)
        elif typ == S_IFLNK:
            if flags & FLAG_DISPLAY:
                display_symlink(ent, check, verbose, out)
        elif typ in (S_IFCHR, S_IFBLK, S_IFIFO, S_IFNAM):
            if flags & FLAG_DISPLAY:
                display_device(ent, check, verbose, out)
        else:
            error(1, "Unknown type")
            break

    # -- checksums ----------------------------------------------------------
    if flags & FLAG_DISPLAY:
        itlr_off = ipos + ihdr.image_size - 4
        if itlr_off + 4 > len(buf):
            error(1, "Early end reading image trailer")
            return
        itlr = struct.unpack_from('>I' if ihdr_be else '<I', buf, itlr_off)[0]
        line = "Checksums: image=%s" % hexhash(itlr)
        if spos != -1:
            stlr_off = spos + shdr.startup_size - 4
            if stlr_off + 4 > len(buf):
                out.write("\n")
                error(1, "Early end reading startup trailer")
                return
            be_shdr = crossendian(bool(shdr.flags1 &
                                       STARTUP_HDR_FLAGS1_BIGENDIAN))
            stlr = struct.unpack_from('>I' if be_shdr else '<I',
                                      buf, stlr_off)[0]
            line += " startup=%s" % hexhash(stlr)
        out.write(line + "\n")
    out.flush()


def make_check(check_files):
    """int check(const char *name): basename equality against filter list."""
    if not check_files:
        return lambda name: 0
    names = set(c_basename(n) for n in check_files)
    return lambda name: 0 if c_basename(name) in names else -1


# ---------------------------------------------------------------------------
# Display routines (exact formats from dumpifs.c)
# ---------------------------------------------------------------------------

def display_shdr(hdr: StartupHeader, spos: int, check, verbose: int, out) -> None:
    if check("Startup-header") != 0:
        return
    out.write(" %8x %8x  %s flags1=%s flags2=%s paddr_bias=%s\n" %
              (spos, hdr.header_size, "Startup-header",
               hexhash(hdr.flags1), hexhash(hdr.flags2),
               hexhash(hdr.paddr_bias)))
    if verbose:
        out.write("                       preboot_size=%s\n" %
                  hexhash(hdr.preboot_size))
        out.write("                       image_paddr=%s stored_size=%s\n" %
                  (hexhash(hdr.image_paddr), hexhash(hdr.stored_size)))
        out.write("                       startup_size=%s imagefs_size=%s\n" %
                  (hexhash(hdr.startup_size), hexhash(hdr.imagefs_size)))
        out.write("                       ram_paddr=%s ram_size=%s\n" %
                  (hexhash(hdr.ram_paddr), hexhash(hdr.ram_size)))
        out.write("                       startup_vaddr=%s\n" %
                  hexhash(hdr.startup_vaddr))


def display_ihdr(buf: bytes, hdr: ImageHeader, ipos: int, check, verbose: int,
                 out) -> None:
    if check("Image-header") != 0:
        return
    out.write(" %8x %8x  %s" % (ipos, 92, "Image-header"))  # sizeof image_header
    if hdr.mountpoint:
        out.write(" mountpoint=%s" % hdr.mountpoint)
    out.write("\n")
    if verbose:
        out.write("                       flags=%s" % hexhash(hdr.flags))
        if hdr.chain_paddr:
            out.write(" chain=%s" % hexhash(hdr.chain_paddr))
        if hdr.script_ino:
            out.write(" script=%d" % hdr.script_ino)
        for i in range(4):
            if hdr.boot_ino[i]:
                out.write(" boot=%d" % hdr.boot_ino[i])
                for j in range(i + 1, 4):
                    if hdr.boot_ino[j]:
                        out.write(",%d" % hdr.boot_ino[j])
                break
        if hdr.spare[0]:
            out.write(" spare=%s" % hexhash(hdr.spare[0]))
            for i in range(1, 10):
                out.write(",%d" % hdr.spare[i])
        out.write(" mntflg=%s\n" % hexhash(hdr.mountflags))


def display_attr(ent: DirEntry, out) -> None:
    out.write("                       gid=%d uid=%d mode=%s ino=%d mtime=%x\n" %
              (ent.gid, ent.uid, octalhash(ent.mode & ~S_IFMT),
               ent.ino, ent.mtime))


def display_dir(ent: DirEntry, check, verbose: int, out) -> None:
    path = ent.path if ent.path else "Root-dirent"
    if check(path) != 0:
        return
    out.write("     ----     ----  %s\n" % path)
    if verbose:
        display_attr(ent, out)


def display_symlink(ent: DirEntry, check, verbose: int, out) -> None:
    if check(ent.path) != 0:
        return
    out.write("     ---- %8x  %s -> %s\n" %
              (ent.sym_size, ent.path, ent.target))
    if verbose:
        display_attr(ent, out)


def display_device(ent: DirEntry, check, verbose: int, out) -> None:
    if check(ent.path) != 0:
        return
    out.write("     ----     ----  %s dev=%d rdev=%d\n" %
              (ent.path, ent.dev, ent.rdev))
    if verbose:
        display_attr(ent, out)


def display_file(buf: bytes, ipos: int, ent: DirEntry, flags: int, check,
                 verbose: int, out) -> None:
    if check(ent.path) != 0:
        return
    out.write(" %8x %8x  %s" % (ipos + ent.offset, ent.filesize, ent.path))
    if flags & FLAG_MD5:
        data = buf[ipos + ent.offset:ipos + ent.offset + ent.filesize]
        out.write(" " + hashlib.md5(data).hexdigest())
    out.write("\n")
    if verbose:
        display_attr(ent, out)
    if verbose > 1:
        display_elf(buf, ipos + ent.offset, ent.filesize,
                    c_basename(ent.path), verbose, out)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _extract_file(buf: bytes, ipos: int, ent: DirEntry, name: str,
                  verbose: int) -> None:
    parent = os.path.dirname(name)
    if parent:
        os.makedirs(parent, exist_ok=True)  # deviation: reference fails here
    try:
        dst = open(name, 'wb')
    except OSError as exc:
        error(0, "Unable to open %s: %s\n" % (name, exc.strerror))
        return
    data = buf[ipos + ent.offset:ipos + ent.offset + ent.filesize]
    if len(data) != ent.filesize:
        dst.close()
        try:
            os.unlink(name)
        except OSError:
            pass
        error(0, "Unable to create file %s: %s\n" % (name, "short read"))
        return
    dst.write(data)
    dst.close()
    try:
        os.chmod(name, ent.mode & 0o7777)
    except OSError:
        pass
    try:
        os.chown(name, ent.uid, ent.gid)
    except OSError:
        pass
    try:
        os.utime(name, (ent.mtime, ent.mtime))
    except OSError:
        pass
    if verbose:
        print("Extracted %s" % name)


# ---------------------------------------------------------------------------
# Script display (union script_cmd decoding)
# ---------------------------------------------------------------------------

def _cstring(data: bytes, off: int) -> str:
    end = data.find(b'\x00', off)
    if end == -1:
        end = len(data)
    return data[off:end].decode('latin-1')


def display_script(buf: bytes, pos: int, length: int, out, assoc: dict,
                   ext_sched: list) -> None:
    off = 0
    while off < length:
        if pos + off + 13 > len(buf):
            break
        cmd = buf[pos + off:pos + off + min(length - off, 13 + 512)]
        if len(cmd) < 4:
            break
        size = cmd[0] | (cmd[1] << 8)
        if size == 0:
            break
        if size > len(cmd):
            cmd = buf[pos + off:pos + off + size]
        typ = cmd[2]
        out.write("                       ")
        if typ == SCRIPT_TYPE_EXTERNAL:
            argc = cmd[10]
            envc = cmd[11]
            cmdstr = _cstring(cmd, 12)
            p = 12 + len(cmdstr) + 1
            argv = []
            for _ in range(argc):
                argv.append(_cstring(cmd, p))
                p += len(argv[-1]) + 1
            envs = []
            for _ in range(envc):
                envs.append(_cstring(cmd, p))
                p += len(envs[-1]) + 1
            argv0 = argv[0] if argv else ""
            argv_differs = c_basename(cmdstr) != argv0
            special = (cmd[5] & (SCRIPT_FLAGS_SESSION | SCRIPT_FLAGS_KDEBUG |
                                 SCRIPT_FLAGS_SCHED_SET | SCRIPT_FLAGS_CPU_SET |
                                 SCRIPT_FLAGS_EXTSCHED))
            if argv_differs or special:
                out.write("[ ")
                if argv_differs:
                    out.write("argv0=%s " % argv0)
                if cmd[5] & SCRIPT_FLAGS_SESSION:
                    out.write("+session ")
                if cmd[5] & SCRIPT_FLAGS_KDEBUG:
                    out.write("+debug ")
                if cmd[5] & SCRIPT_FLAGS_SCHED_SET:
                    out.write("priority=%d" % cmd[9])
                    policy = cmd[8]
                    if policy == SCRIPT_POLICY_FIFO:
                        out.write("f")
                    elif policy == SCRIPT_POLICY_RR:
                        out.write("r")
                    elif policy == SCRIPT_POLICY_OTHER:
                        out.write("o")
                    elif policy != SCRIPT_POLICY_NOCHANGE:
                        out.write("?%d?" % policy)
                    out.write(" ")
                if cmd[5] & SCRIPT_FLAGS_CPU_SET:
                    out.write("cpu=%d " % cmd[4])
                if cmd[5] & SCRIPT_FLAGS_EXTSCHED:
                    out.write("sched_aps=%s " %
                              assoc.get(cmd[6], ""))
                out.write("] ")
            for e in envs:
                out.write("%s " % e)
            out.write("%s" % cmdstr)
            for a in argv[1:]:
                out.write(" %s" % a)
            if cmd[5] & SCRIPT_FLAGS_BACKGROUND:
                out.write(" &")
            out.write("\n")
        elif typ in (SCRIPT_TYPE_WAITFOR, SCRIPT_TYPE_REOPEN):
            out.write("waitfor" if typ == SCRIPT_TYPE_WAITFOR else "reopen")
            checks = cmd[4] | (cmd[5] << 8)
            if checks:
                out.write(" %d.%d" % (checks // 10, checks % 10))
            out.write(" %s\n" % _cstring(cmd, 6))
        elif typ == SCRIPT_TYPE_DISPLAY_MSG:
            out.write("display_msg '%s'\n" % _cstring(cmd, 4))
        elif typ == SCRIPT_TYPE_PROCMGR_SYMLINK:
            src = _cstring(cmd, 4)
            dest = _cstring(cmd, 4 + len(src) + 1)
            out.write("procmgr_symlink '%s' '%s'\n" % (src, dest))
        elif typ == SCRIPT_TYPE_EXTSCHED_APS:
            if ext_sched[0] == SCRIPT_SCHED_EXT_NONE:
                assoc[SCRIPT_APS_SYSTEM_PARTITION_ID] = \
                    SCRIPT_APS_SYSTEM_PARTITION_NAME
            elif ext_sched[0] != SCRIPT_SCHED_EXT_APS:
                out.write("Invalid combination of SCHED_EXT features\n")
            ext_sched[0] = SCRIPT_SCHED_EXT_APS
            pid = cmd[4]
            name = assoc.get(pid) if True else None
            # associate(id, pname): stores pname, returns it
            pname = _cstring(cmd, 9)
            assoc[pid] = pname
            out.write("sched_aps %s %d %d\n" %
                      (pname, cmd[5], (cmd[7] << 8) | cmd[6]))
        else:
            out.write("Unknown type %d\n" % typ)
        off += size


# ---------------------------------------------------------------------------
# ELF display (display_elf)
# ---------------------------------------------------------------------------

ELF_TYPES = ["ET_NONE", "ET_REL", "ET_EXEC", "ET_DYN", "ET_CORE"]
ELF_MACHINES = [
    "EM_NONE", "EM_M32", "EM_SPARC", "EM_386", "EM_68k", "EM_88k", "EM_486",
    "EM_860", "EM_MIPS", "EM_UNKNOWN9", "EM_MIPS_RS3_LE", "EM_RS6000",
    "EM_UNKNOWN12", "EM_UNKNOWN13", "EM_UNKNOWN14", "EM_PA_RISC", "EM_nCUBE",
    "EM_VPP500", "EM_SPARC32PLUS", "EM_UNKNOWN19", "EM_PPC", "EM_UNKNOWN21",
    "EM_UNKNOWN22", "EM_UNKNOWN23", "EM_UNKNOWN24", "EM_UNKNOWN25",
    "EM_UNKNOWN26", "EM_UNKNOWN27", "EM_UNKNOWN28", "EM_UNKNOWN29",
    "EM_UNKNOWN30", "EM_UNKNOWN31", "EM_UNKNOWN32", "EM_UNKNOWN33",
    "EM_UNKNOWN34", "EM_UNKNOWN35", "EM_UNKNOWN36", "EM_UNKNOWN37",
    "EM_UNKNOWN38", "EM_ARM", "EM_UNKNOWN41", "EM_SH",
]
PHDR_TYPES = ["PT_NULL", "PT_LOAD", "PT_DYNAMIC", "PT_INTERP", "PT_NOTE",
              "PT_SHLIB", "PT_PHDR"]
PHDR_FLAGS = ["---", "--X", "-W-", "-WX", "R--", "R-X", "RW-", "RWX"]


def dsp_index(table, index: int) -> str:
    if index < 0 or index >= len(table) or table[index] is None:
        return "Unknown(%d)" % index
    return table[index]


def display_elf(buf: bytes, pos: int, size: int, name: str, verbose: int,
                out) -> None:
    if pos + 64 > len(buf):
        return
    ehdr = buf[pos:pos + 52]
    if ehdr[:4] != b'\x7fELF':
        return
    if verbose <= 2:
        out.write("                       ")
    out.write("----- %s " % name)
    if ehdr[6] != 1:  # EI_VERSION != EV_CURRENT
        out.write("ELF version %d -----\n" % ehdr[6])
        return
    ei_class = ehdr[4]
    if ei_class == 1:
        out.write("- ELF32")
    elif ei_class == 2:
        out.write("- ELF64")
    else:
        out.write("ELF executable - invalid class -----\n")
        return
    ei_data = ehdr[5]
    if ei_data == 1:
        out.write("LE ")
    elif ei_data == 2:
        out.write("BE ")
    else:
        out.write(" executable - unknown endian -----\n")
        return
    if ei_class == 2:
        out.write("-----\n")
        return

    be = crossendian(ei_data == 2)
    fmt = '>' if be else '<'
    (e_type, e_machine, e_version, e_entry, e_phoff, e_shoff, e_flags,
     e_ehsize, e_phentsize, e_phnum, e_shentsize, e_shnum,
     e_shstrndx) = struct.unpack_from(fmt + 'HHIIIIIHHHHHH', ehdr, 16)
    out.write("%s %s -----\n" % (dsp_index(ELF_TYPES, e_type),
                                 dsp_index(ELF_MACHINES, e_machine)))
    if verbose <= 2:
        return
    if e_ehsize < 52:
        out.write("-----\n")
        error(1, "ehdr too small")
        return
    out.write(" e_flags              : %s\n" % hexhash(e_flags))
    if e_entry or e_phoff or e_phnum or e_phentsize:
        out.write(" e_entry              : %s\n" % hexhash(e_entry))
        out.write(" e_phoff              : %d\n" % e_phoff)
        out.write(" e_phentsize          : %d\n" % e_phentsize)
        out.write(" e_phnum              : %d\n" % e_phnum)
    elif e_entry:
        out.write(" e_entry              : %s\n" % hexhash(e_entry))
    if e_shoff or e_shnum or e_shentsize or e_shstrndx:
        out.write(" e_shoff              : %d\n" % e_shoff)
        out.write(" e_shentsize          : %d\n" % e_shentsize)
        out.write(" e_shnum              : %d\n" % e_shnum)
    if e_shstrndx:
        out.write(" e_shstrndx           : %d\n" % e_shstrndx)
    if e_phnum and e_phentsize >= 32:
        for n in range(e_phnum):
            poff = pos + e_phoff + n * e_phentsize
            if poff + 32 > len(buf):
                error(1, "Unable to read phdr %d\n" % n)
                break
            (p_type, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_flags,
             p_align) = struct.unpack_from(fmt + 'IIIIIIII', buf, poff)
            out.write(" segment %d\n" % n)
            out.write("   p_type               : ")
            if p_type == 0x4000:
                out.write("PT_COMPRESS")
            elif p_type == 0x4001:
                out.write("PT_SEGREL")
            else:
                out.write(dsp_index(PHDR_TYPES, p_type))
            out.write("\n")
            out.write("   p_offset             : %d\n" % p_offset)
            out.write("   p_vaddr              : 0x%X\n" % p_vaddr)
            out.write("   p_paddr              : 0x%X\n" % p_paddr)
            out.write("   p_filesz             : %d\n" % p_filesz)
            out.write("   p_memsz              : %d\n" % p_memsz)
            out.write("   p_flags              : %s\n" %
                      dsp_index(PHDR_FLAGS, p_flags))
            out.write("   p_align              : %d\n" % p_align)
    out.write("----------\n")


# ---------------------------------------------------------------------------
# Option parsing (getopt string "f:d:mvxbu:z", GNU-style permutation)
# ---------------------------------------------------------------------------

def parse_args(argv):
    opts = []       # list of (char, value)
    operands = []
    i = 0
    end = False
    while i < len(argv):
        a = argv[i]
        if end or a == '-' or not a.startswith('-'):
            operands.append(a)
            i += 1
            continue
        if a == '--':
            end = True
            i += 1
            continue
        j = 1
        while j < len(a):
            c = a[j]
            if c in 'fdu':
                if j + 1 < len(a):
                    opts.append((c, a[j + 1:]))
                    j = len(a)
                else:
                    if i + 1 < len(argv):
                        i += 1
                        opts.append((c, argv[i]))
                    else:
                        sys.stderr.write(
                            "%s: option requires an argument -- '%s'\n" %
                            (PROGNAME, c))
                    j = len(a)
            elif c in 'mvxbz':
                opts.append((c, None))
                j += 1
            else:
                sys.stderr.write("%s: invalid option -- '%s'\n" %
                                 (PROGNAME, c))
                break
        i += 1
    return opts, operands


def main(argv=None) -> int:
    global PROGNAME
    argv = sys.argv if argv is None else argv
    PROGNAME = os.path.basename(argv[0])
    opts, operands = parse_args(argv[1:])

    flags = 0
    verbose = 0
    zero_check = True
    dest_dir = None
    ucompress_file = None
    extract_files = None       # None: extract all; list: -f selection
    n_extract_sel = 0

    for c, v in opts:
        if c == 'f':
            if extract_files is None:
                extract_files = []
            extract_files.append(v)
            n_extract_sel += 1
            flags |= FLAG_EXTRACT
        elif c == 'd':
            dest_dir = v
            flags |= FLAG_EXTRACT
        elif c == 'm':
            flags |= FLAG_MD5
        elif c == 'x':
            flags |= FLAG_EXTRACT
        elif c == 'b':
            flags |= FLAG_BASENAME
        elif c == 'u':
            ucompress_file = v
        elif c == 'v':
            verbose += 1
        elif c == 'z':
            zero_check = False

    if flags & (FLAG_EXTRACT | FLAG_MD5):
        if verbose > 1:
            flags |= FLAG_DISPLAY
    else:
        flags |= FLAG_DISPLAY

    if not operands:
        error(0, "Missing image file system name\n")
    image = operands[0]
    check_files = operands[1:] if len(operands) > 1 else None

    if image != '-':
        try:
            with open(image, 'rb') as f:
                raw = f.read()
        except OSError as exc:
            error(0, "Unable to open file %s - %s" % (image, exc.strerror))
            return 1
    else:
        raw = sys.stdin.buffer.read()
        image = "-- stdin --"

    old_cwd = None
    if dest_dir is not None:
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError:
            if not os.access(dest_dir, os.W_OK):
                error(0, "Cannot create directory %s to extract %s" %
                      (dest_dir, image))
        old_cwd = os.getcwd()
        try:
            os.chdir(dest_dir)
        except OSError:
            error(0, "Cannot cd to directory %s to extract %s" %
                  (dest_dir, image))

    try:
        process(image, raw, flags, verbose, check_files,
                extract_files if extract_files is not None else None,
                ucompress_file, zero_check)
    finally:
        if old_cwd is not None:
            os.chdir(old_cwd)
    return 0


if __name__ == '__main__':
    sys.exit(main())
