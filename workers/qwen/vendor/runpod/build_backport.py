"""Build the hash-pinned, locally versioned Runpod wheel without installation."""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import zipfile


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def assemble(source, provenance, root):
    if sha256(source.read_bytes()) != provenance['base_wheel']['sha256']:
        raise ValueError('Upstream wheel SHA256 mismatch')
    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder)
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or any(name.startswith('/') or '..' in Path(name).parts or ((entry.external_attr >> 16) & 0o170000) == 0o120000 for name,entry in zip(names,archive.infolist())):
                raise ValueError('Unsafe upstream archive paths')
            archive.extractall(work)
        for patch in provenance['patches']:
            path = root / patch['file']
            if sha256(path.read_bytes()) != patch['sha256']:
                raise ValueError('Source patch SHA256 mismatch')
            for item in patch['files']:
                path_before = work / item['path']
                before = sha256(path_before.read_bytes()) if path_before.exists() else None
                if before != item['before_sha256']:
                    raise ValueError('Unexpected source bytes before patch')
            subprocess.run(['patch','--batch','--forward','-p1','-i',str(root / patch['file'])],cwd=work,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            for item in patch['files']:
                if sha256((work / item['path']).read_bytes()) != item['after_sha256']:
                    raise ValueError('Unexpected patched source bytes')
        version = provenance['local_version']
        info = work / f'runpod-{version}.dist-info'
        (work / 'runpod-1.10.0.dist-info').rename(info)
        metadata = info / 'METADATA'
        raw = metadata.read_bytes()
        if raw.count(b'\nVersion: 1.10.0\n') != 1:
            raise ValueError('Unexpected base version metadata')
        metadata.write_bytes(raw.replace(b'\nVersion: 1.10.0\n',f'\nVersion: {version}\n'.encode()))
        embedded = {key:provenance[key] for key in ('base_wheel','reference_wheel','local_version','patches','scope','excluded_changes')}
        (info / 'PULSEFEED-BACKPORT.json').write_bytes((json.dumps(embedded,indent=2,ensure_ascii=True)+'\n').encode())
        record = info / 'RECORD'
        records = []
        for path in sorted(work.rglob('*')):
            if path.is_file() and path != record:
                value = path.read_bytes()
                digest = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).decode().rstrip('=')
                records.append((path.relative_to(work).as_posix(),'sha256='+digest,str(len(value))))
        records.append((record.relative_to(work).as_posix(),'',''))
        text = io.StringIO(newline='')
        csv.writer(text,lineterminator='\n').writerows(records)
        record.write_bytes(text.getvalue().encode())
        output = io.BytesIO()
        # Stored entries avoid zlib-version-dependent compressed output. Paths,
        # modes, timestamps and ordering are fixed across supported platforms.
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_STORED) as archive:
            for path in sorted(work.rglob('*')):
                if path.is_file():
                    entry = zipfile.ZipInfo(path.relative_to(work).as_posix(),(2026,10,20,0,0,0))
                    entry.create_system = 3
                    entry.external_attr = 0o100644 << 16
                    entry.compress_type = zipfile.ZIP_STORED
                    archive.writestr(entry,path.read_bytes())
        return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-wheel',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    provenance = json.loads((root / 'provenance.json').read_text())
    raw = assemble(args.source_wheel,provenance,root)
    if sha256(raw) != provenance['candidate_wheel']['sha256']:
        raise ValueError('Rebuilt wheel SHA256 mismatch')
    args.output_dir.mkdir(parents=True,exist_ok=True)
    path = args.output_dir / provenance['candidate_wheel']['filename']
    path.write_bytes(raw)
    print(f'Verified reproducible wheel: {path.name} sha256:{sha256(raw)}')


if __name__ == '__main__':
    main()
