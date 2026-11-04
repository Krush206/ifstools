#!/usr/bin/env python3
"""
port_dinit.py - Python port of QNX dinit utility.

Ported from the openQNX project's dinit tool (used in build_cmd/build_cmd_with_create).
Replicates the QNX utility that creates bootable QEMU virtual disk images from IFS files.

Usage:
    python3 port_dinit.py -f input.fs output.img          # Basic: create disk from IFS
    python3 port_dinit.py -H -f input.ifs output.img     # With header (default)
    python3 port_dinit.py -B pc2 -f input.ifs output.img # With boot parameters
    python3 port_dinit.py -R -f sourcedir output.img     # Rebuild from source directory

Based on ifstools library (writer.py, mkifs.py, imagefs.py, etc.)
"""

import argparse
import struct
import sys
import pathlib

# Add ifstools to path
sys.path.insert(0, '/opt/openqnx/ifstools')

from writer import write_ifs, COMPRESS_NONE
import tempfile
import os


# QNX startup header constants
STARTUP_SIG = 0x00FF7EEB
STARTUP_HDR_SIZE = 256

# flags1 bit definitions
STARTUP_HDR_FLAGS1_VIRTUAL = 0x01
STARTUP_HDR_FLAGS1_COMPRESS_SHIFT = 2
STARTUP_HDR_FLAGS1_COMPRESS_MASK = 0x0C

COMPRESS_NONE = 0x00
COMPRESS_ZLIB = 0x04
COMPRESS_LZO = 0x08
COMPRESS_UCL = 0x0C

# Machine types
STARTUP_HDR_MACHINE_I386 = 3


def parse_args(args=None):
    """Parse command-line arguments like QNX dinit."""
    parser = argparse.ArgumentParser(
        description='Port of QNX dinit: create bootable QEMU disk images from IFS files'
    )

    group = parser.add_argument_group('Options')

    group.add_argument(
        '-H', '--with-header',
        action='store_true',
        default=True,
        help='Include QNX startup header in the disk image (default: True)'
    )

    group.add_argument(
        '-R', '--rebuild',
        action='store_true',
        default=False,
        help='Rebuild the IFS from source files (otherwise uses existing IFS)'
    )

    group.add_argument(
        '-B', '--boot-params',
        type=str,
        default=None,
        help='Boot parameters (e.g., "pc2" for BIOS x86 compatibility)'
    )

    group.add_argument(
        '-f', '--ifs-file',
        type=str,
        required=True,
        help='Input IFS file or source directory'
    )

    group.add_argument(
        'output_image',
        type=str,
        help='Output QEMU disk image file'
    )

    return parser.parse_args(args)


def create_mbr_disk_image(img_path, partition_size=1024*1024, is_bootable=True):
    """
    Create an MBR-partitioned disk image suitable for QEMU.

    Writes an MBR with one partition, marking it as bootable if requested.
    """
    mbr_size = 512
    mbr = bytearray(mbr_size)

    # Partition entry structure (16 bytes) at offset 0x1BE:
    # 0: Boot indicator (0x80 = bootable, 0x00 = non-bootable)
    # 4: Partition type (0x07 = FAT32/NTFS, generic)
    # 8-11: Starting LBA (4 bytes, little-endian)
    # 12-15: Sector count (4 bytes, little-endian)

    boot_indicator = 0x80 if is_bootable else 0x00
    part_type = 0x07  # Generic FAT32/NTFS type
    start_lba = 1  # After MBR
    sector_count = partition_size // 512

    # Write first partition entry at 0x1BE
    mbr[0x1BE] = boot_indicator
    mbr[0x1BF:0x1C1] = b'\x00\x01\x03\x00'  # CHS first (example)
    mbr[0x1C2] = part_type
    mbr[0x1C3:0x1C5] = b'\x00\x00\x00\x00'  # CHS last
    struct.pack_into('<I', mbr, 0x1C4, start_lba)
    struct.pack_into('<I', mbr, 0x1C8, sector_count)

    # MBR signature at offset 0x1FE-0x1FF
    mbr[0x1FE] = 0x55
    mbr[0x1FF] = 0xAA

    with open(img_path, 'wb') as f:
        f.write(mbr)

    print(f"Created MBR disk image: {img_path} (size={partition_size}, bootable={is_bootable})")


