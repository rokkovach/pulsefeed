"""Offline provenance and byte-preservation checks for the vendor packaging repair."""
import csv
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest

from repair_vendor_wheel_tag import repair


def vendor_wheel(path, *, tag='manylinux2014_sbsa', extra=None):
    with zipfile.ZipFile(path, 'w') as wheel:
        prefix = 'nvidia_cusparselt_cu12-0.8.1.dist-info/'
        wheel.writestr(prefix + 'WHEEL', 'Wheel-Version: 1.0\nTag: py3-none-' + tag + '\n')
        wheel.writestr(prefix + 'METADATA', 'Metadata-Version: 2.1\nName: nvidia-cusparselt-cu12\nVersion: 0.8.1\n')
        wheel.writestr(prefix + 'RECORD', '')
        wheel.writestr('nvidia/cusparselt/lib/libcusparseLt.so.0', b'fixed-native-payload')
        if extra:
            wheel.writestr(extra, b'unexpected')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_repair_is_deterministic_preserves_payload_and_rebuilds_record(tmp_path):
    source = tmp_path / 'source.whl'
    digest = vendor_wheel(source)
    for name in ('first', 'second'):
        repair(source, digest, tmp_path / name, tmp_path / (name + '.json'))
    first = json.loads((tmp_path / 'first.json').read_text())
    second = json.loads((tmp_path / 'second.json').read_text())
    assert first['output_sha256'] == second['output_sha256']
    assert first['version'] == '0.8.1+pulsefeed.arm64tag1'
    with zipfile.ZipFile(first['output']) as wheel:
        assert wheel.read('nvidia/cusparselt/lib/libcusparseLt.so.0') == b'fixed-native-payload'
        prefix = 'nvidia_cusparselt_cu12-0.8.1+pulsefeed.arm64tag1.dist-info/'
        assert b'Tag: py3-none-manylinux2014_aarch64' in wheel.read(prefix + 'WHEEL')
        rows = list(csv.reader(io.StringIO(wheel.read(prefix + 'RECORD').decode())))
        assert {row[0] for row in rows} == set(wheel.namelist())
        assert all(row[1].startswith('sha256=') for row in rows if not row[0].endswith('/RECORD'))


@pytest.mark.parametrize('case', ['hash', 'tag', 'traversal', 'signature'])
def test_repair_rejects_untrusted_or_unexpected_archives(tmp_path, case):
    source = tmp_path / 'source.whl'
    extras = {'traversal': '../outside', 'signature': 'nvidia_cusparselt_cu12-0.8.1.dist-info/RECORD.jws'}
    digest = vendor_wheel(source, tag='manylinux2014_aarch64' if case == 'tag' else 'manylinux2014_sbsa', extra=extras.get(case))
    with pytest.raises(ValueError):
        repair(source, '0' * 64 if case == 'hash' else digest, tmp_path / 'out', tmp_path / 'proof.json')
    assert not (tmp_path / 'proof.json').exists()
