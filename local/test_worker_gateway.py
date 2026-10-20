"""CPU-only transport tests: fake cores, no weights or external inference."""
import fcntl
import http.client
import json
import multiprocessing
import threading
import time
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from local.worker_gateway import Gateway, MAX_REQUEST_BYTES, Rejected, Server
from local import worker_gateway

TOKEN = "development-test-token-" + "x" * 32


def payload():
    return {"input": {"invocation_id": str(uuid.uuid4()), "deadline_unix_ms": int((time.time()+60)*1000)}}


class Core:
    def __init__(self):
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.result = {"ok": True, "counters": {"inference_attempts": 1}}

    def handle(self, job, cancelled):
        self.calls += 1
        self.entered.set()
        self.release.wait(2)
        return self.result


@pytest.fixture
def core():
    instance = Core()
    yield instance
    instance.release.set()


@pytest.fixture
def gateway(core):
    instance = Gateway(core, TOKEN)
    yield instance
    core.release.set()
    instance.close()


@pytest.fixture
def transport(gateway):
    server = Server(("127.0.0.1", 0), gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False,
                      headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        yield client
    server.shutdown()
    server.server_close()
    thread.join(2)


def completed(gateway, identifier):
    assert gateway.jobs[identifier].done.wait(2)
    return json.loads(gateway.status(identifier))


def test_submit_returns_immediately_and_wraps_output(core, gateway, transport):
    before = time.monotonic()
    submitted = transport.post("/run", json=payload())
    assert submitted.status_code == 200
    assert time.monotonic() - before < 1
    assert core.entered.wait(1)
    identifier = submitted.json()["id"]
    assert transport.get(f"/status/{identifier}").json()["status"] == "IN_PROGRESS"
    core.release.set()
    result = completed(gateway, identifier)
    assert result["status"] == "COMPLETED"
    assert result["output"] == {"result": core.result}
    assert result["expiresAt"] > time.time() + 3500


def test_every_route_requires_token_without_leaking_it(transport):
    for method, route in [("GET", "/health"), ("POST", "/run"), ("GET", "/status/"+"a"*32),
                          ("POST", "/cancel/"+"a"*32)]:
        response = transport.request(method, route, headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401
        assert TOKEN not in response.text
    assert transport.get("/health").json() == {
        "status": "ready", "active_jobs": 0, "retained_jobs": 0, "result_capacity": 128}


def test_limits_validation_and_no_webhook_calls(transport, core):
    assert transport.post("/run", content=b"x"*(MAX_REQUEST_BYTES+1)).status_code == 413
    assert transport.post("/run", json={**payload(), "webhook": "http://localhost"}).status_code == 422
    assert transport.post("/run", content=b'{"input":NaN}', headers={"Content-Type": "application/json"}).status_code == 400
    assert transport.post("/run", json={"input": {}}).status_code == 422
    expired = payload()
    expired["input"]["deadline_unix_ms"] = 1
    assert transport.post("/run", json=expired).status_code == 422
    assert core.calls == 0


def test_same_invocation_deduplicates_and_mismatch_is_rejected(core, gateway):
    request = payload()
    job = gateway.submit(request)
    assert gateway.submit(request)["id"] == job["id"]
    altered = json.loads(json.dumps(request))
    altered["input"]["extra"] = True
    with pytest.raises(Rejected) as exc:
        gateway.submit(altered)
    assert exc.value.status == 422
    with pytest.raises(Rejected, match="") as busy:
        gateway.submit(payload())
    assert busy.value.status == 422
    core.release.set()
    completed(gateway, job["id"])
    assert gateway.submit(request)["id"] == job["id"]
    assert core.calls == 1


def test_cancel_ack_does_not_claim_execution_stopped(core, gateway):
    job = gateway.submit(payload())
    assert core.entered.wait(1)
    response = json.loads(gateway.cancel(job["id"]))
    assert response["status"] == "IN_PROGRESS"
    assert gateway.jobs[job["id"]].cancelled.is_set()
    core.release.set()
    result = completed(gateway, job["id"])
    assert result["output"]["result"]["counters"]["inference_attempts"] == 1


def test_cancel_queued_job_never_calls_model(tmp_path, core):
    path = tmp_path / "gpu.lock"
    with path.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        gateway = Gateway(core, TOKEN, lock_path=path)
        try:
            job = gateway.submit(payload())
            gateway.cancel(job["id"])
            assert completed(gateway, job["id"])["status"] == "CANCELLED"
            assert core.calls == 0
        finally:
            gateway.close()


def test_two_models_share_gpu_lock(tmp_path):
    first, second = Core(), Core()
    a = Gateway(first, TOKEN, lock_path=tmp_path/"gpu.lock")
    b = Gateway(second, TOKEN, lock_path=tmp_path/"gpu.lock")
    try:
        j1 = a.submit(payload())
        assert first.entered.wait(1)
        j2 = b.submit(payload())
        assert not second.entered.wait(0.15)
        first.release.set()
        completed(a, j1["id"])
        assert second.entered.wait(1)
        second.release.set()
        completed(b, j2["id"])
    finally:
        first.release.set()
        second.release.set()
        a.close()
        b.close()


def test_terminal_result_survives_late_view_return_and_expires_at_one_hour(core, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(worker_gateway, "time", SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic))
    gateway = Gateway(core, TOKEN)
    try:
        core.release.set()
        job = gateway.submit(payload())
        result = completed(gateway, job["id"])
        assert result["expiresAt"] == clock[0] + 3600
        clock[0] += 45 * 60
        assert json.loads(gateway.status(job["id"])) == result
        clock[0] = result["expiresAt"] - 0.001
        assert json.loads(gateway.status(job["id"])) == result
        clock[0] = result["expiresAt"]
        with pytest.raises(Rejected) as missing:
            gateway.status(job["id"])
        assert missing.value.status == 404
        assert core.calls == 1  # Reading or expiry never generates a replacement image.
    finally:
        gateway.close()


def test_default_store_bounds_admission_at_128_without_evicting_live_results(core, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(worker_gateway, "time", SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic))
    gateway = Gateway(core, TOKEN)
    try:
        core.release.set()
        requests = [payload() for _ in range(128)]
        retained = []
        for request in requests:
            job = gateway.submit(request)
            retained.append((job["id"], completed(gateway, job["id"])))
        with pytest.raises(Rejected) as full:
            gateway.submit(payload())
        assert full.value.status == 422
        assert "Temporary result capacity reached" in full.value.message
        assert gateway.health() == {"status": "ready", "active_jobs": 0,
                                    "retained_jobs": 128, "result_capacity": 128}
        for identifier, result in retained:
            assert json.loads(gateway.status(identifier)) == result
        assert gateway.submit(requests[0])["id"] == retained[0][0]
        assert core.calls == 128
        clock[0] += 3600
        assert gateway.health()["retained_jobs"] == 0
        request = payload()
        request["input"]["deadline_unix_ms"] = int((clock[0] + 60) * 1000)
        replacement = gateway.submit(request)
        completed(gateway, replacement["id"])
        assert len(gateway.jobs) == 1
        assert core.calls == 129
    finally:
        gateway.close()


def test_full_result_store_does_not_evict_unread_images(core):
    gateway = Gateway(core, TOKEN, capacity=1)
    try:
        core.release.set()
        job = gateway.submit(payload())
        completed(gateway, job["id"])
        with pytest.raises(Rejected) as exc:
            gateway.submit(payload())
        assert exc.value.status == 422
        gateway.jobs[job["id"]].expires_at = time.time()-1
        with pytest.raises(Rejected) as missing:
            gateway.status(job["id"])
        assert missing.value.status == 404
        gateway.submit(payload())
    finally:
        gateway.close()


def test_restart_is_missing_not_a_replayed_job(core, gateway):
    core.release.set()
    job = gateway.submit(payload())
    completed(gateway, job["id"])
    restarted = Gateway(core, TOKEN)
    try:
        with pytest.raises(Rejected) as exc:
            restarted.status(job["id"])
        assert exc.value.status == 404
        assert core.calls == 1
    finally:
        restarted.close()


def test_native_hang_requests_process_exit_without_false_cancel(core):
    exited = threading.Event()
    gateway = Gateway(core, TOKEN, cancel_grace=0.1, force_exit=lambda code: exited.set())
    try:
        job = gateway.submit(payload())
        assert core.entered.wait(1)
        gateway.cancel(job["id"])
        assert exited.wait(1)
        assert json.loads(gateway.status(job["id"]))["status"] == "IN_PROGRESS"
    finally:
        core.release.set()
        gateway.close()


def test_deadline_watchdog_signals_worker(core):
    gateway = Gateway(core, TOKEN, execution_seconds=0.1, cancel_grace=1)
    try:
        job = gateway.submit(payload())
        assert gateway.jobs[job["id"]].cancelled.wait(1)
        core.release.set()
        completed(gateway, job["id"])
    finally:
        core.release.set()
        gateway.close()


def test_worker_exception_is_sanitized(core, gateway):
    def fail(*args, **kwargs):
        raise RuntimeError("private article or secret token")
    core.handle = fail
    result = completed(gateway, gateway.submit(payload())["id"])
    assert result["status"] == "FAILED"
    assert "private" not in json.dumps(result)


def test_invalid_or_oversized_result_is_terminal_failure(core, gateway):
    core.release.set()
    core.result = {"huge": "x"*4_300_000}
    assert completed(gateway, gateway.submit(payload())["id"])["status"] == "FAILED"


def test_duplicate_auth_headers_fail_closed(transport):
    connection = http.client.HTTPConnection(transport.base_url.host, transport.base_url.port, timeout=2)
    try:
        connection.putrequest("GET", "/health")
        connection.putheader("Authorization", "Bearer "+TOKEN)
        connection.putheader("Authorization", "Bearer "+TOKEN)
        connection.endheaders()
        assert connection.getresponse().status == 401
    finally:
        connection.close()


def test_failed_runtime_refuses_new_work_but_preserves_result(core, gateway, transport):
    core.release.set()
    identifier = gateway.submit(payload())["id"]
    completed(gateway, identifier)
    core.ready = False
    assert transport.get("/health").status_code == 503
    assert transport.post("/run", json=payload()).status_code == 422
    assert transport.get(f"/status/{identifier}").json()["status"] == "COMPLETED"


def test_startup_bind_failure_closes_loaded_runtime(monkeypatch):
    worker = Mock()
    factory = Mock(return_value=worker)
    monkeypatch.setenv("LOCAL_WORKER_TOKEN", TOKEN)
    monkeypatch.setenv("LOCAL_WORKER_FACTORY", "test_factory:create_worker")
    monkeypatch.setattr(worker_gateway.importlib, "import_module", lambda name: SimpleNamespace(create_worker=factory))
    monkeypatch.setattr(worker_gateway, "Server", Mock(side_effect=OSError("address in use")))
    with pytest.raises(OSError):
        worker_gateway.main()
    worker.close.assert_called_once()


def test_invalid_port_is_rejected_before_loading_weights(monkeypatch):
    factory = Mock()
    monkeypatch.setenv("LOCAL_WORKER_TOKEN", TOKEN)
    monkeypatch.setenv("LOCAL_WORKER_PORT", "70000")
    monkeypatch.setattr(worker_gateway.importlib, "import_module", factory)
    with pytest.raises(SystemExit):
        worker_gateway.main()
    factory.assert_not_called()


@pytest.mark.parametrize("value", ["", "0", "257", "-1", "1.5", "true", " 128", "128 ", "1e2", "9" * 500])
def test_invalid_result_capacity_is_rejected_before_loading_weights(monkeypatch, value):
    factory = Mock()
    monkeypatch.setenv("LOCAL_WORKER_TOKEN", TOKEN)
    monkeypatch.setenv("LOCAL_RESULT_CAPACITY", value)
    monkeypatch.setattr(worker_gateway.importlib, "import_module", factory)
    with pytest.raises(SystemExit, match="LOCAL_RESULT_CAPACITY must be an integer from 1 to 256"):
        worker_gateway.main()
    factory.assert_not_called()


@pytest.mark.parametrize("value,expected", [(None, 128), ("1", 1), ("128", 128), ("256", 256)])
def test_result_capacity_default_and_valid_boundaries(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("LOCAL_RESULT_CAPACITY", raising=False)
    else:
        monkeypatch.setenv("LOCAL_RESULT_CAPACITY", value)
    assert worker_gateway.result_capacity_from_env() == expected


@pytest.mark.parametrize("value", [0, 257, True, 1.5, "128"])
def test_constructor_rejects_unbounded_result_capacity(core, value):
    with pytest.raises(ValueError):
        Gateway(core, TOKEN, capacity=value)


def test_startup_passes_configured_capacity_to_gateway(monkeypatch):
    worker = Mock()
    gateway_factory = Mock(wraps=Gateway)
    monkeypatch.setenv("LOCAL_WORKER_TOKEN", TOKEN)
    monkeypatch.setenv("LOCAL_WORKER_FACTORY", "test_factory:create_worker")
    monkeypatch.setenv("LOCAL_RESULT_CAPACITY", "77")
    monkeypatch.setattr(worker_gateway.importlib, "import_module", lambda name: SimpleNamespace(create_worker=lambda: worker))
    monkeypatch.setattr(worker_gateway, "Gateway", gateway_factory)
    monkeypatch.setattr(worker_gateway, "Server", Mock(side_effect=OSError("synthetic bind failure")))
    with pytest.raises(OSError):
        worker_gateway.main()
    assert gateway_factory.call_args.kwargs["capacity"] == 77
    worker.close.assert_called_once()


class UnknownOutcomeCore:
    def handle(self, job, cancelled):
        return {"ok": False, "error": {"code": "INFERENCE_OUTCOME_UNKNOWN"}}


def unknown_outcome_process(lock_path):
    gateway = Gateway(UnknownOutcomeCore(), TOKEN, lock_path=lock_path)
    gateway.submit(payload())
    # A faulty implementation returning a terminal envelope exits normally.
    threading.Event().wait(2)
    gateway.close()


def test_unknown_native_outcome_exits_process_before_releasing_gpu_lock(tmp_path):
    path = tmp_path / "gpu.lock"
    process = multiprocessing.get_context("spawn").Process(target=unknown_outcome_process, args=(str(path),))
    process.start()
    process.join(5)
    try:
        assert not process.is_alive()
        assert process.exitcode == 70
        with path.open("a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if process.is_alive():
            process.kill()
            process.join(2)
