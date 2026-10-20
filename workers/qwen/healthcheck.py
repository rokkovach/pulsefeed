"""Fail readiness when either the SDK parent or the GPU engine has stopped."""
import json
import os
from pathlib import Path


def check(path=Path("/tmp/qwen-ready")):
    pids = json.loads(path.read_text())
    for key in ("parent_pid", "engine_pid"):
        pid = pids[key]
        if type(pid) is not int or pid <= 0:
            raise ValueError("Invalid readiness pid")
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] in {"Z", "X"}:
            raise RuntimeError("Worker process is dead")


if __name__ == "__main__":
    check()
