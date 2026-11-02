"""
Generic mkifs buildfile parser.

Implements a minimal but extensible grammar for QNX mkifs buildfiles.
"""
import pathlib
import re
from dataclasses import dataclass, field
from typing import List, Dict, Optional

@dataclass
class VirtualImage:
    name: str
    attrs: Dict[str, str]
    items: List[str] = field(default_factory=list)

@dataclass
class ScriptBlock:
    name: str
    lines: List[str] = field(default_factory=list)

@dataclass
class FileEntry:
    target: str
    host: Optional[str] = None
    attrs: Dict[str, str] = field(default_factory=dict)

@dataclass
class BuildfileAST:
    image_addr: Optional[str] = None
    virtual_images: List[VirtualImage] = field(default_factory=list)
    script_blocks: List[ScriptBlock] = field(default_factory=list)
    files: List[FileEntry] = field(default_factory=list)
    data_section: Optional[str] = None

_attr_re = re.compile(r'\[([^\]]+)\]')
_block_start_re = re.compile(r'(\[([^\]]+)\])?\s*([A-Za-z0-9_\-]+)\s*=\s*\{')

def parse_buildfile(path: pathlib.Path) -> BuildfileAST:
    text = path.read_text(encoding='utf-8', errors='ignore')
    lines = text.splitlines()
    ast = BuildfileAST()
    current_attrs = {}
    current_virtual = None
    current_script = None
    
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        # attribute line may be on its own or before block
        if line.startswith('[') and ']' in line:
            # parse attribute block
            attr_end = line.find(']')
            attr_str = line[1:attr_end]
            current_attrs = parse_attrs(attr_str)
            rest = line[attr_end+1:].strip()
            if rest:
                # same line contains block start
                line = rest
            else:
                continue
        # block start
        m = re.match(r'([A-Za-z0-9_\-]+)\s*=\s*\{', line)
        if m:
            block_name = m.group(1)
            # Determine type
            if 'virtual' in current_attrs:
                current_virtual = VirtualImage(name=block_name, attrs=current_attrs.copy(), items=[])
                ast.virtual_images.append(current_virtual)
                current_attrs = {}
                continue
            # script detection: +script flag or script= key
            is_script = any(k.startswith('script') or k == '+script' or current_attrs.get('script') != '' for k in current_attrs)
            if is_script:
                current_script = ScriptBlock(name=block_name, lines=[])
                ast.script_blocks.append(current_script)
                current_attrs = {}
                continue
        # block end
        if line == '}':
            current_virtual = None
            current_script = None
            current_attrs = {}
            continue
        # inside virtual block
        if current_virtual:
            current_virtual.items.append(line)
            continue
        # inside script block
        if current_script:
            current_script.lines.append(line)
            continue
        # file list / data section
        if line.startswith('[') and ']' in line and '=' in line:
            # e.g. [data=c]
            inner = line[1:-1]
            if '=' in inner:
                k,v = inner.split('=',1)
                if k.strip() == 'data':
                    ast.data_section = v.strip()
            continue
        # file entry: detect target=host
        if '=' in line and not line.startswith('PATH=') and not line.startswith('LD_LIBRARY_PATH='):
            # crude heuristic for file spec
            # If line looks like path=path, treat as file entry
            # Otherwise keep as string
            parts = line.split('=',1)
            target = parts[0].strip()
            host = parts[1].strip()
            # If host looks like a path, accept
            ast.files.append(FileEntry(target=target, host=host, attrs=current_attrs.copy()))
            current_attrs = {}
            continue
        # treat as file entry
        ast.files.append(FileEntry(target=line, host=None, attrs=current_attrs.copy()))
        current_attrs = {}
    return ast

def parse_attrs(s: str) -> Dict[str, str]:
    attrs = {}
    # split by comma and whitespace, keep quoted strings
    # Very simple tokenizer
    parts = re.findall(r'[^\s,]+', s)
    for p in parts:
        if not p:
            continue
        if '=' in p:
            k,v = p.split('=',1)
            attrs[k.strip()] = v.strip()
        else:
            # flag, e.g. +compress, +script
            attrs[p.strip()] = ''
    return attrs
