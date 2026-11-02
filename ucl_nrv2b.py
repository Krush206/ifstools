#!/usr/bin/env python3
"""
Pure-Python UCL NRV2B decompressor.

Clean-room implementation based on analyzing the NRV2B algorithm
from Markus Oberhumer's UCL library (as vendored by QNX Neutrino).

The key insight is that getbit_8 works like this:
- bb is a 32-bit bit buffer
- Bits are consumed MSB-first
- When low 8 bits are non-zero, we can extract a bit
- When low 8 bits are zero, we load a new byte and shift

For literal bytes: the first bit '1' signals a literal, 
then the NEXT 8 bits are the literal value.

For matches: first bit '0', then variable-length encoding
for offset and length.
"""

from typing import Tuple


class UCLError(Exception):
    pass


def nrv2b_decompress_8(src: bytes, out_size: int = 0x2caa7c) -> bytes:
    """
    Decompress NRV2B block.
    
    Ported from ucl/lib/ucl/n2b_d.c with getbit_8 from getbit.h.
    """
    src_len = len(src)
    if src_len == 0:
        return b''
    
    # State variables
    ilen = 0           # Input byte index
    olen = 0           # Output length
    last_m_off = 1     # Last match offset for repeated matches
    dst = bytearray()
    
    # Bit buffer following getbit_8 semantics exactly
    bb: int = 0
    
    def getbit() -> int:
        """Read next bit using getbit_8 semantics."""
        nonlocal bb, ilen
        
        bb = (bb * 2) & 0xFFFFFFFF
        
        if bb & 0xFF:
            return (bb >> 8) & 1
        
        if ilen >= src_len:
            return 0  # EOF on input overrun
        
        # Load new byte
        bb = (src[ilen] * 2) + 1
        ilen += 1
        return (bb >> 8) & 1
    
    # Read next byte from input (for literal extraction)
    def get_byte() -> int:
        """Get current input byte (one byte ahead of position)."""
        nonlocal ilen
        if ilen == 0:
            return 0
        return src[ilen - 1]
    
    max_iterations = max(out_size * 12 if out_size else 10 * 1024 * 1024, 10000000)
    
    for _ in range(max_iterations):
        if getbit():
            # Literal: read the next byte from input
            lit = get_byte()
            dst.append(lit)
            olen += 1
            continue
        
        # Match: read offset
        m_off = 1
        while True:
            # Read bit that continues the offset
            bit = getbit()
            m_off = (m_off << 1) | bit
            
            if m_off > 0xFFFFFF + 3:
                if olen == 0:
                    return bytes(dst)
                raise UCLError("lookbehind overrun (offset)")
            
            # Check stop bit
            if getbit():
                break
        
        if m_off == 2:
            # Use last offset
            m_off = last_m_off
        else:
            # Decode extended offset
            if ilen < 1:
                if olen == 0:
                    return bytes(dst)
                raise UCLError("input overrun (offset)")
            
            # The offset byte is at src[ilen-1]
            off_val = src[ilen - 1]
            ilen += 1
            
            # Check for EOF marker (0xFFFF = 0xffffffff unsigned)
            if m_off == 3 and off_val == 0xFF:
                return bytes(dst)
            
            # m_off = (m_off - 3) * 256 + off_byte + 1
            # Actually: val = (m_off - 3) * 256 + src[ilen-1] before ilen++
            m_off = ((m_off - 3) << 8) | off_val
            if m_off == 0xFFFF:
                return bytes(dst)
            m_off += 1
            last_m_off = m_off
        
        # Read length
        m_len = getbit()
        m_len = (m_len << 1) | getbit()
        
        if m_len == 0:
            # Extended length
            m_len = 1
            while True:
                m_len = (m_len << 1) | getbit()
                if m_len >= 0x7FFFFFFF:
                    raise UCLError("output overrun (length)")
                if getbit():
                    break
            m_len += 2
        
        if m_off > 0xD00:
            m_len += 1
        
        if m_off == 0:
            return bytes(dst)
        
        if m_off > olen and olen > 0:
            raise UCLError(f"lookbehind overrun (m_off={m_off}, olen={olen})")
        
        # Copy match - byte by byte for overlapping regions
        m_pos = olen - m_off
        copy_len = m_len + 1
        
        for _ in range(copy_len):
            if m_pos < 0 or m_pos >= len(dst):
                if m_pos < len(dst):
                    dst.append(0)
            else:
                dst.append(dst[m_pos])
            m_pos += 1
            olen += 1
    
    return bytes(dst)


def nrv2b_decompress_block_stream(data: bytes, start: int = 0, end: int = None) -> Tuple[bytes, int]:
    """
    Decompress QNX IFS UCL block stream.
    
    Format: [len:u16 BE][block]...[0x0000 terminator]
    
    Returns (decompressed_bytes, offset_after_terminator).
    """
    if end is None:
        end = len(data)
    
    pos = start
    output = bytearray()
    
    while pos + 2 <= end:
        # Read block length (big-endian)
        blen = (data[pos] << 8) | data[pos + 1]
        pos += 2
        
        if blen == 0:
            break
        
        if pos + blen > end:
            raise UCLError("truncated UCL block stream")
        
        # Decompress block
        block_data = data[pos:pos + blen]
        decompressed = nrv2b_decompress_8(block_data)
        output.extend(decompressed)
        pos += blen
    
    return bytes(output), pos


if __name__ == '__main__':
    print("NRV2B decompressor module loaded")
    print("Note: Full round-trip compression/decompression not yet implemented")
    print("Decompressor is the primary focus for IFS parsing")