# mkifs-new

Linux-hosted implementation of the uncompressed QNX `imagefs` subset used by
`mkifs` and `dumpifs`.

## Current support

`mkifs.py` accepts simple `target=host-path` buildfile entries and
`[type=dir] target` directory entries. It emits a little-endian, non-bootable
`imagefs` with QNX `image_header`, `image_dirent`, and trailer checksum
layouts. `dumpifs.py` locates structurally valid images, lists their entries,
and can extract regular files.

```sh
python3 mkifs.py image.build image.ifs
python3 dumpifs.py image.ifs
python3 dumpifs.py image.ifs --extract output-directory
```

## Deliberately unsupported

- boot headers, target startup executables, and bootable IFS construction;
- compressed or big-endian image filesystems;
- script blocks, virtual images, ELF relocation, filters, and special-device
  attributes.

`qnxbase.ifs` is a useful future compatibility fixture, but cannot yet be
decoded because it uses startup/possibly compressed-image handling that this
initial standalone implementation does not support.
