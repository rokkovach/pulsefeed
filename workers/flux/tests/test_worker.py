"""CPU evidence: synthetic images and adapters, never live-model evidence."""
import base64
import io
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from PIL import Image

# Also works in the worker's isolated Docker context, without root packages.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import flux_worker as fw


def request(**changes):
    data = {"schema_version": 1, "stage": "flux",
            "run_id": "a876b553-0a9b-4b11-b8e7-d0084f70533f",
            "invocation_id": "0b0923e9-3bdf-4009-a5e0-3e7ca83cb679",
            "deadline_unix_ms": int((time.time() + 600) * 1000),
            "prompt": "An editorial illustration of a train in an alpine valley.",
            "seed": 42, "width": 1024, "height": 1024,
            "num_inference_steps": 4, "guidance_scale": 1.0, "output_format": "webp"}
    data.update(changes)
    return {"input": data}


class FakeEngine:
    metadata = {"id": fw.MODEL_ID, "revision": "synthetic-cpu-fixture",
                "precision": "synthetic", "runtime": "test-double"}

    def __init__(self, failures=(), image_size=(1024, 1024)):
        self.failures, self.image_size = list(failures), image_size
        self.calls, self.cleanups = [], 0

    def validate_prompt(self, prompt):
        pass

    def generate(self, data, deadline):
        self.calls.append(dict(data))
        if self.failures:
            raise self.failures.pop(0)
        return [Image.new("RGB", self.image_size, "#b18fd1")]

    def cleanup(self):
        self.cleanups += 1


@pytest.mark.parametrize("image_size,expected_code", [((1024, 1024), None), ((16, 16), "INVALID_MODEL_OUTPUT")])
def test_cleanup_failure_preserves_outcome_and_invalidates_engine(image_size, expected_code, caplog):
    engine = FakeEngine(image_size=image_size)
    engine.cleanup = mock.Mock(side_effect=RuntimeError("private prompt or credential"))
    worker = fw.Worker(engine)
    result = worker.handle(request())
    assert result["ok"] is (expected_code is None)
    assert (result["error"] or {}).get("code") == expected_code
    assert result["usage"]["image_count"] == (1 if expected_code is None else 0)
    assert result["counters"]["inference_attempts"] == 1
    assert not worker.ready
    assert "exception_type=RuntimeError" in caplog.text
    assert "private prompt or credential" not in caplog.text
    next_result = worker.handle(request())
    assert next_result["error"]["code"] == "MODEL_NOT_READY"
    assert next_result["counters"]["inference_attempts"] == 0
    assert len(engine.calls) == 1
    engine.cleanup.assert_called_once()


def test_cleanup_failure_rejects_job_that_passed_earlier_readiness_check():
    waiting, resume = threading.Event(), threading.Event()
    actual_lock = threading.Lock()

    class AdmissionLock:
        def acquire(self, **kwargs):
            if threading.current_thread().name == "stale-ready":
                waiting.set()
                assert resume.wait(5)
            return actual_lock.acquire(**kwargs)

        def release(self):
            actual_lock.release()

    engine = FakeEngine()
    engine.cleanup = mock.Mock(side_effect=RuntimeError("cleanup failed"))
    worker = fw.Worker(engine)
    worker.lock = AdmissionLock()
    results = []
    stale = threading.Thread(name="stale-ready", target=lambda: results.append(worker.handle(request())))
    stale.start()
    try:
        assert waiting.wait(5)
        known_result = worker.handle(request())
        assert known_result["ok"]
    finally:
        resume.set()
        stale.join(5)
    assert not stale.is_alive()
    assert len(results) == 1
    assert results[0]["error"]["code"] == "MODEL_NOT_READY"
    assert results[0]["counters"]["inference_attempts"] == 0
    assert len(engine.calls) == 1
    engine.cleanup.assert_called_once()


@pytest.mark.parametrize("fmt,expected", [("webp", "WEBP"), ("jpeg", "JPEG")])
def test_success_has_valid_image_and_exact_contract(fmt, expected):
    engine = FakeEngine()
    data = request(output_format=fmt)
    result = fw.Worker(engine).handle(data)
    assert result["ok"] and result["error"] is None
    assert set(result) == {"schema_version", "stage", "run_id", "invocation_id", "ok", "model", "data", "usage", "timings", "counters", "error"}
    raw = base64.b64decode(result["data"]["image_base64"], validate=True)
    assert len(raw) == result["data"]["image_bytes"] <= fw.MAX_IMAGE_BYTES
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        assert image.format == expected and image.size == (1024, 1024)
    assert result["data"]["mime_type"] == f"image/{fmt}"
    assert result["data"]["seed"] == 42
    assert result["usage"] == {"image_count": 1}
    assert result["counters"] == {"inference_attempts": 1, "inference_retries": 0, "dependency_retries": 0}
    assert engine.calls[0]["prompt"] == data["input"]["prompt"]
    assert len(json.dumps(result).encode()) <= fw.MAX_ENVELOPE_BYTES
    assert engine.cleanups == 1


