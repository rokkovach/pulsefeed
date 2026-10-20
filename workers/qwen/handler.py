"""Runpod Qwen contract boundary; importing this module never loads GPU libraries."""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
import logging
import re
import threading
import time
import unicodedata
import sys
from uuid import UUID

# The Docker entrypoint executes this file as a script. Keep exception/dataclass
# identity shared with runtime.py and multiprocessing's spawned entrypoint.
if __name__ in {"__main__", "__mp_main__"}:
    sys.modules["handler"] = sys.modules[__name__]

MODEL_ID = "nvidia/Qwen3-8B-NVFP4"
MODEL_REVISION = "ccd10a893cbca613259517c3efe08e151ddf2b8e"
ARTICLE_TEMPLATE_MARKERS = ("{{article_title}}", "{{article_excerpt}}")
ARTICLE_TEMPLATE_STATIC_LIMIT = 2000
LOG = logging.getLogger(__name__)


class WorkerError(Exception):
    def __init__(self, code, message, retryable=False, *, safe_retry=False,
                 input_tokens=None, output_tokens=None, inference_ms=None):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable
        self.safe_retry = safe_retry
        self.input_tokens, self.output_tokens = input_tokens, output_tokens
        self.inference_ms = inference_ms


@dataclass
class Result:
    text: str
    finish_reason: str
    input_tokens: int | None
    output_tokens: int | None
    inference_ms: float | None


def valid_id(value):
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        parsed = UUID(value)
        return value if str(parsed) == value.lower() else None
    except (ValueError, AttributeError):
        return None


def normalized(value):
    return " ".join(unicodedata.normalize("NFC", value).split())


def validate(value, now_ms):
    if not isinstance(value, dict):
        raise WorkerError("INVALID_INPUT", "Job input must be an object.")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise WorkerError("UNSUPPORTED_SCHEMA", "Supported schema_version is 1.")
    keys = {"schema_version", "stage", "run_id", "invocation_id", "deadline_unix_ms",
            "article", "system_prompt", "max_new_tokens"}
    if set(value) not in (keys, keys | {"article_template"}) or value["stage"] != "qwen":
        raise WorkerError("INVALID_INPUT", "Missing or unsupported Qwen input fields.")
    if not valid_id(value["run_id"]) or not valid_id(value["invocation_id"]):
        raise WorkerError("INVALID_INPUT", "Correlation identifiers must be UUID strings.")
    if (type(value["deadline_unix_ms"]) is not int or
            not 1 <= value["deadline_unix_ms"] <= 9007199254740991):
        raise WorkerError("INVALID_INPUT", "deadline_unix_ms must be a positive safe integer.")
    if value["deadline_unix_ms"] <= now_ms:
        raise WorkerError("DEADLINE_EXCEEDED", "The item deadline has expired.")
    if type(value["max_new_tokens"]) is not int or value["max_new_tokens"] != 256:
        raise WorkerError("INVALID_INPUT", "The supported output preset is 256 tokens.")
    article = value["article"]
    if not isinstance(article, dict) or set(article) != {"title", "excerpt"}:
        raise WorkerError("INVALID_INPUT", "article requires title and excerpt only.")
    if any(not isinstance(article[k], str) for k in article):
        raise WorkerError("INVALID_INPUT", "Article fields must be strings.")
    if not article["title"] or any(article[k] != normalized(article[k]) for k in article):
        raise WorkerError("INVALID_INPUT", "Article text must be nonempty-title normalized plain text.")
    if any(re.search(r"<[^>]+>", article[k]) for k in article):
        raise WorkerError("INVALID_INPUT", "Article text must contain plain text only.")
    if sum(len(article[k]) for k in article) > 4000:
        raise WorkerError("INPUT_TOO_LARGE", "Article text exceeds 4000 characters.")
    system = value["system_prompt"]
    if not isinstance(system, str) or not system.strip():
        raise WorkerError("INVALID_INPUT", "system_prompt must be nonempty text.")
    if len(system) > 4000:
        raise WorkerError("INPUT_TOO_LARGE", "System prompt exceeds 4000 characters.")
    template_text = ()
    if "article_template" in value:
        template = value["article_template"]
        if not isinstance(template, str) or any(template.count(marker) != 1 for marker in ARTICLE_TEMPLATE_MARKERS):
            raise WorkerError("INVALID_INPUT", "article_template requires each article marker exactly once.")
        static_length = len(template) - sum(len(marker) for marker in ARTICLE_TEMPLATE_MARKERS)
        if static_length > ARTICLE_TEMPLATE_STATIC_LIMIT:
            raise WorkerError("INPUT_TOO_LARGE", "Article template static text exceeds 2000 characters.")
        template_text = (template,)
    for text in (*article.values(), system, *template_text):
        if any(unicodedata.category(c) in {"Cs", "Cc"} and c not in "\n\r\t" for c in text):
            raise WorkerError("INVALID_INPUT", "Input contains unsupported control characters.")
    return value


