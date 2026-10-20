"""Validate corrected target inventories and strict dependency gates offline."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import Mock

from workers.qwen.check_dependencies import main
from workers.qwen.handler import MODEL_REVISION
from workers.qwen.runtime import EXPECTED, DGX_EXPECTED

ROOT = Path(__file__).resolve().parents[1]


class DependencyCheckTests(unittest.TestCase):
    def check_result(self, code=0, stdout='No broken requirements found.\n', stderr=''):
        run = Mock(return_value=subprocess.CompletedProcess([], code, stdout, stderr))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = main(run)
        run.assert_called_once_with([sys.executable, '-m', 'pip', 'check'], capture_output=True, text=True, check=False)
        return status

    def test_clean_check_passes(self):
        self.assertEqual(self.check_result(), 0)

    def test_every_nonzero_result_fails_without_inherited_exceptions(self):
        for code in [-9, 1, 2]:
            for diagnostic in ['pygobject requires pycairo.\n', 'torch requires another NCCL.\n', '']:
                with self.subTest(code=code, diagnostic=diagnostic):
                    self.assertNotEqual(self.check_result(code, diagnostic), 0)

    def test_stderr_even_on_success_fails(self):
        self.assertNotEqual(self.check_result(0, stderr='unexpected diagnostic'), 0)


class DependencyLockTests(unittest.TestCase):
    def test_both_full_locks_match_recorded_wheels_and_protected_baselines(self):
        for family, machine, expected in [('runpod', 'x86_64', EXPECTED), ('dgx', 'aarch64', DGX_EXPECTED)]:
            with self.subTest(family=family):
                evidence = json.loads((ROOT / ('requirements-' + family + '.provenance.json')).read_text())
                self.assertEqual(evidence['report_environment'], {'platform_machine': machine, 'platform_system': 'Linux', 'python_version': '3.12'})
                self.assertRegex(evidence['pip_report_sha256'], r'^[a-f0-9]{64}$')
                entries = {}
                for line in (ROOT / ('requirements-' + family + '.lock')).read_text().splitlines():
                    if not line or line.startswith('#'):
                        continue
                    match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([^ ]+) --hash=sha256:([a-f0-9]{64})', line)
                    self.assertIsNotNone(match, line)
                    name, version, digest = match.groups()
                    self.assertNotIn(name, entries)
                    entries[name] = (version, digest)
                self.assertEqual(set(entries), set(evidence['overlay']))
                for name, (version, digest) in entries.items():
                    self.assertEqual((version, digest), (evidence['overlay'][name]['version'], evidence['overlay'][name]['sha256']))
                for name, record in evidence['base_inventory']['distributions'].items():
                    if name.startswith(('torch', 'nvidia-', 'cuda-', 'flashinfer', 'triton')) or name == 'vllm':
                        self.assertNotIn(name, entries)
                        self.assertEqual(evidence['expected_inventory'][name], record['version'])
                for name, version in expected.items():
                    self.assertEqual(evidence['expected_inventory'][name].split('+')[0], version)
                if family == 'dgx':
                    self.assertNotIn('runpod', evidence['expected_inventory'])

    def test_builds_use_public_pinned_bases_offline_inputs_and_strict_gates(self):
        bases = [('Dockerfile', 'amd64', 'f99aa709b3f873aa1e1aadf5c1db35970119e8af9b36fe15d2a667aae0ed005c'),
                 ('Dockerfile.dgx-spark', 'arm64', 'b1bea6490db94268e68577929ae4b800a662b91a3cd4a1fcba81cfb3946ddcff')]
        for filename, arch, digest in bases:
            build = (ROOT / filename).read_text()
            self.assertIn('FROM --platform=linux/' + arch + ' vllm/vllm-openai@sha256:' + digest, build)
            self.assertIn('--network=none', build)
            self.assertIn('--no-index --no-cache-dir --no-deps --require-hashes', build)
            self.assertIn('verify_base.py', build)
            self.assertIn('python3 /worker/check_dependencies.py', build)
            self.assertIn('MODEL_REVISION=' + MODEL_REVISION, build)
            self.assertNotIn('integration/artifacts', build)
            self.assertNotIn('autoremove', build)

    def test_test_environment_uses_explicit_local_security_sdk(self):
        requirements = (ROOT / 'requirements-test.txt').read_text()
        self.assertNotIn('runpod==', requirements)
        self.assertIn('fastapi[all]==0.136.3', requirements)
        vendor = json.loads((ROOT / 'vendor/runpod/provenance.json').read_text())
        self.assertIn('1.10.0+pulsefeed.security.1', json.dumps(vendor))


if __name__ == '__main__':
    unittest.main()
