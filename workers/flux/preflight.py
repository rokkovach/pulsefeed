"""Inspect configured runtime; the default kernel-only check downloads no weights."""
import argparse
from dataclasses import asdict
import json
import platform

try:
    from .flux_worker import MODEL_ID, RuntimeConfig, WorkerFailure, preflight_runtime
except ImportError:
    from flux_worker import MODEL_ID, RuntimeConfig, WorkerFailure, preflight_runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-only", action="store_true", help="CPU configuration check; no GPU compatibility claim")
    args = parser.parse_args()
    try:
        config = RuntimeConfig.from_env()
        report = {"model": MODEL_ID, "revision": config.revision, "profile": config.profile,
                  "cache_source": config.cache_source, "cache_offline": config.local_files_only,
                  "machine": platform.machine(), "gpu_validated": False,
                  "model_loaded": False, "weights_downloaded": False}
        if not args.config_only:
            import torch
            import diffusers
            import transformers
            report["runtime"] = asdict(preflight_runtime(config, torch, diffusers, transformers))
            report["gpu_validated"] = True
        print(json.dumps(report))
    except WorkerFailure as exc:
        print(json.dumps({"ok": False, "code": exc.code, "message": exc.message}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
