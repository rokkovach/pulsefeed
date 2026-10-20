"""Explicit real-GPU validation; never executed by CPU test discovery."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
from uuid import uuid4

if __package__:
    from . import handler as handler_module
    from .handler import QwenWorker, WorkerError
    from .runtime import ProcessRuntime
else:
    import handler as handler_module
    from handler import QwenWorker, WorkerError
    from runtime import ProcessRuntime


def make_input():
    return {"schema_version": 1, "stage": "qwen", "run_id": str(uuid4()), "invocation_id": str(uuid4()),
            "deadline_unix_ms": int(time.time()*1000)+240000, "max_new_tokens": 256,
            "article": {"title": "Residents transform a vacant lot into a community garden",
                        "excerpt": "Neighbors plant trees and vegetables beside apartment buildings."},
            "system_prompt": "Create a concise visual prompt for an editorial illustration. Preserve source context."}


class InjectedDependency:
    """Test-only terminal pre-inference failure; never enabled by a job input."""
    def __init__(self, runtime, failures):
        self.runtime, self.failures = runtime, failures
        self.metadata = runtime.metadata

    @property
    def ready(self):
        return self.runtime.ready

    def prepare(self, value, seconds, cancelled):
        if self.failures:
            self.failures -= 1
            raise WorkerError("DEPENDENCY_UNAVAILABLE", "Synthetic safe dependency failure.", True, safe_retry=True)
        return self.runtime.prepare(value, seconds, cancelled)

    def generate(self, value, seconds, cancelled):
        return self.runtime.generate(value, seconds, cancelled)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-gpu", action="store_true", required=True,
                        help="Explicitly allow startup weight download and real GPU inference")
    parser.add_argument("--evidence-path", default="/tmp/qwen-gpu-evidence.json")
    args = parser.parse_args()
    runtime = ProcessRuntime.from_env()
    evidence = {"validation_kind": "real_gpu_with_labeled_failure_injection", "checks": {}}
    started = time.monotonic()
    os.environ["RUNPOD_LOG_LEVEL"] = "INFO"
    from runpod.serverless.modules.rp_job import run_job
    from runpod.serverless.modules.rp_logger import RunPodLogger
    RunPodLogger().set_level("INFO")
    def invoke(worker, value):
        handler_module.WORKER = worker
        provider_result = asyncio.run(run_job(handler_module.handler, {"id": str(uuid4()), "input": value}))
        assert "error" not in provider_result, "SDK returned an unexpected transport error"
        return provider_result["output"]["result"]
    try:
        runtime.start()
        evidence["initialization_ms"] = (time.monotonic()-started)*1000
        evidence["model"] = runtime.metadata
        worker = QwenWorker(runtime)
        for name in ("first_real_prompt", "warm_real_prompt"):
            out = invoke(worker, make_input())
            evidence["checks"][name] = out
            assert out["ok"], out["error"]
            assert 0 < out["usage"]["output_tokens"] <= 256
            assert out["counters"]["inference_attempts"] == 1
        invalid = make_input(); invalid["max_new_tokens"] = 1024
        out = invoke(worker, invalid)
        evidence["checks"]["invalid_input"] = out
        assert not out["ok"] and out["counters"]["inference_attempts"] == 0
        out = invoke(QwenWorker(InjectedDependency(runtime, 1)), make_input())
        evidence["checks"]["synthetic_pre_inference_failure_not_replayed"] = out
        assert not out["ok"] and out["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert out["counters"]["dependency_retries"] == 0
        assert out["counters"]["inference_attempts"] == 0
    finally:
        runtime.close()
        Path(args.evidence_path).write_text(json.dumps(evidence, indent=2))
        print(f"GPU evidence written to {args.evidence_path}")


if __name__ == "__main__":
    main()
