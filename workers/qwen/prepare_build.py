"""Prepare a private allowlisted offline build context from maintained exact inputs."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.request
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
TARGETS = {
    'arm64': {'machine': 'aarch64', 'prefix': 'workers/qwen', 'dockerfile': 'Dockerfile.dgx-spark',
              'lock': 'requirements-dgx.lock', 'provenance': 'requirements-dgx.provenance.json'},
    'amd64': {'machine': 'x86_64', 'prefix': '', 'dockerfile': 'Dockerfile',
              'lock': 'requirements-runpod.lock', 'provenance': 'requirements-runpod.provenance.json'},
}
ALLOWED_HOSTS = {'pypi.org', 'files.pythonhosted.org', 'ports.ubuntu.com',
                 'archive.ubuntu.com', 'security.ubuntu.com', 'download.pytorch.org'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise ValueError('Artifact redirects are not allowed.')


def checked_url(url):
    if not isinstance(url, str) or any(ord(c) < 33 for c in url):
        raise ValueError('Invalid artifact URL.')
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError('Invalid artifact port.') from None
    if parsed.username or parsed.password or port is not None or parsed.query or parsed.fragment:
        raise ValueError('Artifact URL contains unexpected authority or suffix.')
    if parsed.hostname not in ALLOWED_HOSTS or parsed.scheme not in {'http', 'https'}:
        raise ValueError('Artifact origin is not allowed.')
    if parsed.hostname == 'download.pytorch.org' and parsed.path != (
            '/whl/cu126/torchcodec-0.17.0%2Bcu126-cp312-cp312-'
            'manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl'):
        raise ValueError('Only the reviewed official TorchCodec artifact is allowed.')
    if parsed.scheme == 'http':
        if not parsed.hostname.endswith('.ubuntu.com'):
            raise ValueError('Only hash-pinned Ubuntu origins may use HTTP.')
        url = 'https://' + url[len('http://'):]
    return url


def fetch(url, *, maximum, target=None, size=None, sha256=None):
    url = checked_url(url)
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(url, timeout=60) as response:
        if response.geturl() != url or response.status != 200:
            raise ValueError('Unexpected artifact response.')
        digest = hashlib.sha256()
        count = 0
        chunks = [] if target is None else None
        stream = target.with_suffix(target.suffix + '.part').open('xb') if target else None
        try:
            while block := response.read(1024 * 1024):
                count += len(block)
                if count > maximum:
                    raise ValueError('Artifact exceeds its size bound.')
                digest.update(block)
                if stream:
                    stream.write(block)
                else:
                    chunks.append(block)
        finally:
            if stream:
                stream.close()
    if size is not None and count != size or sha256 is not None and digest.hexdigest() != sha256:
        raise ValueError('Artifact size/hash differs from maintained provenance.')
    if target:
        target.with_suffix(target.suffix + '.part').rename(target)
        return target
    return b''.join(chunks)


def wheel_target(filename, machine):
    if Path(filename).name != filename or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+!-]*\.whl', filename):
        raise ValueError('Invalid wheel filename.')
    python, abi, platforms = filename[:-4].rsplit('-', 3)[1:]
    if platforms != 'any' and any('linux' not in p or not p.endswith('_' + machine) for p in platforms.split('.')):
        raise ValueError('Wheel does not match the target architecture.')
    tags = python.split('.')
    pure = abi == 'none' and 'py3' in tags and set(tags) <= {'py2', 'py3'}
    cpython = abi == 'cp312' and tags == ['cp312']
    stable = abi == 'abi3' and all(re.fullmatch(r'cp3\d+', p) and 32 <= int(p[2:]) <= 312 for p in tags)
    if not (pure or cpython or stable):
        raise ValueError('Wheel does not support target Python3.12/ABI.')



def copy_checked(source, destination, digest=None):
    if source.is_symlink() or not source.is_file():
        raise ValueError('Expected a regular maintained source file.')
    if digest is not None and hashlib.sha256(source.read_bytes()).hexdigest() != digest:
        raise ValueError('Local backport differs from maintained lock.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def prepare(platform, output):
    if platform not in TARGETS:
        raise ValueError('Unsupported platform.')
    output = output.absolute()
    if output.resolve().is_relative_to(REPO) or not output.parent.is_dir() or output.exists() or output.is_symlink():
        raise ValueError('Use a new output directory outside the repository.')
    output.mkdir(mode=0o700)
    os.chmod(output, 0o700)
    cfg = TARGETS[platform]
    destination = output / cfg['prefix']
    destination.mkdir(parents=True, exist_ok=True)
    inputs = destination / 'build-inputs'
    for name in ['gpu', 'os', 'overlay']:
        (inputs / name).mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location('qwen_lock', REPO / 'local/lock_dependencies.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    provenance = helper.read_json(ROOT / cfg['provenance'])
    base = helper.validate_inventory(provenance['base_inventory'])
    if base['platform']['machine'] != cfg['machine'] or base['python']['major_minor'] != '3.12':
        raise ValueError('Maintained provenance target mismatch.')
    overlay = provenance['overlay']
    lines = [line.strip() for line in (ROOT / cfg['lock']).read_text().splitlines() if line.strip() and not line.startswith('#')]
    if lines != [helper.requirement_line(name, overlay[name]) for name in sorted(overlay)]:
        raise ValueError('Lock and maintained provenance differ.')
    for name, record in overlay.items():
        if helper.protected(name):
            raise ValueError('Overlay must not replace protected GPU packages.')
        wheel_target(record['wheel'], cfg['machine'])
        target = inputs / 'overlay' / record['wheel']
        if name in {'setuptools', 'runpod'}:
            copy_checked(ROOT / 'vendor' / name / record['wheel'], target, record['sha256'])
        else:
            metadata = json.loads(fetch('https://pypi.org/pypi/' + name + '/' + record['version'] + '/json', maximum=2_000_000))
            matches = [r for r in metadata['urls'] if r['filename'] == record['wheel'] and r['digests']['sha256'] == record['sha256']]
            if len(matches) != 1:
                raise ValueError('No exact authenticated locked wheel artifact.')
            artifact = matches[0]
            fetch(artifact['url'], maximum=min(artifact['size'], 500_000_000), target=target,
                  size=artifact['size'], sha256=record['sha256'])
    gpu = helper.read_json(ROOT / 'vendor/base-corrections' / (platform + '-gpu.json'))
    gpu_lock = []
    for record in gpu:
        wheel_target(record['filename'], cfg['machine'])
        target = inputs / 'gpu' / record['filename']
        fetch(record['url'], maximum=min(record['bytes'], 500_000_000), target=target,
              size=record['bytes'], sha256=record['sha256'])
        if record['name'] == 'nvidia-cusparselt-cu13':
            repair = helper.read_json(ROOT / 'vendor/cusparselt/provenance.json')
            temporary = inputs / 'repaired'
            subprocess.run([sys.executable, str(ROOT / 'vendor/cusparselt/repair_vendor_wheel_tag.py'),
                            str(target), record['sha256'], str(temporary), str(inputs / 'repair-proof.json')], check=True)
            repaired = next(temporary.glob('*.whl'))
            if hashlib.sha256(repaired.read_bytes()).hexdigest() != repair['output_sha256']:
                raise ValueError('Repaired vendor artifact differs.')
            target.unlink()
            target = inputs / 'gpu' / repaired.name
            repaired.rename(target)
            gpu_lock.append(record['name'] + '==' + repair['local_version'] + ' --hash=sha256:' + repair['output_sha256'])
        else:
            gpu_lock.append(record['name'] + '==' + record['version'] + ' --hash=sha256:' + record['sha256'])
    (destination / 'base-gpu.lock').write_text('\n'.join(gpu_lock) + '\n')
    os_manifest = helper.read_json(ROOT / 'vendor/os-security' / (platform + '.json'))
    if os_manifest['architecture'] != platform or len(os_manifest['artifacts']) != 32:
        raise ValueError('OS correction target mismatch.')
    for record in os_manifest['artifacts']:
        if record['architecture'] not in {platform, 'all'} or Path(record['filename']).name != record['filename']:
            raise ValueError('OS artifact architecture/path mismatch.')
        fetch(record['url'], maximum=min(record['bytes'], 50_000_000), target=inputs / 'os' / record['filename'],
              size=record['bytes'], sha256=record['sha256'])
    for name in ['runtime.py', 'handler.py', 'healthcheck.py', 'smoke_test.py', 'smoke_dgx.py',
                 'local_factory.py', 'check_dependencies.py', 'verify_base.py', cfg['lock'], cfg['provenance']]:
        copy_checked(ROOT / name, destination / name)
    copy_checked(REPO / 'local/lock_dependencies.py', destination / 'lock_dependencies.py')
    copy_checked(ROOT / 'vendor/os-security/apply_updates.py', destination / 'apply_os_updates.py')
    copy_checked(ROOT / 'vendor/os-security' / (platform + '.json'), destination / 'base-os-artifacts.json')
    for suffix, target_name in [('python.json', 'base-python-inventory.json'), ('os.tsv', 'base-os-inventory.tsv')]:
        copy_checked(ROOT / 'vendor/base-corrections' / (platform + '-' + suffix), destination / target_name)
    if platform == 'amd64':
        copy_checked(ROOT / 'verify_runpod_runtime.py', destination / 'verify_runpod_runtime.py')
    if platform == 'arm64':
        copy_checked(REPO / 'local/worker_gateway.py', output / 'local/worker_gateway.py')
        copy_checked(ROOT / cfg['dockerfile'], destination / cfg['dockerfile'])
    copy_checked(ROOT / cfg['dockerfile'], output / 'Dockerfile')
    copy_checked(ROOT / '.dockerignore', output / '.dockerignore')
    files = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(output.rglob('*')) if p.is_file()}
    (output / 'context-inventory.json').write_text(json.dumps({'platform': platform, 'files': files}, indent=2) + '\n')
    print('Verified private offline Qwen context:', output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', choices=sorted(TARGETS), required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.platform, args.output_dir)
