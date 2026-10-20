"""Actual pinned SDK run_job code, isolated from credentials and network helpers."""
import asyncio
import importlib.metadata
import importlib.util
import inspect
from pathlib import Path
import sys
import threading
import time
import types
from unittest import mock

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import flux_worker as fw


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


handlers = load_file("pulsefeed_flux_handler_under_test", Path(__file__).resolve().parents[1] / "handler.py")


@pytest.fixture
def sdk():
    try:
        distribution = importlib.metadata.distribution("runpod")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("Install runpod==1.12.0 --no-deps for the isolated SDK roundtrip tests")
    assert distribution.version == "1.12.0"
    sdk_root = distribution.locate_file("runpod")
    # Do not import runpod.__init__: it reads the operator's ~/.runpod credentials.
    # These are the real unmodified logger/version/job files from the installed wheel.
    logger = load_file("flux_sdk_logger", sdk_root / "serverless/modules/rp_logger.py")
    logger.RunPodLogger().set_level("INFO")
    version = load_file("flux_sdk_version", sdk_root / "version.py")

    def module(name, **attributes):
        value = types.ModuleType(name)
        value.__dict__.update(attributes)
        return value

    modules = {name: module(name, __path__=[]) for name in (
        "runpod", "runpod.serverless", "runpod.serverless.modules", "runpod.serverless.utils")}
    modules.update({
        "aiohttp": module("aiohttp"),
        "runpod.version": version,
        "runpod.http_client": module("runpod.http_client", ClientSession=object, TooManyRequests=RuntimeError),
        "runpod.serverless.modules.rp_logger": logger,
        "runpod.serverless.modules.rp_handler": module("rp_handler", is_generator=inspect.isgeneratorfunction),
        "runpod.serverless.modules.rp_http": module("rp_http", send_result=mock.AsyncMock(), stream_result=mock.AsyncMock()),
        "runpod.serverless.modules.rp_tips": module("rp_tips", check_return_size=mock.Mock()),
        "runpod.serverless.modules.worker_state": module("worker_state", WORKER_ID="synthetic-worker", REF_COUNT_ZERO=0, JobsProgress=mock.Mock),
    })
    modules["runpod.serverless.utils"].rp_debugger = types.SimpleNamespace(
        clear_debugger_output=mock.Mock(), get_debugger_output=mock.Mock())
    with mock.patch.dict(sys.modules, modules), mock.patch.dict("os.environ", {
        "RUNPOD_WEBHOOK_GET_JOB": "https://synthetic.invalid/job-take/$ID",
        "RUNPOD_POD_HOSTNAME": "synthetic", "RUNPOD_POD_ID": "synthetic",
    }):
        job_module = load_file("runpod.serverless.modules.rp_job", sdk_root / "serverless/modules/rp_job.py")
    return job_module


def job():
    return {"id": "synthetic-sdk-job", "input": {
        "schema_version": 1, "stage": "flux",
        "run_id": "a876b553-0a9b-4b11-b8e7-d0084f70533f",
        "invocation_id": "0b0923e9-3bdf-4009-a5e0-3e7ca83cb679",
        "deadline_unix_ms": int((time.time() + 60) * 1000),
        "prompt": "A synthetic SDK test illustration.", "seed": 42,
        "width": 1024, "height": 1024, "num_inference_steps": 4,
        "guidance_scale": 1.0, "output_format": "webp",
    }}


class Engine:
    metadata = {"id": fw.MODEL_ID, "revision": "synthetic-sdk-test",
                "precision": "synthetic", "runtime": "test-double"}
    def validate_prompt(self, _prompt):
        pass
    def generate(self, data, deadline):
        return [Image.new("RGB", (1024, 1024), "purple")]
    def cleanup(self):
        pass


@pytest.mark.parametrize("engine", [Engine(), None])
def test_actual_sdk_preserves_nested_success_or_failure(sdk, engine, capsys):
    worker = fw.Worker(engine)
    with mock.patch.object(handlers, "_worker", worker), mock.patch.object(handlers, "_install_shutdown_handlers"):
        result = asyncio.run(sdk.run_job(handlers.handler, job()))
    assert set(result) == ({"output"} if engine is not None else {"output", "stopPod"})
    envelope = result["output"]["result"]
    assert envelope["ok"] == (engine is not None)
    assert "error" in envelope
    if engine is not None:
        assert envelope["error"] is None
        assert envelope["data"]["image_base64"][:30] not in capsys.readouterr().out
    else:
        assert result["stopPod"] is True
        assert envelope["error"]["code"] == "MODEL_NOT_READY"


@pytest.mark.parametrize("code", ["UNSUPPORTED_RUNTIME", "MODEL_NOT_READY", "MODEL_ACCESS_DENIED"])
def test_actual_sdk_preserves_fatal_result_and_recycles_without_reusing_engine(sdk, code):
    class FatalEngine(Engine):
        def __init__(self):
            self.calls = 0
        def generate(self, data, deadline):
            self.calls += 1
            raise fw.WorkerFailure(code, "Resident engine unavailable.")
    engine = FatalEngine()
    worker = fw.Worker(engine)
    with mock.patch.object(handlers, "_worker", worker), mock.patch.object(handlers, "_install_shutdown_handlers"):
        first = asyncio.run(sdk.run_job(handlers.handler, job()))
        second = asyncio.run(sdk.run_job(handlers.handler, job()))
    assert first["stopPod"] is True and second["stopPod"] is True
    assert set(first["output"]) == {"result"}
    envelope = first["output"]["result"]
    assert envelope["error"]["code"] == code
    assert envelope["counters"]["inference_attempts"] == 1
    assert envelope["model"] == engine.metadata
    assert second["output"]["result"]["error"]["code"] == "MODEL_NOT_READY"
    assert second["output"]["result"]["counters"]["inference_attempts"] == 0
    assert not worker.ready and engine.calls == 1


