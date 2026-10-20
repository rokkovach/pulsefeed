"""Exercise the publication boundary without using any operator credentials."""
from pathlib import Path
import subprocess
from zipfile import ZipFile

import pytest

GUARD = Path(__file__).resolve().parents[1] / 'scripts/check-release.py'


def stage(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    subprocess.run(['git', 'add', '--', name], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    return tmp_path


def run(root, *args):
    import sys
    return subprocess.run([sys.executable, str(GUARD), *map(str, args)], cwd=root,
                          capture_output=True, text=True)


def test_safe_example_is_allowed_and_private_environment_is_rejected(repo):
    stage(repo, '.env.example', b'RUNPOD_API_KEY=\n')
    assert run(repo).returncode == 0
    stage(repo, '.env', b'RUNPOD_API_KEY=\n')
    assert run(repo).returncode != 0


def test_nested_document_token_is_rejected_without_printing_it(repo):
    token = b'ghp_' + b'A' * 36
    archive = repo / 'report.docx'
    with ZipFile(archive, 'w') as z:
        z.writestr('word/document.xml', b'<text>' + token + b'</text>')
    stage(repo, archive.name, archive.read_bytes())
    result = run(repo)
    assert result.returncode != 0 and 'GitHub token' in result.stdout
    assert token.decode() not in result.stdout + result.stderr


def test_unknown_format_private_value_is_found_in_encoded_form(repo):
    import base64
    import json
    secret = 'unique-private-test-value-for-release-check'
    values = repo / 'operator.json'
    values.write_text(json.dumps({'SESSION_SECRET': secret}))
    stage(repo, 'README.md', base64.b64encode(secret.encode()))
    result = run(repo, '--known-secret-file', values)
    assert result.returncode != 0 and 'known private value' in result.stdout
    assert secret not in result.stdout + result.stderr


def test_url_fixture_allowance_does_not_exempt_unknown_credentials(repo):
    fixture = 'app/tests/test_urls.py'
    stage(repo, fixture, b"url='http://user:pass@example.com'\n")
    assert run(repo).returncode == 0
    stage(repo, fixture, ("url='" + "http://" + "unexpected:credential-value@example.com'\n").encode())
    assert run(repo).returncode != 0


def test_known_value_still_rejects_an_allowed_fixture_pair(repo):
    import json
    secret = 'some-private-key-' + 'x' * 24
    values = repo / 'operator.json'; values.write_text(json.dumps({'ADMIN_ACCESS_TOKEN': secret}))
    stage(repo, 'app/tests/test_urls.py', ('# ' + secret + "\nurl='http://user:pass@example.com'\n").encode())
    assert run(repo, '--known-secret-file', values).returncode != 0
