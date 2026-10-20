"""CPU-only profile/preflight evidence; no CUDA libraries or weights required."""
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import flux_worker as fw
import local_factory


def spark_evidence(**changes):
    value = fw.RuntimeEvidence(
        machine="aarch64", torch_version="2.13.0+cu129", cuda_version="12.9",
        diffusers_version="0.41.0", transformers_version="5.20.2", cuda_available=True,
        bf16_supported=True, compute_capability=(12, 1), ngc_release=None,
    )
    return replace(value, **changes)


def test_default_profile_remains_strict_runpod():
    with mock.patch.dict(os.environ, {}, clear=True):
        assert fw.RuntimeConfig.from_env().profile == "runpod"
    with pytest.raises(fw.WorkerFailure):
        fw.validate_runtime_profile("runpod", spark_evidence())
    fw.validate_runtime_profile("runpod", spark_evidence(
        machine="x86_64", torch_version="2.13.0+cu129", cuda_version="12.9",
        compute_capability=(12, 0), ngc_release=None,
    ))


def test_spark_accepts_exact_official_cuda_wheel_without_ngc_metadata():
    fw.validate_runtime_profile("dgx-spark", spark_evidence())


@pytest.mark.parametrize("changes", [
    {"machine": "x86_64"}, {"compute_capability": (12, 0)},
    {"compute_capability": (8, 7)}, {"cuda_available": False},
    {"bf16_supported": False}, {"cuda_version": "12.8"},
    {"cuda_version": "13.1"}, {"torch_version": "2.10.0"},
    {"torch_version": "2.13.0"}, {"torch_version": "2.13.0+cu130"}, {"diffusers_version": "0.36.0"},
    {"transformers_version": "4.55.0"},
])
def test_incompatible_spark_profile_rejected(changes):
    with pytest.raises(fw.WorkerFailure) as caught:
        fw.validate_runtime_profile("dgx-spark", spark_evidence(**changes))
    assert caught.value.code == "UNSUPPORTED_RUNTIME"


def test_profile_from_env_and_invalid_profile_rejection():
    with mock.patch.dict(os.environ, {"FLUX_RUNTIME_PROFILE": "dgx-spark"}):
        assert fw.RuntimeConfig.from_env().profile == "dgx-spark"
    with mock.patch.dict(os.environ, {"FLUX_RUNTIME_PROFILE": "automatic"}):
        with pytest.raises(fw.WorkerFailure):
            fw.RuntimeConfig.from_env()


def fake_torch():
    value = mock.Mock()
    value.__version__ = "2.13.0+cu129"
    value.version.cuda = "12.9"
    value.cuda.is_available.return_value = True
    value.cuda.is_bf16_supported.return_value = True
    value.cuda.get_device_capability.return_value = (12, 1)
    value.inference_mode.return_value = mock.MagicMock()
    return value


def test_preflight_probes_real_call_path_before_model_load_without_weights():
    torch = fake_torch()
    with mock.patch.object(fw.platform, "machine", return_value="aarch64"), mock.patch.dict(
        os.environ, {"NVIDIA_PYTORCH_VERSION": "25.11"}
    ), mock.patch.object(fw.features, "check", return_value=True):
        evidence = fw.preflight_runtime(fw.RuntimeConfig(profile="dgx-spark"), torch,
            SimpleNamespace(__version__="0.41.0"), SimpleNamespace(__version__="5.20.2"))
    assert evidence.compute_capability == (12, 1)
    torch.ones.assert_called_once_with((8, 8), device="cuda", dtype=torch.bfloat16)
    torch.mm.assert_called_once()
    torch.cuda.synchronize.assert_called_once()


def test_preflight_bad_profile_does_not_dispatch_kernel():
    torch = fake_torch()
    with mock.patch.object(fw.platform, "machine", return_value="x86_64"):
        with pytest.raises(fw.WorkerFailure):
            fw.preflight_runtime(fw.RuntimeConfig(profile="dgx-spark"), torch,
                SimpleNamespace(__version__="0.41.0"), SimpleNamespace(__version__="5.20.2"))
    torch.ones.assert_not_called()


