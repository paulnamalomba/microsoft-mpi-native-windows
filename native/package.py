"""Package a native Release build as the fork's runtime-and-SDK zip.

Usage: python -X utf8 native/package.py [--build out/Release-x64] [--output out/native-package] [--require-clean]
                                        [--evidence PATH ...]

Layout mirrors Microsoft's installed runtime and SDK, so consumers can switch
roots without other changes:

  msmpi-native-windows-<version>-x64/
    Bin/        msmpi.dll  mpiexec.exe  smpd.exe       (the app-local runtime)
    Include/    mpi.h  mpif.h  mpi.f90  mpio.h  mspms.h  pmidbg.h  x64/  x86/
    Lib/x64/    msmpi.lib  msmpifec.lib  msmpifmc.lib  msmpifes.lib  msmpifms.lib
    Symbols/    msmpi.pdb  mpiexec.pdb  smpd.pdb
    licenses/   LICENSE.txt (MS-MPI, MIT)  COPYRIGHT-mpich.txt  NetworkDirect-LICENSE.txt (MIT)
    manifest.json

manifest.json records the stamped identity, source, toolchain, pins, evidence
hashes and every file's SHA-256. The zip has sorted entries and fixed
timestamps, so identical files give an identical zip. Packaging never accepts
a build or changes MSMPI_INTERNAL_VERSION.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from environment import NativeEnvironment  # noqa: E402

LAYOUT = {
    'Bin': [('bin/msmpi.dll', 'msmpi.dll'), ('bin/mpiexec.exe', 'mpiexec.exe'), ('bin/smpd.exe', 'smpd.exe')],
    'Include': [(f'bin/sdk/inc/{name}', name) for name in ('mpi.h', 'mpif.h', 'mpi.f90', 'mpio.h', 'mspms.h', 'pmidbg.h')]
               + [('bin/sdk/inc/x64/mpifptr.h', 'x64/mpifptr.h'), ('bin/sdk/inc/x86/mpifptr.h', 'x86/mpifptr.h')],
    'Lib/x64': [(f'bin/sdk/lib/{name}', name) for name in ('msmpi.lib', 'msmpifec.lib', 'msmpifmc.lib', 'msmpifes.lib',
                                                           'msmpifms.lib')],
    'Symbols': [('msmpi/msmpi.pdb', 'msmpi.pdb'), ('mpiexec/mpiexec.pdb', 'mpiexec.pdb'), ('smpd/smpd.pdb', 'smpd.pdb')],
}


def Sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def Main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--build', type=Path, default=ROOT / 'out/Release-x64')
    parser.add_argument('--output', type=Path, default=ROOT / 'out/native-package')
    parser.add_argument('--require-clean', action='store_true', help='Refuse a build stamped from a modified tree')
    parser.add_argument('--evidence', type=Path, action='append', default=[], help='summary.json to hash into the manifest')
    args = parser.parse_args()
    build = args.build.resolve()
    native = json.loads((build / 'native-identity' / 'identity.json').read_text(encoding='utf-8'))
    if native.get('configuration') != 'Release':
        raise SystemExit('Only a Release build is packaged')
    if args.require_clean and native['source'].endswith('-dirty'):
        raise SystemExit('Build was stamped from a modified tree: ' + native['source'])
    _, identity = NativeEnvironment(ROOT)
    commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    if not commit.startswith(native['source'].removesuffix('-dirty')):
        raise SystemExit(f'Checkout {commit} is not the stamped source {native["source"]}')
    name = f'msmpi-native-windows-{native["version"]}-x64'
    stage = args.output.resolve() / name
    if stage.exists():
        shutil.rmtree(stage)
    for folder, entries in LAYOUT.items():
        for source, target in entries:
            destination = stage / folder / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(build / source, destination)
    licenses = stage / 'licenses'
    licenses.mkdir()
    shutil.copy2(ROOT / 'LICENSE.txt', licenses / 'LICENSE.txt')
    shutil.copy2(ROOT / 'COPYRIGHT-mpich.txt', licenses / 'COPYRIGHT-mpich.txt')
    pin = json.loads((ROOT / 'native/package-pins.json').read_text(encoding='utf-8'))['sources']['NetworkDirect']
    shutil.copy2(ROOT / 'out/sources' / f'networkdirect-{pin["commit"]}' / 'LICENSE.txt', licenses / 'NetworkDirect-LICENSE.txt')
    files = sorted((path for path in stage.rglob('*') if path.is_file()), key=lambda path: path.relative_to(stage).as_posix())
    manifest = dict(
        schema=1, product='microsoft-mpi-native-windows', version=native['version'], accepted=False,
        identity=native, source=dict(commit=commit, repository='https://github.com/paulnamalomba/microsoft-mpi-native-windows'),
        upstream=dict(source='microsoft/Microsoft-MPI f2d849ff483b36add9bc2d18c6d7bbc9ce38f4ad', declared='10.1.12498.16',
                      abi_reference='Microsoft MS-MPI 10.1.3 (10.1.12498.52)'),
        ci=dict(run_id=os.environ.get('GITHUB_RUN_ID'), run_attempt=os.environ.get('GITHUB_RUN_ATTEMPT'),
                server=os.environ.get('GITHUB_SERVER_URL'), repository=os.environ.get('GITHUB_REPOSITORY')),
        toolchain=dict(visual_studio=identity['visual_studio']['version'], toolset=identity['visual_studio']['toolset'],
                       windows_sdk=identity['windows_sdk']['version'],
                       tools={key: value.get('version') for key, value in identity['tools'].items()}),
        pins={relative: Sha256(ROOT / relative) for relative in ('native/toolchain-pins.json', 'native/package-pins.json')},
        evidence={str(path): Sha256(path) for path in args.evidence},
        runtime=dict(files=[f'Bin/{target}' for _, target in LAYOUT['Bin']],
                     dependencies='Windows system DLLs only (static CRT): KERNEL32, ADVAPI32, RPCRT4, WS2_32, MSWSOCK, ntdll, '
                                  'NTDSAPI, AUTHZ, SHELL32, SHLWAPI'),
        limitations=['Not code-signed', 'Third-party notice review (G20) open', 'NetworkDirect/RDMA transport untested',
                     'Not yet accepted; acceptance writes .commits/builds/<version>.json'],
        files=[dict(path=path.relative_to(stage).as_posix(), bytes=path.stat().st_size, sha256=Sha256(path)) for path in files])
    (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    archive = args.output.resolve() / f'{name}.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as package:
        for path in sorted((path for path in stage.rglob('*') if path.is_file()),
                           key=lambda path: path.relative_to(stage).as_posix()):
            entry = zipfile.ZipInfo(f'{name}/{path.relative_to(stage).as_posix()}', date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type, entry.external_attr = zipfile.ZIP_DEFLATED, 0o644 << 16
            package.writestr(entry, path.read_bytes())
    result = dict(archive=str(archive), sha256=Sha256(archive), bytes=archive.stat().st_size, version=native['version'],
                  source=native['source'], files=len(files) + 1)
    (args.output.resolve() / f'{name}.zip.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(Main())
