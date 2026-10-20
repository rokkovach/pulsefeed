"""Rebuild the candidate wheel from hash-verified upstream bytes and local patches.

Network access is opt-in. No GPU packages or installed environments are changed.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import urllib.request
import zipfile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(source, output, provenance, root):
    if digest(source) != provenance["base_wheel_sha256"]:
        raise ValueError("Upstream wheel SHA256 mismatch.")
    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder)
        with zipfile.ZipFile(source) as archive:
            if any(name.startswith("/") or ".." in Path(name).parts for name in archive.namelist()):
                raise ValueError("Invalid upstream wheel path.")
            archive.extractall(work)
        for patch in provenance["patches"]:
            path = root / patch["file"]
            if digest(path) != patch["sha256"]:
                raise ValueError("Source patch SHA256 mismatch.")
            subprocess.run(["patch", "--batch", "--forward", "-p1", "-i", str(path)], cwd=work, check=True)
        version = provenance["local_version"]
        info = work / f"setuptools-{version}.dist-info"
        (work / "setuptools-80.10.2.dist-info").rename(info)
        metadata = info / "METADATA"
        text = metadata.read_text()
        if text.count("Version: 80.10.2\n") != 1:
            raise ValueError("Unexpected source version metadata.")
        metadata.write_text(text.replace("Version: 80.10.2\n", f"Version: {version}\n"))
        # Keep this embedded record byte-identical to the reviewed candidate.
        embedded = {key: provenance[key] for key in (
            "base_wheel", "base_wheel_sha256", "local_version", "upstream_security_commit",
            "patch_sha256", "local_patch_sha256", "scope",
        )}
        (info / "PULSEFEED-BACKPORT.json").write_text(json.dumps(embedded, indent=2) + "\n")
        record = info / "RECORD"
        rows = []
        for path in sorted(work.rglob("*")):
            if path.is_file() and path != record:
                raw = path.read_bytes()
                sha = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
                rows.append([path.relative_to(work).as_posix(), "sha256=" + sha, str(len(raw))])
        rows.append([record.relative_to(work).as_posix(), "", ""])
        with record.open("w", newline="") as stream:
            csv.writer(stream).writerows(rows)
        output.mkdir(parents=True, exist_ok=True)
        wheel = output / provenance["candidate_wheel"]
        with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(work.rglob("*")):
                if path.is_file():
                    entry = zipfile.ZipInfo(path.relative_to(work).as_posix(), (2026, 10, 20, 0, 0, 0))
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    entry.external_attr = 0o100644 << 16
                    archive.writestr(entry, path.read_bytes())
        if digest(wheel) != provenance["candidate_wheel_sha256"]:
            raise ValueError("Rebuilt candidate wheel SHA256 mismatch.")
        print(f"Verified reproducible wheel: {wheel.name} sha256:{digest(wheel)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-wheel", type=Path)
    source.add_argument("--download-source", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    provenance = json.loads((root / "provenance.json").read_text())
    if args.source_wheel:
        build(args.source_wheel, args.output_dir, provenance, root)
    else:
        with tempfile.TemporaryDirectory() as folder:
            wheel = Path(folder) / provenance["base_wheel"]
            with urllib.request.urlopen(provenance["base_wheel_url"], timeout=30) as response:
                raw = response.read(3_000_001)
            if len(raw) > 3_000_000:
                raise ValueError("Unexpected upstream wheel size.")
            wheel.write_bytes(raw)
            build(wheel, args.output_dir, provenance, root)


if __name__ == "__main__":
    main()
