"""Rebuild one authenticated NVIDIA ARM64 wheel with an explicit local packaging version.

CUDA/native payload bytes stay identical. Only WHEEL, METADATA, dist-info names,
and the generated RECORD change. Original and rebuilt hashes remain evidence.
"""
import argparse, base64, csv, hashlib, io, json, pathlib, zipfile

def sha(data):
    return hashlib.sha256(data).hexdigest()

def repair(source, expected, destination, evidence):
    raw = source.read_bytes()
    if sha(raw) != expected:
        raise ValueError('Source wheel SHA256 mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        names = [x.filename for x in infos]
        if len(names) != len(set(names)) or any(n.startswith('/') or '..' in pathlib.PurePosixPath(n).parts for n in names):
            raise ValueError('Unsafe or duplicate wheel paths')
        wheel_names = [n for n in names if n.endswith('.dist-info/WHEEL')]
        if len(wheel_names) != 1:
            raise ValueError('Expected exactly one WHEEL')
        old_dir = wheel_names[0].rsplit('/', 1)[0]
        distribution, version = old_dir[:-10].rsplit('-', 1)
        if version != '0.8.1' or not distribution.startswith('nvidia_cusparselt_cu'):
            raise ValueError('Unexpected vendor/version')
        local_version = version + '+pulsefeed.arm64tag1'
        new_dir = distribution + '-' + local_version + '.dist-info'
        wheel = archive.read(old_dir + '/WHEEL')
        if wheel.count(b'Tag: py3-none-manylinux2014_sbsa') != 1:
            raise ValueError('Unexpected WHEEL tag')
        wheel = wheel.replace(b'Tag: py3-none-manylinux2014_sbsa', b'Tag: py3-none-manylinux2014_aarch64')
        metadata = archive.read(old_dir + '/METADATA')
        old_version = b'Version: 0.8.1\n'
        if metadata.count(old_version) != 1:
            raise ValueError('Unexpected METADATA version')
        metadata = metadata.replace(old_version, ('Version: ' + local_version + '\n').encode())
        files = {}
        payload_before = {}
        for info in infos:
            name = info.filename
            if name.endswith(('/RECORD.jws', '/RECORD.p7s')):
                raise ValueError('Signed wheel requires separate signature policy')
            if name == old_dir + '/RECORD':
                continue
            data = archive.read(name)
            target = new_dir + name[len(old_dir):] if name.startswith(old_dir + '/') else name
            if name == old_dir + '/WHEEL': data = wheel
            elif name == old_dir + '/METADATA': data = metadata
            else: payload_before[name] = sha(data)
            files[target] = (data, info.external_attr)
        rows = []
        for name in sorted(files):
            data = files[name][0]
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
            rows.append((name, 'sha256=' + digest, str(len(data))))
        record_name = new_dir + '/RECORD'
        rows.append((record_name, '', ''))
        record = io.StringIO(newline='')
        csv.writer(record, lineterminator='\n').writerows(rows)
        files[record_name] = (record.getvalue().encode(), 0o100644 << 16)
        destination.mkdir(parents=True, exist_ok=True)
        output = destination / f'{distribution}-{local_version}-py3-none-manylinux2014_aarch64.whl'
        if output.exists(): raise ValueError('Refusing to overwrite rebuilt wheel')
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as rebuilt:
            for name in sorted(files):
                data, mode = files[name]
                entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = mode
                rebuilt.writestr(entry, data)
    # Independent read-back verifies every recorded digest and all non-metadata bytes.
    with zipfile.ZipFile(output) as rebuilt:
        for name, digest, size in rows:
            if not digest: continue
            data = rebuilt.read(name)
            actual = 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
            if actual != digest or len(data) != int(size): raise ValueError('RECORD verification failed')
        for name, digest in payload_before.items():
            target = new_dir + name[len(old_dir):] if name.startswith(old_dir + '/') else name
            if sha(rebuilt.read(target)) != digest: raise ValueError('Payload changed')
    result = {'source_sha256': expected, 'source_bytes': len(raw), 'output': str(output),
              'output_sha256': sha(output.read_bytes()), 'output_bytes': output.stat().st_size,
              'version': local_version, 'original_tag': 'py3-none-manylinux2014_sbsa',
              'repaired_tag': 'py3-none-manylinux2014_aarch64',
              'unchanged_payload_files': len(payload_before), 'record_verified': True}
    evidence.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('source', type=pathlib.Path)
    p.add_argument('sha256')
    p.add_argument('destination', type=pathlib.Path)
    p.add_argument('evidence', type=pathlib.Path)
    a = p.parse_args()
    repair(a.source, a.sha256, a.destination, a.evidence)