@pytest.mark.parametrize("changes,code", [
    ({"schema_version": 2}, "UNSUPPORTED_SCHEMA"),
    ({"schema_version": True}, "UNSUPPORTED_SCHEMA"),
    ({"stage": "qwen"}, "INVALID_INPUT"),
    ({"run_id": "bad"}, "INVALID_INPUT"),
    ({"seed": True}, "INVALID_INPUT"),
    ({"seed": -1}, "INVALID_INPUT"),
    ({"seed": 4294967296}, "INVALID_INPUT"),
    ({"width": 512}, "INVALID_INPUT"),
    ({"height": 1024.0}, "INVALID_INPUT"),
    ({"num_inference_steps": 8}, "INVALID_INPUT"),
    ({"guidance_scale": True}, "INVALID_INPUT"),
    ({"guidance_scale": float("nan")}, "INVALID_INPUT"),
    ({"guidance_scale": 10 ** 400}, "INVALID_INPUT"),
    ({"guidance_scale": 2}, "INVALID_INPUT"),
    ({"output_format": "png"}, "INVALID_INPUT"),
    ({"prompt": " "}, "INVALID_INPUT"),
    ({"prompt": "\ud800"}, "INVALID_INPUT"),
    ({"prompt": "x" * 4001}, "INPUT_TOO_LARGE"),
    ({"deadline_unix_ms": 1}, "DEADLINE_EXCEEDED"),
    ({"deadline_unix_ms": True}, "INVALID_INPUT"),
    ({"deadline_unix_ms": 10 ** 400}, "INVALID_INPUT"),
    ({"deadline_unix_ms": 9007199254740992}, "INVALID_INPUT"),
    ({"model": "remote"}, "INVALID_INPUT"),
    ({"image_url": "https://example.com/x"}, "INVALID_INPUT"),
])
def test_rejected_input_never_infers(changes, code):
    engine = FakeEngine()
    result = fw.Worker(engine).handle(request(**changes))
    assert result["error"]["code"] == code
    assert not result["ok"] and result["data"] is None
    assert result["counters"]["inference_attempts"] == 0
    assert result["timings"]["inference_ms"] == 0
    assert not engine.calls


@pytest.mark.parametrize("job", [None, [], {}, {"input": None}, {"input": []}])
def test_malformed_job_has_null_correlation(job):
    result = fw.Worker(None).handle(job)
    assert result["error"]["code"] == "INVALID_INPUT"
    assert result["run_id"] is result["invocation_id"] is None
    assert result["model"]["revision"] is None


def test_model_not_ready_is_zero_attempts():
    result = fw.Worker(None).handle(request())
    assert result["error"]["code"] == "MODEL_NOT_READY"
    assert result["counters"]["inference_attempts"] == 0


