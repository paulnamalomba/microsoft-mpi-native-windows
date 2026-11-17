"""Internal version format and progression: <breaking>.<0-9>.<0-9>.<0-99> (AGENTS.md)."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build  # noqa: E402


def CheckBuildRecords(directory):
    """The versions of every build record in a directory. Raises ValueError when there is no record, or when a
    record's name or its previous, candidate or accepted field is not a canonical version of this fork."""
    records = sorted(directory.glob('*.json'))
    if not records:
        raise ValueError(f'No build records in {directory}')
    for record in records:
        content = json.loads(record.read_text(encoding='utf-8'))
        build.ParseVersion(record.stem)
        for field in ('previous', 'candidate', 'accepted'):
            if not isinstance(content.get(field), str):
                raise ValueError(f'{record.name} has no {field} version')
            build.ParseVersion(content[field])
        if content['accepted'] != record.stem:
            raise ValueError(f'{record.name} records accepted version {content["accepted"]}')
    return [record.stem for record in records]


class VersionTests(unittest.TestCase):
    def test_build_field_counts_through_99(self):
        self.assertEqual(build.NextVersion('10.1.4.2'), '10.1.4.3')
        self.assertEqual(build.NextVersion('10.1.4.9'), '10.1.4.10')
        self.assertEqual(build.NextVersion('10.1.4.10'), '10.1.4.11')
        self.assertEqual(build.NextVersion('10.1.4.98'), '10.1.4.99')

    def test_build_field_carries_at_99(self):
        self.assertEqual(build.NextVersion('10.1.4.99'), '10.1.5.0')
        self.assertEqual(build.NextVersion('10.1.9.99'), '10.2.0.0')
        self.assertEqual(build.NextVersion('10.9.9.99'), '11.0.0.0')

    def test_a_build_field_of_100_and_other_out_of_range_fields_are_refused(self):
        for text in ('10.1.4.100', '10.1.4.010', '10.1.4.00', '10.1.4.01', '10.1.10.0', '10.10.4.0', '010.1.4.0',
                     '10.1.4', '10.1.4.2.1', '10.1.4.x', '10.1.4.-1', ''):
            with self.subTest(text=text), self.assertRaises(ValueError):
                build.ParseVersion(text)
            with self.subTest(next=text), self.assertRaises(ValueError):
                build.NextVersion(text)

    def test_versions_order_numerically(self):
        self.assertLess(build.ParseVersion('10.1.4.9'), build.ParseVersion('10.1.4.10'))
        self.assertLess(build.ParseVersion('10.1.4.99'), build.ParseVersion('10.1.5.0'))
        self.assertEqual(build.ParseVersion('10.1.4.42'), (10, 1, 4, 42))

    def test_non_ascii_digits_are_refused(self):
        for text in ('1\u0660.1.4.2', '10.\u0661.4.2', '10.1.\u0664.2', '10.1.4.\u0662', '10.1.4.1\u0662', '10.1.4.\uff12'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                build.ParseVersion(text)

    def test_recorded_versions_stay_valid(self):
        root = Path(build.__file__).resolve().parents[1]
        build.ParseVersion((root / 'MSMPI_INTERNAL_VERSION').read_text(encoding='utf-8'))
        self.assertGreaterEqual(len(CheckBuildRecords(root / '.commits' / 'builds')), 2)

    def test_a_directory_without_records_fails_the_record_check(self):
        with tempfile.TemporaryDirectory() as scratch:
            with self.assertRaisesRegex(ValueError, 'No build records'):
                CheckBuildRecords(Path(scratch))
            with self.assertRaisesRegex(ValueError, 'No build records'):
                CheckBuildRecords(Path(scratch) / 'missing')

    def test_a_record_with_an_out_of_range_field_fails_the_record_check(self):
        good = dict(previous='10.1.4.1', candidate='10.1.4.2', accepted='10.1.4.2')
        cases = {'10.1.4.100.json': dict(good, candidate='10.1.4.100', accepted='10.1.4.100'),  # build field in the name
                 '10.1.10.0.json': dict(good, candidate='10.1.10.0', accepted='10.1.10.0'),     # minor field in the name
                 '10.1.4.3.json': dict(good, previous='10.1.4.100', candidate='10.1.4.3', accepted='10.1.4.3'),
                 '10.1.4.4.json': dict(good, candidate='10.1.4.4', accepted='10.10.4.4'),
                 '10.1.4.5.json': dict(good, candidate='10.1.4.5', accepted='10.1.4.6'),        # valid, but not this record
                 '10.1.4.6.json': dict(previous='10.1.4.5', candidate='10.1.4.6')}              # accepted is absent
        for name, content in cases.items():
            with self.subTest(record=name), tempfile.TemporaryDirectory() as scratch:
                (Path(scratch) / '10.1.4.2.json').write_text(json.dumps(good), encoding='utf-8')
                self.assertEqual(CheckBuildRecords(Path(scratch)), ['10.1.4.2'])
                (Path(scratch) / name).write_text(json.dumps(content), encoding='utf-8')
                with self.assertRaises(ValueError):
                    CheckBuildRecords(Path(scratch))

    def test_manage_repo_version_rule_passes_its_self_check(self):
        """.commits/manage-repo.ps1 holds its own copy of the rule; its check action covers 10.1.4.99, 10.1.4.100,
        10.10.4.0, non-ASCII digits and a trailing newline. PowerShell 7 is required, so a missing pwsh fails."""
        root = Path(build.__file__).resolve().parents[1]
        pwsh = shutil.which('pwsh')
        self.assertIsNotNone(pwsh, 'pwsh (PowerShell 7) is required for the manage-repo.ps1 self-check')
        result = subprocess.run([pwsh, '-NoProfile', '-NonInteractive', '-File', str(root / '.commits' / 'manage-repo.ps1'),
                                 '-Action', 'check'], capture_output=True, text=True, encoding='utf-8', timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('FAIL', result.stdout)
        for expected in ("ok    version accepts '10.1.4.99'", "ok    version rejects '10.1.4.100'",
                         "ok    version rejects '10.10.4.0'", "ok    version rejects '10.1.4.U+0660'",
                         "ok    version rejects '10.1.4.U+FF12'", "ok    version rejects '10.1.4.2U+000A'",
                         'checks: 28 passed, 0 failed'):
            self.assertIn(expected, result.stdout)


if __name__ == '__main__':
    unittest.main()
