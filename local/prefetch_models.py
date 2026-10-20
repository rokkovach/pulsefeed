"""Download the worker's pinned weights into its existing Compose cache, without inference."""
import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import importlib
import logging
import os
import sys


@contextmanager
def quiet_download():
    """Suppress third-party retry/progress diagnostics, including cached/native streams."""
    previous_logging = logging.root.manager.disable
    saved = {}
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        with open(os.devnull, "w") as sink:
            # Libraries can cache the original stderr or write directly to fd 2.
            # This single-purpose CLI has no concurrent operator output to retain.
            for descriptor in (1, 2):
                saved[descriptor] = os.dup(descriptor)
                os.dup2(sink.fileno(), descriptor)
            logging.disable(sys.maxsize)
            with redirect_stdout(sink), redirect_stderr(sink):
                yield
    finally:
        # Cached Python streams may have buffered writes; drain them while their
        # file descriptors still point at the sink, before restoring operator I/O.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        for descriptor, original in saved.items():
            os.dup2(original, descriptor)
            os.close(original)
        logging.disable(previous_logging)


def prefetch(stage, download=None):
    if stage not in ("qwen", "flux"):
        raise ValueError("Choose qwen or flux")
    # These boundary modules define pins but do not import GPU inference runtimes.
    model = importlib.import_module("handler" if stage == "qwen" else "flux_worker")
    revision = os.getenv("MODEL_REVISION", model.MODEL_REVISION)
    if revision != model.MODEL_REVISION:
        raise ValueError("Configured revision differs from the worker's pinned checkpoint")
    options = {
        "repo_id": model.MODEL_ID,
        "revision": revision,
        "cache_dir": os.getenv("MODEL_CACHE_DIR", "/runpod-volume/huggingface") if stage == "qwen" else os.getenv("HF_HUB_CACHE", "/runpod-volume/huggingface/hub"),
        "token": os.getenv("HF_TOKEN") or None,
        "max_workers": 2,
    }
    if stage == "flux":
        options["allow_patterns"] = ["model_index.json", "scheduler/*", "tokenizer/*", "text_encoder/*", "transformer/*", "vae/*"]
    # Qwen intentionally downloads the complete repo, exactly as runtime.py does.
    # Omitting files here would only defer their downloads into its startup budget.
    if download is None:
        from huggingface_hub import snapshot_download
        download = snapshot_download
    print(f"Prefetching pinned {stage} checkpoint into its persistent cache; no inference.", flush=True)
    with quiet_download():
        download(**options)
    print(f"Pinned {stage} checkpoint cached. Model loading and readiness are still unverified.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("qwen", "flux"))
    args = parser.parse_args()
    try:
        prefetch(args.stage)
    except KeyboardInterrupt:
        print("Prefetch interrupted; cached files are retained for an explicit rerun.", file=sys.stderr)
        return 130
    except Exception as error:
        # Never echo exception messages/tracebacks: download errors can include
        # credential-bearing URLs. Do not print the environment or token either.
        print(f"Prefetch failed ({type(error).__name__}); check model access, network and cache configuration privately. Cached files are retained.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
