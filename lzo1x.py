#!/usr/bin/env python3
"""
Pure-Python LZO1X compressor and decompressor.

Port of the LZO1X decompression algorithm from lzo1x_d.ch
(Markus F.X.J. Oberhumer's LZO data compression library, as vendored in
openQNX trunk/lib/lzo). Handles the LZO1X variant (not LZO1Y/LZO1Z),
little-endian, no dictionary.

Also provides the QNX IFS block-stream framing used by mkifs/dumpifs:
    [len:u16 big-endian][lzo1x block]... [len == 0x0000 terminator]
"""

import struct


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
        if m_pos < 0:
            raise LZOError("lookbehind overrun")
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
            state = 'main'
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
                # literal run in main state: t = 0..15 means t+3 = 3..18 literals
                if t == 0:
                    # extended literal run
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
                    continue
                else:
                    # Normal literal run: t+3 literals
                    need_ip(t + 3)
                    out += src[ip:ip + t + 3]
                    ip += t + 3
                    continue

        if state == 'match':
            while True:
                if t >= 64:
                    # M2 match
                    need_ip(1)
                    b1 = src[ip]
                    ip += 1
                    off = 1 + ((t >> 2) & 7) + (b1 << 3)
                    mlen = (t >> 5) + 2
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
                    t_extra = 0
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
                    t_extra = 0
                else:
                    # M1 match
                    need_ip(1)
                    off = 1 + (t >> 2) + (src[ip] << 2)
                    ip += 1
                    if off > len(out):
                        raise LZOError("lookbehind overrun")
                    copy_match(len(out) - off, 2)
                    t_extra = t & 3

                # match_done / match_next
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


def lzo1x_compress(src: bytes, block_size: int = 64 * 1024) -> bytes:
    """
    Compress data using LZO1X with QNX IFS block framing.

    Returns: [BE16 len][block]...[0x0000]
    """
    if not src:
        return b'\x00\x00'

    output = bytearray()
    pos = 0

    while pos < len(src):
        chunk = src[pos:min(pos + block_size, len(src))]
        compressed = _compress_lzo1x_block(chunk)

        output.extend(struct.pack('>H', len(compressed)) if len(compressed) < 65536 else b'\xff\xff')
        output.extend(compressed)
        pos += len(chunk)

    output.extend(b'\x00\x00')
    return bytes(output)


def _compress_lzo1x_block(data: bytes) -> bytes:
    """Compress a single LZO1X block using greedy matching."""
    if not data:
        return b''

    out = bytearray()
    pos = 0
    n = len(data)

    while pos < n:
        # Find match at current position
        match_len, match_off = _find_lzo_match(data, pos, n, 0x800)

        # Check if we can encode this match
        use_match = False
        if match_len >= 2 and match_off > 0:
            if match_len == 2:
                if match_off < 64:  # M1
                    use_match = True
            else:  # match_len >= 3
                use_match = True

        if use_match:
            _encode_lzo_match(out, pos, match_len, match_off, n)
            pos += match_len
        else:
            # Count literals until next encodeable match
            lit_count = 0
            test_pos = pos
            while test_pos < n:
                ml, mo = _find_lzo_match(data, test_pos, n, 0x800)
                if ml >= 2 and mo > 0:
                    can_encode = True
                    if ml == 2 and mo >= 64:
                        can_encode = False
                    if can_encode:
                        break
                lit_count += 1
                test_pos += 1

            if len(out) == 0:
                # Initial run format: emit 1-18 literals
                lit_count = min(lit_count, 18)
                if lit_count > 0:
                    out.append(17 + lit_count)  # t-17 = lit_count
                    out.extend(data[pos:pos + lit_count])
                    pos += lit_count
                elif n - pos >= 4:
                    # Need to emit something
                    out.append(0x15)  # 4 literals
                    out.extend(data[pos:pos + 4])
                    pos += 4
            else:
                # Main state format: t=1..15 means t+3=4..18 literals
                # Emit in chunks of up to 18
                while pos < n and lit_count > 0:
                    ml, mo = _find_lzo_match(data, pos, n, 0x800)
                    try_use = False
                    if ml >= 2 and mo > 0:
                        if ml == 2:
                            if mo < 64:
                                try_use = True
                        else:
                            try_use = True
                    
                    if try_use:
                        break
             
                    # Count literals again for this chunk
                    chunk_lit_count = 0
                    test = pos
                    while test < n:
                        ml, mo = _find_lzo_match(data, test, n, 0x800)
                        if ml >= 2 and mo > 0:
                            enc = not (ml == 2 and mo >= 64)
                            if enc:
                                break
                        chunk_lit_count += 1
                        test += 1
                    chunk_lit_count = min(chunk_lit_count, 18)
                    
                    if chunk_lit_count == 0:
                        out.append(1)
                        out.extend(data[pos:pos+4])
                        pos += 4
                    elif chunk_lit_count >= 4:
                        out.append(chunk_lit_count - 3)
                        out.extend(data[pos:pos + chunk_lit_count])
                        pos += chunk_lit_count
                    else:
                        out.append(1)
                        out.extend(data[pos:pos+4])
                        pos += 4

    # Write EOF marker if not already present
    has_eof = False
    if len(out) >= 3 and out[-3:] == b'\x11\x00\x00':
        has_eof = True
    if not has_eof:
        out.extend(b'\x11\x00\x00')

    return bytes(out)


