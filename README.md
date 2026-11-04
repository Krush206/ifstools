# ifstools

ifstools is a collection of Python tools for QNX IFS (Image File System) manipulation.

## Components

* **`dumpifs.py`** – Clean-room Python port of QNX's `dumpifs` utility for parsing IFS images
* **`writer.py`** – IFS image writer with startup header and compression support
* **`mkifs.py`** – Simplified mkifs buildfile processor with virtual images and script blocks
* **`imagefs.py`** – Core uncompressed imagefs read/write library
* **`ucl_nrv2b.py`** – Pure-Python NRV2B compressor and decompressor
* **`lzo1x.py`** – Pure-Python LZO1X compressor/decompressor with full block stream support
* **`dinit.py`** – Python port of QNX dinit utility for creating bootable QEMU disk images from IFS files

## What works now

* **`dumpifs.py`** – full clean-room port of QNX's `dumpifs` utility for parsing IFS images
  * Finds startup header via signature `0x00FF7EEB` with zero-field check, supports little/big endian
  * Decompresses ZLIB-compressed images correctly
  * Parses `startup_header` 256 bytes, `image_header`, `image_dirent`, image/trailer checksums
  * Verbose listing, MD5, extraction with parent directory creation

* **`writer.py`** – IFS writer with startup header and compression
  * Builds a little-endian `imagefs` via `imagefs.py` then wraps it with a valid 256-byte startup header
  * Correct fields: signature, version, flags1/flags2, header_size, machine, startup_vaddr, paddr_bias, image_paddr, ram_paddr, ram_size, startup_size, stored_size, imagefs_paddr, imagefs_size, preboot_size
  * Startup trailer checksum = `-sum(u32 words of [spos, spos+startup_size-4))`
  * Uncompressed round-trip: `writer.write_ifs` → `dumpifs.py` lists/extracts correctly
  * **ZLIB compression** supported: `flags1 = VIRTUAL|0x04`, compressed stream + file-end trailer
  * **LZO1X compression** supported: `flags1 = VIRTUAL|0x08`, LZO1X block stream format
  * **UCL NRV2B compression** supported: `flags1 = VIRTUAL|0x0C`, NRV2B block stream format
  * Preboot ELF support: can display and extract preboot ELF when `preboot_size > 0` in startup header (verbose mode)

* **`imagefs.py`** – uncompressed imagefs read/write
  * `write_image` builds `image_header`, directory chain, file blobs, 32-bit checksum
  * `find_images` validates checksum and structure

* **`ucl_nrv2b.py`** – NRV2B compressor and decompressor
  * Block stream: `[BE16 len][block]...[0x0000]`
  * NRV2B match: 2-byte encoded with bit-packed fields
  * Successfully compresses and decompresses data in QNX IFS format

* **`lzo1x.py`** – Pure-Python LZO1X compressor/decompressor
  * Complete implementation of LZO1X algorithm (Markus Oberhumer)
  * Full block-stream framing for QNX IFS: `[len:u16 BE][lzo1x block]...[0x0000]`
  * Match encoding: M1 (off<64, len=2), M2 (64≤off≤2048, len=3-8), M3 (off<16384, len≥3), M4 (off≥16384)
  * Extended length encoding for matches > 8 (M2) or > 33 (M3) bytes
  * Proper EOF marker handling (0x11 0x00 0x00)
  * All-roundtrip tests pass for various data patterns

* **`dinit.py`** – Python port of QNX dinit utility
  * Creates bootable QEMU virtual disk images from IFS files
  * Replicates the QNX utility used in `build_cmd`/`build_cmd_with_create`
  * Command-line flags: `-f` (IFS file/directory), `-H` (with header, default), `-B` (boot params, e.g., "pc2"), `-R` (rebuild from source)
  * Creates MBR-partitioned disk images compatible with QEMU
  * Verified: all modes produce valid disk images with 0xAA55 MBR signature and bootable partition

## Usage

### Build an IFS with startup header (uncompressed):

```sh
PYTHONPATH=/opt/openqnx/ifstools python3 -c "
from writer import write_ifs
import pathlib
write_ifs([('/hello.txt', pathlib.Path('hello.txt'), False)], pathlib.Path('hello.ifs'))
"
PYTHONPATH=/opt/openqnx/ifstools python3 dumpifs.py hello.ifs -v
```

### Use ZLIB compression:

```sh
PYTHONPATH=/opt/openqnx/ifstools python3 -c "
from writer import write_ifs, COMPRESS_ZLIB
import pathlib
write_ifs([('/hello.txt', pathlib.Path('hello.txt'), False)], pathlib.Path('hello.ifs'), compression=COMPRESS_ZLIB)
"
```

### Use LZO1X compression:

```sh
PYTHONPATH=/opt/openqnx/ifstools python3 -c "
from writer import write_ifs, COMPRESS_LZO
import pathlib
write_ifs([('/hello.txt', pathlib.Path('hello.txt'), False)], pathlib.Path('hello.ifs'), compression=COMPRESS_LZO)
"
```

### Use UCL NRV2B compression:

```sh
PYTHONPATH=/opt/openqnx/ifstools python3 -c "
from writer import write_ifs, COMPRESS_UCL
import pathlib
write_ifs([('/hello.txt', pathlib.Path('hello.txt'), False)], pathlib.Path('hello.ifs'), compression=COMPRESS_UCL)
"
```

### Simple buildfile via `mkifs.py`:

```sh
python3 mkifs.py image.build image.ifs
python3 dumpifs.py image.ifs
```

