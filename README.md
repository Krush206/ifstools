# ifstools

ifstools is a collection of Python tools for QNX IFS (Image File System) manipulation.

## Components

* **`dumpifs.py`** – Clean-room Python port of QNX's `dumpifs` utility for parsing IFS images
* **`writer.py`** – IFS image writer with startup header generation
* **`mkifs.py`** – Simplified mkifs buildfile processor (uncompressed standalone images)
* **`imagefs.py`** – Core uncompressed imagefs read/write library
* **`ucl_nrv2b.py`** – Pure-Python NRV2B decompressor for UCL-compressed streams
* **`lzo1x.py`** – Pure-Python LZO1X compressor/decompressor

## What works now

* `dumpifs.py` – full clean-room port of `trunk/utils/m/mkxfs/dumpifs/dumpifs.c`
  * Finds startup header via signature `0x00FF7EEB` with zero-field check, supports little/big endian
  * Decompresses ZLIB-compressed images correctly
  * Parses `startup_header` 256 bytes, `image_header`, `image_dirent`, image/trailer checksums
  * Verbose listing, MD5, extraction with parent directory creation
  * **Note**: UCL/LZO decompression needs refinement to match reference output exactly

* `writer.py` – IFS writer with startup header
  * Builds a little-endian `imagefs` via `imagefs.py` then wraps it with a valid 256-byte startup header
  * Correct fields: signature, version, flags1/flags2, header_size, machine, startup_vaddr, paddr_bias, image_paddr, ram_paddr, ram_size, startup_size, stored_size, imagefs_paddr, imagefs_size, preboot_size
  * Startup trailer checksum = `-sum(u32 words of [spos, spos+startup_size-4))`
  * Uncompressed round-trip: `writer.write_ifs` → `dumpifs.py` lists/extracts correctly
  * ZLIB compressed output supported: `flags1 = VIRTUAL|0x04`, compressed stream + file-end trailer

* `imagefs.py` – uncompressed imagefs read/write
  * `write_image` builds `image_header`, directory chain, file blobs, 32-bit checksum
  * `find_images` validates checksum and structure

* `ucl_nrv2b.py` – NRV2B decompressor implementation
  * Block stream decoder with BE16 length prefix
  * Bit-buffer based decompression (getbit_8 semantics from getbit.h)
  * Successfully parses valid QNX IFS images with UCL compression
  * Note: Full block decompression requires additional refinement for optimal results
  
* `lzo1x.py` – Pure-Python LZO1X compressor/decompressor
  * Complete implementation of LZO1X algorithm (Markus Oberhumer)

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

### Simple buildfile via `mkifs.py`:

```sh
python3 mkifs.py image.build image.ifs
python3 dumpifs.py image.ifs
```

## Known limits

* UCL/LZO decompression of reference images (`qnxbase.ifs`, `qnx.ifs`) requires further tuning to match reference checksums exactly
* UCL NRV2B compressor for writer not yet implemented
* Boot startup ELF, preboot data, script blocks, virtual images not supported
* `mkifs.py` currently rejects virtual images and script blocks

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
