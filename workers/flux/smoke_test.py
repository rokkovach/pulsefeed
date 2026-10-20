"""Operator-run live GPU check. Prints metadata only; never prints image payloads."""
from __future__ import annotations

import argparse
import base64
import io
import json
import signal
import time
import uuid

from PIL import Image

from flux_worker import DiffusersEngine, RuntimeConfig, Worker, WorkerFailure


def make_job(output_format="webp", size=512):
    return {"input": {
        "schema_version": 1, "stage": "flux", "run_id": str(uuid.uuid4()),
        "invocation_id": str(uuid.uuid4()), "deadline_unix_ms": int((time.time() + 600) * 1000),
        "prompt": "Editorial illustration of an alpine railway, soft daylight, clean composition.",
        "seed": 42, "width": size, "height": size, "num_inference_steps": 4,
        "guidance_scale": 1.0, "output_format": output_format,
    }}


class ControlledFailureEngine:
    """An operator-only test wrapper, never selected by a provider job field."""
    def __init__(self, engine, failures):
        self.engine, self.remaining = engine, failures
        self.metadata = engine.metadata

    def validate_prompt(self, prompt):
        self.engine.validate_prompt(prompt)

    def generate(self, data, deadline):
        if self.remaining:
            self.remaining -= 1
            # Deterministically no GPU operation was dispatched by this adapter call.
            raise WorkerFailure("DEPENDENCY_UNAVAILABLE", "Synthetic pre-dispatch failure.", True)
        return self.engine.generate(data, deadline)

    def cleanup(self):
        self.engine.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-format", choices=("webp", "jpeg"), default="webp")
    parser.add_argument("--size", type=int, choices=(256, 512, 1024), default=512)
    parser.add_argument("--controlled-failure", action="store_true",
                        help="Verify a simulated dependency failure does not replay inference")
    args = parser.parse_args()
    config = RuntimeConfig.from_env()
    started = time.monotonic()
    def startup_timeout(_signum, _frame):
        raise WorkerFailure("DEADLINE_EXCEEDED", "GPU smoke-test initialization exceeded its deadline.")
    signal.signal(signal.SIGALRM, startup_timeout)
    signal.setitimer(signal.ITIMER_REAL, config.startup_seconds)
    try:
        engine = DiffusersEngine(config)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    print(json.dumps({"event": "startup", "startup_ms": (time.monotonic() - started) * 1000,
                      "model": engine.metadata}))
    worker = Worker(engine, execution_seconds=config.execution_seconds)
    invalid = worker.handle(make_job() | {"input": make_job()["input"] | {"width": 200}})
    assert not invalid["ok"] and invalid["counters"]["inference_attempts"] == 0
    for label in ("first", "warm"):
        job = make_job(args.output_format, args.size)
        output = worker.handle(job)
        assert output["ok"], output["error"]
        raw = base64.b64decode(output["data"]["image_base64"], validate=True)
        assert len(raw) == output["data"]["image_bytes"] <= 2_097_152
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            assert image.size == (args.size, args.size)
        summary = {key: value for key, value in output.items() if key != "data"}
        summary["image"] = {key: value for key, value in output["data"].items() if key != "image_base64"}
        print(json.dumps({"event": label, "result": summary}))
    if args.controlled_failure:
        output = Worker(ControlledFailureEngine(engine, 1)).handle(make_job(args.output_format, args.size))
        assert not output["ok"] and output["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert output["counters"]["inference_attempts"] == 1
        assert output["counters"]["inference_retries"] == 0
        print(json.dumps({"event": "controlled_adapter_fault", "ok": output["ok"],
                          "counters": output["counters"], "error": output["error"]}))
    print(json.dumps({"event": "smoke_test_passed", "evidence": "real-gpu", "output_format": args.output_format}))


if __name__ == "__main__":
    main()
