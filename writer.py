"""
IFS writer stub.
IFS format (QNX Neutrino 6.4) based on:
  - sys/startup.h  startup_header  (0x100 bytes)
  - sys/image.h    image_header    (0x5c bytes)
  - image directory chain
  - file data blobs
  - 32-bit image checksum (sum of 32-bit words, negated)
"""
import struct
import pathlib

STARTUP_HDR_SIZE = 0x100
IMAGE_HDR_SIZE = 0x5c

# Flags from startup.h
STARTUP_HDR_FLAGS1_VIRTUAL     = 0x8000
STARTUP_HDR_FLAGS1_BIGENDIAN   = 0x4000
STARTUP_HDR_FLAGS1_COMPRESS    = 0x01
STARTUP_HDR_FLAGS1_COMPRESS_ZLIB = 0x01
STARTUP_HDR_FLAGS1_COMPRESS_LZO  = 0x02
STARTUP_HDR_FLAGS1_COMPRESS_UCL  = 0x03

# Machine IDs from startup.h
STARTUP_HDR_MACHINE_I386 = 3

def compute_checksum(data: bytes) -> int:
    """32-bit image checksum: sum of 32-bit words, negated."""
    words = len(data) // 4
    total = 0
    for i in range(words):
        val = struct.unpack_from('<I', data, i * 4)[0]
        total += val
    total = (total & 0xFFFFFFFF)
    return (-total) & 0xFFFFFFFF

def write_startup_header(f, compressed: bool = False, machine: int = STARTUP_HDR_MACHINE_I386):
    """Write a minimal startup header."""
    # Header magic and basic fields (simplified)
    # 0x00FF7EEB signature
    sig = struct.pack('<I', 0x00FF7EEB)
    f.write(sig)
    # version (word)
    f.write(struct.pack('<H', 1))
    # flags1
    flags1 = STARTUP_HDR_FLAGS1_VIRTUAL if machine == STARTUP_HDR_MACHINE_I386 else 0
    if compressed:
        flags1 |= STARTUP_HDR_FLAGS1_COMPRESS_ZLIB
    f.write(struct.pack('<H', flags1))
    # flags2
    f.write(struct.pack('<H', 0))
    # paddr_bias
    f.write(struct.pack('<I', 0))
    # image_paddr
    f.write(struct.pack('<I', 0))
    # ram_paddr
    f.write(struct.pack('<I', 0))
    # ram_size
    f.write(struct.pack('<I', 0))
    # startup_size (size of startup program + header)
    # For now leave placeholder
    f.write(struct.pack('<I', 0x100))
    # stored_size
    f.write(struct.pack('<I', 0x100))
    # image_paddr (again?) 
    f.write(struct.pack('<I', 0))
    # ram_paddr (again)
    f.write(struct.pack('<I', 0))
    # machine
    f.write(struct.pack('<H', machine))
    # reserve (4 bytes)
    f.write(struct.pack('<I', 0))

def write_image_header(f, mountpoint: str = '/'):
    """Write a minimal image header."""
    # Magic
    f.write(b'IFS ')
    # Version
    f.write(struct.pack('<H', 1))
    # Reserved
    f.write(struct.pack('<H', 0))
    # Mountpoint length
    mp_len = len(mountpoint.encode('ascii'))
    f.write(struct.pack('<H', mp_len))
    # Mountpoint
    if mp_len > 0:
        f.write(mountpoint.encode('ascii')[:32])
    # Reserved bytes
    f.write(b'\x00' * (32 - mp_len))
    # Total image size (placeholder)
    f.write(struct.pack('<I', 0))
    # Free space (placeholder)
    f.write(struct.pack('<I', 0))
    # Checksum (placeholder)
    f.write(struct.pack('<I', 0))
    # Directory start (placeholder)
    f.write(struct.pack('<I', 0))
    # Image flags
    f.write(struct.pack('<I', 0))
    # Pad to IMAGE_HDR_SIZE
    used = f.tell()
    remaining = IMAGE_HDR_SIZE - used
    if remaining > 0:
        f.write(b'\x00' * remaining)

def write_empty_ifs(output_path: pathlib.Path):
    with open(output_path, 'wb') as f:
        # Startup header
        write_startup_header(f, compressed=False, machine=3)
        # Image header
        write_image_header(f, mountpoint='/')
        # File data blobs placeholder
        # Each file gets some data
        for _ in range(32):
            f.write(b'\x00' * 0x100)  # 256 bytes per file placeholder
        # Final image checksum placeholder
        f.write(struct.pack('<I', 0))
    print(f"Wrote empty IFS to {output_path}")