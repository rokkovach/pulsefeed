"""Offline trust-boundary checks for external artifact preparation."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from workers.qwen.prepare_build import checked_url, prepare, wheel_target


class PreparationTests(unittest.TestCase):
    def test_credential_suffix_port_and_untrusted_origins_are_rejected(self):
        for url in ['https://user:secret@pypi.org/file', 'https://pypi.org:0/file',
                    'https://pypi.org:443/file', 'https://pypi.org:invalid/file',
                    'https://pypi.org/file?token=fixture', 'https://pypi.org/file#fragment',
                    'https://huggingface.co/model/weights', 'http://pypi.org/file',
                    'https://pypi.org.evil.invalid/file']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                checked_url(url)

    def test_recorded_ubuntu_http_origin_uses_https(self):
        self.assertEqual(checked_url('http://ports.ubuntu.com/ubuntu-ports/pool/fixture.deb'),
                         'https://ports.ubuntu.com/ubuntu-ports/pool/fixture.deb')

    def test_exact_target_python_abi_and_architecture(self):
        for name in ['fixture-1-py3-none-any.whl', 'fixture-1-py2.py3-none-any.whl',
                     'fixture-1-cp312-cp312-manylinux2014_x86_64.whl',
                     'fixture-1-cp39-abi3-manylinux_2_34_x86_64.whl']:
            wheel_target(name, 'x86_64')
        for name in ['fixture-1-py313-none-any.whl', 'fixture-1-cp313-abi3-manylinux2014_x86_64.whl',
                     'fixture-1-cp312-cp311-manylinux2014_x86_64.whl',
                     'fixture-1-cp311-cp311-manylinux2014_x86_64.whl',
                     'fixture-1-py3-none-manylinux2014_aarch64.whl',
                     'fixture-1-py3-none-win_amd64.whl', '../fixture-1-py3-none-any.whl']:
            with self.subTest(wheel=name), self.assertRaises(ValueError):
                wheel_target(name, 'x86_64')

    def test_only_reviewed_official_torchcodec_url_is_allowed(self):
        url = ('https://download.pytorch.org/whl/cu126/torchcodec-0.17.0%2Bcu126-cp312-cp312-'
               'manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl')
        self.assertEqual(checked_url(url), url)
        with self.assertRaises(ValueError):
            checked_url('https://download.pytorch.org/whl/cu130/other.whl')

    def test_repository_and_existing_output_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as folder, patch('workers.qwen.prepare_build.fetch') as fetch:
            for target in [Path(__file__).resolve().parents[1] / 'never-created-build-context', Path(folder)]:
                with self.subTest(target=target), self.assertRaises(ValueError):
                    prepare('amd64', target)
            fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
