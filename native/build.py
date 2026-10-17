"""Native Windows build driver for the MS-MPI fork.

Usage:
  python -X utf8 native/build.py environment
  python -X utf8 native/build.py baseline [--configuration Release] [--candidate 10.1.4.1]

`baseline` builds every project in upstream dirs.proj order with the pinned
toolset, continues after failures, and writes an inventory. It is a port
diagnostic, never an accepted build. Every native build stamps the candidate
version (default: the version after MSMPI_INTERNAL_VERSION), the source commit
and the attempt into the shipped binaries; see NativeIdentity().
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
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


def MessageCompilations(configuration):
    """Native equivalents of the WDK MessageCompile items (WindowsUserModeDriver10.0 toolset).

    traceManifest.vcxproj: GenerateUserModeLoggingMacros=-um, PrefixMacroName=-p Trace,
    RemoveCharsFromSymbolName=-P EVENT_, GeneratedFilesBaseName=-z MpiTraceEvents.
    msmpiLaunchSvcMc.vcxproj: default names. Both write header and RC output to $(O).
    """
    intermediate = Path('x64') / configuration
    common = ROOT / 'src/mpi/common'
    service = ROOT / 'src/launchSvc'
    return [
        (common / 'mpitrace.man', ['-um', '-p', 'Trace', '-P', 'EVENT_', '-z', 'MpiTraceEvents'], common / intermediate),
        (service / 'launchsvcmsg.mc', [], service / intermediate),
    ]


def RunChecked(command, environment, log):
    result = subprocess.run([str(part) for part in command], cwd=ROOT, env=environment, capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=600)
    log.write_text(f'$ {command}\n{result.stdout}{result.stderr}', encoding='utf-8')
    if result.returncode:
        raise RuntimeError(f'Generator failed ({result.returncode}): {command}\n{result.stdout}{result.stderr}')
    return result


def Generate(identity, environment, configuration, output):
    """Produce the generated inputs that upstream obtains from WDK, Perl, gfortran and aliasobj."""
    records = []
    for index, (source, options, directory) in enumerate(MessageCompilations(configuration)):
        directory.mkdir(parents=True, exist_ok=True)
        command = [identity['tools']['mc']['path'], *options, '-h', directory, '-r', directory, source]
        RunChecked(command, environment, output / f'generate-mc-{index}.log')
        records.append(dict(generator='mc', source=str(source.relative_to(ROOT)), output=str(directory.relative_to(ROOT))))
    # mpicommon.vcxproj GenerateMPIErrorFiles: same arguments and working directory as the upstream Perl Exec.
    common = ROOT / 'src/mpi/common'
    (common / 'x64' / configuration).mkdir(parents=True, exist_ok=True)
    source_root, mpi_root = f'{ROOT}/src', f'{ROOT}/src\\mpi'
    command = [sys.executable, '-X', 'utf8', '-B', Path(__file__).with_name('extracterrmsgs.py'),
               r'-skip=obj\i386\errtest.c', f'-outpath=x64\\{configuration}', '-outfile=defmsg.h', '-testfile=errtest.c',
               *[f'{mpi_root}\\{name}' for name in ('common', 'msmpi', 'pmilib', 'mpiexec', 'smpd')], f'{source_root}\\launchSvc']
    result = subprocess.run([str(part) for part in command], cwd=common, env=environment, capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=600)
    (output / 'generate-extracterrmsgs.log').write_text(result.stdout + result.stderr, encoding='utf-8')
    if result.returncode:
        raise RuntimeError('extracterrmsgs reported errors:\n' + result.stderr)
    # fortran.props GenerateEmptyCFile + EditAlias: plain cl.exe -c on an empty C file, then the alias symbols.
    fortran = ROOT / 'src/mpi/msmpi/fortran'
    for project, convention in (('msmpifec', 'cdecl'), ('msmpifes', 'stdcall'), ('msmpifmc', 'cdecl'), ('msmpifms', 'stdcall')):
        directory = fortran / project / 'x64' / configuration
        directory.mkdir(parents=True, exist_ok=True)
        stem = directory / f'amd64.{convention}'
        stem.with_suffix('.c').write_text('//empty file \n', encoding='ascii')
        RunChecked([identity['tools']['cl']['path'], f'/Fo{stem}.obj', '-c', stem.with_suffix('.c')], environment,
                   output / f'generate-alias-{project}.log')
        alias = fortran / f'amd64.{convention}.alias'
        RunChecked([sys.executable, '-X', 'utf8', '-B', Path(__file__).with_name('aliasobj.py'), f'{stem}.obj', alias],
                   environment, output / f'generate-aliasobj-{project}.log')
        records.append(dict(generator='aliasobj.py', project=project, alias=str(alias.relative_to(ROOT)),
                            sha256=hashlib.sha256(Path(f'{stem}.obj').read_bytes()).hexdigest()))
    defmsg = common / 'x64' / configuration / 'defmsg.h'
    records.append(dict(generator='extracterrmsgs.py', output=str(defmsg.relative_to(ROOT)), summary=result.stdout.strip(),
                        sha256=hashlib.sha256(defmsg.read_bytes()).hexdigest()))
    return records


PACKAGE_REFERENCE = re.compile(r'<PackageReference\s+Include="([^"]+)"\s+Version="([^"]+)"\s*/>')


def Packages(cache=ROOT / 'out/packages'):
    """Download, verify and extract the pinned NuGet packages that upstream restored through CBT."""
    import base64
    import hashlib
    import urllib.request
    import zipfile
    pins = json.loads((Path(__file__).with_name('package-pins.json')).read_text(encoding='utf-8'))['packages']
    extracted = {}
    for name, pin in pins.items():
        archive = cache / f'{name.lower()}.{pin["version"]}.nupkg'
        if not archive.is_file():
            archive.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(pin['url'], timeout=120) as response:
                data = response.read()
            archive.write_bytes(data)
        data = archive.read_bytes()
        digest = base64.b64encode(hashlib.sha512(data).digest()).decode()
        if len(data) != pin['bytes'] or digest != pin['sha512_base64']:
            archive.unlink()
            raise RuntimeError(f'Package {name} {pin["version"]} differs from its pin')
        directory = cache / f'{name.lower()}.{pin["version"]}'
        if not (directory / pin['targets']).is_file():
            with zipfile.ZipFile(archive) as package:
                for member in package.infolist():
                    target = (directory / member.filename).resolve()
                    if not target.is_relative_to(directory.resolve()):
                        raise RuntimeError('Package entry escapes its directory: ' + member.filename)
                package.extractall(directory)
        extracted[(name, pin['version'])] = directory / pin['targets']
    return extracted


def ProjectImports(project, packages):
    """Package targets for the PackageReference items this project declares; unknown references fail."""
    imports = []
    for name, version in PACKAGE_REFERENCE.findall(project.read_text(encoding='utf-8')):
        if (name, version) not in packages:
            raise RuntimeError(f'{project.name} references unpinned package {name} {version}')
        imports.append(str(packages[(name, version)]))
    return imports


VERSION = re.compile(r'^(0|[1-9]\d*)\.([0-9])\.([0-9])\.([0-9])$')
LIBRARY_PRODUCT = 'MS-MPI native-windows'
ISSUES_URL = 'https://github.com/paulnamalomba/microsoft-mpi-native-windows/issues'


def ParseVersion(text):
    match = VERSION.match(text.strip())
    if not match:
        raise ValueError('Not a canonical <breaking>.<major>.<minor>.<build> version with single-digit fields: ' + text)
    return tuple(int(field) for field in match.groups())


def NextVersion(text):
    """The next build identity: build, minor and major are 0-9 and carry into the preceding field."""
    breaking, major, minor, build = ParseVersion(text)
    build += 1
    if build > 9:
        build, minor = 0, minor + 1
    if minor > 9:
        minor, major = 0, major + 1
    if major > 9:
        major, breaking = 0, breaking + 1
    return f'{breaking}.{major}.{minor}.{build}'


def SourceIdentity(root=ROOT):
    """Short commit, with +dirty when tracked or unignored files differ from it."""
    def Git(*arguments):
        return subprocess.run(['git', '-C', str(root), *arguments], capture_output=True, text=True, check=True).stdout.strip()
    return Git('rev-parse', '--short=7', 'HEAD') + ('-dirty' if Git('status', '--porcelain') else '')


def NativeIdentity(candidate=None, source=None):
    """Identity stamped into native builds. Persisting acceptance is a separate, later step."""
    accepted = (ROOT / 'MSMPI_INTERNAL_VERSION').read_text(encoding='utf-8').strip()
    version = candidate or NextVersion(accepted)
    if ParseVersion(version) <= ParseVersion(accepted):
        raise ValueError(f'Candidate {version} must follow the accepted version {accepted}')
    source = source or SourceIdentity()
    run = os.environ.get('GITHUB_RUN_ID')
    attempt = f'ci{run}.{os.environ.get("GITHUB_RUN_ATTEMPT", "1")}' if run else 'local'
    library = f'{LIBRARY_PRODUCT} {version} (source {source})'
    if len(library) >= 64:
        raise ValueError('MPI_Get_library_version string exceeds MPI_MAX_LIBRARY_VERSION_STRING: ' + library)
    return dict(accepted=accepted, version=version, source=source, attempt=attempt, library=library,
                product_version=f'{version}+{source}.{attempt}')


def WriteIdentityHeader(configuration, native):
    """msmpi_native_version.h for C/C++ (library string, mpiexec banner) and native/version.rc."""
    fields = ParseVersion(native['version'])
    directory = ROOT / 'out' / f'{configuration}-x64' / 'native-identity'
    directory.mkdir(parents=True, exist_ok=True)
    comments = (f'Derived from Microsoft MPI 10.1.12498.16 source. Source {native["source"]}, attempt {native["attempt"]}. '
                'https://github.com/paulnamalomba/microsoft-mpi-native-windows')
    lines = ['// Generated by native/build.py. Do not edit.', '#pragma once',
             *[f'#define MSMPI_NATIVE_VERSION_{name} {value}' for name, value in zip(('BREAKING', 'MAJOR', 'MINOR', 'BUILD'), fields)],
             f'#define MSMPI_NATIVE_FILEVERSION {",".join(str(field) for field in fields)}',
             f'#define MSMPI_NATIVE_FILEFLAGS {"0x1L" if configuration == "Debug" else "0x0L"}',
             f'#define MSMPI_NATIVE_VERSION_STR "{native["version"]}"',
             f'#define MSMPI_NATIVE_PRODUCTVERSION_STR "{native["product_version"]}"',
             f'#define MSMPI_NATIVE_SOURCE_STR "{native["source"]}"',
             f'#define MSMPI_NATIVE_LIBRARY_STR "{native["library"]}"',
             f'#define MSMPI_NATIVE_PRODUCT_WSTR L"{LIBRARY_PRODUCT}"',
             f'#define MSMPI_NATIVE_LABEL_WSTR L" (source {native["source"]})"',
             f'#define MSMPI_NATIVE_ISSUES_WSTR L"{ISSUES_URL}"',
             f'#define MSMPI_NATIVE_COMMENTS_STR "{comments}"', '']
    header = directory / 'msmpi_native_version.h'
    text = '\n'.join(lines)
    if not header.is_file() or header.read_text(encoding='utf-8') != text:
        header.write_text(text, encoding='utf-8')
    (directory / 'identity.json').write_text(json.dumps(dict(native, configuration=configuration), indent=2) + '\n',
                                             encoding='utf-8')
    return directory


def GlobalProperties(configuration, native):
    pins = Pins()
    return {'MSMPI_NATIVE': 'true', 'Configuration': configuration, 'Platform': 'x64',
            'PlatformToolset': 'v143', 'VCToolsVersion': pins['visual_studio']['toolset_version'],
            'WindowsTargetPlatformVersion': pins['host_tools']['windows_sdk'],
            'MsmpiNativeInclude': str(WriteIdentityHeader(configuration, native)), 'MsmpiNativeRoot': str(ROOT)}


def Baseline(args):
    environment, identity = NativeEnvironment(ROOT)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = ROOT / 'out' / 'native-baseline' / stamp
    output.mkdir(parents=True)
    native = NativeIdentity(args.candidate)
    properties = GlobalProperties(args.configuration, native)
    generated = Generate(identity, environment, args.configuration, output)
    packages = Packages()
    results = []
    for index, project in enumerate(Projects()):
        label = f'{index:02d}-{project.stem}'
        log = output / (label + '.log')
        imports = ProjectImports(project, packages)
        command = [identity['tools']['msbuild']['path'], str(project), '/nologo', '/nr:false', '/m:1',
                   '/v:minimal', f'/flp:LogFile={log};Verbosity=normal;Encoding=UTF-8',
                   *[f'/p:{key}={value}' for key, value in properties.items()],
                   *([f'/p:ForceImportBeforeCppTargets={";".join(imports)}'] if imports else [])]
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
    summary = dict(kind='port-baseline-diagnostic', accepted=False, utc=stamp, identity=native, properties=properties,
                   toolchain=dict(visual_studio=identity['visual_studio']['version'], toolset=identity['visual_studio']['toolset'],
                                  windows_sdk=identity['windows_sdk']['version'], tools=identity['tools']),
                   generated=generated, projects=results, failed=[r['project'] for r in results if r['exit_code']])
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(dict(summary=str(output / 'summary.json'), failed=summary['failed']), indent=2))
    return 1 if summary['failed'] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('environment', 'baseline'))
    parser.add_argument('--configuration', choices=('Release', 'Debug'), default='Release')
    parser.add_argument('--candidate', help='Version to stamp; default is the version after MSMPI_INTERNAL_VERSION')
    args = parser.parse_args()
    if args.command == 'environment':
        environment, identity = NativeEnvironment(ROOT)
        print(json.dumps(identity['tools'], indent=2))
        return 0
    return Baseline(args)


if __name__ == '__main__':
    sys.exit(main())
