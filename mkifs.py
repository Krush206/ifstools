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


def write_standalone_ifs(entries, output_path: pathlib.Path) -> None:
    """Write a standalone IFS without boot headers (just imagefs)."""
    write_image(entries, output_path)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} buildfile [output.ifs]")
        sys.exit(1)
    buildfile = pathlib.Path(sys.argv[1])
    output = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else buildfile.with_suffix('.ifs')
    ast = parse_buildfile(buildfile)
    
    entries = []
    
    # Process virtual images - for now, emit them as files
    # Virtual images are typically in-memory filesystems that should be
    # included as special entries in the IFS. For standalone IFS, we
    # embed them as directories.
    for vimg in ast.virtual_images:
        # Create directory entry for virtual image
        target = vimg.name if vimg.name.startswith('/') else '/' + vimg.name
        entries.append((target, None, True))  # Directory entry
        
        # Process items in virtual image
        for item in vimg.items:
            # Items can be file entries or nested directories
            item_target = item.strip()
            if item_target:
                # Check if it's a directory (ends with /) or a file
                is_dir = item_target.endswith('/')
                if is_dir:
                    item_target = item_target.rstrip('/')
                entries.append((f"{target}/{item_target}", None, is_dir))
    
    # Process script blocks - they become files in the root of the IFS
    for script in ast.script_blocks:
        # Script blocks become executable files
        script_path = f"/{script.name}.script"
        
        # Write script content to a temp file
        import tempfile
        import atexit
        import os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.script', delete=False) as f:
            f.write('\n'.join(script.lines))
            script_content_path = pathlib.Path(f.name)
        
        # Register for cleanup
        def cleanup_temp(p=script_content_path):
            try:
                if p and p.exists():
                    p.unlink()
            except:
                pass
        atexit.register(cleanup_temp)
        
        entries.append((script_path, script_content_path, False))
    
    # Process regular files
    for entry in ast.files:
        source = pathlib.Path(entry.host) if entry.host else None
        if source is not None and not source.is_absolute():
            source = buildfile.parent / source
        entries.append((entry.target, source, entry.attrs.get("type") == "dir"))
    
    write_image(entries, output)
    print(f"Wrote {output} with {len(entries)} entries")


if __name__ == '__main__':
    main()
