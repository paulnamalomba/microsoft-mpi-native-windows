"""Environment isolation contracts for the native MS-MPI build."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build  # noqa: E402
from environment import CWD_LOOKUP_GUARD, Clean, VendorSession  # noqa: E402


class EnvironmentTests(unittest.TestCase):
    def test_vendor_session_allows_current_directory_lookup_only_for_vendor_scripts(self):
        original = {'PATH': r'C:\Windows', 'NoDefaultCurrentDirectoryInExePath': '1', 'TEMP': r'C:\Temp'}
        session = VendorSession(original)
        self.assertNotIn('NoDefaultCurrentDirectoryInExePath', session)
        self.assertEqual(session['PATH'], r'C:\Windows')
        self.assertEqual(original['NoDefaultCurrentDirectoryInExePath'], '1')
        self.assertEqual(CWD_LOOKUP_GUARD, 'NODEFAULTCURRENTDIRECTORYINEXEPATH')

    def test_clean_keeps_native_paths_and_drops_compatibility_layers_and_overrides(self):
        environment = {'Path': ';'.join([r'C:\Windows\System32', r'C:\cygwin64\bin', r'C:\Program Files\Git\usr\bin',
                                          r'C:\msys64\usr\bin', r'C:\Strawberry\perl\bin', r'C:\Program Files\Git\cmd']),
                       'CFLAGS': '-O0', 'CC': 'gcc', 'LIB': r'C:\mingw64\lib;C:\Kits\Lib'}
        clean = Clean(environment)
        self.assertEqual(clean['PATH'], r'C:\Windows\System32;C:\Program Files\Git\cmd')
        self.assertEqual(clean['LIB'], r'C:\Kits\Lib')
        self.assertNotIn('CFLAGS', clean)
        self.assertNotIn('CC', clean)

    def test_traversal_lists_every_upstream_project_once_in_order(self):
        projects = [path.relative_to(build.ROOT).as_posix() for path in build.Projects()]
        self.assertEqual(len(projects), len(set(projects)))
        self.assertEqual(projects[0], 'src/include/binplace.proj')
        self.assertIn('src/mpi/msmpi/dll/msmpi.vcxproj', projects)
        self.assertIn('src/mpi/smpd/smpd.vcxproj', projects)
        self.assertLess(projects.index('src/mpi/common/mpicommon.vcxproj'), projects.index('src/mpi/msmpi/dll/msmpi.vcxproj'))


if __name__ == '__main__':
    unittest.main()