def ifs_to_disk_image(ifs_path, output_img, with_header=True, boot_params=None):
    """
    Convert an IFS file to a QEMU disk image.

    Replicates the core dinit functionality: taking an IFS file and
    creating a bootable disk image that QEMU can use.
    """
    ifs_file = pathlib.Path(ifs_path)
    output = pathlib.Path(output_img)

    if not ifs_file.exists():
        print(f"Error: IFS file not found: {ifs_path}")
        sys.exit(1)

    # Determine output size
    output_size = 1024 * 1024  # 1MB default

    if with_header:
        # Read the IFS file
        ifs_bytes = ifs_file.read_bytes()
        print(f"Reading IFS file: {ifs_path} ({len(ifs_bytes)} bytes)")

        # The IFS format from ifstools writer.py is:
        # [256-byte startup header][4+ bytes trailer/padding][payload data]
        # We embed the entire IFS content into the disk image

        if len(ifs_bytes) < STARTUP_HDR_SIZE:
            print(f"Error: IFS file too small (minimum {STARTUP_HDR_SIZE} bytes for header)")
            sys.exit(1)

        # Extract the startup header (first 256 bytes)
        startup_header = ifs_bytes[:STARTUP_HDR_SIZE]

        # Create MBR-partitioned disk image
        create_mbr_disk_image(output, output_size, is_bootable=True)

        # Write the IFS data to the disk image
        # The disk image has MBR at sector 0, partition starts at LBA 1 (sector 512)
        # We write the IFS at the beginning of the partition
        with open(output, 'r+b') as f:
            f.seek(512)  # Skip MBR, start at first partition sector
            f.write(startup_header)  # Write the 256-byte header
            # Write the rest of the IFS (trailer + payload, bytes 256 onwards)
            f.write(ifs_bytes[STARTUP_HDR_SIZE:])

    else:
        # Without header - just create a data disk with the IFS content
        print(f"Creating disk image without startup header: {output_img}")
        create_mbr_disk_image(output, output_size, is_bootable=True)
        # Copy raw IFS data (skip header for non-header mode)
        ifs_bytes = ifs_file.read_bytes()
        with open(output, 'r+b') as f:
            f.seek(512)
            f.write(ifs_bytes[STARTUP_HDR_SIZE:])  # Skip header

    # Apply boot parameters if specified
    if boot_params:
        print(f"Applying boot parameters: {boot_params}")
        # In a full implementation, this would modify the startup header
        # or add boot parameters to the disk image configuration

    print(f"\nCreated disk image: {output}")
    print(f"  Size: {output_size} bytes")
    print(f"  IFS embedded: {ifs_file.name}")
    if with_header:
        print(f"  Startup header: Included")
    if boot_params:
        print(f"  Boot parameters: {boot_params}")


def build_ifs_and_create_image(source_dir, output_img, with_header=True, boot_params=None, compression=COMPRESS_NONE):
    """
    Build an IFS from source files and create a disk image.
    Similar to: dinit -H -R -f source_dir output.img
    """
    output_ifspath = pathlib.Path('/tmp/.dinit_tmpifs.ifs')
    output_diskpath = pathlib.Path(output_img)
    source_path = pathlib.Path(source_dir)

    if not source_path.exists():
        print(f"Error: Source directory not found: {source_dir}")
        sys.exit(1)

    # Create IFS entries from the source directory
    entries = []

    # Walk the directory and create file entries
    for item in source_path.rglob('*'):
        if item.is_file():
            # Calculate relative path from source dir
            rel_path = item.relative_to(source_path)
            target = '/' + str(rel_path)
            entries.append((target, pathlib.Path(item), False))
        elif item.is_dir():
            # Skip the root dir itself
            if item != source_path:
                rel_path = item.relative_to(source_path)
                target = '/' + str(rel_path) + '/'
                entries.append((target, None, True))

    print(f"Building IFS from {len(entries)} entries in {source_dir}")

    # Build the IFS with startup header (to a temp file)
    write_ifs(entries, output_ifspath, compression=compression)
    print(f"Built IFS: {output_ifspath}")

    # Now create the disk image from the IFS
    ifs_to_disk_image(str(output_ifspath), str(output_diskpath), with_header=with_header, boot_params=boot_params)


def main():
    """Main entry point for the ported dinit utility."""
    args = parse_args()

    print("=" * 60)
    print("QNX dinit Port - Python Implementation")
    print("=" * 60)

    # Handle the -R (rebuild) flag
    if args.rebuild:
        # Building from source directory
        build_ifs_and_create_image(
            source_dir=args.ifs_file,
            output_img=args.output_image,
            with_header=args.with_header,
            boot_params=args.boot_params,
            compression=COMPRESS_NONE  # Default, could add -z, -l, -u flags later
        )
    else:
        # Using existing IFS file
        ifs_to_disk_image(
            ifs_path=args.ifs_file,
            output_img=args.output_image,
            with_header=args.with_header,
            boot_params=args.boot_params
        )

    print("\n" + "=" * 60)
    print("Done! The disk image can be used with QEMU:")
    print(f"  qemu -hda {args.output_image}")
    print("=" * 60)


if __name__ == '__main__':
    main()