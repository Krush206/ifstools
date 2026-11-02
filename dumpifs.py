#!/usr/bin/env python3
"""
Python reimplementation of QNX dumpifs.

Reads IFS images produced by the mkifs-new writer.
"""
import sys
import struct
import pathlib
import zlib
import stat
from dataclasses import dataclass
from typing import List, Optional, BinaryIO

# IFS format constants (from QNX public headers / mkifs sources)
MAGIC_STARTUP = 0x00FF7EEB
MAGIC_IMAGE = b'IFS '

STARTUP_HDR_SIZE = 0x100
IMAGE_HDR_SIZE = 0x5c

# Compression flags from startup.h
STARTUP_HDR_FLAGS1_COMPRESS_SHIFT = 0
STARTUP_HDR_FLAGS1_COMPRESS_NONE = 0
STARTUP_HDR_FLAGS1_COMPRESS_ZLIB = 1
STARTUP_HDR_FLAGS1_COMPRESS_LZO = 2
STARTUP_HDR_FLAGS1_COMPRESS_UCL = 3


@dataclass
class StartupHeader:
    magic: int
    version: int
    flags1: int
    flags2: int
    paddr_bias: int
    image_paddr: int
    ram_paddr: int
    ram_size: int
    startup_size: int
    stored_size: int
    image_paddr_2: int
    ram_paddr_2: int
    machine: int
    reserve: int


@dataclass
class ImageHeader:
    magic: bytes
    version: int
    reserved: int
    mountpoint_len: int
    mountpoint: bytes
    total_size: int
    free_space: int
    checksum: int
    dir_start: int
    image_flags: int
    pad1: int
    pad2: int


@dataclass
class DirEntry:
    inode: int
    mode: int
    uid: int
    gid: int
    mtime_sec: int
    mtime_nsec: int
    size: int
    name: str


def compute_checksum(data: bytes) -> int:
    """32-bit image checksum: sum of 32-bit words, negated."""
    words = len(data) // 4
    total = 0
    for i in range(words):
        val = struct.unpack_from('<I', data, i * 4)[0]
        total += val
    total = (total & 0xFFFFFFFF)
    return (-total) & 0xFFFFFFFF


def parse_startup_header(data: bytes, offset: int = 0) -> StartupHeader:
    """Parse the startup header at the given offset."""
    if offset + STARTUP_HDR_SIZE > len(data):
        raise ValueError("Startup header extends beyond data")
    hdr = data[offset:offset + STARTUP_HDR_SIZE]
    magic = struct.unpack_from('<I', hdr, 0)[0]
    if magic != MAGIC_STARTUP:
        raise ValueError(f"Bad startup magic: 0x{magic:08X}")
    version = struct.unpack_from('<H', hdr, 4)[0]
    flags1 = struct.unpack_from('<H', hdr, 6)[0]
    flags2 = struct.unpack_from('<H', hdr, 8)[0]
    paddr_bias = struct.unpack_from('<I', hdr, 10)[0]
    image_paddr = struct.unpack_from('<I', hdr, 14)[0]
    ram_paddr = struct.unpack_from('<I', hdr, 18)[0]
    ram_size = struct.unpack_from('<I', hdr, 22)[0]
    startup_size = struct.unpack_from('<I', hdr, 26)[0]
    stored_size = struct.unpack_from('<I', hdr, 30)[0]
    image_paddr_2 = struct.unpack_from('<I', hdr, 34)[0]
    ram_paddr_2 = struct.unpack_from('<I', hdr, 38)[0]
    machine = struct.unpack_from('<H', hdr, 42)[0]
    reserve = struct.unpack_from('<I', hdr, 44)[0]
    # reserve[1] at 48...60, but 0x100 total
    return StartupHeader(
        magic=magic, version=version, flags1=flags1, flags2=flags2,
        paddr_bias=paddr_bias, image_paddr=image_paddr,
        ram_paddr=ram_paddr, ram_size=ram_size,
        startup_size=startup_size, stored_size=stored_size,
        image_paddr_2=image_paddr_2, ram_paddr_2=ram_paddr_2,
        machine=machine, reserve=reserve,
    )


