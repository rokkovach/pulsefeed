#!/usr/bin/env python3
"""Lock installed-aware wheels against an immutable base image inventory."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

SCHEMA = 1
NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\Z")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*\Z")
SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
BASE = re.compile(r"(?:[^\s@]+@)?sha256:[0-9a-fA-F]{64}\Z")
PROTECTED = {
    "torch", "torchvision", "torchaudio", "torch-tensorrt", "triton", "pytorch-triton",
    "vllm", "flash-attn", "flashinfer", "xformers", "nvidia-ml-py", "tensorrt",
    "cuda-python", "cuda-bindings", "cuda-pathfinder", "cupy", "bitsandbytes",
    "torchao", "apex", "transformer-engine", "cutlass", "nccl",
}


class LockError(ValueError):
    """An unsafe or inconsistent lock input."""


def canonical_name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise LockError("Invalid distribution name.")
    return re.sub(r"[-_.]+", "-", value).lower()


def exact_version(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise LockError("Invalid exact distribution version.")
    return value


def protected(name):
    return name in PROTECTED or name.startswith(("nvidia-", "flashinfer", "cupy-", "pytorch-triton-",
                                               "tensorrt-", "torch-", "triton-", "vllm-",
                                               "cuda-", "transformer-engine-", "cutlass-", "nccl-"))


def requirements(value):
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise LockError("Requires-Dist must be a list of strings.")
    return sorted(value)


def inventory():
    distributions, shadowed = {}, {}
    for dist in importlib.metadata.distributions():
        name = canonical_name(dist.metadata.get("Name"))
        record = {"version": exact_version(dist.version),
                  "requires_dist": requirements(dist.requires)}
        # Match importlib.metadata.distribution(name): first discovered provider wins.
        if name in distributions:
            shadowed.setdefault(name, []).append(record)
        else:
            distributions[name] = record
    return {"schema_version": SCHEMA,
            "python": {"major_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
                       "version": platform.python_version(), "implementation": platform.python_implementation()},
            "platform": {"machine": platform.machine().lower(), "system": platform.system()},
            "distributions": dict(sorted(distributions.items())),
            "shadowed_distributions": dict(sorted(shadowed.items()))}


def validate_inventory(data):
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA:
        raise LockError("Unsupported inventory schema.")
    python = data.get("python", {})
    target = data.get("platform", {})
    if not isinstance(python, dict) or not re.fullmatch(r"\d+\.\d+", str(python.get("major_minor", ""))):
        raise LockError("Inventory is missing Python major.minor.")
    if not isinstance(target, dict) or not all(isinstance(target.get(key), str) and target[key] for key in ("machine", "system")):
        raise LockError("Inventory is missing target architecture/system.")
    entries = data.get("distributions")
    if not isinstance(entries, dict):
        raise LockError("Inventory distributions must be an object.")
    cleaned = {}
    for raw_name, record in entries.items():
        name = canonical_name(raw_name)
        if name in cleaned:
            raise LockError(f"Duplicate inventory distribution: {name}.")
        if not isinstance(record, dict):
            raise LockError(f"Invalid inventory record: {name}.")
        cleaned[name] = {"version": exact_version(record.get("version")),
                         "requires_dist": requirements(record.get("requires_dist"))}
    shadowed = data.get("shadowed_distributions", {})
    if not isinstance(shadowed, dict):
        raise LockError("Shadowed distributions must be an object.")
    cleaned_shadows = {}
    for raw_name, records in shadowed.items():
        name = canonical_name(raw_name)
        if name in cleaned_shadows or name not in cleaned or not isinstance(records, list) or not records:
            raise LockError("Invalid shadowed distribution inventory.")
        cleaned_shadows[name] = []
        for record in records:
            if not isinstance(record, dict):
                raise LockError("Invalid shadowed distribution metadata.")
            cleaned_shadows[name].append({"version": exact_version(record.get("version")),
                                          "requires_dist": requirements(record.get("requires_dist"))})
    return {"schema_version": SCHEMA, "python": python, "platform": target,
            "distributions": dict(sorted(cleaned.items())),
            "shadowed_distributions": dict(sorted(cleaned_shadows.items()))}


def require_fresh_base(base):
    """Full runtime installation is allowed only when no GPU stack can be replaced."""
    if any(protected(name) for field in ("distributions", "shadowed_distributions") for name in base[field]):
        raise LockError("Fresh-base mode requires a base without protected GPU distributions.")


def official_torch_url(url, record, base):
    """Retain only the selected official Linux torch wheel, never credentialed URLs."""
    if not isinstance(url, str) or any(c.isspace() or ord(c) < 32 for c in url):
        raise LockError("Invalid direct torch wheel URL.")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc not in ("download.pytorch.org", "download-r2.pytorch.org")
            or parsed.query or parsed.fragment or not re.fullmatch(r"/whl/(?:cu[0-9]+|cpu)/[A-Za-z0-9_.+%-]+", parsed.path)
            or re.search(r"%(?!2[bB])", parsed.path)
            or any(part in (".", "..") for part in unquote(parsed.path).split("/"))):
        raise LockError("Only official immutable torch wheel paths are supported.")
    wheel = unquote(parsed.path.rsplit("/", 1)[-1])
    if wheel != record["wheel"] or not SHA256.fullmatch(str(record.get("sha256", ""))):
        raise LockError("Direct torch artifact does not match its wheel/hash.")
    tag = "cp" + base["python"]["major_minor"].replace(".", "")
    machine = base["platform"]["machine"].lower()
    expected = rf"torch-{re.escape(record['version'])}-{tag}-{tag}-(?:manylinux_[0-9_]+|linux)_{re.escape(machine)}\.whl"
    if base["platform"]["system"].lower() != "linux" or not re.fullmatch(expected, wheel):
        raise LockError("Direct torch wheel differs from the target name/version/platform.")
    return url


def report_installs(base, report, *, fresh_base=False):
    if fresh_base:
        require_fresh_base(base)
    if not isinstance(report, dict) or str(report.get("version")) != "1":
        raise LockError("Unsupported pip report schema.")
    target = report.get("environment")
    if not isinstance(target, dict):
        raise LockError("Pip report is missing its target environment.")
    for key, expected in (("python_version", base["python"]["major_minor"]),
                          ("platform_machine", base["platform"]["machine"]),
                          ("platform_system", base["platform"]["system"])):
        actual = target.get(key)
        if not isinstance(actual, str) or actual.lower() != expected.lower():
            raise LockError(f"Pip report target does not match base: {key}.")
    if not isinstance(report.get("install"), list):
        raise LockError("Pip report install must be a list.")
    installs = {}
    for entry in report["install"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("metadata"), dict):
            raise LockError("Pip report install is missing metadata.")
        metadata = entry["metadata"]
        name, version = canonical_name(metadata.get("name")), exact_version(metadata.get("version"))
        if name in installs:
            raise LockError(f"Duplicate/conflicting pip install: {name}.")
        if protected(name) and not fresh_base:
            raise LockError(f"Protected base distribution would be installed: {name}.")
        download = entry.get("download_info", {})
        if not isinstance(download, dict) or not isinstance(download.get("url"), str):
            raise LockError(f"Missing wheel artifact: {name}.")
        if "dir_info" in download or "vcs_info" in download or entry.get("is_editable"):
            raise LockError(f"Only immutable wheel archives are supported: {name}.")
        wheel = unquote(urlsplit(download["url"]).path.rsplit("/", 1)[-1])
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]*\.whl", wheel):
            raise LockError(f"Only wheel artifacts are supported: {name}.")
        archive = download.get("archive_info", {})
        if not isinstance(archive, dict) or not isinstance(archive.get("hashes", {}), dict):
            raise LockError(f"Invalid wheel hashes: {name}.")
        digest = archive.get("hashes", {}).get("sha256")
        legacy = archive.get("hash")
        if legacy is not None:
            if not isinstance(legacy, str) or not legacy.startswith("sha256="):
                if digest is None:
                    raise LockError(f"Missing SHA-256 wheel hash: {name}.")
            elif digest is not None and legacy[7:].lower() != str(digest).lower():
                raise LockError(f"Conflicting SHA-256 wheel hashes: {name}.")
            else:
                digest = legacy[7:]
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise LockError(f"Missing SHA-256 wheel hash: {name}.")
        installs[name] = {"version": version, "sha256": digest.lower(), "wheel": wheel,
                          "requires_dist": requirements(metadata.get("requires_dist"))}
        if fresh_base and name == "torch" and entry.get("is_direct"):
            installs[name]["artifact_url"] = official_torch_url(download["url"], installs[name], base)
    return dict(sorted(installs.items()))


def requirement_line(name, record):
    requirement = f"{name} @ {record['artifact_url']}" if "artifact_url" in record else f"{name}=={record['version']}"
    return requirement + f" --hash=sha256:{record['sha256']}"


def generate(base, report, image, report_sha256, *, fresh_base=False):
    if not isinstance(image, str) or not BASE.fullmatch(image):
        raise LockError("Base must be pinned as image@sha256:<64 hex> or sha256:<64 hex>.")
    base = validate_inventory(base)
    installs = report_installs(base, report, fresh_base=fresh_base)
    expected = {name: record["version"] for name, record in base["distributions"].items()}
    expected.update({name: record["version"] for name, record in installs.items()})
    lock = ("# Fresh-base runtime; use only with its pinned GPU-free base image.\n" if fresh_base else
            "# Installed-aware overlay; use only with its pinned base image.\n")
    lock += "".join(requirement_line(name, record) + "\n" for name, record in installs.items())
    provenance = {"schema_version": SCHEMA, "base_image": image, "base_inventory": base,
                  "pip_report_sha256": report_sha256, "pip_version": report.get("pip_version"),
                  "report_environment": {key: report["environment"][key] for key in
                                         ("python_version", "platform_machine", "platform_system")}, "overlay": installs,
                  "expected_inventory": dict(sorted(expected.items()))}
    if fresh_base:
        provenance["installation_mode"] = "fresh-base"
    return lock, provenance


def verify(provenance, current, *, lock_text=None):
    if not isinstance(provenance, dict) or provenance.get("schema_version") != SCHEMA:
        raise LockError("Unsupported provenance schema.")
    base = validate_inventory(provenance.get("base_inventory"))
    current = validate_inventory(current)
    mode = provenance.get("installation_mode", "overlay")
    if mode not in ("overlay", "fresh-base"):
        raise LockError("Unsupported installation mode.")
    if mode == "fresh-base":
        require_fresh_base(base)
    expected = provenance.get("expected_inventory")
    if not isinstance(expected, dict):
        raise LockError("Missing expected inventory.")
    normalized = {}
    for raw_name, version in expected.items():
        name = canonical_name(raw_name)
        if name in normalized:
            raise LockError("Duplicate expected distribution.")
        normalized[name] = exact_version(version)
    expected = normalized
    overlay = provenance.get("overlay")
    if not isinstance(overlay, dict):
        raise LockError("Missing provenance overlay.")
    reconstructed = {name: record["version"] for name, record in base["distributions"].items()}
    seen = set()
    for raw_name, record in overlay.items():
        name = canonical_name(raw_name)
        if name in seen or (protected(name) and mode != "fresh-base") or not isinstance(record, dict):
            raise LockError("Invalid provenance overlay.")
        if "artifact_url" in record:
            if mode != "fresh-base" or name != "torch":
                raise LockError("Direct wheel provenance requires fresh-base torch.")
            official_torch_url(record["artifact_url"], record, base)
        seen.add(name)
        reconstructed[name] = exact_version(record.get("version"))
    if lock_text is not None:
        # Compare complete requirement lines, including source URL and artifact
        # hash, so a substituted lock cannot pass inventory-only verification.
        lines = [line.strip() for line in lock_text.splitlines() if line.strip() and not line.startswith("#")]
        if lines != [requirement_line(name, overlay[name]) for name in sorted(overlay)]:
            raise LockError("Lock requirements differ from provenance.")
    if reconstructed != expected:
        raise LockError("Expected inventory does not match base plus overlay.")
    differences = []
    for label, old, new in (("Python", base["python"]["major_minor"], current["python"]["major_minor"]),
                            ("architecture", base["platform"]["machine"], current["platform"]["machine"]),
                            ("system", base["platform"]["system"], current["platform"]["system"])):
        if old.lower() != new.lower():
            differences.append(f"Target {label} differs.")
    actual = {name: record["version"] for name, record in current["distributions"].items()}
    for name in sorted(expected.keys() - actual.keys()):
        differences.append(f"Missing: {name}=={expected[name]}")
    for name in sorted(actual.keys() - expected.keys()):
        differences.append(f"Added: {name}=={actual[name]}")
    for name in sorted(expected.keys() & actual.keys()):
        if expected[name] != actual[name]:
            differences.append(f"Changed: {name} {expected[name]} -> {actual[name]}")
    expected_shadows = base["shadowed_distributions"]
    actual_shadows = current["shadowed_distributions"]
    for name in sorted(expected_shadows.keys() - actual_shadows.keys()):
        differences.append(f"Missing shadowed distributions: {name}")
    for name in sorted(actual_shadows.keys() - expected_shadows.keys()):
        differences.append(f"Added shadowed distributions: {name}")
    for name in sorted(expected_shadows.keys() & actual_shadows.keys()):
        if expected_shadows[name] != actual_shadows[name]:
            differences.append(f"Changed shadowed distributions: {name}")
    return differences


def read_json(path):
    # Reject duplicate JSON keys rather than allowing silent last-value wins.
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise LockError("Duplicate JSON key.")
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=pairs)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("inventory", help="Record distribution metadata without importing GPU libraries")
    capture.add_argument("out")
    build = commands.add_parser("generate", help="Validate a pip dry-run report and write lock plus provenance")
    for name in ("base_json", "report_json", "lock_file", "provenance_file"):
        build.add_argument(name)
    build.add_argument("--base", required=True)
    build.add_argument("--fresh-base", action="store_true",
                       help="Allow a full GPU runtime closure only when the base has no protected GPU distributions")
    check = commands.add_parser("verify", help="Compare this environment against the complete expected inventory")
    check.add_argument("provenance_json")
    check.add_argument("--lock-file", help="Also verify every requirement URL/version/hash against provenance")
    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            write_json(args.out, inventory())
            print("Base inventory recorded.")
        elif args.command == "generate":
            report_bytes = Path(args.report_json).read_bytes()
            lock, provenance = generate(read_json(args.base_json), read_json(args.report_json), args.base,
                                        hashlib.sha256(report_bytes).hexdigest(), fresh_base=args.fresh_base)
            Path(args.lock_file).write_text(lock)
            write_json(args.provenance_file, provenance)
            print(f"Locked {len(provenance['overlay'])} overlay wheels.")
        else:
            differences = verify(read_json(args.provenance_json), inventory(),
                                 lock_text=Path(args.lock_file).read_text() if args.lock_file else None)
            for difference in differences:
                print(difference)
            if differences:
                return 1
            print("Complete installed inventory and target match provenance.")
    except (LockError, OSError, ValueError, TypeError, KeyError):
        # Do not echo raw JSON, credentials, URLs, or filesystem paths in errors.
        print("Dependency lock validation failed; inspect the local inputs and target inventory.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
