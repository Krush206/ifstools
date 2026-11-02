#!/usr/bin/env python3
"""
Pure-Python LZO1X decompressor.

Port of the LZO1X decompression algorithm from lzo1x_d.ch
(Markus F.X.J. Oberhumer's LZO data compression library, as vendored in
openQNX trunk/lib/lzo). Handles the LZO1X variant (not LZO1Y/LZO1Z),
little-endian, no dictionary.

Also provides the QNX IFS block-stream framing used by mkifs/dumpifs:
    [len:u16 big-endian][lzo1x block]... [len == 0x0000 terminator]
"""


class LZOError(Exception):
    pass


M2_MAX_OFFSET = 0x0800


def lzo1x_decompress(src: bytes) -> bytes:
    """Decompress a single LZO1X block. Raises LZOError on malformed input."""
    src = bytes(src)
    n = len(src)
    ip = 0
    out = bytearray()

    def need_ip(k):
        if ip + k > n:
            raise LZOError("input overrun")

    def copy_match(m_pos: int, count: int):
        # Overlapping copies must be done byte-by-byte (RLE-style).
        if m_pos < 0:
            raise LZOError("lookbehind overrun")
        base = len(out) - (len(out) - m_pos)  # absolute index of m_pos in out
        for i in range(count):
            out.append(out[m_pos + i])

    # --- initial literal run -------------------------------------------------
    need_ip(1)
    if src[ip] > 17:
        t = src[ip] - 17
        ip += 1
        if t < 4:
            # match_next: copy t literals then read opcode -> match
            out += src[ip:ip + t]
            ip += t
            need_ip(1)
            t = src[ip]
            ip += 1
            state = 'match'
        else:
            out += src[ip:ip + t]
            ip += t
            state = 'first_literal_run'
    else:
        state = 'main'

    while True:
        if state == 'main':
            need_ip(1)
            t = src[ip]
            ip += 1
            if t >= 16:
                state = 'match'
            else:
                # literal run
                if t == 0:
                    need_ip(1)
                    while src[ip] == 0:
                        t += 255
                        ip += 1
                        need_ip(1)
                    t += 15 + src[ip]
                    ip += 1
                need_ip(t + 4)
                out += src[ip:ip + t + 3]
                ip += t + 3
                state = 'first_literal_run'

        if state == 'first_literal_run':
            need_ip(1)
            t = src[ip]
            ip += 1
            if t >= 16:
                state = 'match'
            else:
                # M2-style match with fixed far offset
                need_ip(1)
                off = 1 + M2_MAX_OFFSET + (t >> 2) + (src[ip] << 2)
                ip += 1
                if off > len(out):
                    raise LZOError("lookbehind overrun")
                copy_match(len(out) - off, 3)
                # match_done: t = opcode & 3 (ip[-2] is this opcode)
                t = t & 3
                if t == 0:
                    state = 'main'
                else:
                    need_ip(t)
                    out += src[ip:ip + t]
                    ip += t
                    need_ip(1)
                    t = src[ip]
                    ip += 1
                    state = 'match'
            if state != 'match':
                continue

        if state == 'match':
            # one match iteration; loops internally like the C inner loop
            while True:
                if t >= 64:
                    # M2 match
                    need_ip(1)
                    off = 1 + ((t >> 2) & 7) + (src[ip] << 3)
                    ip += 1
                    mlen = (t >> 5) - 1 + 2  # copy t+2 where t=(op>>5)-1
                    if off > len(out):
                        raise LZOError("lookbehind overrun")
                    copy_match(len(out) - off, mlen)
                    t_extra = t & 3
                elif t >= 32:
                    # M3 match
                    tt = t & 31
                    if tt == 0:
                        need_ip(1)
                        while src[ip] == 0:
                            tt += 255
                            ip += 1
                            need_ip(1)
                        tt += 31 + src[ip]
                        ip += 1
                    need_ip(2)
                    off = 1 + (src[ip] >> 2) + (src[ip + 1] << 6)
                    ip += 2
                    mlen = tt + 2
                    if off > len(out):
                        raise LZOError("lookbehind overrun")
                    copy_match(len(out) - off, mlen)
                    t_extra = src[ip - 2] & 3
                elif t >= 16:
                    # M4 match
                    base_off = (t & 8) << 11
                    tt = t & 7
                    if tt == 0:
                        need_ip(1)
                        while src[ip] == 0:
                            tt += 255
                            ip += 1
                            need_ip(1)
                        tt += 7 + src[ip]
                        ip += 1
                    need_ip(2)
                    extra = (src[ip] >> 2) + (src[ip + 1] << 6)
                    ip += 2
                    if base_off == 0 and extra == 0:
                        # EOF marker
                        return bytes(out)
                    off = base_off + extra + 0x4000
                    mlen = tt + 2
                    if off > len(out):
                        raise LZOError("lookbehind overrun")
                    copy_match(len(out) - off, mlen)
                    t_extra = src[ip - 2] & 3
                else:
                    # M1 match
                    need_ip(1)
                    off = 1 + (t >> 2) + (src[ip] << 2)
                    ip += 1
                    if off > len(out):
                        raise LZOError("lookbehind overrun")
                    copy_match(len(out) - off, 2)
                    t_extra = t & 3

                # match_done
                if t_extra == 0:
                    state = 'main'
                    break
                # match_next
                need_ip(t_extra)
                out += src[ip:ip + t_extra]
                ip += t_extra
                need_ip(1)
                t = src[ip]
                ip += 1
                # loop back to match with new t

        # state == 'main' loops again


def lzo_decompress_block_stream(data: bytes, start: int = 0, end: int = None):
    """Decompress the QNX IFS LZO block stream.

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
            raise LZOError("truncated LZO block stream")
        out += lzo1x_decompress(data[pos:pos + blen])
        pos += blen
    return bytes(out), pos