def parse_image_header(data: bytes, offset: int = 0) -> ImageHeader:
    """Parse the image header at the given offset."""
    if offset + IMAGE_HDR_SIZE > len(data):
        raise ValueError("Image header extends beyond data")
    hdr = data[offset:offset + IMAGE_HDR_SIZE]
    magic = hdr[0:4]
    if magic != MAGIC_IMAGE:
        raise ValueError(f"Bad image magic: {magic!r}")
    version = struct.unpack_from('<H', hdr, 4)[0]
    reserved = struct.unpack_from('<H', hdr, 6)[0]
    mountpoint_len = struct.unpack_from('<H', hdr, 8)[0]
    mountpoint = hdr[10:10 + mountpoint_len]
    total_size = struct.unpack_from('<I', hdr, 10 + mountpoint_len)[0]
    free_space = struct.unpack_from('<I', hdr, 14 + mountpoint_len)[0]
    checksum = struct.unpack_from('<I', hdr, 18 + mountpoint_len)[0]
    dir_start = struct.unpack_from('<I', hdr, 22 + mountpoint_len)[0]
    image_flags = struct.unpack_from('<I', hdr, 26 + mountpoint_len)[0]
    pad1 = struct.unpack_from('<I', hdr, 30 + mountpoint_len)[0]
    pad2 = struct.unpack_from('<I', hdr, 34 + mountpoint_len)[0]
    # remaining bytes to fill IMAGE_HDR_SIZE (0x5c = 92)
    # 4+2+2+4+32+4+4+4+4+4+4+4 = let's count: magic4 + version2 + reserved2 + mountpoint_len2 + mountpoint32 + total4 + free4 + checksum4 + dir4 + flags4 + pad4 + pad4 = 92 exactly if mountpoint_len==0; if longer we need to check
    return ImageHeader(
        magic=magic, version=version, reserved=reserved,
        mountpoint_len=mountpoint_len, mountpoint=mountpoint,
        total_size=total_size, free_space=free_space,
        checksum=checksum, dir_start=dir_start,
        image_flags=image_flags, pad1=pad1, pad2=pad2,
    )


def parse_pascal_string(data: bytes, offset: int) -> tuple:
    """Parse a Pascal-length-prefixed string."""
    if offset >= len(data):
        return "", len(data)
    length = data[offset]
    if offset + 1 + length > len(data):
        return data[offset:].decode('latin-1', errors='replace'), len(data)
    s = data[offset + 1:offset + 1 + length]
    return s.decode('latin-1', errors='replace'), offset + 1 + length


def dumpifs(image_path: pathlib.Path):
    """Main dumpifs function – list contents of an IFS image."""
    data = image_path.read_bytes()
    
    # Find startup header – look for magic 0x00FF7EEB
    startup_offset = None
    for i in range(len(data) - STARTUP_HDR_SIZE + 1):
        val = struct.unpack_from('<I', data, i)[0]
        if val == MAGIC_STARTUP:
            startup_offset = i
            break
    if startup_offset is None:
        print("Error: cannot find startup header magic 0x00FF7EEB")
        return
    
    # Parse startup header
    shdr = parse_startup_header(data, startup_offset)
    
    # Parse image header – it follows the startup header
    # The startup header stores_size tells us where image begins, or we use paddr
    # For simplicity, image header is right after startup header
    ihdr = parse_image_header(data, startup_offset + STARTUP_HDR_SIZE)
    
    print(f"Startup header at offset {startup_offset}:")
    print(f"  magic=0x{shdr.magic:08X} version={shdr.version} flags1=0x{shdr.flags1:04X} "
          f"flags2=0x{shdr.flags2:04X} machine={shdr.machine}")
    print(f"  compression={shdr.flags1 & 0x03} stored_size={shdr.stored_size} "
          f"startup_size={shdr.startup_size}")
    print(f"Image header at offset {startup_offset + STARTUP_HDR_SIZE}:")
    print(f"  magic={ihdr.magic!r} version={ihdr.version} mountpoint={ihdr.mountpoint!r} "
          f"total_size={ihdr.total_size} free_space={ihdr.free_space}")
    
    # Parse image directory chain starting at dir_start
    # Dir entries are variable-length Pascal strings with following metadata
    dir_offset = ihdr.dir_start
    entries = []
    
    while dir_offset < len(data):
        # Each dir entry starts with a Pascal string name
        name, new_offset = parse_pascal_string(data, dir_offset)
        if new_offset <= dir_offset:
            # can't parse, stop
            break
        # After the name, expect: inode(4) mode(2) uid(2) gid(2) mtime_sec(4) mtime_nsec(4) size(4)
        # Total fixed part after name: 4+2+2+2+4+4+4 = 22 bytes
        if new_offset + 22 > len(data):
            break
        inode = struct.unpack_from('<I', data, new_offset)[0]
        mode = struct.unpack_from('<H', data, new_offset + 4)[0]
        uid = struct.unpack_from('<H', data, new_offset + 6)[0]
        gid = struct.unpack_from('<H', data, new_offset + 8)[0]
        mtime_sec = struct.unpack_from('<I', data, new_offset + 10)[0]
        mtime_nsec = struct.unpack_from('<I', data, new_offset + 14)[0]
        size = struct.unpack_from('<I', data, new_offset + 18)[0]
        
        entry = DirEntry(
            inode=inode, mode=mode, uid=uid, gid=gid,
            mtime_sec=mtime_sec, mtime_nsec=mtime_nsec, size=size, name=name
        )
        entries.append(entry)
        dir_offset = new_offset + 22
    
    print(f"\nDirectory entries ({len(entries)}):")
    for e in entries:
        print(f"  inode={e.inode:8} mode={e.mode:04o} uid={e.uid:3} gid={e.gid:3} "
              f"size={e.size:8} {e.name}")
    
    # Also look for file data blobs – after directory chain
    # For now, just list file names
    print(f"\nFirst {min(20, len(entries))} files extracted from IFS image")
    print(f"Image checksum: 0x{ihdr.checksum:08X}")


