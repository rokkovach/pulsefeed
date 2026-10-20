"""Cache policy tests use tiny files and injected Hub calls, never model weights."""
from pathlib import Path
import os
import hashlib
import json
import sys
import time
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import flux_worker as fw


def config(**env):
    with mock.patch.dict(os.environ, env, clear=True):
        return fw.RuntimeConfig.from_env()


def test_default_and_custom_volume_cache_are_preserved():
    value = config()
    assert value.cache_source == "volume" and not value.local_files_only
    assert value.cache_dir == "/runpod-volume/huggingface/hub"
    assert config(HF_HUB_CACHE="/custom/weights").cache_dir == "/custom/weights"


@pytest.mark.parametrize("baked_path", ["/runpod-volume/huggingface/hub", "/custom/weights"])
def test_managed_cache_overrides_existing_baked_path(baked_path):
    value = config(MODEL_CACHE_SOURCE="runpod-managed", HF_HUB_CACHE=baked_path)
    assert value.cache_dir == "/runpod-volume/huggingface-cache/hub"


@pytest.mark.parametrize("env,expected", [
    ({"MODEL_CACHE_OFFLINE": "true"}, True),
    ({"MODEL_CACHE_OFFLINE": "false"}, False),
    ({"FLUX_LOCAL_FILES_ONLY": "1"}, True),
    ({"FLUX_LOCAL_FILES_ONLY": "0"}, False),
    ({"MODEL_CACHE_OFFLINE": "true", "FLUX_LOCAL_FILES_ONLY": "1"}, True),
    ({"MODEL_CACHE_OFFLINE": "false", "FLUX_LOCAL_FILES_ONLY": "0"}, False),
])
def test_offline_flag_and_legacy_compatibility(env, expected):
    assert config(**env).local_files_only is expected


@pytest.mark.parametrize("env", [
    {"MODEL_CACHE_SOURCE": "latest"}, {"MODEL_CACHE_SOURCE": ""},
    {"MODEL_CACHE_OFFLINE": "1"}, {"MODEL_CACHE_OFFLINE": "TRUE"},
    {"MODEL_CACHE_OFFLINE": ""}, {"FLUX_LOCAL_FILES_ONLY": "true"},
    {"MODEL_CACHE_OFFLINE": "true", "FLUX_LOCAL_FILES_ONLY": "0"},
    {"MODEL_CACHE_OFFLINE": "false", "FLUX_LOCAL_FILES_ONLY": "1"},
    {"MODEL_REVISION": "a" * 40}, {"MODEL_REVISION": "main"},
])
def test_invalid_or_conflicting_cache_policy_fails_startup(env):
    with pytest.raises(fw.WorkerFailure) as caught:
        config(**env)
    assert caught.value.code == "MODEL_NOT_READY"


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    # Keep all real required names/shards, but substitute tiny synthetic sizes.
    monkeypatch.setattr(fw, "SNAPSHOT_FILES", {name: 4 for name in fw.SNAPSHOT_FILES})
    monkeypatch.setattr(fw, "METADATA_SHA256", {name: hashlib.sha256(b"fake").hexdigest() for name in fw.METADATA_SHA256})
    path = tmp_path / ("models--" + fw.MODEL_ID.replace("/", "--")) / "snapshots" / fw.MODEL_REVISION
    for name in fw.SNAPSHOT_FILES:
        item = path / name
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_bytes(b"fake")
    return tmp_path, path


def deadline():
    return fw.Deadline(int((time.time() + 60) * 1000), 60)


@pytest.mark.parametrize("name", ["None.py", "text_encoder/transformers.py", "transformer/diffusers.py"])
def test_extra_python_rejected_even_with_matching_metadata(snapshot, name):
    _, path = snapshot
    (path / name).write_text("raise RuntimeError('must never execute')")
    with pytest.raises(fw.WorkerFailure):
        fw.verify_snapshot(path)


@pytest.mark.parametrize("name", list(fw.METADATA_SHA256))
def test_modified_routing_metadata_rejected(snapshot, name):
    _, path = snapshot
    # Same size bypasses the completeness check, so this exercises authentication.
    (path / name).write_bytes(b"evil")
    with pytest.raises(fw.WorkerFailure):
        fw.verify_snapshot(path)


@pytest.mark.parametrize("name", ["text_encoder/model.safetensors.index.json", "tokenizer/chat_template.jinja"])
def test_metadata_fifo_rejected_before_open(snapshot, monkeypatch, name):
    _, path = snapshot
    item = path / name
    item.unlink()
    os.mkfifo(item)
    def unexpected_open(*args, **kwargs):
        pytest.fail("Special files must be rejected before reading metadata")
    monkeypatch.setattr(Path, "open", unexpected_open)
    with pytest.raises(fw.WorkerFailure):
        fw.verify_snapshot(path)


@pytest.mark.parametrize("shard", ["../../outside.safetensors", "/tmp/outside.safetensors", "pipe.safetensors"])
def test_unreviewed_weight_map_rejected_even_when_file_size_matches(snapshot, monkeypatch, shard):
    _, path = snapshot
    name = "text_encoder/model.safetensors.index.json"
    payload = json.dumps({"weight_map": {"weight": shard}}).encode()
    (path / name).write_bytes(payload)
    monkeypatch.setitem(fw.SNAPSHOT_FILES, name, len(payload))
    with pytest.raises(fw.WorkerFailure):
        fw.verify_snapshot(path)


