#!/usr/bin/env python3
"""
Pure-Python UCL NRV2B decompressor (bit-buffer variant 8).

Clean-room port of the NRV2B decompression algorithm from the openQNX tree
(trunk/lib/ucl/n2b_d.c + getbit.h, originally Markus F.X.J. Oberhumer's UCL
library as vendored by QNX).  Handles the getbit_8 bit buffer: bits are read
MSB-first, one byte is loaded whenever the 8 buffered bits are exhausted.

Also provides the QNX IFS block-stream framing used by mkifs/dumpifs for
UCL (and LZO) compressed images:

    [len:u16 big-endian][nrv2b block]... [len == 0x0000 terminator]
"""


class UCLError(Exception):
    pass


def nrv2b_decompress_8(src: bytes) -> bytes:
    """Decompress a single NRV2B block (ucl_nrv2b_decompress_8 semantics).

    Returns the decompressed bytes.  Raises UCLError on malformed input.
    """
    src = bytes(src)
    src_len = len(src)
    ilen = 0
    olen = 0
    last_m_off = 1
    bb = 0
    dst = bytearray()

    def getbit() -> int:
        nonlocal bb, ilen
        bb = (bb * 2) & 0xFFFFFFFF
        if bb & 0xFF:
            return (bb >> 8) & 1
        if ilen >= src_len:
            raise UCLError("input overrun (bit buffer)")
        bb = src[ilen] * 2 + 1
        ilen += 1
        return (bb >> 8) & 1

    while True:
        if getbit():
            # literal
            if ilen >= src_len:
                raise UCLError("input overrun (literal)")
            dst.append(src[ilen])
            ilen += 1
            olen += 1
            continue

        # match: read m_off as (data bit, stop bit) pairs, starting from 1
        m_off = 1
        while True:
            m_off = m_off * 2 + getbit()
            if m_off > 0xFFFFFF + 3:
                raise UCLError("lookbehind overrun (offset)")
            if getbit():
                break

        if m_off == 2:
            m_off = last_m_off
        else:
            if ilen >= src_len:
                raise UCLError("input overrun (offset byte)")
            val = (m_off - 3) * 256 + src[ilen]
            ilen += 1
            if val == 0xFFFFFFFF:
                # EOF marker
                break
            m_off = val + 1
            last_m_off = m_off

        # length
        m_len = getbit()
        m_len = m_len * 2 + getbit()
        if m_len == 0:
            m_len = 1
            while True:
                m_len = m_len * 2 + getbit()
                if m_len >= 0xFFFFFFFF:
                    raise UCLError("output overrun (length)")
                if getbit():
                    break
            m_len += 2
        if m_off > 0xD00:
            m_len += 1

        if m_off > olen:
            raise UCLError("lookbehind overrun (copy)")

        # copy m_len + 1 bytes (overlapping-safe: byte-by-byte)
        m_pos = olen - m_off
        dst.append(dst[m_pos])
        m_pos += 1
        olen += 1
        while True:
            dst.append(dst[m_pos])
            m_pos += 1
            olen += 1
            m_len -= 1
            if m_len <= 0:
                break

    return bytes(dst)


def ucl_decompress_block_stream(data: bytes, start: int = 0, end: int = None):
    """Decompress a QNX IFS UCL block stream.

    Format: [len:u16 BE][block]... [0x0000]
    Returns (decompressed_bytes, offset_after_terminator).
    """
    if end is None:
        end = len(data)
    pos = start
    out = bytearray()
    while pos + 2 <= end:
        blen = (data[pos] << 8) | data[pos + 1]
        pos += 2
        if blen == 0:
            break
        if pos + blen > end:
            raise UCLError("truncated UCL block stream")
        out += nrv2b_decompress_8(data[pos:pos + blen])
        pos += blen
    return bytes(out), pos
