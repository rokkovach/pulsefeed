#!/usr/bin/env python3
"""Check staged release bytes without printing credentials or fetching any network data.

This complements manual review and an organization's secret scanner. It is not a
claim that pattern matching can identify every possible secret. Known operator
values can be supplied privately to catch accidental copies and encoded variants.
"""
import argparse
import base64
import io
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import quote
from urllib.parse import urlsplit
from zipfile import ZipFile, is_zipfile

PATTERNS = {
    'private key': rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----',
    'GitHub token': rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b',
    'Runpod token': rb'\brp_[A-Za-z0-9_-]{30,}\b',
    'Hugging Face token': rb'\bhf_[A-Za-z0-9]{25,}\b',
    'AWS access key': rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    'Slack token': rb'\bxox[baprs]-[A-Za-z0-9-]{20,}\b',
    'OpenAI token': rb'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,}\b',
    'credential URL': rb'https?://[^\s/<>"\x27]+:[^\s/<>"\x27]+@',
}
PRIVATE_PARTS = {'artifacts', '.venv', '.venv-local', '__pycache__', 'node_modules', '.aws', '.codex'}
PRIVATE_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.pem', '.key', '.log', '.safetensors', '.ckpt'}
MAX_EXPANDED_BYTES = 80 * 1024 * 1024
FIXTURE_CREDENTIALS = {('user', 'pass'), ('user', 'password'), ('user', 'secret'),
                       ('name', 'pass'), ('name', 'password'), ('u', 'p')}


def command(*args):
    return subprocess.check_output(['git', *args])


def known_values(paths):
    """Read private operator values, retaining no payloads in the report or index."""
    result = set()
    sensitive = re.compile(r'secret|token|password|api.?key|authorization|cookie|^auth$', re.I)
    def walk(value, key=''):
        if isinstance(value, dict):
            for name, child in value.items(): walk(child, name)
        elif isinstance(value, list):
            for child in value: walk(child, key)
        elif isinstance(value, str) and sensitive.search(key) and len(value) >= 20:
            result.add(value)
    for path in paths:
        content = Path(path).read_text()
        try: walk(json.loads(content))
        except ValueError:
            for line in content.splitlines():
                if '=' in line:
                    name, value = line.split('=', 1); walk(value.strip().strip('"\x27'), name)
    encoded = set()
    for value in result:
        raw = value.encode(); encoded.update((raw, quote(value, safe='').encode(), base64.b64encode(raw), raw.hex().encode()))
    return encoded


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--known-secret-file', action='append', default=[], type=Path)
    parser.add_argument('--ref', help='Scan every file in a committed tree instead of the staged changes')
    args = parser.parse_args()
    known = known_values(args.known_secret_file)
    source = ':'
    if args.ref:
        revision = command('rev-parse', '--verify', args.ref + '^{commit}').decode().strip()
        listing = command('ls-tree', '-r', '--name-only', '-z', revision)
        source = revision + ':'
    else:
        listing = command('diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z')
    paths = [path.decode() for path in listing.split(b'\0') if path]
    if not paths: raise SystemExit('No staged files. Stage the intended release first.')
    findings = []; total = 0; members = 0
    def scan(label, data, depth=0):
        nonlocal total, members
        total += len(data)
        if total > MAX_EXPANDED_BYTES: raise SystemExit('Expanded release exceeds the review bound')
        for name, pattern in PATTERNS.items():
            for match in re.finditer(pattern, data):
                # These exact credentials are deliberate URL-rejection fixtures,
                # reviewed in the named test trees. Never exempt their contents
                # from the known-private-value checks or any other token rule.
                fixture = (label.startswith(('app/tests/', 'local/test_', 'workers/qwen/tests/'))
                           or label.startswith('integration/test_') or label.startswith('integration/') and '.test.cjs' in label)
                if name == 'credential URL' and fixture:
                    parsed = urlsplit(match.group().decode() + 'example.invalid')
                    if (parsed.username, parsed.password) in FIXTURE_CREDENTIALS: continue
                findings.append((label, name))
        if any(value in data for value in known): findings.append((label, 'known private value'))
        if is_zipfile(io.BytesIO(data)):
            if depth >= 3: raise SystemExit('Archive nesting exceeds the review bound: ' + label)
            with ZipFile(io.BytesIO(data)) as archive:
                for member in archive.infolist():
                    if member.is_dir(): continue
                    if member.file_size > MAX_EXPANDED_BYTES - total: raise SystemExit('Oversized archive member: ' + label)
                    members += 1; scan(label + '!' + member.filename, archive.read(member), depth + 1)
    for name in paths:
        path = Path(name)
        if (set(path.parts) & PRIVATE_PARTS or path.suffix.lower() in PRIVATE_SUFFIXES
                or path.name.startswith('.env') and not path.name.endswith('.example')
                or name.startswith(('local/data/', 'output/qa/', 'output/archive/'))
                or re.search(r'(?:credentials|cookies|local-secrets|private-session)', path.name, re.I)):
            findings.append((name, 'private runtime path'))
        scan(name, command('show', source + name))
    if findings:
        for name, kind in findings: print(kind + ': ' + name)
        raise SystemExit('Release check failed; no credential values were printed')
    print(f'PASS: {len(paths)} staged files; {members} nested members; {total / 1024 / 1024:.1f} MiB inspected; no detected credentials or private runtime paths')


if __name__ == '__main__': main()
