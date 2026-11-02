#!/usr/bin/env python3
"""
Pure-Python UCL NRV2B compressor and decompressor.

Implementation based on UCL library v0.90 (Markus Oberhumer).
Clean-room port of the NRV2B algorithm.

NRV2B encoding using getbit_8 bit buffer:
    #define getbit_8(bb, src, ilen) \
        (bb *= 2, bb & 0xff ? (bb >> 8) & 1 : ((bb = src[ilen++] * 2 + 1) >> 8) & 1)

For literals: getbit() returns 1, then src[ilen++] is the literal value
              (the byte AFTER the one used for bit extraction)

Block stream format (QNX IFS):
    [len:u16 BE][nrv2b_block]...[0x0000 terminator]
"""

import struct
from typing import Tuple

M2_MAX_OFFSET = 0xd00


class UCLError(Exception):
    pass


def nrv2b_decompress_8(src: bytes, out_size: int = 0) -> bytes:
    """
    Decompress NRV2B block using getbit_8 bit-buffer semantics.
    
    Ported from ucl/lib/ucl/n2b_d.c.
    Key insight: when getbit returns 1 (literal), the NEXT byte is the literal.
    """
    if not src:
        return b''
    
    src_len = len(src)
    ilen = 0           # Input byte index
    olen = 0           # Output length
    last_m_off = 1     # Previous match offset for m_off==2
    dst = bytearray()
    bb = 0             # 16-bit bit buffer
    oend = out_size or (len(src) * 12 + 10000)
    
    def getbit() -> int:
        """
        Port of getbit_8 macro.
        Returns 0 or 1. Modifies: bb, ilen.
        """
        nonlocal bb, ilen
        
        bb = (bb * 2) & 0xFFFF
        if bb & 0xFF:
            return (bb >> 8) & 1
        
        if ilen >= src_len:
            return 0  # EOF
        
        byte_val = src[ilen]
        ilen += 1
        bb = ((byte_val * 2) | 1) & 0xFFFF
        return (bb >> 8) & 1
    
    # Main decompression loop (from n2b_d.c)
    for _ in range(oend + 10000):
        if getbit():
            # Literal: read NEXT byte (src[ilen] after getbit's increment)
            if ilen >= src_len:
                break
            lit = src[ilen]
            ilen += 1
            dst.append(lit)
            olen += 1
            continue
        
        # Match: decode offset
        m_off = 1
        while True:
            m_off = m_off * 2 + getbit()
            if m_off > 0xFFFFFF + 3:
                if olen == 0:
                    return bytes(dst)
                raise UCLError("lookbehind overrun (offset)")
            if getbit():
                break
        
        if m_off == 2:
            m_off = last_m_off
        else:
            if ilen >= src_len:
                if olen == 0:
                    return bytes(dst)
                raise UCLError("input overrun (offset)")
            
            off_byte = src[ilen]
            ilen += 1
            
            if m_off == 3 and off_byte == 0xFF:
                return bytes(dst)  # EOF marker
            
            m_off = (m_off - 3) * 256 + off_byte
            if m_off == 0xFFFFFFFF:
                return bytes(dst)
            m_off += 1
            last_m_off = m_off
        
        # Decode length
        m_len = getbit()
        m_len = m_len * 2 + getbit()
        
        if m_len == 0:
            m_len = 1
            while True:
                m_len = m_len * 2 + getbit()
                if m_len >= 0x7FFFFFFF:
                    raise UCLError("output overrun")
                if getbit():
                    break
            m_len += 2
        
        if m_off > M2_MAX_OFFSET:
            m_len += 1
        
        if m_off > olen and olen > 0:
            raise UCLError(f"lookbehind: {m_off} > {olen}")
        
        if m_off > olen:
            break  # Can happen at stream start
        
        # Copy match
        m_pos = olen - m_off
        clen = m_len + 1
        
        for _ in range(clen):
            dst.append(dst[m_pos])
            m_pos += 1
            olen += 1
    
    return bytes(dst)


def nrv2b_decompress_block_stream(data: bytes, start: int = 0, 
                                   end: int = None) -> Tuple[bytes, int]:
    """
    Decompress QNX IFS UCL block stream.
    
    Format: [len:u16 BE][block]...[0x0000]
    Returns (decompressed_bytes, next_position).
    """
    if end is None:
        end = len(data)
    
    pos = start
    output = bytearray()
    
    while pos + 2 <= end:
        blen = (data[pos] << 8) | data[pos + 1]
        pos += 2
        
        if blen == 0:
            break
        
        if pos + blen > end:
            raise UCLError("truncated block stream")
        
        output.extend(nrv2b_decompress_8(data[pos:pos + blen]))
        pos += blen
    
    return bytes(output), pos


if __name__ == '__main__':
    # Test with qnxbase.ifs first block
    with open('/opt/openqnx/qnxbase.ifs', 'rb') as f:
        data = f.read()
    
    # First block at 0x138e8 + 2
    block_pos = 0x138e8 + 2
    blen = (data[0x138e8] << 8) | data[0x138e8 + 1]
    block_data = data[block_pos:block_pos + blen]
    
    print(f"First block: length={blen}")
    print(f"First 16 bytes (hex): {block_data[:16].hex()}")
    
    result = nrv2b_decompress_8(block_data)
    print(f"Decompressed: {len(result)} bytes")
    print(f"First 20 bytes: {result[:20]}")
    
    if result[:7] == b'imagefs':
        print("✓ Starts with 'imagefs'")
        
        import struct as st
        img_size = st.unpack('<I', result[8:12])[0]
        print(f"Image size from header: {img_size}")
        
        # Now decompress full stream
        img_stream_start = 0x138e8
        stored_size = 0x14D428
        
        compressed = data[img_stream_start:img_stream_start + stored_size - 0x3F8]
        decompressed, _ = nrv2b_decompress_block_stream(data[img_stream_start:])
        
        print(f"\nFull stream decompressed: {len(decompressed)} bytes")