"""ABI and hardening parity of a fork build against Microsoft MS-MPI 10.1.3.

Usage: python -X utf8 native/check_abi.py [--build out/Release-x64/bin] [--report FILE]

Compares, using the pinned dumpbin:
  1. msmpi.dll export names;
  2. PE DLL characteristics (ASLR, high-entropy VA, NX, CFG, CET) of msmpi.dll, mpiexec.exe and smpd.exe;
  3. the shipped x64 import libraries (msmpi, msmpifec, msmpifmc): defined externals and weak aliases;
  4. SDK headers, byte for byte after CRLF normalization.
Every difference must be listed in native/abi-differences.json with a reason;
anything unexplained fails with exit status 1.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from environment import NativeEnvironment  # noqa: E402

PROGRAM_FILES = Path(os.environ.get('ProgramFiles', r'C:\Program Files'))
PROGRAM_FILES_X86 = Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'))
REFERENCE = dict(
    dll=Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/msmpi.dll',
    bin=PROGRAM_FILES / 'Microsoft MPI/Bin',
    lib=PROGRAM_FILES_X86 / 'Microsoft SDKs/MPI/Lib/x64',
    inc=PROGRAM_FILES_X86 / 'Microsoft SDKs/MPI/Include',
)
SHIPPED_LIBRARIES = ('msmpi.lib', 'msmpifec.lib', 'msmpifmc.lib')
CHARACTERISTICS = ('High Entropy Virtual Addresses', 'Dynamic base', 'NX compatible', 'Control Flow Guard',
                   'CET compatible', 'Guard')


def Dumpbin(dumpbin, *arguments):
    result = subprocess.run([dumpbin, '/nologo', *map(str, arguments)], capture_output=True, text=True,
                            encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError(f'dumpbin failed: {arguments}\n{result.stdout}{result.stderr}')
    return result.stdout


def Exports(dumpbin, path):
    names = set()
    for line in Dumpbin(dumpbin, '/exports', path).splitlines():
        match = re.match(r'^\s+\d+\s+[0-9A-F]+\s+(?:[0-9A-F]{8}\s+)?([A-Za-z_][\w@?$]*)', line)
        if match:
            names.add(match.group(1))
    return names


def Characteristics(dumpbin, path):
    text = Dumpbin(dumpbin, '/headers', '/loadconfig', path)
    found = {flag for flag in CHARACTERISTICS if re.search(r'^\s+' + re.escape(flag) + r'\b', text, re.M)}
    guard = re.search(r'^\s+([0-9A-F]+) Guard Flags', text, re.M)
    return dict(flags=sorted(found), guard_flags=guard.group(1) if guard else None)


def LibrarySymbols(dumpbin, path):
    """External symbols defined by library members, and weak aliases with their default targets."""
    text = Dumpbin(dumpbin, '/symbols', path)
    defined, weak = set(), {}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        external = re.search(r'\s(SECT[0-9A-F]+)\s+\S+(?:\s+\(\))?\s+External\s+\|\s+(\S+)', line)
        if external:
            defined.add(external.group(2))
            continue
        alias = re.search(r'UNDEF\s+\S+(?:\s+\(\))?\s+WeakExternal\s+\|\s+(\S+)', line)
        if alias:
            target = None
            follow = re.search(r'Default index\s+([0-9A-F]+)', lines[index + 1]) if index + 1 < len(lines) else None
            if follow:
                wanted = follow.group(1).lstrip('0') or '0'
                for candidate in lines[max(0, index - 4):index]:
                    match = re.match(r'^([0-9A-F]{3,})\s.*\|\s+(\S+)', candidate)
                    if match and (match.group(1).lstrip('0') or '0') == wanted:
                        target = match.group(2)
            weak[alias.group(1)] = target
    return dict(defined=defined, weak=weak)


def Headers(directory):
    files = {}
    for path in sorted(Path(directory).rglob('*')):
        if path.is_file():
            files[path.relative_to(directory).as_posix().lower()] = path.read_bytes().replace(b'\r\n', b'\n')
    return files


def Compare(name, ours, theirs):
    return dict(check=name, only_in_fork=sorted(ours - theirs), only_in_microsoft=sorted(theirs - ours))


def Main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--build', type=Path, default=ROOT / 'out/Release-x64/bin')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    environment, identity = NativeEnvironment(ROOT)
    dumpbin = identity['tools']['dumpbin']['path']
    build = args.build.resolve()
    differences = []
    differences.append(Compare('msmpi.dll exports', Exports(dumpbin, build / 'msmpi.dll'), Exports(dumpbin, REFERENCE['dll'])))
    hardening = {}
    for name, reference in (('msmpi.dll', REFERENCE['dll']), ('mpiexec.exe', REFERENCE['bin'] / 'mpiexec.exe'),
                            ('smpd.exe', REFERENCE['bin'] / 'smpd.exe')):
        ours, theirs = Characteristics(dumpbin, build / name), Characteristics(dumpbin, reference)
        hardening[name] = dict(fork=ours, microsoft=theirs)
        differences.append(Compare(f'{name} PE characteristics', set(ours['flags']), set(theirs['flags'])))
    for library in SHIPPED_LIBRARIES:
        ours, theirs = LibrarySymbols(dumpbin, build / 'sdk/lib' / library), LibrarySymbols(dumpbin, REFERENCE['lib'] / library)
        differences.append(Compare(f'{library} defined externals', ours['defined'], theirs['defined']))
        differences.append(Compare(f'{library} weak aliases', {f'{k}->{v}' for k, v in ours['weak'].items()},
                                   {f'{k}->{v}' for k, v in theirs['weak'].items()}))
    ours, theirs = Headers(build / 'sdk/inc'), Headers(REFERENCE['inc'])
    differences.append(Compare('SDK header files', set(ours), set(theirs)))
    differences.append(dict(check='SDK header contents', differing=sorted(name for name in set(ours) & set(theirs)
                                                                           if ours[name] != theirs[name])))
    explained = json.loads((Path(__file__).with_name('abi-differences.json')).read_text(encoding='utf-8'))['explained']
    unexplained = []
    for item in differences:
        for key in ('only_in_fork', 'only_in_microsoft', 'differing'):
            for value in item.get(key, []):
                if value not in explained.get(item['check'], {}).get(key, {}):
                    unexplained.append(dict(check=item['check'], side=key, value=value))
    report = dict(kind='abi-parity-diagnostic', reference='Microsoft MS-MPI 10.1.3 (10.1.12498.52)', build=str(build),
                  hardening=hardening, differences=differences, unexplained=unexplained)
    text = json.dumps(report, indent=2)
    if args.report:
        args.report.write_text(text + '\n', encoding='utf-8')
    print(json.dumps(dict(unexplained=len(unexplained), sample=unexplained[:40], hardening=hardening), indent=2))
    return 1 if unexplained else 0


if __name__ == '__main__':
    sys.exit(Main())