def validate_output(result):
    text = result.text.strip() if isinstance(result.text, str) else ""
    if result.finish_reason != "stop":
        raise WorkerError("INVALID_MODEL_OUTPUT", "Prompt generation did not finish cleanly.")
    if result.output_tokens is not None and result.output_tokens > 256:
        raise WorkerError("INVALID_MODEL_OUTPUT", "Model exceeded the output token preset.")
    if len(text) > 2000:
        raise WorkerError("OUTPUT_TOO_LARGE", "Generated prompt exceeds 2000 characters.")
    if (not text or re.search(r"<\/?(?:think|analysis|reasoning)\b|<\|", text, re.I)
            or text[0] in '[{"' or "```" in text
            or "\n" in text or "\r" in text
            or any(unicodedata.category(c) in {"Cs", "Cc"} for c in text)):
        raise WorkerError("INVALID_MODEL_OUTPUT", "Model returned an invalid visual prompt.")
    return text


class QwenWorker:
    def __init__(self, runtime=None, *, execution_seconds=240, clock=time.time,
                 monotonic=time.monotonic):
        self.runtime = runtime
        self.execution_seconds = execution_seconds
        self.clock, self.monotonic = clock, monotonic
        self.lock = threading.Lock()

    def handle(self, job, cancelled=lambda: False):
        raw = job.get("input") if isinstance(job, dict) else None
        raw = raw if isinstance(raw, dict) else {}
        result = {"schema_version": 1, "stage": "qwen",
                  "run_id": valid_id(raw.get("run_id")),
                  "invocation_id": valid_id(raw.get("invocation_id")), "ok": False,
                  "model": {"id": MODEL_ID, "revision": None, "precision": None, "runtime": None},
                  "data": None, "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                  "timings": {"inference_ms": 0},
                  "counters": {"inference_attempts": 0, "inference_retries": 0, "dependency_retries": 0},
                  "error": None}
        acquired = False
        try:
            if self.runtime and self.runtime.metadata:
                result["model"] = dict(self.runtime.metadata)
            value = validate(job.get("input") if isinstance(job, dict) else None, self.clock()*1000)
            deadline = self.monotonic() + min(self.execution_seconds,
                                              value["deadline_unix_ms"]/1000-self.clock())

            def remaining():
                if cancelled():
                    raise WorkerError("CANCELLED", "Generation was cancelled.")
                seconds = deadline-self.monotonic()
                if seconds <= 0:
                    raise WorkerError("DEADLINE_EXCEEDED", "Generation deadline expired.")
                return seconds

            remaining()
            if not self.runtime or not self.runtime.ready:
                raise WorkerError("MODEL_NOT_READY", "Qwen worker is not ready.", True)
            acquired = self.lock.acquire(blocking=False)
            if not acquired:
                raise WorkerError("DEPENDENCY_UNAVAILABLE", "Qwen worker is busy.", True)
            # Each accepted request gets one inference attempt. Retrying a GPU
            # call can duplicate work/cost when its outcome is uncertain. Only
            # the model downloader has a bounded startup retry policy.
            prepared = self.runtime.prepare(value, remaining(), cancelled)
            inference_budget = remaining()
            result["counters"]["inference_attempts"] = 1
            try:
                output = self.runtime.generate(prepared, inference_budget, cancelled)
            except WorkerError as exc:
                self._record_measurements(result, exc)
                raise
            self._record_measurements(result, output)
            # Account for completed GPU work even if the deadline or output
            # validation rejects its result; neither condition replays inference.
            remaining()
            prompt = validate_output(output)
            result.update(ok=True, data={"prompt": prompt})
        except WorkerError as exc:
            result["error"] = {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
        except Exception:
            # Log correlation and type only; detailed runtime diagnostics stay in GPU process logs.
            LOG.error("Unhandled worker failure run_id=%s", result["run_id"])
            if result["counters"]["inference_attempts"]:
                result["usage"] = dict.fromkeys(result["usage"])
                result["timings"]["inference_ms"] = None
            result["error"] = {"code": "INFERENCE_FAILED", "message": "Qwen generation failed.", "retryable": False}
        finally:
            if acquired:
                self.lock.release()
        return result

    @staticmethod
    def _record_measurements(envelope, observation):
        # Missing measurements stay unknown rather than becoming zero usage.
        inp, out = observation.input_tokens, observation.output_tokens
        envelope["usage"] = {"input_tokens": inp, "output_tokens": out,
                             "total_tokens": None if inp is None or out is None else inp+out}
        envelope["timings"]["inference_ms"] = observation.inference_ms


WORKER = QwenWorker()
ACTIVE_CANCELLATIONS = set()


async def handler(job):
    """Keep SDK heartbeat/cancellation tasks runnable and preserve the nested contract."""
    cancelled = threading.Event()
    ACTIVE_CANCELLATIONS.add(cancelled)
    task = asyncio.create_task(asyncio.to_thread(WORKER.handle, job, cancelled.is_set))
    try:
        envelope = await asyncio.shield(task)
        wrapped = {"result": envelope}
        # SDK consumes this control flag; provider output remains only {result: envelope}.
        if WORKER.runtime and not WORKER.runtime.ready:
            wrapped["refresh_worker"] = True
        return wrapped
    except asyncio.CancelledError:
        cancelled.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=3)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            close = getattr(WORKER.runtime, "close", None)
            if close:
                await asyncio.wait_for(asyncio.to_thread(close), timeout=3)
        raise
    finally:
        ACTIVE_CANCELLATIONS.discard(cancelled)