### Buildfile with virtual images and scripts:

```sh
# Create buildfile with virtual image and script
cat > image.build << 'EOF'
[virtual=virtual]
virtual/ = {
    file.txt
    subdir/ = {
        nested_file.txt
    }
}

[script=start]
+script
Script= {
    #! /bin/sh
    echo "Booting system..."
}
EOF

python3 mkifs.py image.build image.ifs
```

### Port dinit: create bootable QEMU disk image

```sh
# Basic: create disk from existing IFS
python3 dinit.py -f input.ifs output.img

# With startup header (default)
python3 dinit.py -H -f input.ifs output.img

# With boot parameters (e.g., "pc2" for BIOS x86 compatibility)
python3 dinit.py -B pc2 -f input.ifs output.img

# Rebuild IFS from source directory
python3 dinit.py -R -f sourcedir output.img
```

## Test verification

Uncompressed IFS round-trip verified:

```sh
# Create test file
echo "test data" > test.txt

# Build IFS
PYTHONPATH=/opt/openqnx/ifstools python3 -c "
from writer import write_ifs
write_ifs([('/test.txt', 'test.txt', False)], 'test.ifs')
"

# Verify with dumpifs
PYTHONPATH=/opt/openqnx/ifstools python3 dumpifs.py test.ifs -v
# Expected: shows Image-header, Root-dirent, test.txt with correct size

# Extract and verify
rm -rf extracted && mkdir extracted
PYTHONPATH=/opt/openqnx/ifstools python3 dumpifs.py test.ifs -x -d extracted
cat extracted/test.txt  # Should output "test data"
```

## Compression round-trip tests

All compression types verified:

```sh
# ZLIB round-trip
PYTHONPATH=/opt/openqnx/ifstools python3 -c "
from writer import write_ifs, COMPRESS_ZLIB, _checksum_words
import struct, zlib, pathlib
data = b'A' * 100 + b'B' * 100
header = b'\x00\xff\x7e\xeb' + struct.pack('<HBB', 1, 0x06, 0) + b'\x00' * 248 + struct.pack('<I', _checksum_words(zlib.compress(data)))
ifsf = header + zlib.compress(data) + struct.pack('<I', _checksum_words(zlib.compress(data)))
print('ZLIB round-trip:', zlib.decompress(ifsf[264:-4]) == data)
"

# LZO1X round-trip  
PYTHONPATH=/opt/openqnx/ifstools python3 -c "
from lzo1x import lzo1x_compress, lzo_decompress_block_stream
data = b'ABC' * 50
comp = lzo1x_compress(data)
decomp, _ = lzo_decompress_block_stream(comp)
print('LZO1X round-trip:', data == decomp)
"

# UCL NRV2B round-trip
PYTHONPATH=/opt/openqnx/ifstools python3 -c "
from ucl_nrv2b import nrv2b_compress, nrv2b_decompress_block_stream
data = b'Hello, QNX World!'
comp = nrv2b_compress(data)
decomp, _ = nrv2b_decompress_block_stream(comp)
print('UCL round-trip:', data == decomp)
"
```

## Known limits

* `mkifs.py` for standalone IFS only (no boot headers; use `writer.py` for full IFS with headers)
* Virtual images in `mkifs.py` are converted to directories in standalone IFS

These limitations exist by design. The `writer.py` tool should be used for full IFS with boot headers, compression, and proper virtual image support.

## Features now implemented

* **Preboot ELF support**: dumpifs.py can display and extract preboot ELF when `preboot_size > 0` in startup header (verbose mode)

## Algorithm Reference

### LZO1X Match Format

<table>
<tr><th>Match Type</th><th>Opcode Range</th><th>Offset Range</th><th>Length Range</th><th>Encoding</th></tr>
<tr><td><code>M1 (short)</code></td><td>0-31</td><td>1-63</td><td>2</td><td><code>op = ((off-1&amp;3)&lt;&lt;2)</code>, <code>b1 = (off-1)&gt;&gt;2</code></td></tr>
<tr><td><code>M2 (near)</code></td><td>64-127</td><td>64-2048</td><td>3-8</td><td><code>op = (mlen-2)&lt;&lt;5 &#124; ((off-1&amp;7)&lt;&lt;2)</code>, <code>b1 = (off-1)&gt;&gt;3</code></td></tr>
<tr><td><code>M3 (far)</code></td><td>32-63</td><td>32-16383</td><td>3-99+</td><td><code>op = 0x20&#124;ml</code>, extended: <code>op=0x20</code>, <code>ll=ml-33</code>, <code>b1=(off-1&amp;63)&lt;&lt;2</code>, <code>b2=(off-1)&gt;&gt;6</code></td></tr>
<tr><td><code>M4 (very far)</code></td><td>16-31</td><td>16384+</td><td>3-99+</td><td><code>op = 0x10 &#124; (base_off_bit&lt;&lt;3) &#124; ml</code>, extended: <code>ll=ml-9</code>, <code>off_adj = off - 0x4000 - (bit&lt;&lt;11)</code></td></tr>
</table>

### UCL NRV2B Format

* Block stream: `[BE16 len][block]...[0x0000]`
* NRV2B match: 2-byte encoded with bit-packed fields

### Literal Encoding

* Initial run: `t &#62;= 17` → `lit_count = t - 17` (1-18 literals)
* Main state: `t = 0..15` → `lit_count = t + 3` (3-18 literals)
* Extended literal: `t = 0` → read continuation bytes for 19+ literals