def test_kernel_failure_is_permanent_before_download():
    torch = fake_torch()
    torch.mm.side_effect = RuntimeError("unsupported kernel sensitive detail")
    with pytest.raises(fw.WorkerFailure) as error:
        fw.probe_bf16_kernel(torch)
    assert error.value.code == "UNSUPPORTED_RUNTIME" and not error.value.retryable
    assert "sensitive" not in error.value.message


def test_factory_returns_ready_worker_without_sdk_and_preserves_bound():
    engine = mock.Mock()
    config = fw.RuntimeConfig(profile="dgx-spark", execution_seconds=123)
    with mock.patch.object(local_factory.RuntimeConfig, "from_env", return_value=config), mock.patch.object(
        local_factory, "DiffusersEngine", return_value=engine
    ) as create:
        worker = local_factory.create_worker()
    create.assert_called_once_with(config)
    assert worker.engine is engine and worker.execution_seconds == 123
    assert isinstance(worker, local_factory.LocalFluxWorker) and worker.ready is True


def readiness_job():
    return {"input": {"schema_version": 1, "stage": "flux",
        "run_id": str(uuid.uuid4()), "invocation_id": str(uuid.uuid4()),
        "deadline_unix_ms": int((time.time() + 60) * 1000),
        "prompt": "Synthetic readiness test.", "seed": 42, "width": 1024,
        "height": 1024, "num_inference_steps": 4, "guidance_scale": 1.0,
        "output_format": "webp"}}


def readiness_engine(error):
    engine = mock.Mock()
    engine.metadata = {"id": fw.MODEL_ID, "revision": "synthetic-readiness-test",
                       "precision": "synthetic", "runtime": "test-double"}
    engine.generate.side_effect = error
    return engine


def test_fatal_kernel_result_is_preserved_and_readiness_latches():
    engine = readiness_engine(RuntimeError("no kernel image is available"))
    worker = local_factory.LocalFluxWorker(engine)
    result = worker.handle(readiness_job())
    assert result["error"]["code"] == "UNSUPPORTED_RUNTIME"
    assert result["counters"] == {"inference_attempts": 1, "inference_retries": 0, "dependency_retries": 0}
    assert result["model"] == engine.metadata and result["data"] is None
    assert worker.ready is False
    rejected = worker.handle(readiness_job())
    assert rejected["error"]["code"] == "MODEL_NOT_READY"
    assert rejected["counters"]["inference_attempts"] == 0
    assert result["counters"]["inference_attempts"] == 1
    assert engine.generate.call_count == 1


@pytest.mark.parametrize("code", ["MODEL_NOT_READY", "MODEL_ACCESS_DENIED", "INFERENCE_OUTCOME_UNKNOWN"])
def test_fatal_engine_configuration_latches_unavailable(code):
    worker = local_factory.LocalFluxWorker(readiness_engine(fw.WorkerFailure(code, "Unavailable engine.")))
    assert worker.handle(readiness_job())["error"]["code"] == code
    assert worker.ready is False


@pytest.mark.parametrize("code", ["DEPENDENCY_UNAVAILABLE", "GPU_OOM", "CANCELLED",
                                 "DEADLINE_EXCEEDED", "INFERENCE_FAILED"])
def test_expected_invocation_failures_do_not_poison_readiness(code):
    worker = local_factory.LocalFluxWorker(readiness_engine(fw.WorkerFailure(code, "Expected invocation failure.")))
    assert worker.handle(readiness_job())["error"]["code"] == code
    assert worker.ready is True