def test_actual_sdk_unknown_native_outcome_exits_before_delivery_and_gpu_cleanup(sdk):
    class ProcessExit(BaseException):
        pass
    class UncertainEngine(Engine):
        def generate(self, data, deadline):
            raise TimeoutError("Synthetic native completion uncertainty")
    engine = UncertainEngine()
    engine.cleanup = mock.Mock()
    worker = fw.Worker(engine)
    hard_exit = mock.Mock(side_effect=ProcessExit)
    with mock.patch.object(handlers, "_worker", worker), mock.patch.object(handlers, "_install_shutdown_handlers"), mock.patch.object(handlers, "_hard_exit", hard_exit):
        with pytest.raises(ProcessExit):
            asyncio.run(sdk.run_job(handlers.handler, job()))
    hard_exit.assert_called_once_with(1)
    engine.cleanup.assert_not_called()
    assert not worker.ready
    rejected = worker.handle(job())
    assert rejected["error"]["code"] == "MODEL_NOT_READY"
    assert rejected["counters"]["inference_attempts"] == 0


def test_actual_sdk_cancellation_reaches_thread_and_cleans_up(sdk):
    started, cleaned = threading.Event(), threading.Event()
    class BlockingEngine(Engine):
        def generate(self, data, deadline):
            started.set()
            while not deadline.cancelled.is_set():
                time.sleep(0.001)
            deadline.check()
        def cleanup(self):
            cleaned.set()
    worker = fw.Worker(BlockingEngine())

    async def exercise():
        task = asyncio.create_task(sdk.run_job(handlers.handler, job()))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()
        assert not worker.lock.locked()

    with mock.patch.object(handlers, "_worker", worker), mock.patch.object(handlers, "_install_shutdown_handlers"):
        asyncio.run(exercise())


def test_actual_sdk_cancellation_with_unknown_native_outcome_forces_exit(sdk):
    class ProcessExit(BaseException):
        pass
    started = threading.Event()
    class UncertainAfterCancellation(Engine):
        def generate(self, data, deadline):
            started.set()
            while not deadline.cancelled.is_set():
                time.sleep(0.001)
            raise TimeoutError("Synthetic native completion uncertainty after cancellation")
    engine = UncertainAfterCancellation()
    engine.cleanup = mock.Mock()
    worker = fw.Worker(engine)
    hard_exit = mock.Mock(side_effect=ProcessExit)

    async def exercise():
        task = asyncio.create_task(sdk.run_job(handlers.handler, job()))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(ProcessExit):
            await task

    with mock.patch.object(handlers, "_worker", worker), mock.patch.object(handlers, "_install_shutdown_handlers"), mock.patch.object(handlers, "_hard_exit", hard_exit):
        asyncio.run(exercise())
    hard_exit.assert_called_once_with(1)
    engine.cleanup.assert_not_called()
    assert not worker.ready


def test_shutdown_handlers_reinstalled_after_sdk_startup_and_bounded():
    loop = mock.Mock()
    active = mock.Mock()
    event = threading.Event()
    with mock.patch.object(handlers, "_signal_loop", None), mock.patch.object(handlers, "_cancelled", event), mock.patch.object(handlers, "_active_handlers", {active}):
        handlers._install_shutdown_handlers(loop)
        assert loop.add_signal_handler.call_count == 2
        callback = loop.add_signal_handler.call_args_list[0].args[1]
        callback()
        assert event.is_set()
        active.cancel.assert_called_once()
        assert loop.call_later.call_args.args[0] == handlers.CANCELLATION_GRACE_SECONDS


def test_uncooperative_cancellation_forces_worker_exit_and_prevents_overlap(sdk):
    started, release, cleaned = threading.Event(), threading.Event(), threading.Event()
    class UncooperativeEngine(Engine):
        def generate(self, data, deadline):
            started.set()
            assert release.wait(3), "Test teardown must release the synthetic native call"
            deadline.check()
        def cleanup(self):
            cleaned.set()
    worker = fw.Worker(UncooperativeEngine())
    hard_exit = mock.Mock()

    async def exercise():
        task = asyncio.create_task(sdk.run_job(handlers.handler, job()))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            hard_exit.assert_called_once_with(1)
            assert worker.lock.locked()
            overlap = worker.handle(job())
            assert overlap["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
            assert overlap["counters"]["inference_attempts"] == 0
        finally:
            release.set()
            assert await asyncio.to_thread(cleaned.wait, 2)
        assert not worker.lock.locked()

    with mock.patch.object(handlers, "_worker", worker), mock.patch.object(handlers, "_install_shutdown_handlers"), mock.patch.object(handlers, "CANCELLATION_GRACE_SECONDS", 0.01), mock.patch.object(handlers, "_hard_exit", hard_exit):
        asyncio.run(exercise())
