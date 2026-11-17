"""Internal version format and progression: <breaking>.<0-9>.<0-9>.<0-99> (AGENTS.md)."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build  # noqa: E402


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

    def test_recorded_versions_stay_valid(self):
        root = Path(build.__file__).resolve().parents[1]
        build.ParseVersion((root / 'MSMPI_INTERNAL_VERSION').read_text(encoding='utf-8'))
        for record in sorted((root / '.commits' / 'builds').glob('*.json')):
            with self.subTest(record=record.name):
                build.ParseVersion(record.stem)


if __name__ == '__main__':
    unittest.main()
