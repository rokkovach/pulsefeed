"""CPU checks for the opt-in GB10 profile; no NVIDIA wheels or weights imported."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

from workers.qwen.handler import MODEL_ID, MODEL_REVISION, Result, WorkerError
from workers.qwen.local_factory import LocalQwenWorker, create_worker
from workers.qwen.runtime import (DGX_EXPECTED, DGX_KV_BYTES, EXPECTED, ProcessRuntime,
                                  configure_backend, engine_process, engine_settings,
                                  package_versions, render_tokens, validate_device, write_dgx_runtime_lock)


def gpu(name="NVIDIA GB10", capability=(12, 1), cuda="13.0"):
    torch = types.SimpleNamespace(cuda=Mock(), version=types.SimpleNamespace(cuda=cuda))
    torch.cuda.is_available.return_value = True
    torch.cuda.device_count.return_value = 1
    torch.cuda.get_device_name.return_value = name
    torch.cuda.get_device_capability.return_value = capability
    torch.cuda.get_device_properties.return_value = types.SimpleNamespace(total_memory=32*1024**3)
    return torch


def request():
    return {"schema_version": 1, "stage": "qwen", "run_id": "11111111-1111-4111-8111-111111111111",
            "invocation_id": "22222222-2222-4222-8222-222222222222",
            "deadline_unix_ms": int(time.time()*1000)+60000, "max_new_tokens": 256,
            "article": {"title": "A community garden", "excerpt": ""}, "system_prompt": "Make a visual prompt."}


class DGXTests(unittest.TestCase):
    def test_transformers5_batch_encoding_default_is_explicitly_disabled(self):
        def apply_template(messages, *, return_dict=True, **kwargs):
            return {"input_ids": [0, 11, 12]} if return_dict else [0, 11, 12]

        tokenizer = Mock(chat_template="enable_thinking")
        tokenizer.apply_chat_template.side_effect = apply_template
        self.assertEqual(render_tokens(tokenizer, request()), [0, 11, 12])
        self.assertIs(tokenizer.apply_chat_template.call_args.kwargs["return_dict"], False)
        self.assertIs(tokenizer.apply_chat_template.call_args.kwargs["enable_thinking"], False)

    def test_malformed_token_ids_fail_before_inference(self):
        for tokens in (None, {}, {"input_ids": [1]}, [], [[1]], (1, 2), [True], [False], [-1], [1.0], ["1"]):
            with self.subTest(tokens=tokens):
                tokenizer = Mock(chat_template="enable_thinking")
                tokenizer.apply_chat_template.return_value = tokens
                with self.assertRaises(WorkerError) as error:
                    render_tokens(tokenizer, request())
                self.assertEqual(error.exception.code, "UNSUPPORTED_RUNTIME")

    def test_missing_or_malformed_template_fails_with_controlled_error(self):
        for template in (None, "", "  ", {}, ["enable_thinking"], 1, "no thinking switch"):
            with self.subTest(template=template):
                tokenizer = Mock(chat_template=template)
                with self.assertRaises(WorkerError) as error:
                    render_tokens(tokenizer, request())
                self.assertEqual(error.exception.code, "UNSUPPORTED_RUNTIME")
                tokenizer.apply_chat_template.assert_not_called()

    def test_official_base_starts_gateway_directly(self):
        dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile.dgx-spark"
        entrypoints = [line.removeprefix("ENTRYPOINT ") for line in dockerfile.read_text().splitlines()
                       if line.startswith("ENTRYPOINT ")]
        self.assertEqual(len(entrypoints), 1)
        self.assertEqual(json.loads(entrypoints[0]), ["python3", "-u", "/gateway/worker_gateway.py"])

    def test_default_profile_preserves_runpod_and_spark_is_explicit(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(ProcessRuntime.from_env().config["profile"], "runpod")
        with patch.dict(os.environ, {"QWEN_RUNTIME_PROFILE": "dgx-spark"}, clear=True):
            config = ProcessRuntime.from_env().config
        self.assertEqual(config["profile"], "dgx-spark")
        self.assertEqual(config["revision"], MODEL_REVISION)
        self.assertEqual(config["context_tokens"], 4096)
        self.assertTrue(config["runtime_lock"].endswith("dgx-runtime-lock.json"))

    def test_unknown_profile_and_model_substitution_fail(self):
        for values in ({"QWEN_RUNTIME_PROFILE": "auto"},
                       {"QWEN_RUNTIME_PROFILE": "dgx-spark", "MODEL_REVISION": "main"}):
            with patch.dict(os.environ, values, clear=True), self.assertRaises(WorkerError):
                ProcessRuntime.from_env()

    def test_spark_requires_linux_arm64_gb10_sm121_and_pinned_cuda(self):
        with patch("workers.qwen.runtime.platform.system", return_value="Linux"), \
                patch("workers.qwen.runtime.platform.machine", return_value="aarch64"):
            torch = gpu()
            self.assertEqual(validate_device(torch, "dgx-spark"), "NVIDIA GB10")
            torch.cuda.get_device_properties.assert_not_called()  # UMA is not discrete-card VRAM.
            for invalid in (gpu(capability=(12,0)), gpu(name="NVIDIA RTX PRO 4500 Blackwell"), gpu(cuda="12.9")):
                with self.assertRaises(WorkerError):
                    validate_device(invalid, "dgx-spark")
        with patch("workers.qwen.runtime.platform.system", return_value="Linux"), \
                patch("workers.qwen.runtime.platform.machine", return_value="x86_64"), self.assertRaises(WorkerError):
            validate_device(gpu(), "dgx-spark")

    def test_runpod_device_guard_is_not_relaxed_for_spark(self):
        with self.assertRaises(WorkerError):
            validate_device(gpu(), "runpod")
        self.assertEqual(validate_device(gpu("NVIDIA RTX PRO 4500 Blackwell", (12,0), "12.9"), "runpod"),
                         "NVIDIA RTX PRO 4500 Blackwell")

    def test_runpod_accepts_sm120_without_model_name_allowlist(self):
        for name in ("NVIDIA GeForce RTX 5090", "NVIDIA RTX PRO 6000 Blackwell"):
            self.assertEqual(validate_device(gpu(name, (12, 0), "12.9"), "runpod"), name)

    def test_runpod_still_rejects_wrong_architecture_memory_and_cuda(self):
        for capability in ((8, 6), (8, 9), (9, 0), (10, 0), (12, 1)):
            with self.assertRaises(WorkerError):
                validate_device(gpu("Other GPU", capability, "12.9"), "runpod")
        low_memory = gpu("NVIDIA GeForce RTX 5090", (12, 0), "12.9")
        low_memory.cuda.get_device_properties.return_value.total_memory = 24*1024**3
        for invalid in (low_memory, gpu("NVIDIA GeForce RTX 5090", (12, 0), "13.0")):
            with self.assertRaises(WorkerError):
                validate_device(invalid, "runpod")

    def test_spark_memory_settings_cap_kv_and_preserve_model_precision(self):
        settings = engine_settings({"profile":"dgx-spark", "context_tokens":4096})
        self.assertEqual(settings["kv_cache_memory_bytes"], 1024**3)
        self.assertEqual(settings["max_num_seqs"], 1)
        self.assertEqual(settings["max_model_len"], 4096)
        self.assertEqual(settings["max_num_batched_tokens"], 4096)
        self.assertEqual(settings["attention_backend"], "FLASHINFER")
        self.assertEqual(settings["kv_cache_dtype"], "bfloat16")
        self.assertTrue(settings["enforce_eager"])
        original = engine_settings({"context_tokens":4096})
        self.assertEqual(original["gpu_memory_utilization"], 0.85)
        self.assertNotIn("kv_cache_memory_bytes", original)

    def test_native_nvfp4_kernel_cannot_be_overridden_by_emulation_flags(self):
        for flag in ("VLLM_BATCH_INVARIANT", "VLLM_USE_FBGEMM", "VLLM_USE_NVFP4_CT_EMULATIONS"):
            with patch.dict(os.environ, {flag:"1"}, clear=True), self.assertRaises(WorkerError):
                configure_backend("dgx-spark")
        with patch.dict(os.environ, {}, clear=True):
            configure_backend("dgx-spark")
            self.assertNotIn("VLLM_NVFP4_GEMM_BACKEND", os.environ)

    def test_vendor_build_lock_captures_full_suffix_without_installing_packages(self):
        versions = {key: value + "+vendor" for key, value in DGX_EXPECTED.items()}
        with tempfile.TemporaryDirectory() as folder, patch("workers.qwen.runtime.importlib.metadata.version", side_effect=versions.get):
            path = Path(folder)/"lock.json"
            write_dgx_runtime_lock(path)
            self.assertEqual(json.loads(path.read_text()), versions)
            self.assertNotIn("runpod", package_versions("dgx-spark"))
        with patch("workers.qwen.runtime.importlib.metadata.version", return_value="unreviewed"), self.assertRaises(WorkerError):
            package_versions("dgx-spark")

    def test_missing_or_changed_vendor_build_lock_fails_before_model_download(self):
        with tempfile.TemporaryDirectory() as folder:
            for locked in (None, {**DGX_EXPECTED, "torch": "2.12.0a0+different-vendor-build"}):
                lock = Path(folder)/"lock.json"
                if locked is not None:
                    lock.write_text(json.dumps(locked))
                connection = Mock()
                with patch("workers.qwen.runtime.os.setsid"), \
                    patch("workers.qwen.runtime.validate_cached_snapshot"), \
                        patch("workers.qwen.runtime.importlib.metadata.version", side_effect=DGX_EXPECTED.get), \
                        patch("workers.qwen.runtime.download_snapshot") as download:
                    engine_process(connection,{"profile":"dgx-spark","runtime_lock":str(lock)})
                download.assert_not_called()
                self.assertEqual(connection.send.call_args.args[0]["error"]["code"], "UNSUPPORTED_RUNTIME")
                self.assertNotIn("ready", connection.send.call_args.args[0])

    def test_local_factory_passes_gateway_cancellation_event_and_closes_runtime(self):
        runtime = Mock(ready=True)
        runtime.metadata = {"id":MODEL_ID, "revision":MODEL_REVISION, "precision":"NVFP4", "runtime":"synthetic-test-only"}
        worker = LocalQwenWorker(runtime, 240)
        event = threading.Event(); event.set()
        output = worker.handle({"input":request()}, cancelled=event)
        self.assertEqual(output["error"]["code"], "CANCELLED")
        self.assertEqual(output["counters"]["inference_attempts"], 0)
        runtime.generate.assert_not_called()
        worker.close(); runtime.close.assert_called_once()

    def test_local_factory_loads_once_and_does_not_invoke_sdk(self):
        runtime = Mock(config={"profile":"dgx-spark"}, ready=True)
        with patch("workers.qwen.local_factory.ProcessRuntime.from_env", return_value=runtime), \
                patch.dict(os.environ, {"EXECUTION_TIMEOUT_SECONDS":"240"}):
            worker = create_worker()
        self.assertIsInstance(worker, LocalQwenWorker)
        runtime.start.assert_called_once()

    def test_local_factory_failure_cleans_up_without_retry(self):
        runtime = Mock(config={"profile":"dgx-spark"})
        runtime.start.side_effect = WorkerError("UNSUPPORTED_RUNTIME", "Kernel incompatible")
        with patch("workers.qwen.local_factory.ProcessRuntime.from_env", return_value=runtime), self.assertRaises(WorkerError):
            create_worker()
        runtime.start.assert_called_once(); runtime.close.assert_called_once()

    def test_spark_adapter_wiring_uses_locked_vendor_stack_and_real_handler_contract(self):
        tokenizer = Mock(chat_template="enable_thinking")
        tokenizer.apply_chat_template.side_effect = lambda *args, return_dict=True, **kwargs: (
            {"input_ids": [11,12,13]} if return_dict else [11,12,13])
        completion = types.SimpleNamespace(text="Editorial garden illustration.", finish_reason="stop", token_ids=[21,22])
        generated = types.SimpleNamespace(prompt_token_ids=[11,12,13], outputs=[completion])
        engine = Mock(); engine.get_tokenizer.return_value = tokenizer; engine.generate.return_value = [generated]
        llm, sampling, torch = Mock(return_value=engine), Mock(), gpu()
        connection = Mock(); connection.recv.side_effect = [("prepare",request()), ("generate",[11,12,13]), EOFError()]
        with tempfile.TemporaryDirectory() as folder:
            Path(folder,"hf_quant_config.json").write_text('{"quantization":{"quant_algo":"NVFP4"}}')
            lock = Path(folder,"lock.json"); lock.write_text(json.dumps(DGX_EXPECTED))
            modules = {"torch":torch, "vllm":types.SimpleNamespace(LLM=llm, SamplingParams=sampling),
                       "huggingface_hub":types.SimpleNamespace(snapshot_download=Mock(return_value=folder))}
            with patch.dict(sys.modules,modules), patch.dict(os.environ,{},clear=True), \
                    patch("workers.qwen.runtime.platform.system",return_value="Linux"), \
                    patch("workers.qwen.runtime.platform.machine",return_value="aarch64"), \
                    patch("workers.qwen.runtime.os.setsid"), \
                    patch("workers.qwen.runtime.validate_cached_snapshot"), \
                    patch("workers.qwen.runtime.importlib.metadata.version",side_effect=DGX_EXPECTED.get):
                engine_process(connection,{"profile":"dgx-spark","runtime_lock":str(lock),"revision":MODEL_REVISION,
                                           "cache":folder,"startup_seconds":1,"context_tokens":4096})
        self.assertEqual(llm.call_args.kwargs["kv_cache_memory_bytes"], DGX_KV_BYTES)
        self.assertEqual(llm.call_args.kwargs["quantization"], "modelopt_fp4")
        self.assertEqual(llm.call_args.kwargs["attention_backend"], "FLASHINFER")
        self.assertEqual(engine.generate.call_count, 2)
        self.assertFalse(tokenizer.apply_chat_template.call_args.kwargs["enable_thinking"])
        self.assertEqual(sampling.call_args.kwargs["max_tokens"], 256)
        packets = [call.args[0] for call in connection.send.call_args_list]
        self.assertIn("profile=dgx-spark", packets[0]["ready"]["runtime"])
        self.assertEqual(packets[-1]["result"]["output_tokens"], 2)


if __name__ == "__main__":
    unittest.main()
