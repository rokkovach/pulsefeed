"""Offline adversarial contracts for the bounded OS package correction."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('os_correction', Path(__file__).with_name('apply_updates.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.records = []
        for index in range(32):
            name = 'fixture-package-' + str(index)
            data = ('benign archive fixture ' + name).encode()
            path = self.root / (name + '.deb')
            path.write_bytes(data)
            self.records.append(dict(package=name, version='2', architecture='arm64',
                                     filename=path.name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
        self.before = {r['package']: ['1', 'arm64'] for r in self.records}
        self.before['nvidia-driver-fixture'] = ['unchanged', 'arm64']
        self.after = copy.deepcopy(self.before)
        for record in self.records:
            self.after[record['package']][0] = '2'
        self.plan = '\n'.join('Inst ' + r['package'] + ' [1] (2 local-deb [arm64])' for r in self.records)
        self.manifest = self.root / 'manifest.json'
        self.save()

    def save(self):
        self.manifest.write_text(json.dumps({'artifacts': self.records}))

    def run_apply(self, *, plan=None, after=None, audit='', closure=''):
        results = [subprocess.CompletedProcess([], 0, self.plan if plan is None else plan, ''),
                   subprocess.CompletedProcess([], 0, '', ''),
                   subprocess.CompletedProcess([], 0, audit, ''),
                   subprocess.CompletedProcess([], 0, closure, '')]
        with patch.object(module, 'installed', side_effect=[self.before, self.after if after is None else after]), \
                patch.object(module.subprocess, 'run', side_effect=results) as calls:
            try:
                module.apply(self.manifest, self.root)
            finally:
                self.calls = calls.call_args_list

    def assert_no_install(self):
        self.assertFalse(any('-y' in c.args[0] or c.args[0][0] == 'dpkg' for c in self.calls))

    def test_exact_plan_uses_ordered_apt_and_complete_audits(self):
        self.run_apply()
        command = self.calls[1].args[0]
        self.assertEqual(command[:2], ['apt-get', '-y'])
        for option in ['--no-remove', '--only-upgrade', '--no-install-recommends']:
            self.assertIn(option, command)
        self.assertNotIn('--no-download', command)  # Local-file staging needs apt's normal ordering.
        self.assertEqual(self.calls[2].args[0], ['dpkg', '--audit'])
        self.assertEqual(self.calls[3].args[0], ['apt-get', '--simulate', '--no-download', '--no-remove', '-f', 'install'])

    def test_extra_install_is_rejected_before_mutation(self):
        with self.assertRaises(ValueError):
            self.run_apply(plan=self.plan + '\nInst unwanted [1] (2 local-deb [arm64])')
        self.assert_no_install()

    def test_removal_is_rejected_before_mutation(self):
        with self.assertRaises(ValueError):
            self.run_apply(plan=self.plan + '\nRemv fixture-package-0 [1]')
        self.assert_no_install()

    def test_revision_change_is_rejected_before_mutation(self):
        with self.assertRaises(ValueError):
            self.run_apply(plan=self.plan.replace('(2 local-deb', '(3 local-deb', 1))
        self.assert_no_install()

    def test_changed_hash_is_rejected_before_any_subprocess(self):
        (self.root / self.records[0]['filename']).write_bytes(b'altered bytes')
        with self.assertRaises(ValueError):
            self.run_apply()
        self.assertEqual(self.calls, [])

    def test_symlink_archive_is_rejected_before_any_subprocess(self):
        path = self.root / self.records[0]['filename']
        path.rename(self.root / 'target.deb')
        path.symlink_to(self.root / 'target.deb')
        with self.assertRaises(ValueError):
            self.run_apply()
        self.assertEqual(self.calls, [])

    def test_protected_package_change_fails_complete_inventory_gate(self):
        after = copy.deepcopy(self.after)
        after['nvidia-driver-fixture'][0] = 'changed'
        with self.assertRaises(ValueError):
            self.run_apply(after=after)

    def test_incomplete_configuration_or_dependency_closure_is_rejected(self):
        for values in [dict(audit='unfinished configuration'), dict(closure='Inst unwanted [1] (2 local-deb [arm64])')]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.run_apply(**values)


if __name__ == '__main__':
    unittest.main()
