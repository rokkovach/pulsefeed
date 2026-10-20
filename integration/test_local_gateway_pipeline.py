"""Real local HTTP transport + production handlers, explicitly synthetic engines."""
import asyncio
from contextlib import contextmanager
import threading
import uuid

import pytest

from app.pulsefeed.config import Config
from app.pulsefeed.errors import AppError
from app.pulsefeed.provider import LocalHTTPAdapter, unwrap_output, validate_envelope
from integration.test_worker_boundaries import SyntheticQwen, SyntheticFlux, request
from local.worker_gateway import Gateway, Server
from workers.qwen.local_factory import LocalQwenWorker
from workers.flux.flux_worker import Worker as FluxWorker

TOKEN = "synthetic-local-transport-test-" + "x"*32


@contextmanager
def servers():
    qwen = LocalQwenWorker(SyntheticQwen(), 240)
    # SyntheticQwen has no subprocess to close.
    qwen.close = lambda: None
    gateways = [Gateway(qwen, TOKEN), Gateway(FluxWorker(SyntheticFlux()), TOKEN)]
    serving = [Server(("127.0.0.1", 0), gateway) for gateway in gateways]
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in serving]
    for thread in threads:
        thread.start()
    config = Config(session_secret="s"*40, inference_provider="local_http", local_worker_token=TOKEN,
                    local_qwen_url=f"http://127.0.0.1:{serving[0].server_port}",
                    local_flux_url=f"http://127.0.0.1:{serving[1].server_port}", cookie_secure=False)
    config.validate()
    try:
        yield config, gateways
    finally:
        for server in serving:
            server.shutdown()
            server.server_close()
        for gateway in gateways:
            gateway.close()
        for thread in threads:
            thread.join(2)


@pytest.mark.parametrize("image_size", [256,512,1024])
def test_laptop_adapter_to_both_handler_cores_over_authenticated_http(image_size):
    with servers() as (config, gateways):
        async def pipeline():
            provider = LocalHTTPAdapter(config)
            prompt = None
            run_id = str(uuid.uuid4())
            try:
                for index, stage in enumerate(("qwen", "flux")):
                    value = request(stage)
                    value.update(run_id=run_id, invocation_id=str(uuid.uuid4()))
                    if stage == "flux":
                        value["prompt"] = prompt
                        value.update(width=image_size, height=image_size)
                    job_id = await provider.submit(stage, value)
                    for _ in range(100):
                        result = await provider.status(stage, job_id)
                        if result["status"] == "COMPLETED":
                            break
                        await asyncio.sleep(0.01)
                    assert result["status"] == "COMPLETED"
                    envelope = unwrap_output(result["output"])
                    invocation = {"id": value["invocation_id"], "stage": stage, "run_id": run_id}
                    image = validate_envelope(envelope, invocation, value)
                    assert envelope["ok"]
                    assert envelope["model"]["runtime"] == "synthetic"
                    assert envelope["counters"]["inference_attempts"] == 1
                    if stage == "qwen":
                        prompt = envelope["data"]["prompt"]
                        assert envelope["usage"]["total_tokens"] == 36
                    else:
                        assert image and len(image) == envelope["data"]["image_bytes"]
                    # An expired or restarted gateway is uncertainty, not a resubmit.
                    gateways[index].jobs.clear()
                    assert (await provider.status(stage, job_id))["status"] == "MISSING"
            finally:
                await provider.close()
        asyncio.run(pipeline())


def test_wrong_local_token_is_definitive_rejection_before_inference():
    with servers() as (config, gateways):
        config.local_worker_token = "wrong"*10
        async def call():
            provider = LocalHTTPAdapter(config)
            try:
                with pytest.raises(AppError) as exc:
                    await provider.submit("qwen", request("qwen"))
                assert exc.value.code == "PROVIDER_REJECTED"
                assert not gateways[0].jobs
            finally:
                await provider.close()
        asyncio.run(call())
