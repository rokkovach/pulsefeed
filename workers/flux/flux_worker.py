"""Validated, bounded FLUX execution with an injectable inference adapter."""
from __future__ import annotations

import base64
import errno
import io
import json
import hashlib
import logging
import os
import platform
import random
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from PIL import Image, features

MODEL_ID = "black-forest-labs/FLUX.2-klein-4B"
MODEL_REVISION = "e7b7dc27f91deacad38e78976d1f2b499d76a294"
RUNPOD_MANAGED_CACHE = "/runpod-volume/huggingface-cache/hub"
# Exact pinned-revision Hub metadata, not a latest/main lookup. File-size checks
# catch incomplete snapshots without rereading 16 GB on every worker startup.
# Loading safetensors/configs still validates their contents before readiness.
SNAPSHOT_FILES = {
    "model_index.json": 446,
    "scheduler/scheduler_config.json": 486,
    "text_encoder/config.json": 1536,
    "text_encoder/generation_config.json": 214,
    "text_encoder/model-00001-of-00002.safetensors": 4967215360,
    "text_encoder/model-00002-of-00002.safetensors": 3077766632,
    "text_encoder/model.safetensors.index.json": 32855,
    "tokenizer/added_tokens.json": 707,
    "tokenizer/chat_template.jinja": 4168,
    "tokenizer/merges.txt": 1671853,
    "tokenizer/special_tokens_map.json": 613,
    "tokenizer/tokenizer.json": 11422654,
    "tokenizer/tokenizer_config.json": 5404,
    "tokenizer/vocab.json": 2776833,
    "transformer/config.json": 541,
    "transformer/diffusion_pytorch_model.safetensors": 7751109744,
    "vae/config.json": 821,
    "vae/diffusion_pytorch_model.safetensors": 168120878,
}
# SHA256 of raw small metadata at MODEL_REVISION; weights are not authenticated.
# Source: huggingface.co/black-forest-labs/FLUX.2-klein-4B/resolve/<MODEL_REVISION>/<file>
METADATA_SHA256 = {
    'model_index.json': '51a76cb1cf3ed37423a1128c79c22faee8e6fbe7f5aaeb737f0a258930dbaac0',
    'scheduler/scheduler_config.json': '067afb012cef64553a763447d1efd93daeffcc0123ca7e25b09f8de20b90762e',
    'text_encoder/config.json': '214b4c29a0d975e9fddf9994a5673f22cb2c4c5750352f9227c2c3251ebeab40',
    'transformer/config.json': '09733c74a3da6d17dd0a0472a091a8950c7c6935889c32c16cc800ede05029de',
    'vae/config.json': '0d6dfb69ae95a5e2ac9836284bbb63d8b38ce67b25ba2dff380752b2a10ab948',
    'tokenizer/tokenizer_config.json': '443bfa629eb16387a12edbf92a76f6a6f10b2af3b53d87ba1550adfcf45f7fa0',
    'text_encoder/model.safetensors.index.json': '06b3d5319b6d76d1a4a2433419180016cfd54ed62d086a5e6567a809f8c82634',
    'text_encoder/generation_config.json': '4347b1aeed2b2b78bc059920a0b7f5fec71482e1344952b76d7665d638d71f13',
    'tokenizer/chat_template.jinja': 'a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8',
    'tokenizer/special_tokens_map.json': '76862e765266b85aa9459767e33cbaf13970f327a0e88d1c65846c2ddd3a1ecd',
    'tokenizer/added_tokens.json': 'c0284b582e14987fbd3d5a2cb2bd139084371ed9acbae488829a1c900833c680',
}
MAX_IMAGE_BYTES = 2_097_152
MAX_BASE64_CHARS = 2_796_204
MAX_ENVELOPE_BYTES = 4_194_304
QUALITIES = (95, 85, 75, 65, 55)
IMAGE_SIZES = (256, 512, 1024)
INPUT_KEYS = frozenset({
    "schema_version", "stage", "run_id", "invocation_id", "deadline_unix_ms",
    "prompt", "seed", "width", "height", "num_inference_steps",
    "guidance_scale", "output_format",
})


