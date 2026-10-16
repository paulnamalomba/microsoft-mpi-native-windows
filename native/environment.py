"""Pinned native Windows build environment for the MS-MPI fork.

Captures the Visual Studio Build Tools and Intel oneAPI vendor environment,
removes excluded compatibility layers from search paths, and verifies every
tool and pinned installed file before a build may use it.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

PINS_FILE = Path(__file__).with_name('toolchain-pins.json')
FORBIDDEN = re.compile(r'(?:cygwin|msys|mingw|strawberry|[\\/]Git[\\/](?:usr|mingw64|bin)(?:[\\/]|$))', re.I)
SEARCH_PATHS = ('PATH', 'INCLUDE', 'LIB', 'LIBPATH', 'CPATH', 'C_INCLUDE_PATH', 'CPLUS_INCLUDE_PATH', 'LIBRARY_PATH')
COMPILER_OVERRIDES = ('CC', 'CXX', 'FC', 'F77', 'F90', 'CFLAGS', 'CXXFLAGS', 'FCFLAGS', 'FFLAGS', 'LDFLAGS', 'LIBS',
                      'CL', '_CL_', 'LINK', '_LINK_', 'ICXCFG', 'IFXCFG', 'IFORTCFG', 'MSYSTEM', 'CYGWIN', 'MAKEFLAGS')
# Intel's setvars.bat calls each component's env\vars.bat by relative name. When
# NoDefaultCurrentDirectoryInExePath is set, cmd.exe skips the current directory,
# every component fails with "'vars.bat' is not recognized" and setvars still exits 0.
CWD_LOOKUP_GUARD = 'NODEFAULTCURRENTDIRECTORYINEXEPATH'
TOOLS = {'cl': 'cl.exe', 'link': 'link.exe', 'lib': 'lib.exe', 'dumpbin': 'dumpbin.exe',
         'midl': 'midl.exe', 'mc': 'mc.exe', 'rc': 'rc.exe', 'ifx': 'ifx.exe'}


def Pins():
    return json.loads(PINS_FILE.read_text(encoding='utf-8'))


def Sha256(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def Clean(environment):
    clean = {key.upper(): value for key, value in environment.items()}
    for name in SEARCH_PATHS:
        if name in clean:
            clean[name] = os.pathsep.join(part for part in clean[name].split(os.pathsep) if part and not FORBIDDEN.search(part))
    for name in COMPILER_OVERRIDES:
        clean.pop(name, None)
    return clean


def VerifyInstalled(root, files, label):
    observed = {}
    for relative, pin in (files or {}).items():
        path = Path(root) / relative
        if not path.is_file():
            raise RuntimeError(f'{label} pinned file is missing: {path}')
        digest = Sha256(path)
        if digest != pin['sha256']:
            raise RuntimeError(f'{label} file differs from its pin: {path}')
        observed[relative] = digest
    return observed


def VendorSession(environment):
    """Environment for running vendor scripts, which need cmd.exe current-directory lookup."""
    return {key: value for key, value in environment.items() if key.upper() != CWD_LOOKUP_GUARD}


def Capture(scripts, environment, cwd):
    for script in scripts:
        if any(character in str(script) for character in '"\r\n%!'):
            raise ValueError('Unsupported vendor environment-script path: ' + str(script))
    with tempfile.TemporaryDirectory(prefix='msmpi-native-env-') as temporary:
        batch = Path(temporary) / 'capture.cmd'
        lines = ['@echo off']
        for script, arguments in scripts.items():
            lines += [f'call "{script}" {arguments} >nul', 'if errorlevel 1 exit /b 1']
        batch.write_text('\n'.join(lines + ['set']) + '\n', encoding='utf-8')
        command = f'"{environment["COMSPEC"]}" /d /u /s /c ""{batch}""'
        result = subprocess.run(command, cwd=cwd, env=VendorSession(environment), shell=False, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError('Vendor environment initialization failed: ' + result.stderr.decode('utf-16le', errors='replace'))
    captured = {}
    for line in result.stdout.decode('utf-16le', errors='strict').splitlines():
        key, separator, value = line.partition('=')
        if separator and key:
            captured[key.upper()] = value
    return captured


def NativeEnvironment(root):
    if os.name != 'nt':
        raise RuntimeError('The MS-MPI native build runs on native Windows only')
    pins = Pins()
    base = Clean(os.environ)
    vswhere = Path(os.environ['ProgramFiles(x86)']) / 'Microsoft Visual Studio/Installer/vswhere.exe'
    listing = subprocess.run([str(vswhere), '-products', 'Microsoft.VisualStudio.Product.BuildTools',
                              '-requires', 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-format', 'json'],
                             capture_output=True, text=True, check=True, timeout=60)
    instances = [item for item in json.loads(listing.stdout)
                 if item['installationVersion'] == pins['visual_studio']['installation_version']]
    if len(instances) != 1:
        raise RuntimeError('Expected exactly one VS Build Tools instance at ' + pins['visual_studio']['installation_version'])
    studio = Path(instances[0]['installationPath'])
    intel = next((Path(os.environ[key]) / 'Intel/oneAPI' for key in ('ProgramFiles(x86)', 'ProgramFiles')
                  if (Path(os.environ[key]) / 'Intel/oneAPI/setvars.bat').is_file()), None)
    if intel is None:
        raise RuntimeError('Intel oneAPI is missing; install the pinned ifx compiler')
    captured = Capture({studio / 'VC/Auxiliary/Build/vcvars64.bat': '', intel / 'setvars.bat': '--force'}, base, root)
    environment = Clean(captured)
    if CWD_LOOKUP_GUARD in base:
        environment[CWD_LOOKUP_GUARD] = base[CWD_LOOKUP_GUARD]
    if not environment.get('ONEAPI_ROOT') or not environment.get('CMPLR_ROOT'):
        raise RuntimeError('Intel oneAPI components did not initialize (setvars.bat set no ONEAPI_ROOT/CMPLR_ROOT)')
    sdk = environment.get('WINDOWSSDKVERSION', '').rstrip('\\/')
    if sdk != pins['host_tools']['windows_sdk']:
        raise RuntimeError('Windows SDK differs from the pin: ' + sdk)
    toolset = environment.get('VCTOOLSVERSION', '').rstrip('\\/')
    if toolset != pins['visual_studio']['toolset_version']:
        raise RuntimeError('MSVC toolset differs from the pin: ' + toolset)
    tools = {}
    for name, executable in TOOLS.items():
        path = shutil.which(executable, path=environment['PATH'])
        if not path or FORBIDDEN.search(path):
            raise RuntimeError('Native tool missing or excluded: ' + executable)
        tools[name] = dict(path=str(Path(path).resolve()), sha256=Sha256(path))
    msbuild = studio / 'MSBuild/Current/Bin/amd64/MSBuild.exe'
    if not msbuild.is_file():
        raise RuntimeError('MSBuild is missing from the pinned VS Build Tools instance')
    tools['msbuild'] = dict(path=str(msbuild), sha256=Sha256(msbuild))
    for name in ('cl', 'link', 'lib', 'dumpbin'):
        if not Path(tools[name]['path']).is_relative_to(studio):
            raise RuntimeError(f'{name} did not resolve inside the pinned VS instance: {tools[name]["path"]}')
    if Path(tools['ifx']['path']) != (intel / pins['ifx']['executable']).resolve():
        raise RuntimeError('ifx did not resolve to the pinned Intel compiler: ' + tools['ifx']['path'])
    identity = dict(visual_studio=dict(path=str(studio), version=instances[0]['installationVersion'], toolset=toolset,
                                       installed_files=VerifyInstalled(studio, pins['visual_studio'].get('installed_files'), 'Visual Studio')),
                    windows_sdk=dict(version=sdk, installed_files=VerifyInstalled(environment['WINDOWSSDKDIR'],
                                     pins['windows_sdk'].get('installed_files'), 'Windows SDK')),
                    ifx=dict(installed_files=VerifyInstalled(intel, pins['ifx'].get('installed_files'), 'ifx')),
                    tools=tools, pins_sha256=Sha256(PINS_FILE))
    return environment, identity


if __name__ == '__main__':
    environment, identity = NativeEnvironment(Path(__file__).resolve().parents[1])
    print(json.dumps(dict(tools=identity['tools'], visual_studio=identity['visual_studio']['version'],
                          toolset=identity['visual_studio']['toolset'], windows_sdk=identity['windows_sdk']['version']), indent=2))
