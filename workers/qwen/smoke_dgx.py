"""Standalone opt-in Spark evidence run. Stop resident model containers first."""
import argparse
import json
from pathlib import Path
import threading
import time

if __package__:
    from .handler import QwenWorker
    from .local_factory import create_worker
    from .smoke_test import InjectedDependency, make_input
else:
    from handler import QwenWorker
    from local_factory import create_worker
    from smoke_test import InjectedDependency, make_input


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-gpu", action="store_true", required=True)
    parser.add_argument("--evidence-path", default="/tmp/qwen-spark-evidence.json")
    args = parser.parse_args()
    evidence = {"profile": "dgx-spark", "validation_kind": "real_gpu_with_labeled_failure_injection", "checks": {}}
    worker = None
    started = time.monotonic()
    try:
        worker = create_worker()
        evidence.update(initialization_ms=(time.monotonic()-started)*1000, model=worker.runtime.metadata)
        for name in ("first_real_prompt", "warm_real_prompt"):
            result = worker.handle({"input":make_input()}, cancelled=threading.Event())
            evidence["checks"][name] = result
            assert result["ok"], result["error"]
            assert 0 < result["usage"]["output_tokens"] <= 256
        invalid = make_input(); invalid["max_new_tokens"] = 1024
        result = worker.handle({"input":invalid})
        evidence["checks"]["invalid_input"] = result
        assert not result["ok"] and result["counters"]["inference_attempts"] == 0
        result = QwenWorker(InjectedDependency(worker.runtime, 1)).handle({"input": make_input()})
        evidence["checks"]["synthetic_pre_inference_failure_not_replayed"] = result
        assert not result["ok"] and result["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert result["counters"]["dependency_retries"] == 0
        assert result["counters"]["inference_attempts"] == 0
    finally:
        if worker is not None:
            worker.close()
        Path(args.evidence_path).write_text(json.dumps(evidence,indent=2))
        print(f"Spark evidence written to {args.evidence_path}")


if __name__ == "__main__":
    main()