class WorkerFailure(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


def valid_uuid(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        return value if str(uuid.UUID(value)) == value.lower() else None
    except (ValueError, AttributeError):
        return None


def validate_input(job: Any, now_ms: int) -> dict[str, Any]:
    if not isinstance(job, dict) or not isinstance(job.get("input"), dict):
        raise WorkerFailure("INVALID_INPUT", "A Runpod job with an input object is required.")
    data = job["input"]
    if set(data) != INPUT_KEYS:
        raise WorkerFailure("INVALID_INPUT", "Required input fields are missing or unknown fields were supplied.")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise WorkerFailure("UNSUPPORTED_SCHEMA", "Only schema version 1 is supported.")
    if data["stage"] != "flux" or not all(valid_uuid(data[k]) for k in ("run_id", "invocation_id")):
        raise WorkerFailure("INVALID_INPUT", "The stage and correlation UUIDs must match the FLUX contract.")
    if type(data["deadline_unix_ms"]) is not int or not 1 <= data["deadline_unix_ms"] <= 9_007_199_254_740_991:
        raise WorkerFailure("INVALID_INPUT", "The deadline must be a positive JavaScript-safe integer.")
    if data["deadline_unix_ms"] <= now_ms:
        raise WorkerFailure("DEADLINE_EXCEEDED", "The generation deadline has expired.", True)
    prompt = data["prompt"]
    if not isinstance(prompt, str) or not prompt.strip():
        raise WorkerFailure("INVALID_INPUT", "A nonempty final prompt is required.")
    if len(prompt) > 4000:
        raise WorkerFailure("INPUT_TOO_LARGE", "The final prompt exceeds 4,000 characters.")
    # Lone UTF-16 surrogates cannot be transported as valid UTF-8 prompt text.
    try:
        prompt.encode("utf-8")
    except UnicodeEncodeError:
        raise WorkerFailure("INVALID_INPUT", "The prompt must contain valid Unicode text.") from None
    if type(data["seed"]) is not int or not 0 <= data["seed"] <= 4_294_967_295:
        raise WorkerFailure("INVALID_INPUT", "The seed is outside the allowed integer range.")
    if (type(data["width"]) is not int or type(data["height"]) is not int
            or data["width"] not in IMAGE_SIZES or data["height"] != data["width"]):
        raise WorkerFailure("INVALID_INPUT", "Use a square 256, 512 or 1024 image preset.")
    if type(data["num_inference_steps"]) is not int or data["num_inference_steps"] != 4:
        raise WorkerFailure("INVALID_INPUT", "Only the four-step preset is supported.")
    guidance = data["guidance_scale"]
    if type(guidance) not in (int, float) or guidance != 1:
        raise WorkerFailure("INVALID_INPUT", "Guidance scale must be 1.0.")
    if data["output_format"] not in ("webp", "jpeg"):
        raise WorkerFailure("INVALID_INPUT", "Output format must be webp or jpeg.")
    return dict(data)


@dataclass(frozen=True)
class RuntimeConfig:
    revision: str = MODEL_REVISION
    cache_dir: str = "/runpod-volume/huggingface/hub"
    local_files_only: bool = False
    startup_seconds: int = 600
    execution_seconds: int = 540
    profile: str = "runpod"
    cache_source: str = "volume"

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        profile = os.getenv("FLUX_RUNTIME_PROFILE", "runpod")
        if profile not in ("runpod", "dgx-spark"):
            raise WorkerFailure("MODEL_NOT_READY", "FLUX_RUNTIME_PROFILE must be runpod or dgx-spark.")
        revision = os.getenv("MODEL_REVISION", MODEL_REVISION)
        if revision != MODEL_REVISION:
            raise WorkerFailure("MODEL_NOT_READY", "MODEL_REVISION must match the worker's pinned checkpoint.")
        source = os.getenv("MODEL_CACHE_SOURCE", "volume")
        if source not in ("volume", "runpod-managed"):
            raise WorkerFailure("MODEL_NOT_READY", "MODEL_CACHE_SOURCE must be volume or runpod-managed.")
        legacy_offline = os.getenv("FLUX_LOCAL_FILES_ONLY")
        if legacy_offline is not None and legacy_offline not in ("0", "1"):
            raise WorkerFailure("MODEL_NOT_READY", "FLUX_LOCAL_FILES_ONLY must be 0 or 1.")
        offline = os.getenv("MODEL_CACHE_OFFLINE")
        if offline is not None and offline not in ("true", "false"):
            raise WorkerFailure("MODEL_NOT_READY", "MODEL_CACHE_OFFLINE must be true or false.")
        if offline is not None and legacy_offline is not None and (offline == "true") != (legacy_offline == "1"):
            raise WorkerFailure("MODEL_NOT_READY", "MODEL_CACHE_OFFLINE conflicts with FLUX_LOCAL_FILES_ONLY.")
        local_only = offline == "true" if offline is not None else legacy_offline == "1"
        values = []
        for key, default in (("FLUX_STARTUP_TIMEOUT_SECONDS", 600), ("FLUX_EXECUTION_TIMEOUT_SECONDS", 540)):
            try:
                value = int(os.getenv(key, str(default)))
            except ValueError:
                raise WorkerFailure("MODEL_NOT_READY", "Worker timeouts must be integer seconds.") from None
            if not 1 <= value <= 600:
                raise WorkerFailure("MODEL_NOT_READY", "Worker timeouts must be between 1 and 600 seconds.")
            values.append(value)
        cache_dir = RUNPOD_MANAGED_CACHE if source == "runpod-managed" else os.getenv("HF_HUB_CACHE", cls.cache_dir)
        return cls(revision=revision, cache_dir=cache_dir,
                   local_files_only=local_only, startup_seconds=values[0],
                   execution_seconds=values[1], profile=profile, cache_source=source)


@dataclass(frozen=True)
class RuntimeEvidence:
    machine: str
    torch_version: str
    cuda_version: str | None
    diffusers_version: str
    transformers_version: str
    cuda_available: bool
    bf16_supported: bool
    compute_capability: tuple[int, int] | None
    ngc_release: str | None = None


def inspect_runtime(torch, diffusers, transformers) -> RuntimeEvidence:
    cuda_available = torch.cuda.is_available()
    return RuntimeEvidence(
        machine=platform.machine().lower(), torch_version=torch.__version__,
        cuda_version=torch.version.cuda, diffusers_version=diffusers.__version__,
        transformers_version=transformers.__version__, cuda_available=cuda_available,
        bf16_supported=cuda_available and torch.cuda.is_bf16_supported(),
        compute_capability=tuple(torch.cuda.get_device_capability()) if cuda_available else None,
        ngc_release=os.getenv("NVIDIA_PYTORCH_VERSION"),
    )


def validate_runtime_profile(profile: str, evidence: RuntimeEvidence) -> None:
    if not evidence.cuda_available or not evidence.bf16_supported:
        raise WorkerFailure("UNSUPPORTED_RUNTIME", "A CUDA GPU with BF16 support is required.")
    if (evidence.diffusers_version, evidence.transformers_version) != ("0.41.0", "5.20.2"):
        raise WorkerFailure("UNSUPPORTED_RUNTIME", "The installed inference libraries do not match the pinned runtime.")
    if evidence.torch_version != "2.13.0+cu129" or evidence.cuda_version != "12.9":
        raise WorkerFailure("UNSUPPORTED_RUNTIME", "The runtime requires PyTorch 2.13.0+cu129 with CUDA 12.9.")
    if profile == "runpod":
        if evidence.machine not in ("x86_64", "amd64"):
            raise WorkerFailure("UNSUPPORTED_RUNTIME", "The Runpod profile requires AMD64.")
    elif profile == "dgx-spark":
        if evidence.machine not in ("aarch64", "arm64") or evidence.compute_capability != (12, 1):
            raise WorkerFailure("UNSUPPORTED_RUNTIME", "The DGX Spark profile requires ARM64 and GB10 compute capability 12.1.")
    else:
        raise WorkerFailure("UNSUPPORTED_RUNTIME", "The selected runtime profile is unsupported.")


def probe_bf16_kernel(torch) -> None:
    """Real kernel check before any model-weight download/load."""
    try:
        with torch.inference_mode():
            probe = torch.ones((8, 8), device="cuda", dtype=torch.bfloat16)
            torch.mm(probe, probe)
            torch.cuda.synchronize()
            del probe
    except Exception as exc:
        failure = classify_inference_error(exc)
        if failure.code == "GPU_OOM":
            raise failure from None
        raise WorkerFailure("UNSUPPORTED_RUNTIME", "The pinned runtime failed its BF16 GPU kernel preflight.") from None


def preflight_runtime(config: RuntimeConfig, torch, diffusers, transformers) -> RuntimeEvidence:
    evidence = inspect_runtime(torch, diffusers, transformers)
    validate_runtime_profile(config.profile, evidence)
    if not features.check("webp"):
        raise WorkerFailure("UNSUPPORTED_RUNTIME", "The image runtime requires WebP support.")
    probe_bf16_kernel(torch)
    return evidence


class Deadline:
    def __init__(self, unix_ms: int, seconds: int, *, wall: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic, cancelled: threading.Event | None = None):
        self.wall, self.monotonic, self.unix_ms = wall, monotonic, unix_ms
        self.end = monotonic() + seconds
        self.cancelled = cancelled

    def remaining(self) -> float:
        return min(self.unix_ms / 1000 - self.wall(), self.end - self.monotonic())

    def check(self) -> None:
        if self.cancelled is not None and self.cancelled.is_set():
            raise WorkerFailure("CANCELLED", "The worker is shutting down.", True)
        if self.remaining() <= 0:
            raise WorkerFailure("DEADLINE_EXCEEDED", "The generation deadline has expired.", True)


class CancellationState:
    def __init__(self, *events: threading.Event | None):
        self.events = tuple(event for event in events if event is not None)

    def is_set(self) -> bool:
        return any(event.is_set() for event in self.events)


class Engine(Protocol):
    metadata: dict[str, Any]
    def validate_prompt(self, prompt: str) -> None: ...
    def generate(self, data: dict[str, Any], deadline: Deadline) -> list[Image.Image]: ...
    def cleanup(self) -> None: ...


def classify_inference_error(exc: Exception) -> WorkerFailure:
    if isinstance(exc, WorkerFailure):
        return exc
    # torch is imported only by the live adapter, so CPU tests do not need it.
    name, message = type(exc).__name__, str(exc).lower()
    if name == "OutOfMemoryError" or "cuda out of memory" in message:
        return WorkerFailure("GPU_OOM", "GPU memory was exhausted; review the worker configuration.")
    if "no kernel image" in message or "invalid device function" in message:
        return WorkerFailure("UNSUPPORTED_RUNTIME", "The GPU and pinned CUDA runtime are incompatible.")
    if isinstance(exc, TimeoutError):
        return WorkerFailure("INFERENCE_OUTCOME_UNKNOWN", "Inference completion could not be confirmed.", True)
    return WorkerFailure("INFERENCE_FAILED", "Image inference failed; inspect the worker deployment logs.", True)


def encode_image(image: Image.Image, output_format: str, deadline: Deadline,
                 *, byte_limit: int = MAX_IMAGE_BYTES,
                 expected_size: tuple[int, int] = (1024, 1024)) -> tuple[bytes, str]:
    if not isinstance(image, Image.Image) or image.size != expected_size:
        raise WorkerFailure("INVALID_MODEL_OUTPUT", "The model did not return the requested image dimensions.")
    fmt, mime = ("WEBP", "image/webp") if output_format == "webp" else ("JPEG", "image/jpeg")
    try:
        with image.convert("RGB") as rgb:
            for quality in QUALITIES:
                deadline.check()
                buffer = io.BytesIO()
                options = {"quality": quality}
                options.update({"method": 4} if fmt == "WEBP" else {"optimize": True})
                rgb.save(buffer, format=fmt, **options)
                raw = buffer.getvalue()
                if len(raw) > byte_limit:
                    continue
                # Validate the bytes, rather than trusting the encoder's settings.
                with Image.open(io.BytesIO(raw)) as decoded:
                    if decoded.format != fmt or decoded.size != expected_size:
                        raise WorkerFailure("INVALID_MODEL_OUTPUT", "Encoded image metadata is invalid.")
                    decoded.verify()
                with Image.open(io.BytesIO(raw)) as decoded:
                    decoded.load()
                deadline.check()
                return raw, mime
    except WorkerFailure:
        raise
    except (OSError, ValueError, TypeError):
        raise WorkerFailure("INVALID_MODEL_OUTPUT", "The generated image could not be encoded and verified.") from None
    raise WorkerFailure("OUTPUT_TOO_LARGE", "The image exceeds the transport size limit after bounded compression.")


class Worker:
    # These failures invalidate the resident engine across both transports.
    # Input errors, bounded cancellation and memory pressure remain job outcomes.
    FATAL_CODES = frozenset({"UNSUPPORTED_RUNTIME", "MODEL_NOT_READY", "MODEL_ACCESS_DENIED",
                             "INFERENCE_OUTCOME_UNKNOWN"})

    def __init__(self, engine: Engine | None, *, execution_seconds: int = 540,
                 wall: Callable[[], float] = time.time, monotonic: Callable[[], float] = time.monotonic,
                 cancelled: threading.Event | None = None):
        self.engine, self.execution_seconds = engine, execution_seconds
        self.wall, self.monotonic = wall, monotonic
        self.cancelled = cancelled
        self.lock = threading.Lock()
        self._ready = threading.Event()
        if engine is not None:
            self._ready.set()

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def close(self) -> None:
        # Do not synchronize CUDA during shutdown: transport watchdogs bound
        # native work by terminating the process when cooperative stop fails.
        self._ready.clear()

    def handle(self, job: Any, *, cancelled: threading.Event | None = None) -> dict[str, Any]:
        source = job.get("input", {}) if isinstance(job, dict) else {}
        source = source if isinstance(source, dict) else {}
        result = {
            "schema_version": 1, "stage": "flux", "run_id": valid_uuid(source.get("run_id")),
            "invocation_id": valid_uuid(source.get("invocation_id")), "ok": False,
            "model": dict(self.engine.metadata) if self.engine else {
                "id": MODEL_ID, "revision": None, "precision": None, "runtime": None},
            "data": None, "usage": {"image_count": 0}, "timings": {"inference_ms": 0},
            "counters": {"inference_attempts": 0, "inference_retries": 0, "dependency_retries": 0},
            "error": None,
        }
        images = None
        locked = False
        admitted = False
        try:
            data = validate_input(job, int(self.wall() * 1000))
            deadline = Deadline(data["deadline_unix_ms"], self.execution_seconds,
                                wall=self.wall, monotonic=self.monotonic,
                                cancelled=CancellationState(self.cancelled, cancelled))
            deadline.check()
            if not self.ready:
                raise WorkerFailure("MODEL_NOT_READY", "The image model is not ready.", True)
            # An endpoint is configured for concurrency one; reject accidental overlap.
            locked = self.lock.acquire(blocking=False)
            if not locked:
                raise WorkerFailure("DEPENDENCY_UNAVAILABLE", "The image worker is already processing a job.", True)
            # A previous job may have invalidated the engine after our first
            # readiness check but before this lock was acquired.
            if not self.ready:
                raise WorkerFailure("MODEL_NOT_READY", "The image model is not ready.", True)
            admitted = True
            self.engine.validate_prompt(data["prompt"])
            deadline.check()
            # One GPU dispatch per request. A timeout cannot prove that native
            # work stopped, so the handler never automatically replays inference.
            result["counters"]["inference_attempts"] = 1
            started = self.monotonic()
            outcome_unknown = False
            try:
                images = self.engine.generate(data, deadline)
            except Exception as exc:
                failure = classify_inference_error(exc)
                outcome_unknown = failure.code == "INFERENCE_OUTCOME_UNKNOWN"
                raise failure from None
            finally:
                result["timings"]["inference_ms"] = (
                    None if outcome_unknown
                    else max(0, (self.monotonic() - started) * 1000)
                )
            deadline.check()
            if not isinstance(images, (list, tuple)) or len(images) != 1:
                raise WorkerFailure("INVALID_MODEL_OUTPUT", "The model must return exactly one image.")
            raw, mime = encode_image(images[0], data["output_format"], deadline,
                                     expected_size=(data["width"], data["height"]))
            encoded = base64.b64encode(raw).decode("ascii")
            if len(encoded) > MAX_BASE64_CHARS:
                raise WorkerFailure("OUTPUT_TOO_LARGE", "The encoded image exceeds the transport size limit.")
            result["data"] = {"image_base64": encoded, "mime_type": mime, "width": data["width"],
                              "height": data["height"], "seed": data["seed"], "image_bytes": len(raw)}
            result["ok"], result["usage"]["image_count"] = True, 1
            if len(json.dumps({"result": result}, ensure_ascii=False).encode("utf-8")) > MAX_ENVELOPE_BYTES:
                raise WorkerFailure("OUTPUT_TOO_LARGE", "The worker response exceeds the transport size limit.")
            deadline.check()
        except WorkerFailure as exc:
            result.update(ok=False, data=None, error={"code": exc.code, "message": exc.message, "retryable": exc.retryable})
            result["usage"]["image_count"] = 0
        except Exception as exc:
            # Unknown validation/encoding failures never trigger automatic inference.
            error = classify_inference_error(exc)
            result.update(ok=False, data=None, error={"code": error.code, "message": error.message, "retryable": error.retryable})
            result["usage"]["image_count"] = 0
        finally:
            failure_code = (result.get("error") or {}).get("code")
            if failure_code in self.FATAL_CODES:
                # Latch before releasing admission, including when the SDK has
                # not yet consumed its request to replace this worker.
                self._ready.clear()
            if isinstance(images, (list, tuple)):
                for image in images:
                    if isinstance(image, Image.Image):
                        image.close()
            if locked:
                try:
                    # Unknown native work must reach the transport's hard exit
                    # without another potentially blocking CUDA operation.
                    if admitted and failure_code != "INFERENCE_OUTCOME_UNKNOWN":
                        self.engine.cleanup()
                except Exception as exc:
                    # Preserve the known outcome, but do not admit more work on
                    # an engine whose hooks or CUDA state could not be cleaned.
                    self._ready.clear()
                    logging.error("FLUX cleanup failed exception_type=%s", type(exc).__name__)
                self.lock.release()
        return result


def classify_startup_error(exc: Exception) -> WorkerFailure:
    if isinstance(exc, WorkerFailure):
        return exc
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return WorkerFailure("MODEL_ACCESS_DENIED", "Model access was denied; check the worker's model-access configuration.")
    if status == 429 or (isinstance(status, int) and status >= 500):
        return WorkerFailure("DEPENDENCY_UNAVAILABLE", "Model download service is temporarily unavailable.", True)
    if isinstance(exc, (ConnectionError, TimeoutError)) or type(exc).__name__ in {
        "ConnectTimeout", "ReadTimeout", "ConnectError", "ReadTimeoutError", "ConnectionError", "TimeoutException",
    }:
        return WorkerFailure("DEPENDENCY_UNAVAILABLE", "Model download service could not be reached.", True)
    if isinstance(exc, OSError) and exc.errno in (errno.ECONNRESET, errno.ETIMEDOUT):
        return WorkerFailure("DEPENDENCY_UNAVAILABLE", "Model download was interrupted.", True)
    return WorkerFailure("MODEL_NOT_READY", "The pinned model could not be initialized; check cache and runtime configuration.")


def download_with_retries(download: Callable[[], str], deadline: Deadline, *,
                          sleep: Callable[[float], None] = time.sleep,
                          jitter: Callable[[], float] = random.random) -> str:
    for attempt in range(3):
        deadline.check()
        try:
            path = download()
            deadline.check()
            return path
        except Exception as exc:
            failure = classify_startup_error(exc)
            if not failure.retryable or attempt == 2:
                raise failure from None
            delay = min(2 ** attempt + jitter(), 3)
            deadline.check()
            if deadline.remaining() <= delay:
                raise WorkerFailure("DEADLINE_EXCEEDED", "Model initialization exceeded its deadline.")
            sleep(delay)
    raise AssertionError("unreachable")


def verify_snapshot(snapshot: Path) -> str:
    """Check every selected file, including all weight shards, without GPU work.

    Hub snapshots can exist while interrupted downloads leave dangling symlinks,
    missing files or truncated blobs. A cached directory alone is not readiness.
    Small routing metadata is authenticated against the pinned revision; weights
    receive size checks only. Cache writers remain trusted: startup validation
    cannot eliminate concurrent replacement between checking and model loading.
    """
    try:
        resolved = snapshot.resolve(strict=True)
        blobs = snapshot.parent.parent / "blobs"
        if snapshot.is_symlink() or blobs.is_symlink():
            raise WorkerFailure("MODEL_NOT_READY", "Snapshot and blob directories must not be symbolic links.")
        for item in snapshot.rglob("*"):
            # The local diffusers resolver can import None.py or a component
            # library.py even with an explicit pipeline class and offline mode.
            if item.suffix.lower() == ".py" or (item.is_symlink() and item.is_dir()):
                raise WorkerFailure("MODEL_NOT_READY", "Executable checkpoint cache entries are unsupported.")
            if item.is_symlink():
                target = item.resolve(strict=True)
                if not target.is_relative_to(resolved) and not target.is_relative_to(blobs.resolve()):
                    raise WorkerFailure("MODEL_NOT_READY", "Checkpoint cache link escapes the snapshot/blob store.")
        # Reject FIFOs/devices before opening metadata; a bounded read alone can
        # still block forever on a named pipe. Ordinary Hub blob links work.
        if any(not (snapshot / name).is_file() or (snapshot / name).stat().st_size != size
               for name, size in SNAPSHOT_FILES.items()):
            raise WorkerFailure("MODEL_NOT_READY", "The exact pinned model snapshot is missing or incomplete; prefetch it before offline startup.")
        for name, digest in METADATA_SHA256.items():
            with (snapshot / name).open("rb") as metadata:
                if hashlib.sha256(metadata.read(65_537)).hexdigest() != digest:
                    raise WorkerFailure("MODEL_NOT_READY", "Checkpoint routing metadata differs from the reviewed revision.")
    except (OSError, RuntimeError):
        raise WorkerFailure("MODEL_NOT_READY", "The exact pinned model snapshot cannot be read.") from None
    return str(snapshot)


def resolve_model_snapshot(config: RuntimeConfig, deadline: Deadline, download: Callable[..., str]) -> str:
    """Resolve only the fixed checkpoint; offline misses never contact the Hub."""
    if config.revision != MODEL_REVISION:
        raise WorkerFailure("MODEL_NOT_READY", "MODEL_REVISION must match the worker's pinned checkpoint.")
    expected = (Path(config.cache_dir).expanduser().absolute()
                / ("models--" + MODEL_ID.replace("/", "--")) / "snapshots" / MODEL_REVISION)
    if config.local_files_only:
        verify_snapshot(expected)
    path = download_with_retries(lambda: download(
        repo_id=MODEL_ID, revision=MODEL_REVISION, cache_dir=config.cache_dir,
        token=os.getenv("HF_TOKEN"), local_files_only=config.local_files_only, max_workers=2,
        allow_patterns=["model_index.json", "scheduler/*", "tokenizer/*", "text_encoder/*", "transformer/*", "vae/*"],
    ), deadline)
    if Path(path).expanduser().absolute() != expected:
        raise WorkerFailure("MODEL_NOT_READY", "The resolved snapshot does not match the exact pinned cache location.")
    return verify_snapshot(expected)


class DiffusersEngine:
    """Live GPU adapter. All model/network initialization precedes SDK readiness."""
    def __init__(self, config: RuntimeConfig):
        from huggingface_hub import snapshot_download

        startup = Deadline(int((time.time() + config.startup_seconds) * 1000), config.startup_seconds)
        model_path = resolve_model_snapshot(config, startup, snapshot_download)
        startup.check()
        import torch
        import diffusers
        import transformers
        from diffusers import Flux2KleinPipeline

        evidence = preflight_runtime(config, torch, diffusers, transformers)
        self.torch = torch
        self.pipeline = Flux2KleinPipeline.from_pretrained(
            model_path, torch_dtype=torch.bfloat16, use_safetensors=True, local_files_only=True,
        ).to("cuda")
        if not self.pipeline.config.is_distilled:
            raise WorkerFailure("MODEL_NOT_READY", "The checkpoint must be the distilled FLUX.2 klein model.")
        if any(component.dtype != torch.bfloat16 for component in (
            self.pipeline.transformer, self.pipeline.text_encoder, self.pipeline.vae,
        )):
            raise WorkerFailure("UNSUPPORTED_RUNTIME", "Loaded model components do not match the BF16 preset.")
        startup.check()
        self.pipeline.set_progress_bar_config(disable=True)
        self.metadata = {
            "id": MODEL_ID, "revision": config.revision,
            "precision": str(self.pipeline.transformer.dtype).removeprefix("torch."),
            "runtime": f"diffusers/{diffusers.__version__};torch/{torch.__version__};transformers/{transformers.__version__};cuda/{torch.version.cuda};profile/{config.profile};architecture/{evidence.machine}",
        }

    def validate_prompt(self, prompt: str) -> None:
        tokenizer = self.pipeline.tokenizer
        rendered = tokenizer.apply_chat_template(
            [{"role": "user", "content": prompt}], tokenize=False,
            add_generation_prompt=True, enable_thinking=False,
        )
        ids = tokenizer(rendered, truncation=False, add_special_tokens=True)["input_ids"]
        if len(ids) > 512:
            raise WorkerFailure("INPUT_TOO_LARGE", "The final prompt exceeds the image model's 512-token context budget.")

    def generate(self, data: dict[str, Any], deadline: Deadline) -> list[Image.Image]:
        def on_step_end(_pipeline, _step, _timestep, callback_kwargs):
            deadline.check()
            return callback_kwargs
        deadline.check()
        generator = self.torch.Generator(device="cuda").manual_seed(data["seed"])
        with self.torch.inference_mode():
            output = self.pipeline(
                prompt=data["prompt"], width=data["width"], height=data["height"],
                num_inference_steps=4, guidance_scale=1.0, num_images_per_prompt=1,
                generator=generator, output_type="pil", max_sequence_length=512,
                callback_on_step_end=on_step_end,
            )
            # CUDA dispatch is asynchronous; timing and retry decisions need completion.
            self.torch.cuda.synchronize()
        return output.images

    def cleanup(self) -> None:
        self.pipeline.maybe_free_model_hooks()
        # The local gateway shares a GPU with Qwen. Return unused allocations
        # between jobs even though retaining the allocator cache can be faster.
        self.torch.cuda.empty_cache()
