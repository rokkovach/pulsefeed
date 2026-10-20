"""Exercise both real handler boundaries with explicitly synthetic inference."""

import json
import time

import pytest
from PIL import Image

from integration.contract_checks import CONTRACTS, check_output
from workers.flux.flux_worker import Worker as FluxWorker
from workers.qwen.handler import QwenWorker, Result


class SyntheticQwen:
    ready = True
    metadata = {"id": "nvidia/Qwen3-8B-NVFP4", "revision": "synthetic", "precision": "synthetic", "runtime": "synthetic"}

    def prepare(self, payload, seconds, cancelled):
        return payload

    def generate(self, prepared, seconds, cancelled):
        return Result("Editorial illustration of a community garden beside a library.", "stop", 24, 12, 5.0)


class SyntheticFlux:
    metadata = {"id": "black-forest-labs/FLUX.2-klein-4B", "revision": "synthetic", "precision": "synthetic", "runtime": "synthetic"}

    def validate_prompt(self, prompt):
        pass

    def generate(self, data, deadline):
        return [Image.new("RGB", (data["width"], data["height"]), (27, 67, 73))]

    def cleanup(self):
        pass


def request(stage):
    payload = json.loads((CONTRACTS / "fixtures" / f"{stage}-input.json").read_text())
    payload["deadline_unix_ms"] = int(time.time() * 1000) + 600_000
    return payload


def handler(stage, loaded=True):
    return QwenWorker(SyntheticQwen() if loaded else None) if stage == "qwen" else FluxWorker(SyntheticFlux() if loaded else None)


@pytest.mark.parametrize("stage", ["qwen", "flux"])
def test_actual_handler_success_obeys_shared_schema(stage):
    payload = request(stage)
    output = handler(stage).handle({"input": payload})
    assert output["ok"], output["error"]
    check_output(stage, output, payload)


@pytest.mark.parametrize("stage", ["qwen", "flux"])
@pytest.mark.parametrize("invalid", ["expired", "unknown-key", "wrong-stage", "bool-version", "missing-id", "overflow-deadline"])
def test_actual_handler_rejection_is_a_valid_zero_attempt_result(stage, invalid):
    payload = request(stage)
    if invalid == "expired":
        payload["deadline_unix_ms"] = 1
    elif invalid == "unknown-key":
        payload["extra"] = True
    elif invalid == "wrong-stage":
        payload["stage"] = "other"
    elif invalid == "bool-version":
        payload["schema_version"] = True
    elif invalid == "overflow-deadline":
        payload["deadline_unix_ms"] = 10**400
    else:
        del payload["run_id"]
    output = handler(stage).handle({"input": payload})
    assert output["ok"] is False
    assert output["counters"]["inference_attempts"] == 0
    if invalid == "overflow-deadline":
        assert output["error"]["code"] == "INVALID_INPUT"
        assert output["error"]["retryable"] is False
    check_output(stage, output)


@pytest.mark.parametrize("stage", ["qwen", "flux"])
def test_unloaded_worker_never_pretends_to_generate(stage):
    payload = request(stage)
    output = handler(stage, loaded=False).handle({"input": payload})
    assert output["ok"] is False
    assert output["error"]["code"] == "MODEL_NOT_READY"
    check_output(stage, output, payload)