def test_dependency_failure_is_not_replayed():
    engine = FakeEngine([fw.WorkerFailure("DEPENDENCY_UNAVAILABLE", "Unavailable", True)])
    result = fw.Worker(engine).handle(request())
    assert not result["ok"]
    assert result["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert result["counters"]["inference_attempts"] == 1
    assert result["counters"]["inference_retries"] == 0
    assert len(engine.calls) == 1


@pytest.mark.parametrize("failure,code", [
    (RuntimeError("CUDA out of memory secret-internal-detail"), "GPU_OOM"),
    (RuntimeError("no kernel image available"), "UNSUPPORTED_RUNTIME"),
    (RuntimeError("permanent secret-internal-detail"), "INFERENCE_FAILED"),
    (TimeoutError("remote acceptance unknown"), "INFERENCE_OUTCOME_UNKNOWN"),
    (fw.WorkerFailure("CANCELLED", "Cancelled."), "CANCELLED"),
])
def test_permanent_and_uncertain_failures_do_not_retry(failure, code):
    engine = FakeEngine([failure])
    result = fw.Worker(engine).handle(request())
    assert result["error"]["code"] == code
    assert result["counters"]["inference_attempts"] == 1
    assert result["counters"]["inference_retries"] == 0
    assert "secret-internal-detail" not in json.dumps(result)


def test_wrong_dimensions_fail_without_retry():
    result = fw.Worker(FakeEngine(image_size=(32, 32))).handle(request())
    assert result["error"]["code"] == "INVALID_MODEL_OUTPUT"
    assert result["usage"]["image_count"] == 0
    assert result["counters"]["inference_attempts"] == 1


def test_multiple_images_fail_and_close():
    engine = FakeEngine()
    images = [Image.new("RGB", (1024, 1024)), Image.new("RGB", (1024, 1024))]
    engine.generate = lambda *_: images
    result = fw.Worker(engine).handle(request())
    assert result["error"]["code"] == "INVALID_MODEL_OUTPUT"
    for image in images:
        with pytest.raises(ValueError):
            image.getpixel((0, 0))


def test_compression_exhaustion_has_finite_passes_and_no_regeneration():
    engine = FakeEngine()
    original = fw.encode_image
    with mock.patch.object(fw, "encode_image", side_effect=lambda i, f, d, **kw: original(i, f, d, byte_limit=1, **kw)):
        result = fw.Worker(engine).handle(request())
    assert result["error"]["code"] == "OUTPUT_TOO_LARGE"
    assert len(engine.calls) == 1


def test_encoding_passes_follow_quality_sequence():
    deadline = fw.Deadline(int((time.time() + 60) * 1000), 60)
    original = Image.Image.save
    calls = []
    def save(image, *args, **kwargs):
        calls.append(kwargs["quality"])
        return original(image, *args, **kwargs)
    with Image.new("RGB", (1024, 1024)) as image, mock.patch.object(Image.Image, "save", save):
        with pytest.raises(fw.WorkerFailure) as caught:
            fw.encode_image(image, "webp", deadline, byte_limit=1)
    assert caught.value.code == "OUTPUT_TOO_LARGE"
    assert calls == list(fw.QUALITIES)


def test_envelope_size_limit_never_regenerates():
    engine = FakeEngine()
    with mock.patch.object(fw, "MAX_ENVELOPE_BYTES", 10):
        result = fw.Worker(engine).handle(request())
    assert result["error"]["code"] == "OUTPUT_TOO_LARGE"
    assert result["data"] is None and len(engine.calls) == 1


def test_cancelled_before_inference():
    cancelled = threading.Event()
    cancelled.set()
    result = fw.Worker(FakeEngine(), cancelled=cancelled).handle(request())
    assert result["error"]["code"] == "CANCELLED"
    assert result["counters"]["inference_attempts"] == 0


def test_expiry_after_inference_discards_image_without_retry():
    engine = FakeEngine()
    wall = [1000.0]
    original = engine.generate
    def generate(data, deadline):
        image = original(data, deadline)
        wall[0] = 1001.0
        return image
    engine.generate = generate
    result = fw.Worker(engine, wall=lambda: wall[0]).handle(request(deadline_unix_ms=1000500))
    assert result["error"]["code"] == "DEADLINE_EXCEEDED"
    assert result["counters"]["inference_attempts"] == 1


def test_encoding_is_excluded_from_inference_time():
    now = [1000.0]
    engine = FakeEngine()
    original = engine.generate
    def generate(*args):
        now[0] += 0.1
        return original(*args)
    engine.generate = generate
    result = fw.Worker(engine, wall=lambda: now[0], monotonic=lambda: now[0]).handle(
        request(deadline_unix_ms=1100000))
    assert result["ok"]
    assert result["timings"]["inference_ms"] == pytest.approx(100)


def test_concurrency_guard_rejects_overlap():
    engine = FakeEngine()
    worker = fw.Worker(engine)
    worker.lock.acquire()
    try:
        result = worker.handle(request())
    finally:
        worker.lock.release()
    assert result["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert result["counters"]["inference_attempts"] == 0
    assert not engine.calls


def test_tokenizer_limit_is_checked_without_truncating_prompt():
    engine = object.__new__(fw.DiffusersEngine)
    tokenizer = mock.Mock()
    tokenizer.apply_chat_template.return_value = "rendered exact prompt"
    tokenizer.return_value = {"input_ids": list(range(513))}
    engine.pipeline = SimpleNamespace(tokenizer=tokenizer)
    with pytest.raises(fw.WorkerFailure) as error:
        engine.validate_prompt("final exact prompt")
    assert error.value.code == "INPUT_TOO_LARGE"
    assert tokenizer.call_args.kwargs["truncation"] is False
    assert tokenizer.apply_chat_template.call_args.kwargs["enable_thinking"] is False


def test_live_adapter_passes_fixed_settings_seed_and_deadline_callback():
    engine = object.__new__(fw.DiffusersEngine)
    image = Image.new("RGB", (1024, 1024))
    pipeline = mock.Mock(return_value=SimpleNamespace(images=[image]))
    torch = mock.Mock()
    torch.inference_mode.return_value = mock.MagicMock()
    engine.pipeline, engine.torch = pipeline, torch
    deadline = fw.Deadline(int((time.time() + 60) * 1000), 60)
    try:
        assert engine.generate(request()["input"], deadline) == [image]
        kwargs = pipeline.call_args.kwargs
        assert kwargs["width"] == kwargs["height"] == 1024
        assert kwargs["num_inference_steps"] == 4 and kwargs["guidance_scale"] == 1.0
        assert kwargs["num_images_per_prompt"] == 1 and kwargs["output_type"] == "pil"
        torch.Generator.assert_called_once_with(device="cuda")
        torch.Generator.return_value.manual_seed.assert_called_once_with(42)
        torch.cuda.synchronize.assert_called_once()
        callback_kwargs = {"latents": "synthetic"}
        assert kwargs["callback_on_step_end"](None, 0, None, callback_kwargs) is callback_kwargs
    finally:
        image.close()


@pytest.mark.parametrize("failure,expected,attempts", [
    (ConnectionError("secret URL"), "DEPENDENCY_UNAVAILABLE", 3),
    (ValueError("invalid config secret"), "MODEL_NOT_READY", 1),
])
def test_startup_retry_cap_and_permanent_rejection(failure, expected, attempts):
    download = mock.Mock(side_effect=failure)
    deadline = fw.Deadline(int((time.time() + 60) * 1000), 60)
    with pytest.raises(fw.WorkerFailure) as error:
        fw.download_with_retries(download, deadline, sleep=lambda _: None, jitter=lambda: 0)
    assert error.value.code == expected
    assert download.call_count == attempts
    assert "secret" not in error.value.message


def test_startup_recovers_on_third_attempt():
    download = mock.Mock(side_effect=[ConnectionError(), ConnectionError(), "/synthetic/snapshot"])
    deadline = fw.Deadline(int((time.time() + 60) * 1000), 60)
    assert fw.download_with_retries(download, deadline, sleep=lambda _: None) == "/synthetic/snapshot"
    assert download.call_count == 3


def test_access_denied_never_startup_retries():
    error = RuntimeError("secret denied URL")
    error.response = SimpleNamespace(status_code=403)
    download = mock.Mock(side_effect=error)
    deadline = fw.Deadline(int((time.time() + 60) * 1000), 60)
    with pytest.raises(fw.WorkerFailure) as caught:
        fw.download_with_retries(download, deadline)
    assert caught.value.code == "MODEL_ACCESS_DENIED"
    assert download.call_count == 1


def test_environment_requires_pinned_revision_and_finite_timeouts():
    with mock.patch.dict("os.environ", {"MODEL_REVISION": "main"}):
        with pytest.raises(fw.WorkerFailure):
            fw.RuntimeConfig.from_env()
    with mock.patch.dict("os.environ", {"FLUX_EXECUTION_TIMEOUT_SECONDS": "601"}):
        with pytest.raises(fw.WorkerFailure):
            fw.RuntimeConfig.from_env()


def test_actual_worker_output_against_shared_schema_if_checkout_available():
    # The isolated Docker target has no root contracts; root integration owns them.
    contracts = next((parent / "contracts" for parent in Path(__file__).resolve().parents
                      if (parent / "contracts" / "flux-output.schema.json").is_file()), None)
    if contracts is None:
        pytest.skip("Shared contracts are not in this isolated Docker context")
    from jsonschema import Draft202012Validator, FormatChecker
    schema = json.loads((contracts / "flux-output.schema.json").read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for result in (fw.Worker(FakeEngine()).handle(request()), fw.Worker(None).handle(None)):
        validator.validate(result)


@pytest.mark.parametrize("size", [256, 512, 1024])
def test_supported_square_presets_return_exact_decoded_dimensions(size):
    result = fw.Worker(FakeEngine(image_size=(size, size))).handle(request(width=size, height=size))
    assert result["ok"]
    assert result["data"]["width"] == result["data"]["height"] == size
    with Image.open(io.BytesIO(base64.b64decode(result["data"]["image_base64"]))) as image:
        image.load()
        assert image.size == (size, size)


@pytest.mark.parametrize("width,height", [(200, 200), (512, 256), (2048, 2048), (True, True)])
def test_unsupported_presets_fail_before_inference(width, height):
    engine = FakeEngine()
    result = fw.Worker(engine).handle(request(width=width, height=height))
    assert result["error"]["code"] == "INVALID_INPUT"
    assert not engine.calls
