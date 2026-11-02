"""Read and write uncompressed, little-endian QNX imagefs images."""
import os
import pathlib
import stat
import struct
from dataclasses import dataclass

IMAGE_SIGNATURE = b"imagefs"
IMAGE_FLAGS_INO_BITS = 0x04
HEADER_FIXED_SIZE = 88
ATTR_SIZE = 24


def align(value: int, boundary: int = 4) -> int:
    return (value + boundary - 1) & -boundary


def checksum(data: bytes) -> int:
    padded = data + b"\0" * ((-len(data)) & 3)
    return (-sum(struct.unpack(f"<{len(padded) // 4}I", padded))) & 0xFFFFFFFF


@dataclass
class Entry:
    path: str
    inode: int
    mode: int
    uid: int
    gid: int
    mtime: int
    offset: int = 0
    size: int = 0
    link_target: str | None = None


@dataclass
class Image:
    offset: int
    size: int
    mountpoint: str
    entries: list[Entry]
    data: bytes


def _metadata(source: pathlib.Path | None, directory: bool) -> tuple[int, int, int, int]:
    if source is not None and source.exists():
        st = source.lstat()
        return st.st_mode, st.st_uid, st.st_gid, int(st.st_mtime)
    return (stat.S_IFDIR | 0o755 if directory else stat.S_IFREG | 0o644), 0, 0, 0


def write_image(entries: list[tuple[str, pathlib.Path | None, bool]], destination: pathlib.Path,
                mountpoint: str = "") -> None:
    """Write a standalone imagefs; boot headers and compression are not emitted."""
    records: list[tuple[Entry, bytes]] = []
    for inode, (target, source, directory) in enumerate(entries, start=2):
        mode, uid, gid, mtime = _metadata(source, directory)
        target = target.lstrip("/")
        if stat.S_ISLNK(mode):
            payload = b""
            link_target = os.readlink(source) if source else ""
        elif stat.S_ISREG(mode):
            if source is None:
                raise ValueError(f"{target}: a source path is required")
            payload = source.read_bytes()
            link_target = None
        else:
            payload, link_target = b"", None
        records.append((Entry(target, inode, mode, uid, gid, mtime, size=len(payload),
                              link_target=link_target), payload))

    mount = mountpoint.encode() + b"\0"
    header_size = align(HEADER_FIXED_SIZE + len(mount))

    def encode(entry: Entry) -> bytes:
        path = entry.path.encode() + b"\0"
        attr = struct.pack("<HHIIIII", 0, 0, entry.inode, entry.mode,
                           entry.gid, entry.uid, entry.mtime)
        if stat.S_ISREG(entry.mode):
            value = attr + struct.pack("<II", entry.offset, entry.size) + path
        elif stat.S_ISLNK(entry.mode):
            target = entry.link_target.encode() + b"\0"
            value = attr + struct.pack("<HH", len(path), len(target)) + path + target
        elif stat.S_ISDIR(entry.mode):
            value = attr + path
        else:
            value = attr + struct.pack("<II", 0, 0) + path
        value += b"\0" * ((-len(value)) & 3)
        return struct.pack("<H", len(value)) + value[2:]

    root = Entry("", 1, stat.S_IFDIR | 0o755, 0, 0, 0)
    directory_size = len(encode(root)) + sum(len(encode(entry)) for entry, _ in records) + 4
    cursor = header_size + directory_size
    for entry, payload in records:
        entry.offset = cursor
        cursor = align(cursor + len(payload))
    image_size = cursor + 4

    header = struct.pack("<7sB20I", IMAGE_SIGNATURE, IMAGE_FLAGS_INO_BITS,
                         image_size, header_size + directory_size, header_size,
                         0, 0, 0, 0, 0, 0, *([0] * 10), 0)
    image = bytearray(header + mount)
    image.extend(b"\0" * (header_size - len(image)))
    image.extend(encode(root))
    image.extend(b"".join(encode(entry) for entry, _ in records))
    image.extend(b"\0\0\0\0")
    for entry, payload in records:
        image.extend(b"\0" * (entry.offset - len(image)))
        image.extend(payload)
        image.extend(b"\0" * (align(len(image)) - len(image)))
    image.extend(struct.pack("<I", checksum(image)))
    if len(image) != image_size:
        raise AssertionError(f"image size mismatch: {len(image)} != {image_size}")
    destination.write_bytes(image)


def find_images(data: bytes) -> list[Image]:
    """Find structurally valid uncompressed little-endian imagefs instances."""
    images = []
    start = 0
    while True:
        offset = data.find(IMAGE_SIGNATURE, start)
        if offset < 0:
            return images
        start = offset + 1
        if offset + HEADER_FIXED_SIZE > len(data):
            continue
        flags = data[offset + 7]
        if flags & 1:  # big-endian support intentionally deferred
            continue
        values = struct.unpack_from("<20I", data, offset + 8)
        image_size, hdr_dir_size, dir_offset = values[:3]
        if (image_size < HEADER_FIXED_SIZE + 4 or offset + image_size > len(data)
                or dir_offset < HEADER_FIXED_SIZE or hdr_dir_size > image_size
                or dir_offset > hdr_dir_size):
            continue
        stored_checksum = struct.unpack_from("<I", data, offset + image_size - 4)[0]
        if stored_checksum != checksum(data[offset:offset + image_size - 4]):
            continue
        mount_end = data.find(b"\0", offset + HEADER_FIXED_SIZE, offset + dir_offset)
        if mount_end < 0:
            continue
        entries, position, valid = [], offset + dir_offset, True
        while position + 2 <= offset + hdr_dir_size:
            size = struct.unpack_from("<H", data, position)[0]
            if size == 0:
                break
            if size < ATTR_SIZE or position + size > offset + hdr_dir_size:
                valid = False
                break
            _, _, inode, mode, gid, uid, mtime = struct.unpack_from("<HHIIIII", data, position)
            path_offset = position + (ATTR_SIZE + 8 if stat.S_ISREG(mode) else
                                      ATTR_SIZE + 4 if stat.S_ISLNK(mode) else ATTR_SIZE)
            path_end = data.find(b"\0", path_offset, position + size)
            if path_end < 0:
                valid = False
                break
            path = data[path_offset:path_end].decode(errors="surrogateescape")
            entry = Entry(path, inode, mode, uid, gid, mtime)
            if stat.S_ISREG(mode):
                entry.offset, entry.size = struct.unpack_from("<II", data, position + ATTR_SIZE)
                if entry.offset + entry.size > image_size:
                    valid = False
                    break
            elif stat.S_ISLNK(mode):
                link_offset, link_size = struct.unpack_from("<HH", data, position + ATTR_SIZE)
                link_start = path_offset + link_offset
                entry.link_target = data[link_start:link_start + link_size - 1].decode(errors="surrogateescape")
            entries.append(entry)
            position += size
        if valid and position + 2 <= offset + hdr_dir_size:
            images.append(Image(offset, image_size,
                                data[offset + HEADER_FIXED_SIZE:mount_end].decode(errors="surrogateescape"),
                                entries, data))
