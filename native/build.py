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


def PackagePins():
    return json.loads((Path(__file__).with_name('package-pins.json')).read_text(encoding='utf-8'))


def SourceTree(name, cache=ROOT / 'out/packages'):
    """Extract a pinned source archive, keeping only the pinned files and verifying each one's SHA-256."""
    import urllib.request
    import zipfile
    pin = PackagePins()['sources'][name]
    archive = cache / f'{name.lower()}-{pin["commit"]}.zip'
    if not archive.is_file():
        archive.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(pin['url'], timeout=120) as response:
            archive.write_bytes(response.read())
    tree = ROOT / 'out' / 'sources' / f'{name.lower()}-{pin["commit"]}'
    with zipfile.ZipFile(archive) as package:
        for relative, digest in pin['files'].items():
            data = package.read(pin['archive_prefix'] + relative)
            if hashlib.sha256(data).hexdigest() != digest:
                raise RuntimeError(f'{name} source file differs from its pin: {relative}')
            target = tree / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file() or target.read_bytes() != data:
                target.write_bytes(data)
    return tree, pin, hashlib.sha256(archive.read_bytes()).hexdigest()


def NetworkDirect(identity, environment, configuration, output):
    """Build ndutil.lib from the pinned NetworkDirect source with the pinned toolset.

    Mirrors src/ndutil/ndutil.vcxproj: precompiled precomp.h created by ndaddr.cpp, _LIB, and ndstatus.h from
    ndstatus.mc. Code generation follows this fork's configuration: Release /O2 /GL /guard:cf (matching the
    /LTCG and CFG link of msmpi.dll), Debug /Od without CFG, as upstream's Debug. Both use /MT /Zl like
    upstream mpi.props, with a PDB. Public symbols are compared against the NuGet 2.0.1 library in
    PublicSymbolDifferences().
    """
    tree, pin, archive_sha256 = SourceTree('NetworkDirect')
    source = tree / 'src' / 'ndutil'
    root = ROOT / 'out' / f'{configuration}-x64' / 'networkdirect'
    include, lib, objects = root / 'include', root / 'lib', root / 'obj'
    for directory in (include, lib, objects):
        directory.mkdir(parents=True, exist_ok=True)
    RunChecked([identity['tools']['mc']['path'], '-h', include, '-r', objects, source / 'ndstatus.mc'], environment,
               output / 'generate-ndstatus.log')
    for relative in pin['public_headers']:
        (include / Path(relative).name).write_bytes((tree / relative).read_bytes())
    optimization = (['/O2', '/Oi', '/GL', '/guard:cf', '/D', 'NDEBUG'] if configuration == 'Release'
                    else ['/Od', '/D', '_DEBUG'])
    # /sdl and /GR- match the NuGet 2.0.1 objects' symbol tables (__autoclassinit2 members, no RTTI descriptors).
    common = [identity['tools']['cl']['path'], '/nologo', '/c', '/W3', '/EHsc', '/GS', '/sdl', '/GR-', '/Gy', '/MT', '/Zl', '/Zi',
              f'/Fd{lib / "ndutil.pdb"}', '/D', '_LIB', *optimization, f'/I{source}', f'/I{include}',
              f'/Fp{objects / "ndutil.pch"}', f'/Fo{objects}\\']
    RunChecked([*common, '/Yc' + 'precomp.h', source / 'ndaddr.cpp'], environment, output / 'build-ndutil-ndaddr.log')
    RunChecked([*common, '/Yu' + 'precomp.h', source / 'ndfrmwrk.cpp', source / 'ndprov.cpp'], environment,
               output / 'build-ndutil.log')
    library = lib / 'ndutil.lib'
    RunChecked([identity['tools']['lib']['path'], '/NOLOGO', *(['/LTCG'] if configuration == 'Release' else []),
                f'/OUT:{library}', *[objects / f'{stem}.obj' for stem in ('ndaddr', 'ndfrmwrk', 'ndprov')]],
               environment, output / 'link-ndutil.log')
    reference = SupersededNuget('NetworkDirect')
    generated_status = (include / 'ndstatus.h').read_bytes().replace(b'\r\n', b'\n')
    status_matches = reference['ndstatus.h'].replace(b'\r\n', b'\n') == generated_status
    # The NuGet library is a Release build, so only the Release interface is comparable.
    differences = (PublicSymbolDifferences(identity, environment, reference['lib'], library)
                   if configuration == 'Release' else None)
    if differences or not status_matches:
        raise RuntimeError(f'Source-built ndutil differs from NuGet 2.0.1: symbols {differences}, '
                           f'ndstatus.h identical={status_matches}')
    return dict(generator='NetworkDirect ndutil', commit=pin['commit'], archive_sha256=archive_sha256, root=str(root),
                ndutil_sha256=hashlib.sha256(library.read_bytes()).hexdigest(), ndstatus_h_matches_nuget=status_matches,
                public_symbols_match_nuget=(differences == {}) if configuration == 'Release' else 'not applicable (Debug)')


