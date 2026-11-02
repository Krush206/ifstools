#!/usr/bin/env python3
"""
Pure-Python UCL NRV2B compressor and decompressor.

Implementation based on UCL library v0.90 (Markus Oberhumer).
Clean-room implementation following:
- trunk/lib/ucl/n2b_d.c (decompressor)
- trunk/lib/ucl/getbit.h (getbit_8 macro)
- trunk/lib/ucl/n2b_99.c (compressor)

NRV2B encoding (bit-buffer approach):
- Bits are accumulated in a 16-bit buffer, flushed MSB-first to output
- For literals: 1 bit '1' flag + 8 bits value
- For matches: 0 bit + variable offset + variable length

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
    """
    if not src:
        return b''
    
    src_len = len(src)
    ilen = 0
    olen = 0
    last_m_off = 1
    dst = bytearray()
    bb = 0
    oend = out_size or (len(src) * 12 + 10000)
    
    def getbit() -> int:
        nonlocal bb, ilen
        bb = (bb * 2) & 0xFFFF
        if bb & 0xFF:
            return (bb >> 8) & 1
        if ilen >= src_len:
            return 0
        byte_val = src[ilen]
        ilen += 1
        bb = ((byte_val * 2) | 1) & 0xFFFF
        return (bb >> 8) & 1
    
    for _ in range(oend + 10000):
        if getbit():
            # Literal: next byte from input is the value
            if ilen >= src_len:
                break
            lit = src[ilen]
            ilen += 1
            dst.append(lit)
            olen += 1
            continue
        
        # Match
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
                raise UCLError("input overrun")
            off_byte = src[ilen]
            ilen += 1
            if m_off == 3 and off_byte == 0xFF:
                return bytes(dst)
            m_off = (m_off - 3) * 256 + off_byte
            if m_off == 0xFFFFFFFF:
                return bytes(dst)
            m_off += 1
            last_m_off = m_off
        
        m_len = getbit()
        m_len = m_len * 2 + getbit()
        
        if m_len == 0:
            m_len = 1
            while True:
                m_len = m_len * 2 + getbit()
                if getbit():
                    break
            m_len += 2
        
        if m_off > 0xD00:
            m_len += 1
        
        if m_off > olen:
            if olen == 0:
                break
            raise UCLError(f"lookbehind: {m_off} > {olen}")
        
        m_pos = olen - m_off
        clen = m_len + 1
        for _ in range(clen):
            dst.append(dst[m_pos])
            m_pos += 1
            olen += 1
    
    return bytes(dst)


def nrv2b_decompress_block_stream(data: bytes, start: int = 0,
                                   end: int = None) -> Tuple[bytes, int]:
    """Decompress QNX IFS UCL block stream."""
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


class BitBuffer:
    """MSB-first bit buffer for NRV2B encoding (like bbPutBit in UCL)."""
    
    def __init__(self, bitsize: int = 8):
        self.bb = 0          # Bit buffer
        self.nbits = 0       # Bits in buffer
        self.output = bytearray()
        self.bitsize = bitsize
    
    def put_bit(self, bit: int):
        """Put one bit into buffer."""
        self.bb = (self.bb << 1) | (bit & 1)
        self.nbits += 1
        
        while self.nbits >= self.bitsize:
            self.nbits -= self.bitsize
            self.output.append((self.bb >> self.nbits) & 0xFF)
    
    def put_byte(self, byte_val: int):
        """Put a complete byte to output (used for literal values in NRV2B)."""
        self.flush()  # Flush any pending bits first
        self.output.append(byte_val & 0xFF)
    
    def flush(self, pad: int = 0):
        """Flush remaining bits with padding."""
        if self.nbits > 0:
            if self.nbits < self.bitsize:
                pad_bits = self.bitsize - self.nbits
                self.bb = (self.bb << pad_bits) | ((1 << pad_bits) - 1)
            self.output.append(self.bb & 0xFF)
            self.nbits = 0
    
    def get_bytes(self) -> bytes:
        self.flush()
        return bytes(self.output)


def nrv2b_compress(src: bytes, block_size: int = 64 * 1024) -> bytes:
    """
    Compress data using NRV2B with QNX IFS block framing.
    
    Returns: [BE16 len][block]...[0x0000]
    """
    if not src:
        return b'\x00\x00'
    
    output = bytearray()
    pos = 0
    
    while pos < len(src):
        chunk = src[pos:min(pos + block_size, len(src))]
        compressed = _compress_literals(chunk)
        
        output.extend(struct.pack('>H', len(compressed)))
        output.extend(compressed)
        pos += len(chunk)
    
    output.extend(b'\x00\x00')
    return bytes(output)


def _compress_literals(data: bytes) -> bytes:
    """
    Encode data as literal stream.
    
    Each literal uses 1 bit '1' + 8 bit value.
    Output uses 8-bit bit buffer (MSB-first).
    """
    if not data:
        return b''
    
    bb = BitBuffer(8)
    
    for byte_val in data:
        # Literal: 1 bit flag + 8 bits value
        bb.put_bit(1)
        bb.put_byte(byte_val)
    
    return bb.get_bytes()


if __name__ == '__main__':
    import sys
    
    # Test round-trip
    test_data = b"Hello, QNX World!"
    
    print(f"Input: {test_data}")
    
    compressed = nrv2b_compress(test_data)
    print(f"Compressed: {len(compressed)-2} bytes")
    
    # Skip BE16 length prefix for decompression
    decompressed = nrv2b_decompress_8(compressed[2:])
    print(f"Decompressed: {decompressed}")
    
    if test_data == decompressed:
        print("PASS: Round-trip works!")
    else:
        print("FAIL: Data mismatch")
        sys.exit(1)