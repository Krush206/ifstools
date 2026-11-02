#!/usr/bin/env python3
"""
mkifs-new: clean-room re-implementation of QNX mkifs for Linux.

Creates a standalone, uncompressed imagefs. Boot headers, compression, scripts,
and virtual images require target-runtime support and are rejected.
"""
import sys
import pathlib
from parser import parse_buildfile
from imagefs import write_image

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} buildfile [output.ifs]")
        sys.exit(1)
    buildfile = pathlib.Path(sys.argv[1])
    output = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else buildfile.with_suffix('.ifs')
    ast = parse_buildfile(buildfile)
    if ast.virtual_images or ast.script_blocks:
        raise SystemExit("virtual images and script blocks are not supported yet")
    entries = []
    for entry in ast.files:
        source = pathlib.Path(entry.host) if entry.host else None
        if source is not None and not source.is_absolute():
            source = buildfile.parent / source
        entries.append((entry.target, source, entry.attrs.get("type") == "dir"))
    write_image(entries, output)
    print(f"Wrote {output} with {len(entries)} entries")


if __name__ == '__main__':
    main()