def test_input_validation_and_nonblocking_close_do_not_call_gpu():
    engine = readiness_engine(RuntimeError("Must not be invoked"))
    worker = local_factory.LocalFluxWorker(engine)
    result = worker.handle({"input": {}})
    assert result["error"]["code"] == "INVALID_INPUT" and worker.ready is True
    worker.close()
    assert worker.ready is False
    engine.generate.assert_not_called()
    engine.cleanup.assert_not_called()


def test_actual_gateway_denies_new_jobs_after_fatal_kernel_but_retains_old_result():
    source = next((parent / "local/worker_gateway.py" for parent in Path(__file__).resolve().parents
                   if (parent / "local/worker_gateway.py").is_file()), None)
    if source is None:
        pytest.skip("Root-owned gateway is outside the isolated worker Docker context")
    spec = importlib.util.spec_from_file_location("flux_readiness_gateway_test", source)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {spec.name: module}):
        spec.loader.exec_module(module)
    engine = readiness_engine(RuntimeError("no kernel image is available"))
    worker = local_factory.LocalFluxWorker(engine)
    gateway = module.Gateway(worker, "synthetic-private-test-token-" + "x" * 32)
    try:
        accepted = gateway.submit(readiness_job())
        assert gateway.jobs[accepted["id"]].done.wait(2)
        prior = json.loads(gateway.status(accepted["id"]))
        assert prior["output"]["result"]["error"]["code"] == "UNSUPPORTED_RUNTIME"
        assert prior["output"]["result"]["counters"]["inference_attempts"] == 1
        with pytest.raises(module.Rejected) as health:
            gateway.health()
        assert health.value.status == 503
        with pytest.raises(module.Rejected) as admission:
            gateway.submit(readiness_job())
        assert admission.value.status == 422
        assert json.loads(gateway.status(accepted["id"])) == prior
        assert engine.generate.call_count == 1
    finally:
        gateway.close()


def test_spark_entrypoint_starts_shared_gateway_on_clean_python():
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile.dgx-spark"
    if not dockerfile.is_file():
        pytest.skip("Spark Dockerfile is outside the isolated worker CPU target")
    entrypoints = [json.loads(line.split(None, 1)[1])
                   for line in dockerfile.read_text().splitlines() if line.startswith("ENTRYPOINT ")]
    assert entrypoints == [["python", "-u", "/gateway/worker_gateway.py"]]


def test_spark_vendor_wheel_repair_is_hashed_and_kept_out_of_runtime_layers():
    directory = Path(__file__).resolve().parents[1]
    dockerfile = directory / "Dockerfile.dgx-spark"
    if not dockerfile.is_file():
        pytest.skip("Spark Dockerfile is outside the isolated worker CPU target")
    build = dockerfile.read_text()
    assert "ADD --checksum=sha256:5c72f727722f74762380e5f8755557c788b26d8fdcc49df1641c1b08e16d256c" in build
    assert "--mount=from=vendor-wheel,source=/vendor,target=/opt/vendor,ro" in build
    assert "nvidia-cusparselt-cu12==0.8.1+pulsefeed.arm64tag1 --hash=sha256:85a1d82bc5f9aeef8f02de7ee67eb50994a26525b5b2e34a34425549f0df2cdf" in (directory / "requirements-dgx.lock").read_text()
    assert "python -m pip check" in build
    assert "COPY --from=vendor-wheel /vendor/repair.json" in build
    assert "pynvml" not in (directory / "requirements-inference.txt").read_text()


def test_cpu_config_only_preflight_truthfully_reports_no_gpu_validation():
    script = Path(__file__).resolve().parents[1] / "preflight.py"
    result = subprocess.run([sys.executable, str(script), "--config-only"], capture_output=True,
        text=True, timeout=5, env={**os.environ, "FLUX_RUNTIME_PROFILE": "dgx-spark"})
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["profile"] == "dgx-spark"
    assert report["gpu_validated"] is report["model_loaded"] is report["weights_downloaded"] is False
    assert report["model"] == fw.MODEL_ID