def _find_lzo_match(data: bytes, pos: int, n: int, max_off: int) -> tuple:
    """Find the best match in the sliding window for LZO."""
    if pos < 2:
        return 0, 0

    best_len = 0
    best_off = 0

    window_start = max(0, pos - max_off)

    for i in range(window_start, pos - 1):
        if i + 1 < n and pos + 1 < n and data[i] == data[pos] and data[i+1] == data[pos+1]:
            length = 2
            # Extend match while characters match
            while pos + length < n and data[i + length] == data[pos + length]:
                length += 1

            if length > best_len:
                best_len = length
                best_off = pos - i

    return best_len, best_off


def _encode_lzo_match(out: bytearray, pos: int, match_len: int, match_off: int, total_len: int):
    """Encode an LZO match."""
    # M1: match_off in [1, 63], length == 2
    if match_off < 0x40 and match_len == 2:
        off_minus_1 = match_off - 1
        op = ((off_minus_1 & 3) << 2)
        b1 = off_minus_1 >> 2
        out.append(op)
        out.append(b1)
        return

    # M2: match_off in [64, 2048], length 3-8
    if 0x40 <= match_off <= 0x800 and 3 <= match_len <= 8:
        mlen_code = match_len - 2  # 1-6 for len 3-8
        off_minus_1 = match_off - 1
        op = (mlen_code << 5) | ((off_minus_1 & 7) << 2)
        b1 = off_minus_1 >> 3
        out.append(op)
        out.append(b1)
        return

    # M3: match_off in [64, 16383], length 3-99+
    if match_off < 0x4000 and match_len >= 3:
        ml = match_len - 2
        if ml <= 31:
            op = 0x20 | ml
            out.append(op)
        else:
            op = 0x20
            out.append(op)
            ll = match_len - 33
            while ll > 255:
                out.append(0)
                ll -= 255
            out.append(ll)
        off_minus_1 = match_off - 1
        b1 = (off_minus_1 & 63) << 2
        b2 = off_minus_1 >> 6
        out.append(b1)
        out.append(b2)
        return

    # M4: match_off >= 16384, length >= 3
    base_off_bit = 1 if match_off >= 0x8000 else 0
    off_adj = match_off - 0x4000 - (base_off_bit << 11)
    ml = match_len - 2

    if ml <= 0:
        ml = 1

    if ml <= 255:
        op = 0x10 | (base_off_bit << 3) | ml
        out.append(op)
        b1 = (off_adj & 63) << 2
        b2 = off_adj >> 6
        out.append(b1)
        out.append(b2)
    else:
        op = 0x10 | (base_off_bit << 3)
        out.append(op)
        ll = match_len - 9
        while ll > 255:
            out.append(0)
            ll -= 255
        out.append(ll)
        b1 = (off_adj & 63) << 2
        b2 = off_adj >> 6
        out.append(b1)
        out.append(b2)