"""Verify a native fork build with its own SDK and app-local runtime.

Usage: python -X utf8 native/verify.py [--build out/Release-x64/bin]

Stages msmpi.dll, mpiexec.exe and smpd.exe into a fresh directory, builds the
probes in native/tests/probes against the fork's SDK, runs them at 1, 2 and 4
ranks, and requires every PASS marker and that rank 0 loaded the staged DLL.
It writes out/native-verify/<UTC>/summary.json; any failure exits 1.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from environment import NativeEnvironment  # noqa: E402

RUNTIME = ('msmpi.dll', 'mpiexec.exe', 'smpd.exe')
RANKS = (1, 2, 4)


def Sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Recorder:
    def __init__(self, directory, environment):
        self.directory, self.environment, self.commands = directory, environment, []

    def Run(self, label, command, cwd, timeout=300, env=None):
        started = time.monotonic()
        try:
            result = subprocess.run([str(part) for part in command], cwd=cwd, env=env or self.environment, capture_output=True,
                                    text=True, encoding='utf-8', errors='replace', timeout=timeout)
            record = dict(label=label, argv=[str(part) for part in command], exit_code=result.returncode,
                          stdout=result.stdout[-20000:], stderr=result.stderr[-20000:])
        except subprocess.TimeoutExpired as error:
            record = dict(label=label, argv=[str(part) for part in command], exit_code=None, timeout=timeout,
                          stdout=(error.stdout or '')[-20000:] if isinstance(error.stdout, str) else '',
                          stderr=(error.stderr or '')[-20000:] if isinstance(error.stderr, str) else '')
        record['elapsed_seconds'] = round(time.monotonic() - started, 3)
        self.commands.append(record)
        return record


def Main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--build', type=Path, default=ROOT / 'out/Release-x64/bin')
    args = parser.parse_args()
    build = args.build.resolve()
    environment, identity = NativeEnvironment(ROOT)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    work = ROOT / 'out/native-verify' / stamp
    runtime = work / 'runtime'
    runtime.mkdir(parents=True)
    for name in RUNTIME:
        shutil.copy2(build / name, runtime / name)
    sdk = build / 'sdk'
    recorder = Recorder(work, environment)
    probes = Path(__file__).with_name('tests') / 'probes'
    checks = []

    def Check(name, passed, detail):
        checks.append(dict(check=name, passed=bool(passed), detail=detail))

    c_exe, f_exe = runtime / 'aint_check_c.exe', runtime / 'aint_check_f.exe'
    built_c = recorder.Run('build-aint-c', [identity['tools']['cl']['path'], '/nologo', '/MD', '/W4', f'/I{sdk / "inc"}',
                                            f'/I{sdk / "inc" / "x64"}', probes / 'aint_check.c', f'/Fe{c_exe}', f'/Fo{work / "aint_check.obj"}',
                                            '/link', f'/LIBPATH:{sdk / "lib"}', 'msmpi.lib'], work)
    Check('build aint_check.c with fork SDK', built_c['exit_code'] == 0, built_c['stdout'][-400:])
    built_f = recorder.Run('build-aint-fortran', [identity['tools']['ifx']['path'], '/nologo', '/libs:dll', '/threads',
                                                  f'/I{sdk / "inc"}', f'/I{sdk / "inc" / "x64"}', probes / 'aint_check.f90',
                                                  f'/exe:{f_exe}', f'/object:{work / "aint_check_f.obj"}', '/link',
                                                  f'/LIBPATH:{sdk / "lib"}', 'msmpifec.lib', 'msmpi.lib'], work)
    Check('build aint_check.f90 with fork SDK (msmpifec aliases)', built_f['exit_code'] == 0, built_f['stdout'][-400:])
    for ranks in RANKS:
        if built_c['exit_code'] == 0:
            result = recorder.Run(f'aint-c-{ranks}', [runtime / 'mpiexec.exe', '-n', str(ranks), c_exe], runtime)
            marker = f'AINT_C ranks={ranks} passed={ranks}'
            Check(f'C MPI_Aint_add/diff at {ranks} ranks', result['exit_code'] == 0 and marker in result['stdout']
                  and result['stdout'].rstrip().endswith('PASS') and f'dll=[{runtime / "msmpi.dll"}]' in result['stdout'],
                  result['stdout'][-400:] + result['stderr'][-400:])
        if built_f['exit_code'] == 0:
            result = recorder.Run(f'aint-fortran-{ranks}', [runtime / 'mpiexec.exe', '-n', str(ranks), f_exe], runtime)
            marker = f'AINT_FORTRAN ranks={ranks} passed={ranks} PASS'
            Check(f'Fortran MPI_AINT_ADD/DIFF at {ranks} ranks', result['exit_code'] == 0 and marker in result['stdout'],
                  result['stdout'][-400:] + result['stderr'][-400:])
    summary = dict(kind='native-verify', accepted=False, utc=stamp, build=str(build),
                   runtime={name: Sha256(runtime / name) for name in RUNTIME}, checks=checks,
                   passed=sum(check['passed'] for check in checks), failed=[c['check'] for c in checks if not c['passed']],
                   commands=recorder.commands)
    (work / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(summary=str(work / 'summary.json'), passed=summary['passed'], total=len(checks),
                          failed=summary['failed']), indent=2))
    return 1 if summary['failed'] else 0


if __name__ == '__main__':
    sys.exit(Main())
