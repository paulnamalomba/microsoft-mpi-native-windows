"""Repeated multi-rank launches of the fork runtime on a restricted CPU allocation.

Usage: python -X utf8 native/tests/stress.py [--build out/Release-x64/bin] [--launches 200] [--ranks 4] [--cpus 2]

Required check 3 of AGENTS.md (launch-race regression, repeated launches under
starved CPU allocations). The process affinity is restricted to the first
--cpus logical processors before launching, so mpiexec, smpd and every rank
inherit it and four ranks compete for two CPUs, the condition under which
hosted Windows runners showed "rejecting unknown process id". Every launch
must exit 0 with the probe's PASS marker, and no launch may print the
rejection. Writes out/native-stress/<UTC>/summary.json; any failure exits 1.
"""
import argparse
import ctypes
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'native'))
from environment import NativeEnvironment  # noqa: E402
from verify import RUNTIME, REJECTION, Recorder, Sha256  # noqa: E402


def RestrictAffinity(cpus):
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.SetProcessAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    mask = (1 << cpus) - 1
    if not kernel32.SetProcessAffinityMask(kernel32.GetCurrentProcess(), mask):
        raise OSError(ctypes.get_last_error(), 'SetProcessAffinityMask')
    return mask


def Main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--build', type=Path, default=ROOT / 'out/Release-x64/bin')
    parser.add_argument('--launches', type=int, default=200)
    parser.add_argument('--ranks', type=int, default=4)
    parser.add_argument('--cpus', type=int, default=2)
    args = parser.parse_args()
    environment, identity = NativeEnvironment(ROOT)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    work = ROOT / 'out/native-stress' / stamp
    runtime = work / 'runtime'
    runtime.mkdir(parents=True)
    build = args.build.resolve()
    for name in RUNTIME:
        shutil.copy2(build / name, runtime / name)
    sdk, probe = build / 'sdk', runtime / 'aint_check_c.exe'
    recorder = Recorder(work, environment)
    built = recorder.Run('build-aint-c', [identity['tools']['cl']['path'], '/nologo', '/MD', '/W4', f'/I{sdk / "inc"}',
                                          f'/I{sdk / "inc" / "x64"}', ROOT / 'native/tests/probes/aint_check.c', f'/Fe{probe}',
                                          f'/Fo{work / "aint_check.obj"}', '/link', f'/LIBPATH:{sdk / "lib"}', 'msmpi.lib'], work)
    if built['exit_code'] != 0:
        print(built['stdout'] + built['stderr'])
        return 1
    mask = RestrictAffinity(args.cpus)
    marker = f'AINT_C ranks={args.ranks} passed={args.ranks}'
    outcomes, started = [], time.monotonic()
    for attempt in range(args.launches):
        result = recorder.Run(f'launch-{attempt}', [runtime / 'mpiexec.exe', '-n', str(args.ranks), probe], runtime, timeout=180)
        text = result['stdout'] + result['stderr']
        outcome = dict(attempt=attempt, exit_code=result['exit_code'], elapsed_seconds=result['elapsed_seconds'],
                       passed=result['exit_code'] == 0 and marker in result['stdout'], rejected=REJECTION in text)
        outcomes.append(outcome)
        if not outcome['passed'] or outcome['rejected']:
            outcome['output'] = text[-4000:]
        else:
            recorder.commands[-1].pop('stdout', None)  # keep the summary small; failures keep full output
        if (attempt + 1) % 25 == 0:
            print(f'{attempt + 1}/{args.launches} launches, {sum(not o["passed"] for o in outcomes)} failed', flush=True)
    failed = [o for o in outcomes if not o['passed'] or o['rejected']]
    summary = dict(kind='native-stress', accepted=False, utc=stamp, build=str(build), launches=args.launches, ranks=args.ranks,
                   affinity_mask=hex(mask), logical_processors=__import__('os').cpu_count(),
                   elapsed_seconds=round(time.monotonic() - started, 1),
                   runtime={name: Sha256(runtime / name) for name in RUNTIME},
                   passed=len(outcomes) - len(failed), failed=len(failed), rejections=sum(o['rejected'] for o in outcomes),
                   failures=failed, commands=[c for c in recorder.commands if c['label'] == 'build-aint-c'])
    (work / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: summary[key] for key in ('launches', 'ranks', 'affinity_mask', 'passed', 'failed', 'rejections',
                                                    'elapsed_seconds')} | dict(summary=str(work / 'summary.json')), indent=2))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(Main())
