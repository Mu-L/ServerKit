"""Stdlib tests for accounting boundaries, missing data and real file sampling."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('measure_runtime', Path(__file__).parents[1] / 'measure-runtime.py')
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)

INSTALL_SPEC = importlib.util.spec_from_file_location('measure_installed', Path(__file__).with_name('measure-installed-runtime.py'))
installed = importlib.util.module_from_spec(INSTALL_SPEC)
INSTALL_SPEC.loader.exec_module(installed)

REPORT_SPEC = importlib.util.spec_from_file_location('vm_report', Path(__file__).with_name('report.py'))
vm_report = importlib.util.module_from_spec(REPORT_SPEC)
REPORT_SPEC.loader.exec_module(vm_report)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'service'
        self.path.mkdir()
        (self.path / 'memory.current').write_text('104857600\n')
        (self.path / 'cpu.stat').write_text('usage_usec 500000\nuser_usec 400000\nsystem_usec 100000\n')
        self.target = {'name': 'serverkit.service', 'kind': 'unit',
                       'cgroup': str(self.path), 'inode': self.path.stat().st_ino}

    def row(self, usage, clock):
        return {'cpu_stat': {'usage_usec': usage}, 'monotonic_seconds': clock, 'error': None}

    def test_cpu_normalizes_by_actual_time_and_one_core(self):
        self.assertEqual(runtime.cpu_percent(self.row(0, 10), self.row(2_000_000, 12)), 100)
        self.assertEqual(runtime.cpu_percent(self.row(0, 10), self.row(4_000_000, 12)), 200)
        self.assertEqual(runtime.cpu_percent(self.row(0, 10), self.row(500_000, 20)), 5)
        self.assertIsNone(runtime.cpu_percent(None, self.row(2_000_000, 12)))

    def test_counter_reset_is_invalid_not_zero_cpu(self):
        current = self.row(0, 12)
        self.assertIsNone(runtime.cpu_percent(self.row(100, 10), current))
        self.assertIsNotNone(current['error'])

    def test_missing_optional_data_stays_null(self):
        row = runtime.read_target(self.target)
        self.assertEqual(row['memory_bytes'], 104857600)
        self.assertIsNone(row['swap_bytes'])
        self.assertIsNone(row['tasks'])
        self.assertIsNone(row['io_by_device'])
        self.assertIn('io.stat', row['unavailable'])
        self.assertIsNone(row['error'])

    def test_missing_required_data_and_replaced_group_fail(self):
        (self.path / 'memory.current').unlink()
        self.assertEqual(runtime.read_target(self.target)['error'], 'required_counters_unavailable')
        self.target['inode'] += 1
        self.assertEqual(runtime.read_target(self.target)['error'], 'cgroup_replaced')

    def test_device_io_and_memory_breakdown_preserved(self):
        (self.path / 'io.stat').write_text('8:0 rbytes=12 wbytes=20 rios=1 wios=2\n253:0 rbytes=12 wbytes=20\n')
        (self.path / 'memory.stat').write_text('anon 100\nfile 50\nshmem 20\n')
        row = runtime.read_target(self.target)
        self.assertEqual(row['io_by_device']['8:0']['wbytes'], 20)
        self.assertEqual(len(row['io_by_device']), 2)
        self.assertEqual(row['memory_stat']['file'], 50)

    def test_reject_root_and_traversal(self):
        for group in ('/', '', '/../../etc', 'relative'):
            with self.subTest(group=group), self.assertRaises(ValueError):
                runtime.group_path(group, Path(self.temp.name))

    def test_reject_overlapping_scopes(self):
        for second in (self.path, self.path / 'child'):
            with self.subTest(second=second), self.assertRaises(ValueError):
                runtime.validate_scopes([self.target, {'cgroup': str(second)}])

    def test_real_sampling_and_summary_exclude_failures(self):
        with patch.object(runtime, 'host_memory', return_value={'MemAvailable': 1024}):
            samples, interrupted = runtime.collect([self.target], .025, .01)
        self.assertFalse(interrupted)
        self.assertGreaterEqual(len(samples), 2)
        summary = runtime.summarize(samples, [self.target])[0]
        self.assertEqual(summary['memory_bytes']['p50'], 104857600)
        self.assertEqual(summary['cpu_percent_one_core']['count'], len(samples) - 1)
        self.assertEqual(summary['swap_bytes']['count'], 0)
        self.assertIsNone(summary['swap_bytes']['sampled_max'])
        samples[-1]['targets'][0]['error'] = 'cgroup_replaced'
        samples[-1]['targets'][0]['memory_bytes'] = 999999999999
        summary = runtime.summarize(samples, [self.target])[0]
        self.assertEqual(summary['failed_samples'], 1)
        self.assertEqual(summary['memory_bytes']['sampled_max'], 104857600)

    def test_nonfinite_durations_rejected_before_discovery(self):
        for duration in ('nan', 'inf', '-1'):
            with self.subTest(duration=duration), patch('sys.stderr'), self.assertRaises(SystemExit) as exc:
                runtime.main(['--unit', 'serverkit', '--scenario', 'idle', '--revision', 'test',
                              '--duration', duration, '--output', str(self.path / 'out.json')])
            self.assertEqual(exc.exception.code, 2)

    def test_vm_report_preserves_unavailable_and_escapes_names(self):
        self.assertIn('unavailable', vm_report.runtime_table(None))
        self.assertIn('cgroup v2', vm_report.runtime_table({'status': 'unavailable', 'reason': 'cgroup v2'}))
        rendered = vm_report.runtime_table({'status': 'measured', 'summary': [{
            'name': '<script>', 'memory_bytes': {'p50': 1048576, 'sampled_max': 2097152},
            'cpu_percent_one_core': {'p50': 0, 'p95': None},
        }]})
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertIn('1.00', rendered)
        self.assertIn('unavailable', rendered)

    def test_install_wrapper_marks_unsupported_without_sampling(self):
        with patch.object(installed.platform, 'system', return_value='Windows'), \
                patch.object(installed.runtime, 'collect') as collect:
            result = installed.observe(self.path, duration=5, settle=0)
        self.assertEqual(result['status'], 'unavailable')
        collect.assert_not_called()

    def test_overlay_revision_tracks_archive_content_not_installer_git(self):
        archive = self.path / 'overlay.tar.gz'
        archive.write_bytes(b'first source')
        first = installed.source_identity(self.path, archive)
        archive.write_bytes(b'changed source')
        self.assertNotEqual(first, installed.source_identity(self.path, archive))

    def test_install_wrapper_keeps_raw_samples_and_failed_status(self):
        (self.path / 'cgroup.controllers').write_text('cpu memory')
        row = runtime.read_target(self.target)
        row.update(error='cgroup_replaced', cpu_percent_one_core=None)
        with patch.object(installed.platform, 'system', return_value='Linux'), \
                patch.object(installed.runtime, 'CGROUP_ROOT', self.path), \
                patch.object(installed, 'source_identity', return_value='revision'), \
                patch.object(installed.runtime, 'resolve_target', return_value=self.target), \
                patch.object(installed.runtime, 'command', return_value='inactive'), \
                patch.object(installed.runtime, 'validate_scopes'), \
                patch.object(installed.platform, 'freedesktop_os_release', return_value={'PRETTY_NAME': 'Test Linux'}), \
                patch.object(installed.runtime, 'collect', return_value=([{'targets': [row]}], False)):
            result = installed.observe(self.path, duration=5, settle=0)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['summary'][0]['failed_samples'], 1)
        self.assertEqual(result['samples'][0]['targets'][0]['error'], 'cgroup_replaced')
        json.dumps(result, allow_nan=False)

    def test_bom_from_windows_vm_capture_is_readable(self):
        artifact = self.path / 'runtime.json'
        artifact.write_text('{"status": "measured"}', encoding='utf-8-sig')
        self.assertEqual(vm_report.load_json(artifact)['status'], 'measured')


if __name__ == '__main__':
    unittest.main()
