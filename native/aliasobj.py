"""Replacement for the Microsoft-internal aliasobj tool (src/mpi/msmpi/fortran/fortran.props).

Upstream compiles an empty C file with plain `cl.exe -c` and runs aliasobj on
the result with a `.alias` file whose lines read `alias <name> <target>`.
Microsoft's 10.1.3 msmpifec.lib member `amd64.cdecl.obj` shows the result: for
every line, an external undefined symbol for the target, then a weak external
for the name. The weak external carries one auxiliary record whose tag index
is that target and whose characteristics are IMAGE_WEAK_EXTERN_SEARCH_ALIAS.
This tool appends exactly that symbol pattern to the compiled COFF object.

Usage: python -X utf8 native/aliasobj.py OBJECT ALIASFILE
"""
from pathlib import Path
import struct
import sys

IMAGE_SYM_CLASS_EXTERNAL = 2
IMAGE_SYM_CLASS_WEAK_EXTERNAL = 105
IMAGE_WEAK_EXTERN_SEARCH_ALIAS = 3
SYMBOL = struct.Struct('<8sIhHBB')
HEADER = struct.Struct('<HHIIIHH')


def ReadAliases(path):
    aliases = []
    for number, line in enumerate(Path(path).read_text(encoding='ascii').splitlines(), 1):
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 3 or fields[0] != 'alias':
            raise ValueError(f'{path}({number}): expected "alias <name> <target>"')
        aliases.append((fields[1], fields[2]))
    if not aliases:
        raise ValueError(f'{path}: no aliases')
    return aliases


def AppendAliases(data, aliases):
    machine, sections, stamp, table, count, optional, characteristics = HEADER.unpack_from(data, 0)
    if optional != 0 or table == 0:
        raise ValueError('Expected a COFF object with a symbol table')
    strings_offset = table + count * SYMBOL.size
    strings_size = struct.unpack_from('<I', data, strings_offset)[0]
    strings = bytearray(data[strings_offset + 4:strings_offset + strings_size])
    if strings_offset + strings_size != len(data):
        raise ValueError('Unexpected data after the COFF string table')
    symbols = bytearray(data[table:strings_offset])

    def Name(text):
        encoded = text.encode('ascii')
        if len(encoded) <= 8:
            return encoded.ljust(8, b'\0')
        offset = 4 + len(strings)
        strings.extend(encoded + b'\0')
        return struct.pack('<II', 0, offset)

    index = count
    for name, target in aliases:
        symbols += SYMBOL.pack(Name(target), 0, 0, 0, IMAGE_SYM_CLASS_EXTERNAL, 0)
        target_index = index
        symbols += SYMBOL.pack(Name(name), 0, 0, 0, IMAGE_SYM_CLASS_WEAK_EXTERNAL, 1)
        symbols += struct.pack('<II10x', target_index, IMAGE_WEAK_EXTERN_SEARCH_ALIAS)
        index += 3
    header = HEADER.pack(machine, sections, stamp, table, index, optional, characteristics)
    return header + data[HEADER.size:table] + bytes(symbols) + struct.pack('<I', 4 + len(strings)) + bytes(strings)


def Main(obj, alias):
    path = Path(obj)
    path.write_bytes(AppendAliases(path.read_bytes(), ReadAliases(alias)))
    return 0


if __name__ == '__main__':
    sys.exit(Main(*sys.argv[1:3]))