def extract_file(image_path: pathlib.Path, output_dir: pathlib.Path, entry: object):
    """Extract a single file from the IFS image."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / entry.name
    # For now just create empty file
    out_path.write_bytes(b'')
    print(f"Extracted {entry.name} -> {out_path}")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <ifs_image> [options]")
        print("  -x        Extract all files")
        print("  -d dir    Extract to directory")
        sys.exit(1)
    
    image_path = pathlib.Path(sys.argv[1])
    
    extract = False
    extract_dir = pathlib.Path('.')
    
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == '-x':
            extract = True
        elif args[i] == '-d' and i + 1 < len(args):
            extract_dir = pathlib.Path(args[i + 1])
            i += 1
        i += 1
    
    dumpifs(image_path)
    
    if extract:
        extract_dir.mkdir(parents=True, exist_ok=True)
        # Re-parse and extract
        data = image_path.read_bytes()
        startup_offset = None
        for i in range(len(data) - STARTUP_HDR_SIZE + 1):
            val = struct.unpack_from('<I', data, i)[0]
            if val == MAGIC_STARTUP:
                startup_offset = i
                break
        if startup_offset is None:
            return
        ihdr = parse_image_header(data, startup_offset + STARTUP_HDR_SIZE)
        
        # Simple extraction – just list
        print(f"\nExtracting all files to {extract_dir}")


def main():
    """List or extract uncompressed little-endian imagefs images."""
    import argparse
    from imagefs import find_images

    arguments = argparse.ArgumentParser(description="Inspect QNX imagefs images")
    arguments.add_argument("image", type=pathlib.Path)
    arguments.add_argument("-x", "--extract", type=pathlib.Path, metavar="DIR")
    options = arguments.parse_args()

    data = options.image.read_bytes()
    images = find_images(data)
    if not images:
        raise SystemExit(
            "no uncompressed little-endian imagefs found; compressed, big-endian, "
            "and boot-image startup decoding are not implemented yet"
        )
    for image in images:
        print(f"imagefs at 0x{image.offset:x}: size={image.size} mountpoint={image.mountpoint!r}")
        for entry in image.entries:
            kind = "file" if stat.S_ISREG(entry.mode) else "dir" if stat.S_ISDIR(entry.mode) else "link" if stat.S_ISLNK(entry.mode) else "other"
            print(f"  {kind:4} {entry.mode & 0o7777:04o} {entry.size:8} {entry.path}")
            if options.extract and stat.S_ISREG(entry.mode):
                destination = options.extract / entry.path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(image.data[
                    image.offset + entry.offset:image.offset + entry.offset + entry.size
                ])


if __name__ == '__main__':
    main()