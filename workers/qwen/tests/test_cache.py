"""CPU cache and image-packaging checks; synthetic files never represent real weights."""
import json
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from workers.qwen.handler import MODEL_ID, MODEL_REVISION, WorkerError
from workers.qwen.runtime import (ProcessRuntime, RUNPOD_MANAGED_CACHE, cache_settings,
                                  download_snapshot, engine_process, validate_cached_snapshot)


def synthetic_snapshot(folder, *, sharded=True):
    root = Path(folder)
    for name in ("config.json", "hf_quant_config.json", "tokenizer.json", "tokenizer_config.json", "generation_config.json"):
        (root / name).write_text("{}")
    (root / "hf_quant_config.json").write_text('{"quantization":{"quant_algo":"NVFP4"}}')
    (root / "chat_template.jinja").write_text("enable_thinking")
    if sharded:
        (root / "model.safetensors.index.json").write_text(json.dumps(
            {"weight_map": {"layer.one": "model-00001-of-00002.safetensors", "layer.two": "model-00002-of-00002.safetensors"}}))
        for name in ("model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"):
            (root / name).write_bytes(b"synthetic CPU fixture, not model weights")
    else:
        (root / "model.safetensors").write_bytes(b"synthetic CPU fixture, not model weights")
    return root


class CacheTests(unittest.TestCase):
    def test_validation_identifies_failed_check_without_logging_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            root = synthetic_snapshot(folder)
            (root / "config.json").write_text("secret-content-must-not-be-logged")
            with self.assertLogs("workers.qwen.runtime", level="ERROR") as logs, self.assertRaises(WorkerError) as error:
                validate_cached_snapshot(root)
            self.assertIn("metadata_sha256:config.json", str(error.exception))
            output = "\n".join(logs.output)
            self.assertIn("metadata_sha256:config.json", output)
            self.assertNotIn("secret-content", output)
            self.assertNotIn(folder, output)

    def test_fifo_metadata_is_rejected_before_open(self):
        from workers.qwen import runtime
        for name in runtime.METADATA_SHA256:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = synthetic_snapshot(folder)
                (root / name).unlink()
                os.mkfifo(root / name)
                # Earlier metadata may be read, but the FIFO itself must never
                # reach open; no blocking subprocess or real FIFO read is used.
                original_open = os.open
                def open_regular(path, *args, **kwargs):
                    self.assertNotEqual(Path(path), root / name)
                    return original_open(path, *args, **kwargs)
                with patch.object(runtime.os, "open", side_effect=open_regular), self.assertRaises(WorkerError):
                    validate_cached_snapshot(root)

    def test_modified_routing_metadata_and_executable_files_fail_closed(self):
        for mutation in ("auto_map", "python", "external_link"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as folder:
                root = synthetic_snapshot(folder)
                if mutation == "auto_map":
                    (root / "config.json").write_text('{"auto_map":{"AutoConfig":"extra.Config"}}')
                elif mutation == "python":
                    (root / "extra.py").write_text("raise RuntimeError('must never execute')")
                else:
                    with tempfile.NamedTemporaryFile() as external:
                        (root / "tokenizer.json").unlink()
                        (root / "tokenizer.json").symlink_to(external.name)
                        with self.assertRaises(WorkerError):
                            validate_cached_snapshot(root)
                    continue
                with self.assertRaises(WorkerError):
                    validate_cached_snapshot(root)

    def test_pinned_index_and_template_fit_bounded_reads_and_recognize_shards(self):
        from workers.qwen import runtime
        fixtures = Path(__file__).with_name("fixtures")
        with tempfile.TemporaryDirectory() as folder:
            root = synthetic_snapshot(folder)
            expected = dict(runtime.METADATA_SHA256)
            for name, digest in {
                "model.safetensors.index.json": "d7cddcad23c80b41201f23e3fccb22ef200ed551da067c61afb0b09f67864f47",
                "chat_template.jinja": "a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8",
            }.items():
                raw = (fixtures / ("pinned-" + name)).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
                (root / name).write_bytes(raw)
                expected[name] = digest
            self.assertEqual((root / "model.safetensors.index.json").stat().st_size, 105_079)
            with patch.object(runtime, "METADATA_SHA256", expected):
                validate_cached_snapshot(root)
                for replacement in (b"{}", b"x" * 131_073):
                    (root / "model.safetensors.index.json").write_bytes(replacement)
                    with self.assertRaises(WorkerError):
                        validate_cached_snapshot(root)

    def test_alternate_unreviewed_weight_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = synthetic_snapshot(folder)
            (root / "model.safetensors").write_bytes(b"unreviewed alternate file")
            with self.assertRaises(WorkerError):
                validate_cached_snapshot(root)

    def test_external_shared_cache_requires_pinned_bytes_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as shared:
            root = synthetic_snapshot(folder)
            external = Path(shared) / "shared-tokenizer"
            raw = (root / "tokenizer.json").read_bytes()
            external.write_bytes(raw)
            (root / "tokenizer.json").unlink()
            (root / "tokenizer.json").symlink_to(external)
            pins = {"tokenizer.json": {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}}
            with patch("workers.qwen.runtime.PINNED_ARTIFACTS", pins):
                validate_cached_snapshot(root)
                # Same size, wrong contents must fail despite a valid cache link.
                external.write_bytes(b"!!")
                with self.assertRaises(WorkerError) as error:
                    validate_cached_snapshot(root)
                self.assertIn("external_artifact_identity:tokenizer.json", str(error.exception))
                external.write_bytes(raw + b"truncated-or-extra")
                with self.assertRaises(WorkerError):
                    validate_cached_snapshot(root)

    def test_unpinned_external_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.NamedTemporaryFile() as external:
            root = synthetic_snapshot(folder)
            (root / "unexpected.txt").symlink_to(external.name)
            with self.assertRaises(WorkerError):
                validate_cached_snapshot(root)

    def test_external_fifo_is_rejected_without_reading(self):
        from workers.qwen.runtime import validate_external_artifact
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "fifo"
            os.mkfifo(target)
            with self.assertRaises(ValueError):
                validate_external_artifact(root / "tokenizer.json", target, root)

    def test_normal_hub_blob_symlink_is_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            root = repo / "snapshots" / MODEL_REVISION
            root.mkdir(parents=True)
            synthetic_snapshot(root)
            blobs = repo / "blobs"
            blobs.mkdir()
            (root / "tokenizer.json").rename(blobs / "tokenizer")
            (root / "tokenizer.json").symlink_to("../../blobs/tokenizer")
            validate_cached_snapshot(root)

    def test_online_malicious_metadata_rejects_before_gpu_import(self):
        with tempfile.TemporaryDirectory() as folder:
            root = synthetic_snapshot(folder)
            (root / "config.json").write_text('{"auto_map":{"AutoConfig":"extra.Config"}}')
            connection = Mock()
            imports = []
            original_import = __import__
            def importing(name, *args, **kwargs):
                imports.append(name)
                return original_import(name, *args, **kwargs)
            with patch.dict(sys.modules, {"huggingface_hub": types.SimpleNamespace(snapshot_download=Mock(return_value=folder))}), \
                    patch("workers.qwen.runtime.package_versions", return_value={}), \
                    patch("workers.qwen.runtime.os.setsid"), patch("builtins.__import__", side_effect=importing):
                engine_process(connection, {"revision": MODEL_REVISION, "cache": folder, "startup_seconds": 1})
            self.assertEqual(connection.send.call_args.args[0]["error"]["code"], "MODEL_NOT_READY")
            self.assertNotIn("torch", imports)
            self.assertNotIn("vllm", imports)

    def setUp(self):
        # Explicit synthetic metadata expectations; these are not real model hashes.
        values = {name: b"{}" for name in ("config.json", "tokenizer_config.json", "generation_config.json")}
        values["chat_template.jinja"] = b"enable_thinking"
        values["model.safetensors.index.json"] = json.dumps({"weight_map": {"layer.one": "model-00001-of-00002.safetensors", "layer.two": "model-00002-of-00002.safetensors"}}).encode()
        values["hf_quant_config.json"] = b'{"quantization":{"quant_algo":"NVFP4"}}'
        patcher = patch("workers.qwen.runtime.METADATA_SHA256", {name: hashlib.sha256(raw).hexdigest() for name, raw in values.items()})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_volume_default_and_spark_prefetch_path_are_preserved(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cache_settings(), {"cache": "/runpod-volume/huggingface",
                                              "cache_source": "volume", "cache_offline": False})
        with patch.dict(os.environ, {"QWEN_RUNTIME_PROFILE": "dgx-spark",
                                    "MODEL_CACHE_DIR": "/existing/spark-cache"}, clear=True):
            config = ProcessRuntime.from_env().config
        self.assertEqual(config["cache"], "/existing/spark-cache")
        self.assertEqual(config["revision"], MODEL_REVISION)

    def test_runpod_managed_source_overrides_baked_or_custom_volume_path(self):
        for previous in ("/runpod-volume/huggingface", "/custom/cache"):
            with patch.dict(os.environ, {"MODEL_CACHE_SOURCE": "runpod-managed", "MODEL_CACHE_DIR": previous,
                                        "MODEL_CACHE_OFFLINE": "true"}, clear=True):
                config = ProcessRuntime.from_env().config
            self.assertEqual(config["cache"], RUNPOD_MANAGED_CACHE)
            self.assertTrue(config["cache_offline"])
            self.assertEqual(config["revision"], MODEL_REVISION)

    def test_invalid_cache_configuration_is_rejected(self):
        for values in ({"MODEL_CACHE_SOURCE": "auto"}, {"MODEL_CACHE_OFFLINE": "perhaps"},
                       {"MODEL_CACHE_DIR": "relative/cache"}, {"MODEL_CACHE_DIR": ""},
                       {"MODEL_REVISION": "main", "MODEL_CACHE_SOURCE": "runpod-managed"}):
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True), self.assertRaises(WorkerError):
                ProcessRuntime.from_env()

    def test_offline_exact_revision_uses_complete_cache_without_auth_or_network_fallback(self):
        for sharded in (True,):
            with self.subTest(sharded=sharded), tempfile.TemporaryDirectory() as folder:
                synthetic_snapshot(folder, sharded=sharded)
                download = Mock(return_value=folder)
                result = download_snapshot(MODEL_REVISION, "/cache", float("inf"), download, offline=True)
                self.assertEqual(result, folder)
                download.assert_called_once_with(MODEL_ID, revision=MODEL_REVISION, cache_dir="/cache",
                                                 token=False, max_workers=2, local_files_only=True)

    def test_missing_offline_cache_fails_once_without_retry_or_online_download(self):
        download = Mock(side_effect=FileNotFoundError("private cache diagnostic"))
        with patch("workers.qwen.runtime.time.sleep") as sleep, self.assertRaises(WorkerError) as error:
            download_snapshot(MODEL_REVISION, "/cache", float("inf"), download, offline=True)
        self.assertEqual(error.exception.code, "MODEL_NOT_READY")
        self.assertFalse(error.exception.retryable)
        self.assertNotIn("private cache diagnostic", str(error.exception))
        self.assertEqual(download.call_count, 1)
        self.assertTrue(download.call_args.kwargs["local_files_only"])
        sleep.assert_not_called()

    def test_partial_offline_snapshot_requires_every_shard_and_tokenizer_file(self):
        for missing in ("config.json", "hf_quant_config.json", "tokenizer.json", "tokenizer_config.json",
                        "chat_template.jinja", "model-00002-of-00002.safetensors", "model.safetensors.index.json"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as folder:
                root = synthetic_snapshot(folder)
                (root / missing).unlink()
                download = Mock(return_value=folder)
                with self.assertRaises(WorkerError) as error:
                    download_snapshot(MODEL_REVISION, "/cache", float("inf"), download, offline=True)
                self.assertEqual(error.exception.code, "MODEL_NOT_READY")
                self.assertEqual(download.call_count, 1)

    def test_corrupt_weight_index_and_empty_weights_fail_closed(self):
        for index in ("not json", "[]", "{}", '{"weight_map":{}}',
                      '{"weight_map":{"layer":"../outside.safetensors"}}',
                      '{"weight_map":{"layer":[]}}'):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as folder:
                root = synthetic_snapshot(folder)
                (root / "model.safetensors.index.json").write_text(index)
                with self.assertRaises(WorkerError):
                    validate_cached_snapshot(root)
        with tempfile.TemporaryDirectory() as folder:
            root = synthetic_snapshot(folder, sharded=False)
            (root / "model.safetensors").write_bytes(b"")
            with self.assertRaises(WorkerError):
                validate_cached_snapshot(root)

    def test_online_startup_keeps_downloads_and_classified_retry_policy_explicit(self):
        download = Mock(side_effect=[ConnectionError("temporary"), "/resolved/pinned-snapshot"])
        with patch("workers.qwen.runtime.time.sleep"), patch("workers.qwen.runtime.validate_cached_snapshot"):
            result = download_snapshot(MODEL_REVISION, "/cache", float("inf"), download)
        self.assertEqual(result, "/resolved/pinned-snapshot")
        self.assertEqual(download.call_count, 2)
        self.assertTrue(all(call.kwargs["revision"] == MODEL_REVISION and
                            call.kwargs["local_files_only"] is False for call in download.call_args_list))

    def test_offline_engine_readiness_requires_complete_snapshot_before_model_loading(self):
        for complete in (False, True):
            with self.subTest(complete=complete), tempfile.TemporaryDirectory() as folder:
                if complete:
                    root = synthetic_snapshot(folder)
                    (root / "hf_quant_config.json").write_text('{"quantization":{"quant_algo":"NVFP4"}}')
                tokenizer = Mock(chat_template="enable_thinking")
                tokenizer.apply_chat_template.return_value = [1, 2]
                engine = Mock()
                engine.get_tokenizer.return_value = tokenizer
                llm = Mock(return_value=engine)
                download = Mock(return_value=folder)
                connection = Mock()
                connection.recv.side_effect = EOFError
                modules = {"torch": types.SimpleNamespace(cuda=Mock(), version=types.SimpleNamespace(cuda="12.9")),
                           "vllm": types.SimpleNamespace(LLM=llm, SamplingParams=Mock()),
                           "huggingface_hub": types.SimpleNamespace(snapshot_download=download)}
                with patch.dict(sys.modules, modules), patch.dict(os.environ, {}, clear=True), \
                        patch("workers.qwen.runtime.os.setsid"), \
                        patch("workers.qwen.runtime.package_versions", return_value={"vllm": "synthetic-test"}), \
                        patch("workers.qwen.runtime.validate_device", return_value="synthetic-test-GPU"):
                    engine_process(connection, {"revision": MODEL_REVISION, "cache": folder,
                                               "startup_seconds": 1, "context_tokens": 4096, "cache_offline": True})
                    self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
                self.assertTrue(download.call_args.kwargs["local_files_only"])
                packet = connection.send.call_args_list[0].args[0]
                if complete:
                    self.assertIn("ready", packet)
                    self.assertEqual(llm.call_args.kwargs["model"], folder)
                    self.assertEqual(engine.generate.call_count, 1)  # Synthetic startup probe only.
                else:
                    self.assertEqual(packet["error"]["code"], "MODEL_NOT_READY")
                    llm.assert_not_called()

    def test_spark_image_copies_only_runtime_and_reviewed_build_metadata(self):
        root = Path(__file__).resolve().parents[1]
        lines = (root / "Dockerfile.dgx-spark").read_text().splitlines()
        sources = [source for line in lines if line.startswith("COPY ") for source in line.split()[1:-1]]
        self.assertEqual(set(sources), {"workers/qwen/" + name for name in ['base-gpu.lock', 'apply_os_updates.py', 'base-os-artifacts.json', 'base-python-inventory.json', 'base-os-inventory.tsv', 'verify_base.py', 'lock_dependencies.py', 'requirements-dgx.lock', 'requirements-dgx.provenance.json', 'check_dependencies.py', 'handler.py', 'runtime.py', 'local_factory.py', 'healthcheck.py', 'smoke_dgx.py']} | {"local/worker_gateway.py"})
        self.assertIn(".env.*", (root / ".dockerignore").read_text().splitlines())


if __name__ == "__main__":
    unittest.main()