@pytest.mark.parametrize("name", ["tokenizer/tokenizer.json", "text_encoder/model.safetensors.index.json", "tokenizer/chat_template.jinja"])
def test_normal_hub_blob_symlink_supported_but_external_links_rejected(snapshot, tmp_path, name):
    _, path = snapshot
    blobs = path.parent.parent / "blobs"
    blobs.mkdir()
    source = path / name
    source.rename(blobs / "tokenizer")
    source.symlink_to("../../../blobs/tokenizer")
    assert fw.verify_snapshot(path) == str(path)
    source.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"fake")
    source.symlink_to(outside)
    with pytest.raises(fw.WorkerFailure):
        fw.verify_snapshot(path)


def test_symlinked_component_directory_is_rejected(snapshot, tmp_path):
    _, path = snapshot
    target = tmp_path / "component"
    (path / "scheduler").rename(target)
    (path / "scheduler").symlink_to(target, target_is_directory=True)
    with pytest.raises(fw.WorkerFailure):
        fw.verify_snapshot(path)


def test_malicious_cache_fails_before_gpu_import(snapshot):
    root, path = snapshot
    (path / "None.py").write_text("raise RuntimeError('must never execute')")
    imports = []
    original_import = __import__
    def importing(name, *args, **kwargs):
        imports.append(name)
        return original_import(name, *args, **kwargs)
    with mock.patch.dict(sys.modules, {"huggingface_hub": SimpleNamespace(snapshot_download=mock.Mock())}), \
            mock.patch("builtins.__import__", side_effect=importing):
        with pytest.raises(fw.WorkerFailure):
            fw.DiffusersEngine(fw.RuntimeConfig(cache_dir=str(root), local_files_only=True))
    assert not {"torch", "diffusers", "transformers"}.intersection(imports)


def test_cached_only_complete_snapshot_uses_exact_revision_and_offline_hub(snapshot):
    root, path = snapshot
    download = mock.Mock(return_value=str(path))
    value = fw.RuntimeConfig(cache_dir=str(root), local_files_only=True)
    assert fw.resolve_model_snapshot(value, deadline(), download) == str(path)
    assert download.call_count == 1
    options = download.call_args.kwargs
    assert options["local_files_only"] is True
    assert options["revision"] == fw.MODEL_REVISION and options["repo_id"] == fw.MODEL_ID


@pytest.mark.parametrize("name", ["model_index.json", "text_encoder/config.json",
    "text_encoder/model-00002-of-00002.safetensors", "tokenizer/tokenizer.json",
    "transformer/diffusion_pytorch_model.safetensors", "vae/diffusion_pytorch_model.safetensors"])
def test_offline_missing_file_or_shard_rejects_before_hub_lookup(snapshot, name):
    root, path = snapshot
    (path / name).unlink()
    download = mock.Mock()
    with pytest.raises(fw.WorkerFailure) as caught:
        fw.resolve_model_snapshot(fw.RuntimeConfig(cache_dir=str(root), local_files_only=True), deadline(), download)
    assert caught.value.code == "MODEL_NOT_READY"
    download.assert_not_called()


@pytest.mark.parametrize("replacement", [b"", b"x", b"too-long"])
def test_truncated_or_incorrect_size_cache_rejects_without_network(snapshot, replacement):
    root, path = snapshot
    (path / "text_encoder/model-00001-of-00002.safetensors").write_bytes(replacement)
    download = mock.Mock()
    with pytest.raises(fw.WorkerFailure):
        fw.resolve_model_snapshot(fw.RuntimeConfig(cache_dir=str(root), local_files_only=True), deadline(), download)
    download.assert_not_called()


def test_dangling_blob_symlink_is_incomplete(snapshot):
    _, path = snapshot
    item = path / "transformer/diffusion_pytorch_model.safetensors"
    item.unlink()
    item.symlink_to(path / "missing-blob")
    with pytest.raises(fw.WorkerFailure):
        fw.verify_snapshot(path)


def test_other_revision_and_refs_main_do_not_restore_missing_pinned_snapshot(snapshot):
    root, path = snapshot
    path.rename(path.parent / ("a" * 40))
    refs = path.parent.parent / "refs"
    refs.mkdir()
    (refs / "main").write_text("a" * 40)
    download = mock.Mock()
    with pytest.raises(fw.WorkerFailure):
        fw.resolve_model_snapshot(fw.RuntimeConfig(cache_dir=str(root), local_files_only=True), deadline(), download)
    download.assert_not_called()


def test_downloader_cannot_redirect_to_another_revision(snapshot):
    root, path = snapshot
    download = mock.Mock(return_value=str(path.parent / ("b" * 40)))
    with pytest.raises(fw.WorkerFailure) as caught:
        fw.resolve_model_snapshot(fw.RuntimeConfig(cache_dir=str(root)), deadline(), download)
    assert caught.value.code == "MODEL_NOT_READY" and download.call_count == 1


def test_online_download_also_verifies_all_files(snapshot):
    root, path = snapshot
    (path / "tokenizer/tokenizer.json").unlink()
    download = mock.Mock(return_value=str(path))
    with pytest.raises(fw.WorkerFailure):
        fw.resolve_model_snapshot(fw.RuntimeConfig(cache_dir=str(root)), deadline(), download)
    assert download.call_args.kwargs["local_files_only"] is False
    assert download.call_count == 1


def test_missing_offline_cache_does_not_deserialize_or_load_model(tmp_path):
    pipeline = mock.Mock()
    download = mock.Mock()
    with mock.patch.dict(sys.modules, {
        "torch": mock.Mock(), "transformers": mock.Mock(),
        "diffusers": SimpleNamespace(Flux2KleinPipeline=pipeline),
        "huggingface_hub": SimpleNamespace(snapshot_download=download),
    }), mock.patch.object(fw, "preflight_runtime"):
        with pytest.raises(fw.WorkerFailure):
            fw.DiffusersEngine(fw.RuntimeConfig(cache_dir=str(tmp_path), local_files_only=True))
    pipeline.from_pretrained.assert_not_called()
    download.assert_not_called()
