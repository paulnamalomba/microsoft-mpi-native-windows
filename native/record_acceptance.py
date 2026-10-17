"""Write the immutable acceptance record of a hosted candidate build.

Usage: python -X utf8 native/record_acceptance.py --run-id ID --download DIR --package-check SUMMARY
                                                 [--limitation TEXT ...]

DIR is `gh run download ID` output (msmpi-native-windows-x64 and
msmpi-native-windows-evidence). SUMMARY is the native/check_package.py summary
for the downloaded zip. The record is written only when the run succeeded at
the checked-out source, every hosted step passed, the zip's stamped version is
the next version after MSMPI_INTERNAL_VERSION, and the local package check
passed. It writes .commits/builds/<version>.json in the shared record schema
and prints the facts for the family note. Persisting MSMPI_INTERNAL_VERSION
and the note in the same commit stays a separate, reviewed step.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build import NextVersion  # noqa: E402


def Sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def Main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--run-id', type=int, required=True)
    parser.add_argument('--download', type=Path, required=True)
    parser.add_argument('--package-check', type=Path, required=True)
    parser.add_argument('--limitation', action='append', default=[])
    args = parser.parse_args()
    accepted = (ROOT / 'MSMPI_INTERNAL_VERSION').read_text(encoding='utf-8').strip()
    candidate = NextVersion(accepted)
    view = json.loads(subprocess.run(['gh', 'run', 'view', str(args.run_id), '--repo', 'paulnamalomba/microsoft-mpi-native-windows',
                                      '--json', 'headSha,conclusion,event,url,jobs'], capture_output=True, text=True, check=True).stdout)
    job = view['jobs'][0]
    failed_steps = [step['name'] for step in job['steps'] if step['conclusion'] != 'success']
    archives = sorted((args.download / 'msmpi-native-windows-x64').glob('*.zip'))
    if view['conclusion'] != 'success' or failed_steps or len(archives) != 1:
        raise SystemExit(f'Run {args.run_id} is not a complete success: {view["conclusion"]}, failed {failed_steps}, {len(archives)} zips')
    archive = archives[0]
    with zipfile.ZipFile(archive) as package:
        manifest = json.loads(package.read(f'{archive.stem}/manifest.json'))
    if manifest['version'] != candidate:
        raise SystemExit(f'Candidate {manifest["version"]} is not the next version after {accepted} ({candidate})')
    if manifest['source']['commit'] != view['headSha'] or manifest['identity']['source'].endswith('-dirty'):
        raise SystemExit('Package source is not the clean run commit')
    check = json.loads(args.package_check.read_text(encoding='utf-8'))
    if check['failed'] or check['archive_sha256'] != Sha256(archive) or check['version'] != candidate:
        raise SystemExit('Local package check did not pass for this archive')
    evidence_root = next(path for path in args.download.joinpath('msmpi-native-windows-evidence').rglob('out') if path.is_dir())
    summaries = {path.relative_to(evidence_root).as_posix(): json.loads(path.read_text(encoding='utf-8'))
                 for path in evidence_root.rglob('summary.json')}
    verify = next(value for value in summaries.values() if value.get('kind') == 'native-verify')
    stress = next(value for value in summaries.values() if value.get('kind') == 'native-stress')
    baselines = [value for value in summaries.values() if value.get('kind') == 'port-baseline-diagnostic']
    if verify['failed'] or stress['failed'] or any(value['failed'] for value in baselines) or len(baselines) != 2:
        raise SystemExit('Hosted evidence contains a failed check')
    installs = []
    for path in args.download.joinpath('msmpi-native-windows-evidence').rglob('result.json'):
        result = json.loads(path.read_text(encoding='utf-8-sig'))
        installs.append(dict(component=result['component'], version=result['requestedVersion'], installer_sha256=result['sha256'].lower(),
                             exit_code=result['exitCode'], authenticode=result['signatureStatus'], signer=result['signatureSubject']))
    toolchain = baselines[0]['toolchain']
    utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    files = manifest['files']
    record = dict(
        schema=2, product='microsoft-mpi-native-windows', previous=accepted, candidate=candidate, accepted=candidate, status='passed',
        artifact_produced=True, utc=utc, family_note=f'.commits/{".".join(candidate.split(".")[:3])}.txt',
        source=dict(commit=view['headSha'], stamped_source=manifest['identity']['source'], clean=True,
                    upstream='microsoft/Microsoft-MPI f2d849ff483b36add9bc2d18c6d7bbc9ce38f4ad (declares 10.1.12498.16)'),
        build=dict(kind='hosted clean checkout', ci_run=view['url'], run_id=args.run_id, event=view['event'], runner='windows-2022 (GitHub-hosted)',
                   started=job['startedAt'], completed=job['completedAt'], product_version=manifest['identity']['product_version'],
                   configurations=[value['properties']['Configuration'] for value in baselines]),
        toolchain=dict(visual_studio_build_tools=toolchain['visual_studio'], msvc_toolset=toolchain['toolset'], windows_sdk=toolchain['windows_sdk'],
                       installs=sorted(installs, key=lambda item: item['component']), pins=manifest['pins']),
        checks=dict(hosted_steps={step['name']: step['conclusion'] for step in job['steps']},
                    baselines=[dict(configuration=value['properties']['Configuration'], projects=len(value['projects']), failed=value['failed'])
                               for value in baselines],
                    verify=dict(passed=verify['passed'], total=len(verify['checks']), checks=[c['check'] for c in verify['checks']]),
                    stress=dict(passed=stress['passed'], launches=stress['launches'], rejections=stress['rejections'],
                                affinity_mask=stress['affinity_mask'], logical_processors=stress['logical_processors']),
                    local_package_check=dict(passed=check['passed'], total=len(check['checks']), utc=check['utc']),
                    downstream='Required check 5 (PETSc fork and SESKA requalification) gates SESKA adoption, not this acceptance'),
        evidence={name: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest() for name, value in summaries.items()},
        artifacts=[dict(path=archive.name, bytes=archive.stat().st_size, sha256=Sha256(archive))]
                  + [dict(path=f'{archive.name}!{entry["path"]}', bytes=entry['bytes'], sha256=entry['sha256']) for entry in files],
        limitations=['Not code-signed', 'Third-party notice review (G20) open', 'NetworkDirect/RDMA transport untested (no hardware)',
                     *args.limitation])
    target = ROOT / '.commits/builds' / f'{candidate}.json'
    if target.exists():
        raise SystemExit(f'{target} exists; acceptance records are immutable')
    target.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(record=str(target), previous=accepted, accepted=candidate, source=view['headSha'], run=view['url'],
                          zip=dict(name=archive.name, sha256=record['artifacts'][0]['sha256'], bytes=record['artifacts'][0]['bytes']),
                          verify=f'{verify["passed"]}/{len(verify["checks"])}', stress=f'{stress["passed"]}/{stress["launches"]}',
                          package_check=f'{check["passed"]}/{len(check["checks"])}', started=job['startedAt'], completed=job['completedAt']), indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(Main())
