"""
IFS writer for QNX Neutrino 6.4.
Builds a starter IFS with startup header + uncompressed imagefs.
Clean-room reimplementation based on openQNX sources.
"""
import struct
import pathlib
import tempfile
from imagefs import write_image

STARTUP_SIG = 0x00FF7EEB
STARTUP_HDR_SIZE = 256

# startup_header.flags1 bits
STARTUP_HDR_FLAGS1_VIRTUAL = 0x01
STARTUP_HDR_FLAGS1_BIGENDIAN = 0x02
STARTUP_HDR_FLAGS1_COMPRESS_SHIFT = 2
STARTUP_HDR_FLAGS1_COMPRESS_MASK = 0x0C

COMPRESS_NONE = 0x00
COMPRESS_ZLIB = 0x04
COMPRESS_LZO = 0x08
COMPRESS_UCL = 0x0C

STARTUP_HDR_MACHINE_I386 = 3
STARTUP_HDR_DEFAULT_STARTUP_VADDR = 0x100000  # Reasonable default

def _checksum_words(data: bytes) -> int:
    """Sum of little-endian u32 words, negated."""
    n = len(data) // 4
    total = 0
    # manual loop for speed not needed
    for i in range(n):
        total += struct.unpack_from('<I', data, i * 4)[0]
    return (-(total & 0xFFFFFFFF)) & 0xFFFFFFFF

def _ucl_compress(data: bytes, block_size: int = 0xfa00) -> bytes:
    """
    Compress data using UCL NRV2B with QNX IFS block framing.
    
    The format is: [len:u16 BE][nrv2b block]...[0x0000 terminator]
    Each block is <= 65535 bytes decompressed.
    """
    if len(data) == 0:
        return b''
    
    # Use a simple greedy compressor or fallback to storing uncompressed
    # For now, we use a basic approach: if compression ratio is good, use it
    
    # Simple dictionary compression for NRV2B-like output
    from ucl_nrv2b import NRV2BCompress
    
    # Process in blocks, up to 64KB each for IFS compatibility
    pos = 0
    output = bytearray()
    data_len = len(data)
    
    comp = NRV2BCompress(max_offset=0xd00)
    
    while pos < data_len:
        chunk_len = min(block_size, data_len - pos)
        
        # Compress this block
        compressed = comp.compress_block(data, pos, chunk_len)
        
        # Only use compressed if it's actually smaller
        if len(compressed) < chunk_len:
            blen = len(compressed)
            output.extend(struct.pack('>H', blen))
            output.extend(compressed)
        else:
            # Store uncompressed (raw)
            blen = chunk_len
            output.extend(struct.pack('>H', blen))
            output.extend(data[pos:pos + chunk_len])
        
        pos += chunk_len
    
    # Terminate block stream
    output.extend(b'\x00\x00')
    
    return bytes(output)

def build_startup_header(flags1: int, flags2: int, machine: int,
                        startup_vaddr: int, paddr_bias: int, image_paddr: int,
                        ram_paddr: int, ram_size: int, startup_size: int,
                        stored_size: int, imagefs_paddr: int, imagefs_size: int,
                        preboot_size: int) -> bytes:
    """Return 256-byte startup header."""
    header = struct.pack(
        '<I H B B H H I I I I I I I I I H',
        STARTUP_SIG,
        1,                     # version
        flags1,
        flags2,
        STARTUP_HDR_SIZE,      # header_size
        machine,
        startup_vaddr,
        paddr_bias,
        image_paddr,
        ram_paddr,
        ram_size,
        startup_size,
        stored_size,
        imagefs_paddr,
        imagefs_size,
        preboot_size,
    )
    # Pad to 256 bytes
    if len(header) != 50:
        raise RuntimeError('header size mismatch')
    header += b'\x00' * (STARTUP_HDR_SIZE - len(header))
    # zero_ok fields at +52..+64
    # already zero
    return header

def write_ifs(entries, output_path: pathlib.Path,
              compression: int = COMPRESS_NONE,
              preboot_data: bytes = b'',
              mountpoint: str = '/'):
    """
    Build an IFS with startup header wrapping an imagefs.
    entries: iterable of (target, source_path_or_None, is_dir) as used by mkifs.py
    compression: COMPRESS_NONE, COMPRESS_ZLIB currently supported.
    """
    # Build imagefs in memory via temporary file
    with tempfile.TemporaryDirectory() as td:
        tmp_img = pathlib.Path(td) / 'image.fs'
        write_image(entries, tmp_img, mountpoint=mountpoint)
        imagefs_bytes = tmp_img.read_bytes()

    # Minimal startup layout: header + trailer, no startup code
    startup_size = STARTUP_HDR_SIZE + 4  # header + trailer

    flags1 = STARTUP_HDR_FLAGS1_VIRTUAL
    if compression != COMPRESS_NONE:
        flags1 |= compression
    flags2 = 0

    # Prepare payload
    if compression == COMPRESS_NONE:
        payload = imagefs_bytes
        file_end_trailer = b''
        stored_size = len(preboot_data) + startup_size + len(payload) - len(preboot_data)
    elif compression == COMPRESS_ZLIB:
        import zlib
        payload = zlib.compress(imagefs_bytes)
        # file-end trailer = -sum(u32 words of compressed stream)
        file_end_trailer = struct.pack('<I', _checksum_words(payload))
        stored_size = startup_size + len(payload) + 4
    elif compression == COMPRESS_LZO:
        # LZO compression using system lib (via subprocess) or pure Python implementation
        # For now, use the lzo1x module's compressor if available
        try:
            from lzo1x import lzo1x_compress
            payload = lzo1x_compress(imagefs_bytes)
        except ImportError:
            # Fallback to uncompressed
            payload = imagefs_bytes
        file_end_trailer = struct.pack('<I', _checksum_words(payload))
        stored_size = startup_size + len(payload) + 4
    elif compression == COMPRESS_UCL:
        # UCL NRV2B compression
        from ucl_nrv2b import nrv2b_decompress_block_stream
        payload = _ucl_compress(imagefs_bytes)
        file_end_trailer = struct.pack('<I', _checksum_words(payload))
        stored_size = startup_size + len(payload) + 4
    else:
        raise ValueError('unsupported compression')

    stored_size = len(preboot_data) + startup_size + len(payload) + len(file_end_trailer) - len(preboot_data)

    header = build_startup_header(
        flags1=flags1,
        flags2=flags2,
        machine=STARTUP_HDR_MACHINE_I386,
        startup_vaddr=0,
        paddr_bias=0,
        image_paddr=0,
        ram_paddr=0,
        ram_size=0,
        startup_size=startup_size,
        stored_size=stored_size,
        imagefs_paddr=0,
        imagefs_size=len(imagefs_bytes),
        preboot_size=len(preboot_data),
    )
    # Startup trailer checksum over [spos, spos+startup_size-4)
    startup_region = header[:STARTUP_HDR_SIZE]
    trailer = struct.pack('<I', _checksum_words(startup_region))

    with open(output_path, 'wb') as f:
        if preboot_data:
            f.write(preboot_data)
        f.write(header)
        f.write(trailer)
        f.write(payload)
        if file_end_trailer:
            f.write(file_end_trailer)
    # print(f"Wrote IFS to {output_path}")

def write_empty_ifs(output_path: pathlib.Path):
    """Legacy stub kept for compatibility."""
    write_ifs([], output_path)
    print(f"Wrote empty IFS to {output_path}")