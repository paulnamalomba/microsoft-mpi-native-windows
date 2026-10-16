"""Native Windows build driver for the MS-MPI fork.

Usage:
  python -X utf8 native/build.py environment
  python -X utf8 native/build.py baseline [--configuration Release]

`baseline` builds every project in upstream dirs.proj order with the pinned
toolset, continues after failures, and writes an inventory. It is a port
diagnostic, never an accepted build.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from environment import NativeEnvironment, Pins  # noqa: E402

PROJECT_ITEM = re.compile(r'<ProjectFile\s+Include="\$\(MSBuildThisFileDirectory\)([^"]+)"\s*/?>')
ERROR_LINE = re.compile(r'(?:error|fatal error) [A-Z]+\d+|: error :|error MSB\d+', re.I)


def Projects(traversal=ROOT / 'src/dirs.proj'):
    """Expand upstream dirs.proj files into the ordered list of buildable projects."""
    ordered = []
    for relative in PROJECT_ITEM.findall(traversal.read_text(encoding='utf-8')):
        path = (traversal.parent / relative.replace('\\', '/')).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise ValueError('Traversal entry is outside the checkout or missing: ' + relative)
        ordered += Projects(path) if path.name == 'dirs.proj' else [path]
    return ordered


def GlobalProperties(configuration):
    pins = Pins()
    return {'MSMPI_NATIVE': 'true', 'Configuration': configuration, 'Platform': 'x64',
            'PlatformToolset': 'v143', 'VCToolsVersion': pins['visual_studio']['toolset_version'],
            'WindowsTargetPlatformVersion': pins['host_tools']['windows_sdk']}


def Baseline(args):
    environment, identity = NativeEnvironment(ROOT)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = ROOT / 'out' / 'native-baseline' / stamp
    output.mkdir(parents=True)
    properties = GlobalProperties(args.configuration)
    results = []
    for index, project in enumerate(Projects()):
        label = f'{index:02d}-{project.stem}'
        log = output / (label + '.log')
        command = [identity['tools']['msbuild']['path'], str(project), '/nologo', '/nr:false', '/m:1',
                   '/v:minimal', f'/flp:LogFile={log};Verbosity=normal;Encoding=UTF-8',
                   *[f'/p:{key}={value}' for key, value in properties.items()]]
        started = time.monotonic()
        result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=3600)
        errors = [line.strip() for line in (log.read_text(encoding='utf-8', errors='replace') if log.exists() else result.stdout).splitlines()
                  if ERROR_LINE.search(line)]
        record = dict(project=str(project.relative_to(ROOT)), exit_code=result.returncode,
                      elapsed_seconds=round(time.monotonic() - started, 1), log=str(log.relative_to(ROOT)),
                      error_count=len(errors), first_errors=list(dict.fromkeys(errors))[:8])
        results.append(record)
        print(f'{label}: exit {result.returncode}, {len(errors)} error lines', flush=True)
    summary = dict(kind='port-baseline-diagnostic', accepted=False, utc=stamp, properties=properties,
                   toolchain=dict(visual_studio=identity['visual_studio']['version'], toolset=identity['visual_studio']['toolset'],
                                  windows_sdk=identity['windows_sdk']['version'], tools=identity['tools']),
                   projects=results, failed=[r['project'] for r in results if r['exit_code']])
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(summary=str(output / 'summary.json'), failed=summary['failed']), indent=2))
    return 1 if summary['failed'] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('environment', 'baseline'))
    parser.add_argument('--configuration', choices=('Release', 'Debug'), default='Release')
    args = parser.parse_args()
    if args.command == 'environment':
        environment, identity = NativeEnvironment(ROOT)
        print(json.dumps(identity['tools'], indent=2))
        return 0
    return Baseline(args)


if __name__ == '__main__':
    sys.exit(main())
