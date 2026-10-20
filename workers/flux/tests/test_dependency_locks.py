"""Check target-native lock provenance without resolving/installing GPU packages."""
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote

from packaging.specifiers import SpecifierSet
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import flux_worker as fw


def normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def roots(path):
    pins = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            pins.update(roots(path.parent / line[3:]))
            continue
        if " @ " in line:
            name, url = line.split(" @ ", 1)
            version = unquote(url.rsplit("/", 1)[-1]).split("-")[1]
        else:
            name, version = line.split("==", 1)
        pins[normalized(name)] = version
    return pins


@pytest.mark.parametrize("profile,dockerfile,requirements,machine,python", [
    ("dgx", "Dockerfile.dgx-spark", "requirements-inference.txt", "aarch64", "3.12"),
    ("runpod", "Dockerfile", "requirements.txt", "x86_64", "3.12"),
])
def test_resolved_overlay_matches_target_base_hashes_and_required_versions(profile, dockerfile, requirements, machine, python):
    lockfile = ROOT / f"requirements-{profile}.lock"
    provenance = ROOT / f"requirements-{profile}.provenance.json"
    if not lockfile.is_file() or not provenance.is_file():
        pytest.skip("Target lock is pending or outside the isolated CPU build context")
    evidence = json.loads(provenance.read_text())
    build = (ROOT / dockerfile).read_text()
    assert evidence["base_image"] in build
    assert f"--no-deps --require-hashes --only-binary=:all: -r requirements-{profile}.lock" in build
    assert f"MODEL_REVISION={fw.MODEL_REVISION}" in build
    assert "--cache-dir=/root/.cache/pip" in build and "--no-cache-dir" not in build
    assert evidence["report_environment"]["platform_machine"] == machine
    assert evidence["report_environment"]["python_version"] == python
    assert re.fullmatch(r"[a-f0-9]{64}", evidence["pip_report_sha256"])
    expected = evidence["expected_inventory"]
    for name, version in roots(ROOT / requirements).items():
        assert SpecifierSet("==" + version).contains(expected[name]), name
    entries = {}
    for line in lockfile.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^ ]+) --hash=sha256:([a-f0-9]{64})", line)
        if match:
            name, version, sha = match.groups()
        else:
            direct = re.fullmatch(r"([A-Za-z0-9_.-]+) @ (https://[^ ]+) --hash=sha256:([a-f0-9]{64})", line)
            assert direct, line
            name, url, sha = direct.groups()
            package = evidence["overlay"][normalized(name)]
            assert url == package["artifact_url"]
            version = package["version"]
        name = normalized(name)
        assert name not in entries
        entries[name] = (version, sha)
    assert set(entries) == set(evidence["overlay"])
    for name, (version, sha) in entries.items():
        package = evidence["overlay"][name]
        assert (version, sha) == (package["version"], package["sha256"])
        assert package["wheel"].endswith(".whl")
        assert expected[name] == version
    for name, package in evidence["base_inventory"]["distributions"].items():
        if name.startswith(("torch", "nvidia-", "pytorch-triton")) or name == "triton":
            assert name not in entries, name
            assert expected[name] == package["version"], name
