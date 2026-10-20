"""Runpod entry point. No weights are loaded merely by importing this module."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading

if __package__:
    from .flux_worker import DiffusersEngine, RuntimeConfig, Worker, WorkerFailure, classify_startup_error
else:  # Docker executes this file as a script from /worker.
    from flux_worker import DiffusersEngine, RuntimeConfig, Worker, WorkerFailure, classify_startup_error

_worker = Worker(None)
_cancelled = threading.Event()
_active_handlers = set()
_signal_loop = None
CANCELLATION_GRACE_SECONDS = 5

# SDK DEBUG logs contain full handler outputs. Set before importing its modules.
os.environ["RUNPOD_LOG_LEVEL"] = "INFO"


def _hard_exit(code):
    """An unresponsive native CUDA operation requires process-level teardown."""
    os._exit(code)


def _stop_if_outcome_unknown(result):
    if (result.get("error") or {}).get("code") == "INFERENCE_OUTCOME_UNKNOWN":
        # SDK refresh happens after terminal delivery. Uncertain native work
        # instead requires teardown, including when cancellation drained a result.
        _hard_exit(1)
        raise SystemExit(1)  # Defensive if an injected exit hook returns.


def _install_shutdown_handlers(loop):
    """SDK startup replaces signals; install compatible asyncio handlers afterward."""
    global _signal_loop
    if _signal_loop is loop:
        return

    def shutdown():
        _cancelled.set()
        for task in tuple(_active_handlers):
            task.cancel()
        # A native CUDA kernel may not return immediately. Bound process shutdown.
        loop.call_later(CANCELLATION_GRACE_SECONDS, _hard_exit, 0)

    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, shutdown)
    _signal_loop = loop


async def handler(job):
    loop = asyncio.get_running_loop()
    _install_shutdown_handlers(loop)
    cancelled = threading.Event()
    current = asyncio.current_task()
    _active_handlers.add(current)
    inference = asyncio.create_task(asyncio.to_thread(_worker.handle, job, cancelled=cancelled))
    try:
        result = await asyncio.shield(inference)
        _stop_if_outcome_unknown(result)
        # The SDK consumes a top-level `error` field. Keep the contract nested.
        wrapped = {"result": result}
        if not _worker.ready:
            # Pinned SDK consumes this flag; provider output remains {result: ...}.
            wrapped["refresh_worker"] = True
        return wrapped
    except asyncio.CancelledError:
        cancelled.set()
        try:
            result = await asyncio.wait_for(asyncio.shield(inference), CANCELLATION_GRACE_SECONDS)
            _stop_if_outcome_unknown(result)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # SDK cancellation stops the job task, not its native CUDA thread.
            # Recycle the process rather than acknowledging abandoned GPU work.
            logging.error(json.dumps({"event": "flux_cancel_cleanup_timeout",
                                      "message": "Recycling worker after unresponsive inference cancellation."}))
            _hard_exit(1)
        raise
    finally:
        _active_handlers.discard(current)


def main():
    global _worker
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    def terminate(_signum, _frame):
        _cancelled.set()
        # The SDK process is terminated; no successful payload or accounting is invented.
        raise SystemExit(0)

    def startup_timeout(_signum, _frame):
        raise WorkerFailure("DEADLINE_EXCEEDED", "Worker initialization exceeded its configured deadline.")

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    try:
        config = RuntimeConfig.from_env()
        signal.signal(signal.SIGALRM, startup_timeout)
        signal.setitimer(signal.ITIMER_REAL, config.startup_seconds)
        engine = DiffusersEngine(config)
        _worker = Worker(engine, execution_seconds=config.execution_seconds, cancelled=_cancelled)
        logging.info(json.dumps({"event": "flux_ready", "model": engine.metadata}))
    except Exception as exc:
        failure = classify_startup_error(exc)
        logging.error(json.dumps({"event": "flux_startup_failed", "code": failure.code,
                                  "exception_type": type(exc).__name__, "message": failure.message}))
        raise SystemExit(1) from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    import runpod
    from runpod.serverless.modules.rp_logger import RunPodLogger
    RunPodLogger().set_level("INFO")
    runpod.serverless.start({"handler": handler, "concurrency_modifier": lambda current: 1})


if __name__ == "__main__":
    main()
