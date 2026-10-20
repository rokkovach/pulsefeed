"""Configuration-only checks: no daemon, private env, downloads or model startup."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def clean_env():
    return {"PATH": os.defpath + ":/opt/homebrew/bin:/usr/local/bin",
            "PULSEFEED_PYTHON": sys.executable}


@pytest.mark.parametrize("qwen,flux", [(None, None), ("true", "false"), ("false", "true")])
@pytest.mark.parametrize("capacity", [None, "1", "256"])
def test_rendered_compose_preserves_caches_and_bounds_logs(tmp_path, qwen, flux, capacity):
    if not shutil.which("docker", path=clean_env()["PATH"]):
        pytest.skip("Docker Compose CLI unavailable; no daemon is required")
    env_file = tmp_path / "synthetic.env"
    values = ["LOCAL_WORKER_TOKEN=" + "synthetic-test-" * 4]
    if capacity is not None:
        values.append(f"LOCAL_RESULT_CAPACITY={capacity}")
    for key, value in (("QWEN_CACHE_OFFLINE", qwen), ("FLUX_CACHE_OFFLINE", flux)):
        if value is not None:
            values.append(f"{key}={value}")
    env_file.write_text("\n".join(values) + "\n")
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(env_file), "-f",
         str(ROOT / "local/compose.yaml"), "config", "--format", "json"],
        env=clean_env(), capture_output=True, text=True, check=True,
    )
    config = json.loads(result.stdout)
    for stage, offline in (("qwen", qwen), ("flux", flux)):
        service = config["services"][stage]
        assert service["logging"] == {
            "driver": "local", "options": {"max-size": "10m", "max-file": "3"}}
        assert service["environment"]["MODEL_CACHE_SOURCE"] == "volume"
        assert service["environment"]["MODEL_CACHE_OFFLINE"] == (offline or "false")
        assert service["environment"]["LOCAL_RESULT_CAPACITY"] == (capacity or "128")
        mounts = {mount["target"]: mount for mount in service["volumes"]}
        assert mounts["/runpod-volume/huggingface"]["source"].endswith(stage + "-cache")
        assert mounts["/coordination"]["source"].endswith("coordination")
        assert service["ports"][0]["host_ip"] == "127.0.0.1"
        assert service["platform"] == "linux/arm64"
    assert config["services"]["qwen"]["environment"]["MODEL_CACHE_DIR"] == "/runpod-volume/huggingface"
    assert config["services"]["flux"]["environment"]["HF_HUB_CACHE"] == "/runpod-volume/huggingface/hub"


def invoke_control(tmp_path, extra):
    local = tmp_path / "local"
    local.mkdir()
    shutil.copyfile(ROOT / "local/control.sh", local / "control.sh")
    env_file = local / ".env"
    env_file.write_text("LOCAL_WORKER_TOKEN=" + "synthetic-worker-" * 3 +
                        "\nSESSION_SECRET=" + "synthetic-session-" * 3 + "\n" + extra)
    env_file.chmod(0o600)
    commands = tmp_path / "bin"
    commands.mkdir()
    docker = commands / "docker"
    docker.write_text('#!/bin/sh\nexec "$PULSEFEED_PYTHON" -c \'import json,os,sys; print(json.dumps({"argv":sys.argv[1:],"qwen":os.getenv("QWEN_CACHE_OFFLINE"),"flux":os.getenv("FLUX_CACHE_OFFLINE"),"capacity":os.getenv("LOCAL_RESULT_CAPACITY")}))\' "$@"\n')
    docker.chmod(0o700)
    env = clean_env()
    env["PATH"] = str(commands) + ":" + env["PATH"]
    return subprocess.run(["bash", str(local / "control.sh"), "config"],
                          env=env, capture_output=True, text=True)


def test_raw_parser_allows_independent_offline_keys(tmp_path):
    result = invoke_control(tmp_path, "QWEN_CACHE_OFFLINE=true\nFLUX_CACHE_OFFLINE=false\n")
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert (value["qwen"], value["flux"]) == ("true", "false")
    assert value["argv"][-2:] == ["config", "--quiet"]


def test_raw_parser_does_not_evaluate_offline_values(tmp_path):
    sentinel = tmp_path / "must-not-exist"
    literal = f"$(touch {sentinel})"
    result = invoke_control(tmp_path, f"QWEN_CACHE_OFFLINE={literal}\nFLUX_CACHE_OFFLINE=true\n")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["qwen"] == literal
    assert not sentinel.exists()


def test_raw_parser_rejects_unlisted_cache_setting(tmp_path):
    result = invoke_control(tmp_path, "MODEL_CACHE_SOURCE=runpod-managed\n")
    assert result.returncode != 0
    assert "Unexpected local/.env key" in result.stderr
    assert not result.stdout


def test_raw_parser_allows_result_capacity_without_evaluating_it(tmp_path):
    sentinel = tmp_path / "must-not-exist"
    literal = f"$(touch {sentinel})"
    result = invoke_control(tmp_path, f"LOCAL_RESULT_CAPACITY={literal}\n")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["capacity"] == literal
    assert not sentinel.exists()


def test_example_keeps_offline_startup_opt_in():
    example = (ROOT / "local/.env.example").read_text()
    assert "\nQWEN_CACHE_OFFLINE=false\n" in example
    assert "\nFLUX_CACHE_OFFLINE=false\n" in example
    assert "\nLOCAL_RESULT_CAPACITY=128\n" in example


@pytest.mark.parametrize("preparation_fails", [False, True])
def test_build_uses_private_prepared_context_and_cleans_on_failure(tmp_path, preparation_fails):
    """Exercise the launcher with fake Docker/host detection, without downloads."""
    local = tmp_path / "local"
    local.mkdir()
    shutil.copyfile(ROOT / "local/control.sh", local / "control.sh")
    env_file = local / ".env"
    env_file.write_text("LOCAL_WORKER_TOKEN=" + "synthetic-worker-" * 3 +
                        "\nSESSION_SECRET=" + "synthetic-session-" * 3 + "\n")
    env_file.chmod(0o600)
    worker = tmp_path / "workers/qwen"
    worker.mkdir(parents=True)
    preparation_record = tmp_path / "prepared.json"
    (worker / "prepare_build.py").write_text(
        "import json,os,pathlib,sys\n"
        "target=pathlib.Path(sys.argv[sys.argv.index('--output-dir')+1])\n"
        "assert not target.exists()\n"
        "assert sys.argv[sys.argv.index('--platform')+1]=='arm64'\n"
        "assert target.parent.stat().st_mode & 0o077 == 0\n"
        "target.mkdir()\n"
        f"pathlib.Path({str(preparation_record)!r}).write_text(json.dumps(str(target)))\n"
        f"sys.exit({1 if preparation_fails else 0})\n")
    commands = tmp_path / "bin"
    commands.mkdir()
    (commands / "uname").write_text(
        '#!/bin/sh\ncase "$1" in -s) echo Linux ;; -m) echo aarch64 ;; *) exit 1 ;; esac\n')
    (commands / "docker").write_text(
        '#!/bin/sh\nexec "$PULSEFEED_PYTHON" -c '
        "'import json,os,pathlib,sys; p=pathlib.Path(os.environ[\"QWEN_BUILD_CONTEXT\"]); "
        "assert p.is_dir(); print(json.dumps({\"context\":str(p),\"argv\":sys.argv[1:]}))' \"$@\"\n")
    for command in commands.iterdir():
        command.chmod(0o700)
    env = clean_env()
    env["PATH"] = str(commands) + ":" + env["PATH"]
    env["TMPDIR"] = str(tmp_path)
    result = subprocess.run(["bash", str(local / "control.sh"), "build"],
                            env=env, capture_output=True, text=True)
    prepared = Path(json.loads(preparation_record.read_text()))
    assert not prepared.parent.exists(), "Private archive staging must be removed on every exit"
    if preparation_fails:
        assert result.returncode != 0
        assert not result.stdout, "Docker must not run after failed preparation"
    else:
        assert result.returncode == 0, result.stderr
        rendered = json.loads(result.stdout)
        assert rendered["context"] == str(prepared)
        assert rendered["argv"][-1] == "build"
