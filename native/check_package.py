"""Check a released runtime-and-SDK zip the way a consumer uses it.

Usage: python -X utf8 native/check_package.py ZIP [--sha256 HEX] [--launches 50]

Extracts the zip into a fresh directory and checks that:
- the archive and every manifest entry match their SHA-256 hashes, with no extra
  or missing files;
- Bin VERSIONINFO, MPI_Get_library_version and the mpiexec banner match the
  manifest identity;
- the C and Fortran probes build against Include and Lib/x64 and pass at 1, 2
  and 4 ranks on the Bin runtime copied next to them, loading that msmpi.dll;
- repeated four-rank launches on two CPUs print no launch rejection.

It writes out/package-check/<UTC>/summary.json. Any failure exits 1. It never
changes MSMPI_INTERNAL_VERSION.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / 'tests'))
from environment import NativeEnvironment  # noqa: E402
from verify import REJECTION, Recorder, VersionStrings  # noqa: E402
from stress import RestrictAffinity  # noqa: E402

RANKS = (1, 2, 4)


def Sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def Main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('zip', type=Path)
    parser.add_argument('--sha256', help='Expected SHA-256 of the zip')
    parser.add_argument('--launches', type=int, default=50)
    args = parser.parse_args()
    environment, identity = NativeEnvironment(ROOT)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    work = ROOT / 'out/package-check' / stamp
    work.mkdir(parents=True)
    checks, recorder = [], Recorder(work, environment)

    def Check(name, passed, detail):
        checks.append(dict(check=name, passed=bool(passed), detail=detail))

    archive_sha256 = Sha256(args.zip)
    Check('archive SHA-256', not args.sha256 or archive_sha256 == args.sha256.lower(), archive_sha256)
    extract = work / 'extract'
    with zipfile.ZipFile(args.zip) as package:
        for member in package.infolist():
            target = (extract / member.filename).resolve()
            if not target.is_relative_to(extract.resolve()):
                raise SystemExit('Archive entry escapes its directory: ' + member.filename)
        package.extractall(extract)
    roots = [path for path in extract.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise SystemExit('Expected one top-level directory in the archive')
    root = roots[0]
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    listed = {entry['path']: entry for entry in manifest['files']}
    present = {path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_file()} - {'manifest.json'}
    mismatched = sorted(path for path, entry in listed.items()
                        if path in present and (Sha256(root / path) != entry['sha256'] or (root / path).stat().st_size != entry['bytes']))
    Check('manifest file set and hashes', set(listed) == present and not mismatched,
          dict(missing=sorted(set(listed) - present), extra=sorted(present - set(listed)), mismatched=mismatched))
    version, native = manifest['version'], manifest['identity']
    for name in ('msmpi.dll', 'mpiexec.exe', 'smpd.exe'):
        strings = VersionStrings(root / 'Bin' / name) or {}
        Check(f'{name} VERSIONINFO {version}', strings.get('fixed_file_version') == version == strings.get('FileVersion')
              and strings.get('ProductVersion') == native['product_version'], strings)
    run = work / 'run'
    run.mkdir()
    for path in (root / 'Bin').iterdir():
        shutil.copy2(path, run / path.name)
    include, lib = root / 'Include', root / 'Lib' / 'x64'
    probes = ROOT / 'native/tests/probes'
    c_exe, f_exe = run / 'aint_check_c.exe', run / 'aint_check_f.exe'
    built_c = recorder.Run('build-c', [identity['tools']['cl']['path'], '/nologo', '/MD', '/W4', f'/I{include}', f'/I{include / "x64"}',
                                       probes / 'aint_check.c', f'/Fe{c_exe}', f'/Fo{work / "c.obj"}', '/link', f'/LIBPATH:{lib}',
                                       'msmpi.lib'], work)
    built_f = recorder.Run('build-f', [identity['tools']['ifx']['path'], '/nologo', '/libs:dll', '/threads', f'/I{include}',
                                       f'/I{include / "x64"}', probes / 'aint_check.f90', f'/exe:{f_exe}', f'/object:{work / "f.obj"}',
                                       '/link', f'/LIBPATH:{lib}', 'msmpifec.lib', 'msmpi.lib'], work)
    Check('C and Fortran probes build against the package SDK', built_c['exit_code'] == 0 == built_f['exit_code'],
          built_c['stdout'][-300:] + built_f['stdout'][-300:])
    for ranks in RANKS:
        result = recorder.Run(f'c-{ranks}', [run / 'mpiexec.exe', '-n', str(ranks), c_exe], run)
        Check(f'C probe at {ranks} ranks loads the package msmpi.dll and reports {native["library"]}',
              result['exit_code'] == 0 and f'AINT_C ranks={ranks} passed={ranks}' in result['stdout']
              and f'library=[{native["library"]}]' in result['stdout'] and f'dll=[{run / "msmpi.dll"}]' in result['stdout'],
              result['stdout'][-400:] + result['stderr'][-300:])
        result = recorder.Run(f'f-{ranks}', [run / 'mpiexec.exe', '-n', str(ranks), f_exe], run)
        Check(f'Fortran probe at {ranks} ranks', result['exit_code'] == 0
              and f'AINT_FORTRAN ranks={ranks} passed={ranks} PASS' in result['stdout'], result['stdout'][-300:])
    banner = recorder.Run('banner', [run / 'mpiexec.exe', '-help'], run, timeout=60)
    first = banner['stdout'].lstrip().splitlines()[0] if banner['stdout'].strip() else ''
    Check('mpiexec banner', first == f'MS-MPI native-windows Startup Program [Version {version}] (source {native["source"]})', first)
    mask = RestrictAffinity(2)
    failures = []
    for attempt in range(args.launches):
        result = recorder.Run(f'stress-{attempt}', [run / 'mpiexec.exe', '-n', '4', c_exe], run, timeout=180)
        if result['exit_code'] != 0 or 'AINT_C ranks=4 passed=4' not in result['stdout'] or REJECTION in result['stdout'] + result['stderr']:
            failures.append(dict(attempt=attempt, exit_code=result['exit_code'], output=(result['stdout'] + result['stderr'])[-2000:]))
        recorder.commands[-1].pop('stdout', None)
    Check(f'{args.launches} four-rank launches on CPU mask {hex(mask)} without rejection', not failures, failures)
    summary = dict(kind='package-check', utc=stamp, archive=str(args.zip), archive_sha256=archive_sha256, version=version,
                   source=native['source'], ci=manifest.get('ci'), checks=checks, passed=sum(c['passed'] for c in checks),
                   failed=[c['check'] for c in checks if not c['passed']],
                   commands=[c for c in recorder.commands if not c['label'].startswith('stress-')])
    (work / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(summary=str(work / 'summary.json'), passed=summary['passed'], total=len(checks),
                          failed=summary['failed']), indent=2))
    return 1 if summary['failed'] else 0


if __name__ == '__main__':
    sys.exit(Main())
