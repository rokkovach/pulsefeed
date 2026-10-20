"""App consumer accepts the actual worker envelopes and rejects wire drift."""

import pytest

from app.pulsefeed.errors import AppError
from app.pulsefeed.provider import unwrap_output, validate_envelope
from integration.test_worker_boundaries import handler, request


@pytest.mark.parametrize("stage", ["qwen", "flux"])
@pytest.mark.parametrize("loaded", [True, False])
def test_app_consumes_actual_handler_success_and_failure(stage, loaded):
    payload = request(stage)
    invocation = {"id": payload["invocation_id"], "run_id": payload["run_id"], "stage": stage}
    result = handler(stage, loaded=loaded).handle({"input": payload})
    envelope = unwrap_output({"result": result})
    image = validate_envelope(envelope, invocation, payload)
    assert envelope["ok"] is loaded
    assert bool(image) is (loaded and stage == "flux")
    with pytest.raises(AppError):
        unwrap_output(result)


def test_app_rejects_shared_retry_budget_violation():
    payload = request("flux")
    result = handler("flux").handle({"input": payload})
    result["counters"] = {"inference_attempts": 2, "inference_retries": 1, "dependency_retries": 1}
    invocation = {"id": payload["invocation_id"], "run_id": payload["run_id"], "stage": "flux"}
    with pytest.raises(AppError):
        validate_envelope(result, invocation, payload)