def SupersededNuget(name, cache=ROOT / 'out/packages'):
    """The pinned NuGet package a source build replaced, verified by SHA-512, kept only as a comparison reference."""
    import base64
    import urllib.request
    import zipfile
    pin = PackagePins()['packages'][name]
    nuget = pin['superseded_nuget']
    archive = cache / f'{name.lower()}.{pin["version"]}.nupkg'
    if not archive.is_file():
        archive.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(nuget['url'], timeout=120) as response:
            archive.write_bytes(response.read())
    data = archive.read_bytes()
    if len(data) != nuget['bytes'] or base64.b64encode(hashlib.sha512(data).digest()).decode() != nuget['sha512_base64']:
        archive.unlink()
        raise RuntimeError(f'Reference package {name} {pin["version"]} differs from its pin')
    reference = cache / f'{name.lower()}.{pin["version"]}-reference'
    reference.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        (reference / 'ndutil.lib').write_bytes(package.read('lib/x64/ndutil.lib'))
        return dict(lib=reference / 'ndutil.lib', **{'ndstatus.h': package.read('include/ndstatus.h')})


def PublicSymbolDifferences(identity, environment, reference, library):
    """Archive public symbols present in only one library, ignoring two classes that carry no interface:
    the precompiled-header marker (__@@_PchSym_, derived from the build path) and the string-literal
    decoration change of newer compilers (??_C@...?$AA@ versus ??_C@...@)."""
    def Symbols(path):
        listing = subprocess.run([str(identity['tools']['dumpbin']['path']), '/nologo', '/linkermember:1', str(path)], env=environment,
                                 capture_output=True, text=True, encoding='utf-8', errors='replace', check=True).stdout
        names = set()
        for line in listing.splitlines():
            match = re.match(r'^\s+[0-9A-F]+\s+(\S+)$', line)
            if match and not match.group(1).startswith('__@@_PchSym_'):
                name = match.group(1)
                names.add(name[:-len('?$AA@')] + '@' if name.startswith('??_C@') and name.endswith('?$AA@') else name)
        return names
    old, new = Symbols(reference), Symbols(library)
    return dict(only_reference=sorted(old - new), only_source=sorted(new - old)) if old != new else {}


def Packages(cache=ROOT / 'out/packages'):
    """Download, verify and extract the pinned NuGet packages that upstream restored through CBT.

    A package whose provider is 'source' maps to its native targets file instead; NetworkDirect() builds it.
    """
    import base64
    import urllib.request
    import zipfile
    pins = PackagePins()['packages']
    extracted = {}
    for name, pin in pins.items():
        if pin.get('provider') == 'source':
            extracted[(name, pin['version'])] = ROOT / pin['targets']
            continue
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
    networkdirect = NetworkDirect(identity, environment, args.configuration, output)
    generated.append(networkdirect)
    properties['MsmpiNetworkDirectRoot'] = networkdirect['root']
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
