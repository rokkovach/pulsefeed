"""Remove the pinned base's obsolete ensurepip wheel after installing the locked pip."""
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import pip

assert metadata.version("pip") == pip.__version__ == "26.2.1"
path = Path("/usr/local/lib/python3.12/ensurepip/_bundled/pip-25.0.1-py3-none-any.whl")
digest = hashlib.sha256(path.read_bytes()).hexdigest()
assert digest == "c46efd13b6aa8279f33f2864459c8ce587ea6a1a59ee20de055868d8f7688f7f"
record = {"removed_path": str(path), "removed_sha256": digest, "removed_bytes": path.stat().st_size,
          "active_pip_version": pip.__version__, "purpose": "Remove unused bootstrap archive; active pip and its metadata remain intact.",
          "runtime_policy": "This worker image does not bootstrap new virtual environments. Dependency changes require an image rebuild."}
path.unlink()
assert not path.exists()
removed_tools = []
for tool in (Path("/worker/lock_dependencies.py"), Path("/worker/capture_environment.py")):
    if tool.exists():
        tool.unlink()
        removed_tools.append(str(tool))
record["removed_candidate_build_tools"] = removed_tools
Path("/opt/flux-build-evidence/bootstrap-cleanup.json").write_text(json.dumps(record, indent=2) + "\n")
print(json.dumps(record))
