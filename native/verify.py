"""Verify a native fork build with its own SDK and app-local runtime.

Usage: python -X utf8 native/verify.py [--build out/Release-x64/bin] [--debug-build out/Debug-x64/bin]

Stages msmpi.dll, mpiexec.exe and smpd.exe into a fresh directory, builds the
probes in native/tests/probes against the fork's SDK, runs them at 1, 2 and 4
ranks, and requires every PASS marker and that rank 0 loaded the staged DLL.
It also checks the stamped identity, runs the deterministic launch-race test,
and repeats the probes on the Debug runtime (build it first with
build.py baseline --configuration Debug). It writes
out/native-verify/<UTC>/summary.json; any failure exits 1.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
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


REJECTION = 'rejecting unknown process id'
LAUNCH_DELAY_MS = '300'


def VersionStrings(path):
    """VERSIONINFO fixed file version and English (0409/04B0) strings, read through version.dll."""
    import ctypes
    from ctypes import wintypes
    version = ctypes.WinDLL('version')
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, data):
        return None
    pointer, length = ctypes.c_void_p(), wintypes.UINT()

    class FixedFileInfo(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in ('signature', 'struc_version', 'file_ms', 'file_ls', 'product_ms',
                                                        'product_ls', 'flags_mask', 'flags', 'os', 'type', 'subtype',
                                                        'date_ms', 'date_ls')]
    result = {}
    if version.VerQueryValueW(data, '\\', ctypes.byref(pointer), ctypes.byref(length)):
        fixed = ctypes.cast(pointer, ctypes.POINTER(FixedFileInfo)).contents
        result['fixed_file_version'] = f'{fixed.file_ms >> 16}.{fixed.file_ms & 0xFFFF}.{fixed.file_ls >> 16}.{fixed.file_ls & 0xFFFF}'
        result['fixed_product_version'] = (f'{fixed.product_ms >> 16}.{fixed.product_ms & 0xFFFF}.'
                                           f'{fixed.product_ls >> 16}.{fixed.product_ls & 0xFFFF}')
        result['file_flags'] = fixed.flags & fixed.flags_mask
    for name in ('CompanyName', 'FileDescription', 'FileVersion', 'InternalName', 'LegalCopyright', 'OriginalFilename',
                 'ProductName', 'ProductVersion', 'Comments'):
        if version.VerQueryValueW(data, f'\\StringFileInfo\\040904B0\\{name}', ctypes.byref(pointer), ctypes.byref(length)):
            result[name] = ctypes.wstring_at(pointer, length.value).rstrip('\0')
    return result


def IdentityChecks(build, runtime, recorder, Check, library_output):
    """Every shipped binary, the library string and the mpiexec banner name the same fork build."""
    native = json.loads((build.parent / 'native-identity' / 'identity.json').read_text(encoding='utf-8'))
    for name in RUNTIME:
        strings = VersionStrings(runtime / name) or {}
        expected = dict(fixed_file_version=native['version'], fixed_product_version=native['version'],
                        FileVersion=native['version'], ProductVersion=native['product_version'], OriginalFilename=name)
        mismatched = {key: strings.get(key) for key, value in expected.items() if strings.get(key) != value}
        # The fork's own name contains "microsoft"; reject only a claim to be Microsoft's company or product.
        microsoft = [key for key in ('CompanyName', 'ProductName')
                     if key not in strings or strings[key].lower().startswith('microsoft ')
                     or 'microsoft corporation' in strings[key].lower() or '®' in strings[key]]
        Check(f'{name} VERSIONINFO reports fork {native["version"]}', not mismatched and not microsoft,
              dict(strings=strings, mismatched=mismatched, claims_microsoft=microsoft))
    Check('MPI_Get_library_version reports the fork identity', f'library=[{native["library"]}]' in library_output,
          dict(expected=native['library'], output=library_output[-400:]))
    banner = recorder.Run('mpiexec-help', [runtime / 'mpiexec.exe', '-help'], runtime, timeout=60)
    first = banner['stdout'].lstrip().splitlines()[0] if banner['stdout'].strip() else ''
    expected = f'MS-MPI native-windows Startup Program [Version {native["version"]}] (source {native["source"]})'
    Check('mpiexec banner reports the fork identity', first == expected and 'askmpi@microsoft.com' not in banner['stdout'],
          dict(expected=expected, observed=first))
    return native


def LaunchRegistration(identity, environment, work, runtime, probe, recorder, Check, native):
    """Deterministic regression test for registering ranks before they can connect.

    A Release smpd.exe compiled with MSMPI_NATIVE_LAUNCH_TEST in a disposable copy
    of the source tree (its own intermediates and staging; never shipped) sleeps
    after resuming each rank, which widens the window far beyond rank startup time.
    With the fixed order every launch must succeed. The legacy-order sensitivity
    control must reproduce the original rejection, proving the test can see the
    defect.
    """
    import importlib.util
    tree = work / 'launch-test-tree'
    shutil.copytree(ROOT, tree, ignore=shutil.ignore_patterns('.git', 'out', 'x64', 'Release', 'Debug', '__pycache__'))
    spec = importlib.util.spec_from_file_location('launch_test_build', tree / 'native' / 'build.py')
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    generated = build.Generate(identity, environment, 'Release', work)
    smpd = build.ROOT / 'src/mpi/smpd/smpd.vcxproj'
    properties = build.GlobalProperties('Release', native)
    test_environment = dict(environment, CL='/DMSMPI_NATIVE_LAUNCH_TEST')
    log = work / 'launch-test-smpd-build.log'
    built = recorder.Run('build-launch-test-smpd', [identity['tools']['msbuild']['path'], smpd, '/nologo', '/nr:false', '/m:1',
                                                    '/v:minimal', f'/flp:LogFile={log};Verbosity=normal;Encoding=UTF-8',
                                                    *[f'/p:{key}={value}' for key, value in properties.items()]],
                         build.ROOT, timeout=3600, env=test_environment)
    test_smpd = build.ROOT / 'out/Release-x64/bin/smpd.exe'
    Check('build MSMPI_NATIVE_LAUNCH_TEST smpd (Release, disposable source copy)', built['exit_code'] == 0 and test_smpd.is_file(),
          built['stdout'][-600:])
    if built['exit_code'] != 0 or not test_smpd.is_file():
        return
    staged = work / 'runtime-launch-test'
    staged.mkdir()
    for name in ('msmpi.dll', 'mpiexec.exe'):
        shutil.copy2(runtime / name, staged / name)
    shutil.copy2(test_smpd, staged / 'smpd.exe')
    shutil.copy2(probe, staged / probe.name)
    for order, launches, expect_success in (('fixed', 5, True), ('legacy', 3, False)):
        variables = dict(environment, MSMPI_NATIVE_TEST_RESUME_DELAY_MS=LAUNCH_DELAY_MS,
                         MSMPI_NATIVE_TEST_LEGACY_ORDER='1' if order == 'legacy' else '0')
        outcomes = []
        for attempt in range(launches):
            result = recorder.Run(f'launch-{order}-{attempt}', [staged / 'mpiexec.exe', '-debug', '3', '-n', '4', staged / probe.name],
                                  staged, timeout=180, env=variables)
            rejected = REJECTION in result['stderr'] or REJECTION in result['stdout']
            succeeded = result['exit_code'] == 0 and 'AINT_C ranks=4 passed=4' in result['stdout']
            outcomes.append(dict(attempt=attempt, exit_code=result['exit_code'], succeeded=succeeded, rejected=rejected))
        if expect_success:
            Check(f'registration before resume: {launches} four-rank launches with a {LAUNCH_DELAY_MS} ms resume delay',
                  all(o['succeeded'] and not o['rejected'] for o in outcomes), outcomes)
        else:
            Check(f'sensitivity control: legacy order reproduces "{REJECTION}" in {launches} of {launches} launches',
                  all(o['rejected'] and not o['succeeded'] for o in outcomes), outcomes)
    return staged


TABLE_ROW = re.compile(r'^\s*(\d{5}) - .* - ([0-9a-f]{8,16})\s*$')
AFFINITY_ROW = re.compile(r'^AFFINITY rank=(\d+) cpusets=(-?\d+) cpuset=(\d+):0x([0-9a-f]+) process=0x([0-9a-f]+) groups=(\d+) '
                          r'observed=(\d+):0x([0-9a-f]+) other_groups=(\d+)$')


def CpuSetsAvailable():
    import ctypes
    kernelbase = ctypes.WinDLL('kernelbase')
    return hasattr(kernelbase, 'SetProcessDefaultCpuSetMasks')


def AffinityChecks(identity, environment, work, runtime, sdk, recorder, Check, legacy_runtime=None):
    """Applied affinity must equal the layout mpiexec computed and prints (MPIEXEC_AFFINITY_TABLE=1), rank by rank.

    On Windows 11 and Server 2022 the runtime assigns each rank's default CPU Sets and leaves its affinity mask
    unrestricted, as Microsoft MS-MPI 10.1.3 does. CPU Sets are soft, so observed processors are recorded but not
    required to stay inside the set. The legacy affinity-mask path (forced in the verification-only smpd) is hard:
    observed processors must stay inside the mask. Without -affinity nothing is bound.
    """
    probe = runtime / 'affinity_check.exe'
    built = recorder.Run('build-affinity-check', [identity['tools']['cl']['path'], '/nologo', '/MD', '/W4', f'/I{sdk / "inc"}',
                                                  f'/I{sdk / "inc" / "x64"}', Path(__file__).with_name('tests') / 'probes' / 'affinity_check.c',
                                                  f'/Fe{probe}', f'/Fo{work / "affinity_check.obj"}', '/link', f'/LIBPATH:{sdk / "lib"}',
                                                  'msmpi.lib'], work)
    Check('build affinity_check.c with fork SDK', built['exit_code'] == 0, built['stdout'][-400:])
    if built['exit_code'] != 0:
        return
    cpu_sets = CpuSetsAvailable()
    full_mask = None

    def Launch(label, directory, arguments, extra):
        result = recorder.Run(label, [directory / 'mpiexec.exe', *arguments, probe], directory, timeout=180,
                              env=dict(environment, MPIEXEC_AFFINITY_TABLE='1', **extra))
        table = {int(m.group(1)): int(m.group(2), 16) for m in map(TABLE_ROW.match, result['stdout'].splitlines()) if m}
        ranks = {}
        for match in filter(None, map(AFFINITY_ROW.match, result['stdout'].splitlines())):
            rank, count, group, cpuset, process, groups, observed_group, observed, other = match.groups()
            ranks[int(rank)] = dict(cpusets=int(count), cpuset_group=int(group), cpuset=int(cpuset, 16), process=int(process, 16),
                                    groups=int(groups), observed_group=int(observed_group), observed=int(observed, 16),
                                    other_groups=int(other))
        return result, table, ranks

    if (legacy_runtime is not None) and not (legacy_runtime / probe.name).exists():
        shutil.copy2(probe, legacy_runtime / probe.name)
    result, table, ranks = Launch('affinity-control', runtime, ['-n', '4'], {})
    full_mask = next(iter(ranks.values()))['process'] if ranks else None
    Check('affinity control: without -affinity no rank is bound', result['exit_code'] == 0 and len(ranks) == 4 and not table
          and all(r['cpusets'] in (0, -1) and r['process'] == full_mask for r in ranks.values()), ranks)
    cases = [('-affinity', ['-affinity', '-n', str(n)]) for n in RANKS] + [('-affinity_layout seq', ['-affinity_layout', 'seq', '-n', '4'])]
    paths = [('CPU Sets' if cpu_sets else 'affinity mask', runtime, {})]
    if legacy_runtime is not None and cpu_sets:
        paths.append(('legacy affinity mask', legacy_runtime, {'MSMPI_NATIVE_TEST_LEGACY_AFFINITY': '1'}))
    for path, directory, extra in paths:
        hard = path != 'CPU Sets'
        for name, arguments in cases:
            count = int(arguments[-1])
            result, table, ranks = Launch(f'affinity-{path}-{name}-{count}'.replace(' ', '_'), directory, arguments, extra)
            expected_ok = result['exit_code'] == 0 and len(table) == count == len(ranks) and set(table) == set(ranks)
            if name == '-affinity':
                masks = list(table.values())
                expected_ok = expected_ok and len(set(masks)) == count and all(mask and not mask & (mask - 1) for mask in masks)
            if hard:
                applied = all(ranks[r]['process'] == table[r] and ranks[r]['cpusets'] in (0, -1)
                              and ranks[r]['observed'] & ~table[r] == 0 for r in table) if expected_ok else False
            else:
                applied = all(ranks[r]['cpusets'] == 1 and ranks[r]['cpuset_group'] == 0 and ranks[r]['cpuset'] == table[r]
                              and ranks[r]['process'] == full_mask for r in table) if expected_ok else False
            inside = sum(1 for r in table if r in ranks and ranks[r]['observed'] & ~table[r] == 0)
            Check(f'{path} affinity {name} at {count} ranks matches the computed layout rank by rank', applied,
                  dict(table={r: hex(m) for r, m in sorted(table.items())},
                       ranks={r: {k: (hex(v) if k in ('cpuset', 'process', 'observed') else v) for k, v in d.items()}
                              for r, d in sorted(ranks.items())},
                       observed_inside=f'{inside}/{len(table)}', exit_code=result['exit_code']))
    # Sensitivity control: Microsoft MS-MPI 10.1.3 (the installed ABI reference) assigns each host's layout entries in
    # launch order, the reverse of the layout it prints. The rank-by-rank check above must detect that.
    microsoft = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Microsoft MPI/Bin/mpiexec.exe'
    if cpu_sets and microsoft.is_file():
        isolated = work / 'affinity-microsoft'
        isolated.mkdir()
        shutil.copy2(probe, isolated / probe.name)  # no app-local msmpi.dll: ranks load Microsoft's System32 copy
        result = recorder.Run('affinity-microsoft-seq-4', [microsoft, '-affinity_layout', 'seq', '-n', '4', isolated / probe.name],
                              isolated, timeout=180, env=dict(environment, MPIEXEC_AFFINITY_TABLE='1'))
        table = {int(m.group(1)): int(m.group(2), 16) for m in map(TABLE_ROW.match, result['stdout'].splitlines()) if m}
        applied = {int(m.group(1)): int(m.group(4), 16) for m in map(AFFINITY_ROW.match, result['stdout'].splitlines()) if m}
        Check('sensitivity control: Microsoft MS-MPI 10.1.3 applies the reversed seq layout, which the check rejects',
              len(table) == len(applied) == 4 and all(applied[r] == table[3 - r] for r in range(4)) and applied != table,
              dict(table={r: hex(m) for r, m in sorted(table.items())}, applied={r: hex(m) for r, m in sorted(applied.items())}))


def ProbeChecks(label, sdk, runtime, work, identity, recorder, Check):
    """Build the C and Fortran probes against an SDK and run them on a staged runtime at 1, 2 and 4 ranks."""
    probes = Path(__file__).with_name('tests') / 'probes'
    tag = label.strip().lower() or 'release'
    c_exe, f_exe = runtime / 'aint_check_c.exe', runtime / 'aint_check_f.exe'
    built_c = recorder.Run(f'build-aint-c-{tag}', [identity['tools']['cl']['path'], '/nologo', '/MD', '/W4', f'/I{sdk / "inc"}',
                                                   f'/I{sdk / "inc" / "x64"}', probes / 'aint_check.c', f'/Fe{c_exe}',
                                                   f'/Fo{work / f"aint_check_{tag}.obj"}', '/link', f'/LIBPATH:{sdk / "lib"}',
                                                   'msmpi.lib'], work)
    Check(f'{label}build aint_check.c with fork SDK', built_c['exit_code'] == 0, built_c['stdout'][-400:])
    built_f = recorder.Run(f'build-aint-fortran-{tag}', [identity['tools']['ifx']['path'], '/nologo', '/libs:dll', '/threads',
                                                         f'/I{sdk / "inc"}', f'/I{sdk / "inc" / "x64"}', probes / 'aint_check.f90',
                                                         f'/exe:{f_exe}', f'/object:{work / f"aint_check_f_{tag}.obj"}', '/link',
                                                         f'/LIBPATH:{sdk / "lib"}', 'msmpifec.lib', 'msmpi.lib'], work)
    Check(f'{label}build aint_check.f90 with fork SDK (msmpifec aliases)', built_f['exit_code'] == 0, built_f['stdout'][-400:])
    library_output = ''
    for ranks in RANKS:
        if built_c['exit_code'] == 0:
            result = recorder.Run(f'aint-c-{tag}-{ranks}', [runtime / 'mpiexec.exe', '-n', str(ranks), c_exe], runtime)
            library_output += result['stdout']
            marker = f'AINT_C ranks={ranks} passed={ranks}'
            Check(f'{label}C MPI_Aint_add/diff at {ranks} ranks', result['exit_code'] == 0 and marker in result['stdout']
                  and result['stdout'].rstrip().endswith('PASS') and f'dll=[{runtime / "msmpi.dll"}]' in result['stdout'],
                  result['stdout'][-400:] + result['stderr'][-400:])
        if built_f['exit_code'] == 0:
            result = recorder.Run(f'aint-fortran-{tag}-{ranks}', [runtime / 'mpiexec.exe', '-n', str(ranks), f_exe], runtime)
            marker = f'AINT_FORTRAN ranks={ranks} passed={ranks} PASS'
            Check(f'{label}Fortran MPI_AINT_ADD/DIFF at {ranks} ranks', result['exit_code'] == 0 and marker in result['stdout'],
                  result['stdout'][-400:] + result['stderr'][-400:])
    return built_c, c_exe, library_output


def Main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--build', type=Path, default=ROOT / 'out/Release-x64/bin')
    parser.add_argument('--debug-build', type=Path, default=ROOT / 'out/Debug-x64/bin')
    parser.add_argument('--skip-debug', action='store_true', help='Omit the required Debug runtime stage (diagnostics only)')
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
    checks = []

    def Check(name, passed, detail):
        checks.append(dict(check=name, passed=bool(passed), detail=detail))

    built_c, c_exe, library_output = ProbeChecks('', sdk, runtime, work, identity, recorder, Check)
    native = IdentityChecks(build, runtime, recorder, Check, library_output)
    legacy_runtime = None
    if built_c['exit_code'] == 0:
        legacy_runtime = LaunchRegistration(identity, environment, work, runtime, c_exe, recorder, Check, native)
    AffinityChecks(identity, environment, work, runtime, sdk, recorder, Check, legacy_runtime)
    runtimes = {'release': {name: Sha256(runtime / name) for name in RUNTIME}}
    if not args.skip_debug:
        # Required: the Debug configuration (built separately by build.py --configuration Debug) must run.
        debug = args.debug_build.resolve()
        present = all((debug / name).is_file() for name in RUNTIME)
        Check(f'Debug runtime present in {debug}', present, [name for name in RUNTIME if not (debug / name).is_file()])
        if present:
            debug_runtime = work / 'runtime-debug'
            debug_runtime.mkdir()
            for name in RUNTIME:
                shutil.copy2(debug / name, debug_runtime / name)
            runtimes['debug'] = {name: Sha256(debug_runtime / name) for name in RUNTIME}
            debug_identity = json.loads((debug.parent / 'native-identity' / 'identity.json').read_text(encoding='utf-8'))
            for name in RUNTIME:
                strings = VersionStrings(debug_runtime / name) or {}
                Check(f'Debug {name} VERSIONINFO is {native["version"]} with VS_FF_DEBUG',
                      strings.get('fixed_file_version') == native['version'] == debug_identity['version']
                      and strings.get('file_flags', 0) & 0x1, strings)
            ProbeChecks('Debug ', debug / 'sdk', debug_runtime, work, identity, recorder, Check)
    summary = dict(kind='native-verify', accepted=False, utc=stamp, build=str(build), identity=native,
                   runtime=runtimes, checks=checks,
                   passed=sum(check['passed'] for check in checks), failed=[c['check'] for c in checks if not c['passed']],
                   commands=recorder.commands)
    (work / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(summary=str(work / 'summary.json'), passed=summary['passed'], total=len(checks),
                          failed=summary['failed']), indent=2))
    return 1 if summary['failed'] else 0


if __name__ == '__main__':
    sys.exit(Main())
