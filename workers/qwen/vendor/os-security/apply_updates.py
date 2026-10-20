"""Apply only authenticated, reviewed Ubuntu revisions without touching GPU packages."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def installed():
    result = subprocess.run(['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${Architecture}\n'],
                            capture_output=True, text=True, check=True)
    return {row.split('\t')[0]: row.split('\t')[1:] for row in result.stdout.splitlines()}


def apply(manifest_path, archive_root):
    manifest = json.loads(Path(manifest_path).read_text())
    archives = Path(archive_root).resolve(strict=True)
    records = manifest['artifacts']
    if len(records) != 32 or len({r['package'] for r in records}) != 32:
        raise ValueError('Expected exactly 32 reviewed Ubuntu packages.')
    before = installed()
    files = []
    expected = {}
    for record in records:
        name, version = record['package'], record['version']
        if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', name) or any(
                marker in name for marker in ('cuda', 'nvidia', 'nccl', 'torch', 'vllm')):
            raise ValueError('Unexpected or protected package.')
        filename = record['filename']
        if Path(filename).name != filename or not filename.endswith('.deb'):
            raise ValueError('Invalid archive filename.')
        path = archives / filename
        if path.is_symlink() or not path.is_file() or path.stat().st_size != record['bytes']:
            raise ValueError('Invalid archive.')
        with path.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != record['sha256']:
                raise ValueError('Archive differs from authenticated apt metadata.')
        package_key = name if name in before else name + ':' + record['architecture']
        if package_key not in before:
            raise ValueError('New packages are outside this correction.')
        files.append(str(path))
        expected[name] = version
    command = ['apt-get', '--no-download', '--no-remove', '--only-upgrade',
               '--no-install-recommends', 'install', *files]
    simulation = subprocess.run([*command[:1], '--simulate', *command[1:]],
                                capture_output=True, text=True, check=True)
    changes = {}
    for line in simulation.stdout.splitlines():
        if line.startswith('Remv '):
            raise ValueError('Unexpected removal.')
        if line.startswith('Inst '):
            match = re.match(r'Inst (\S+) \[[^]]+\] \((\S+) ', line)
            if not match:
                raise ValueError('Unexpected package addition or simulation syntax.')
            changes[match[1]] = match[2]
    if changes != expected:
        raise ValueError('Upgrade plan differs from reviewed revisions.')
    # --no-download also blocks apt's local-archive staging step. Use the
    # authenticated local files with normal apt ordering inside a network-none
    # build; the closed simulation above forbids any extra package changes.
    installation = [item for item in command if item != '--no-download']
    subprocess.run([installation[0], '-y', '-o', 'Dpkg::Options::=--force-confold',
                    *installation[1:]], check=True,
                   env=dict(os.environ, DEBIAN_FRONTEND='noninteractive'))
    audit = subprocess.run(['dpkg', '--audit'], capture_output=True, text=True, check=True)
    if audit.stdout.strip() or audit.stderr.strip():
        raise ValueError('Package configuration audit failed.')
    closure = subprocess.run(['apt-get', '--simulate', '--no-download', '--no-remove', '-f', 'install'],
                             capture_output=True, text=True, check=True)
    if any(line.startswith(('Inst ', 'Remv ')) for line in closure.stdout.splitlines()):
        raise ValueError('Installed dependency closure needs additional changes.')
    after = installed()
    if before.keys() != after.keys():
        raise ValueError('Package additions/removals detected.')
    actual = {name.split(':', 1)[0]: record[0] for name, record in after.items() if before[name] != record}
    if actual != expected:
        raise ValueError('Installed changes differ from reviewed revisions.')
    print('Verified 32 exact Ubuntu upgrades; every other package is unchanged.')


if __name__ == '__main__':
    apply(sys.argv[1], sys.argv[2])