def managed_scaler(runtime, base_class):
    """Pinned SDK integration: JobScaler.start registers this overridden callback."""
    class ManagedScaler(base_class):
        def handle_shutdown(self, received, frame):
            for event in tuple(ACTIVE_CANCELLATIONS):
                event.set()
            runtime.close()  # Process group kill + join is bounded to two seconds.
            super().handle_shutdown(received, frame)
    return ManagedScaler


def main():
    import atexit
    import os
    import signal
    os.environ["RUNPOD_LOG_LEVEL"] = "INFO"
    import runpod
    if __package__:
        from .runtime import ProcessRuntime
    else:
        from runtime import ProcessRuntime
    global WORKER
    runtime = ProcessRuntime.from_env()
    atexit.register(runtime.close)
    def terminate(signum, frame):
        runtime.close()
        raise SystemExit(128+signum)
    # These handlers cover initialization, before the SDK takes ownership. After
    # startup ManagedScaler preserves engine cleanup through the SDK lifecycle.
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    execution_seconds = int(os.getenv("EXECUTION_TIMEOUT_SECONDS", "240"))
    if not 1 <= execution_seconds <= 600:
        raise WorkerError("INVALID_INPUT", "Execution limit must be between 1 and 600 seconds.")
    runtime.start()  # Raises on incompatibility; no provider readiness before this completes.
    WORKER = QwenWorker(runtime, execution_seconds=execution_seconds)
    # SDK replaces both signal handlers. Use its pinned scaler's actual registration
    # path; config-level job fetch overrides are ignored in production SDK mode.
    from runpod.serverless.modules import rp_scale
    rp_scale.JobScaler = managed_scaler(runtime, rp_scale.JobScaler)
    sys.argv.extend(["--rp_log_level", "INFO"])
    runpod.serverless.start({"handler": handler, "concurrency_modifier": lambda current: 1})


if __name__ == "__main__":
    main()
