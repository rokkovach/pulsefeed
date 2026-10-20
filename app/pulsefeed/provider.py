"""Bounded provider transport and validation at the untrusted result boundary.

Read-only status calls may retry transient failures. Job submission is deliberately
single-attempt because a lost response does not mean the provider rejected work.
Validate identity, accounting, and decoded image bytes before accepting success.
"""
import asyncio
import base64
import copy
import io
import json
import math
import random
import time
from email.utils import parsedate_to_datetime
from urllib.parse import quote
import httpx
from PIL import Image, ImageDraw
from .errors import AppError

MAX_IMAGE_BYTES = 2097152
MAX_BASE64 = 2796204
MAX_OUTPUT_BYTES = 4194304
MAX_READ_RETRY_DELAY = 10.0
MODELS = {"qwen": "nvidia/Qwen3-8B-NVFP4", "flux": "black-forest-labs/FLUX.2-klein-4B"}
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}
ERROR_CODES = {"INVALID_INPUT", "INPUT_TOO_LARGE", "UNSUPPORTED_SCHEMA", "MODEL_NOT_READY", "MODEL_ACCESS_DENIED", "UNSUPPORTED_RUNTIME", "DEPENDENCY_UNAVAILABLE", "GPU_OOM", "INFERENCE_FAILED", "INFERENCE_OUTCOME_UNKNOWN", "INVALID_MODEL_OUTPUT", "OUTPUT_TOO_LARGE", "DEADLINE_EXCEEDED", "CANCELLED"}


class SubmissionUnknown(Exception):
    pass


class ReadUnavailable(Exception):
    pass


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def nullable_metric(value):
    return value is None or finite_number(value)


def validate_envelope(output, invocation, request_input, decode_image=True):
    """Validate before persisting metadata or returning bytes. Reject false success."""
    try:
        if not isinstance(output, dict) or len(json.dumps(output, ensure_ascii=False, allow_nan=False).encode()) > MAX_OUTPUT_BYTES:
            raise ValueError("Envelope size/type")
        required = {"schema_version", "stage", "run_id", "invocation_id", "ok", "model", "data", "usage", "timings", "counters", "error"}
        if set(output) != required:
            raise ValueError("Envelope fields")
        if type(output["schema_version"]) is not int or output["schema_version"] != 1:
            raise ValueError("Schema version")
        for key in ("stage", "run_id", "invocation_id"):
            expected = invocation["id"] if key == "invocation_id" else invocation[key]
            if output[key] != expected:
                raise ValueError("Correlation mismatch")
        stage = invocation["stage"]
        model = output["model"]
        if not isinstance(model, dict) or set(model) != {"id", "revision", "precision", "runtime"} or model["id"] != MODELS[stage]:
            raise ValueError("Model identity")
        if any(value is not None and not isinstance(value, str) for value in model.values()):
            raise ValueError("Model metadata")
        if type(output["ok"]) is not bool:
            raise ValueError("Success flag")
        counters = output["counters"]
        if not isinstance(counters, dict) or set(counters) != {"inference_attempts", "inference_retries", "dependency_retries"}:
            raise ValueError("Counters")
        for value in counters.values():
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Counter value")
        attempts, retries = counters["inference_attempts"], counters["inference_retries"]
        dependency_retries = counters["dependency_retries"]
        if any(value is not None and value > 1 for value in (retries, dependency_retries)):
            raise ValueError("Retry cap")
        if retries is not None and dependency_retries is not None and retries + dependency_retries > 1:
            raise ValueError("Shared retry budget")
        if attempts is not None and attempts > 2:
            raise ValueError("Attempt cap")
        if attempts is not None and retries is not None and retries != max(0, attempts-1):
            raise ValueError("Retry accounting")
        if not isinstance(output["timings"], dict) or set(output["timings"]) != {"inference_ms"} or not nullable_metric(output["timings"]["inference_ms"]):
            raise ValueError("Timings")
        usage = output["usage"]
        if stage == "qwen":
            if not isinstance(usage, dict) or set(usage) != {"input_tokens", "output_tokens", "total_tokens"}:
                raise ValueError("Qwen usage")
            if any(v is not None and (type(v) is not int or v < 0) for v in usage.values()):
                raise ValueError("Tokens")
            if usage["total_tokens"] is not None and (usage["input_tokens"] is None or usage["output_tokens"] is None or usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]):
                raise ValueError("Total tokens")
        elif not isinstance(usage, dict) or set(usage) != {"image_count"} or type(usage["image_count"]) is not int or usage["image_count"] != (1 if output["ok"] else 0):
            raise ValueError("Image usage")
        if not output["ok"]:
            error = output["error"]
            if output["data"] is not None or not isinstance(error, dict) or set(error) != {"code", "message", "retryable"} or type(error["retryable"]) is not bool or not isinstance(error["code"], str) or error["code"] not in ERROR_CODES or not isinstance(error["message"], str) or not error["message"]:
                raise ValueError("Error payload")
            return None
        if output["error"] is not None or attempts == 0 or any(not isinstance(model[key], str) or not model[key] for key in model):
            raise ValueError("Success metadata")
        data = output["data"]
        if stage == "qwen":
            if not isinstance(data, dict) or set(data) != {"prompt"}:
                raise ValueError("Prompt data")
            prompt = data["prompt"]
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000 or "<think" in prompt.lower() or "</think" in prompt.lower() or prompt.lstrip().startswith(("{", "[")):
                raise ValueError("Prompt output")
            return None
        if not isinstance(data, dict) or set(data) != {"image_base64", "mime_type", "width", "height", "seed", "image_bytes"}:
            raise ValueError("Image data")
        if data["width"] not in (256,512,1024) or data["height"] != data["width"]:
            raise ValueError("Image dimensions")
        encoded = data["image_base64"]
        if not isinstance(encoded, str) or len(encoded) > MAX_BASE64:
            raise ValueError("Encoded image size")
        for key in ("width", "height", "seed"):
            if type(data[key]) is not int or data[key] != request_input[key]:
                raise ValueError("Image preset")
        if data["mime_type"] != {"webp": "image/webp", "jpeg": "image/jpeg"}[request_input["output_format"]]:
            raise ValueError("Image MIME")
        image_bytes = base64.b64decode(encoded, validate=True)
        if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES or type(data["image_bytes"]) is not int or len(image_bytes) != data["image_bytes"]:
            raise ValueError("Image bytes")
        # Select only contract-supported decoders before reading image headers;
        # checking image.format afterward still invokes other format plugins.
        with Image.open(io.BytesIO(image_bytes),formats=["JPEG","WEBP"]) as image:
            if image.size != (data["width"], data["height"]) or image.format not in ("WEBP", "JPEG") or Image.MIME[image.format] != data["mime_type"]:
                raise ValueError("Decoded image metadata")
            image.load()  # Header verification alone accepts truncated JPEG scan data.
        return image_bytes if decode_image else None
    except (ValueError, KeyError, TypeError, OSError, OverflowError, UnicodeError) as exc:
        raise AppError("INVALID_MODEL_OUTPUT", "The model returned an invalid result.", 502) from exc


def unwrap_output(value):
    if not isinstance(value, dict) or set(value) != {"result"}:
        raise AppError("INVALID_MODEL_OUTPUT", "Provider output must contain the documented result wrapper.", 502)
    try:
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_OUTPUT_BYTES:
            raise ValueError("Output size")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise AppError("INVALID_MODEL_OUTPUT", "Provider output is malformed or oversized.", 502) from exc
    return value["result"]


def metadata_only(output):
    result = copy.deepcopy(output)
    if isinstance(result.get("data"), dict):
        result["data"].pop("image_base64", None)
    return result


class RunpodAdapter:
    def __init__(self, config, client=None, sleep=asyncio.sleep):
        self.key = config.runpod_key
        self.endpoints = {"qwen": config.qwen_endpoint, "flux": config.flux_endpoint}
        self.client = client or httpx.AsyncClient(timeout=30, follow_redirects=False)
        self.sleep = sleep
    def authorization(self):
        return self.key
    def url(self, stage, operation):
        return f"https://api.runpod.ai/v2/{quote(self.endpoints[stage], safe='')}/{operation}"
    async def request(self, method, stage, operation, body=None):
        async with asyncio.timeout(30):
            return await self._request(method, stage, operation, body)
    async def _request(self, method, stage, operation, body=None):
        async with self.client.stream(method, self.url(stage, operation), headers={"Authorization": self.authorization()}, json=body) as response:
            if response.status_code >= 400:
                response.raise_for_status()
            result = bytearray()
            async for chunk in response.aiter_bytes():
                result.extend(chunk)
                if len(result) > MAX_OUTPUT_BYTES + 65536:
                    raise AppError("OUTPUT_TOO_LARGE", "Provider response exceeds the transport limit.", 502)
            return json.loads(result)
    async def submit(self, stage, payload):
        try:
            result = await self.request("POST", stage, "run", {"input": payload})
            if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
                raise SubmissionUnknown()
            return result["id"]
        except httpx.HTTPStatusError as exc:
            # Only a definitive authentication/validation rejection is known not accepted.
            # Timeouts, server failures and malformed acceptance responses must
            # retain reservations: the provider may already be spending GPU time.
            if exc.response.status_code in (400, 401, 403, 422):
                raise AppError("PROVIDER_REJECTED", "The inference provider rejected the request. Check endpoint configuration.", 502) from exc
            raise SubmissionUnknown() from exc
        except (httpx.TransportError, ValueError, AppError, asyncio.TimeoutError) as exc:
            raise SubmissionUnknown() from exc
    async def safe_read(self, stage, operation):
        for attempt in range(4):
            try:
                result = await self.request("GET", stage, operation)
                if not isinstance(result, dict):
                    raise ReadUnavailable()
                return result
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 404:
                    return {"status": "MISSING"}
                if status != 429 and status < 500:
                    raise ReadUnavailable() from exc
                retry_after = exc.response.headers.get("Retry-After", "")
                try:
                    delay = float(retry_after)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
                    except (ValueError, TypeError, OverflowError):
                        delay = 2 ** attempt + random.random()
            except (httpx.TransportError, ValueError, asyncio.TimeoutError):
                delay = 2 ** attempt + random.random()
            # Provider hints cannot hold a reader request or stage slot forever.
            # Nonfinite values are malformed hints, not valid sleep durations.
            if not math.isfinite(delay):
                delay = 2 ** attempt + random.random()
            delay = min(MAX_READ_RETRY_DELAY, max(0, delay))
            if attempt == 3:
                raise ReadUnavailable()
            await self.sleep(delay)
        raise ReadUnavailable()
    async def status(self, stage, provider_id):
        return await self.safe_read(stage, "status/" + quote(provider_id, safe=""))
    async def cancel(self, stage, provider_id):
        try:
            # Cancellation is not evidence of termination; caller polls status afterward.
            return await self.request("POST", stage, "cancel/" + quote(provider_id, safe=""))
        except (httpx.HTTPError, ValueError, AppError, asyncio.TimeoutError):
            return None
    async def close(self):
        await self.client.aclose()


class LocalHTTPAdapter(RunpodAdapter):
    """Static loopback worker origins, with the same native asynchronous job protocol."""
    def __init__(self, config, client=None, sleep=asyncio.sleep):
        config.validate()
        self.key = config.local_worker_token
        self.endpoints = {"qwen":config.local_qwen_url.rstrip("/"),"flux":config.local_flux_url.rstrip("/")}
        self.client = client or httpx.AsyncClient(timeout=30,follow_redirects=False,trust_env=False)
        self.sleep = sleep

    def authorization(self):
        return "Bearer "+self.key

    def url(self, stage, operation):
        return self.endpoints[stage]+"/"+operation


class FakeRunpodAdapter:
    """Explicit local-only synthetic inference. No real model evidence or stored images."""
    def __init__(self):
        self.jobs = {}
        self.submissions = []
        self.behavior = {}
        self.delay = 0.15
    async def submit(self, stage, payload):
        from .db import new_id
        self.submissions.append((stage, copy.deepcopy(payload)))
        if self.behavior.get(stage) == "unknown":
            raise SubmissionUnknown()
        job_id = "fake-" + new_id()
        self.jobs[job_id] = {"payload": copy.deepcopy(payload), "created": time.time(), "cancelled": False}
        return job_id
    async def status(self, stage, provider_id):
        job = self.jobs.get(provider_id)
        if not job:
            return {"status": "MISSING"}
        if job["cancelled"]:
            return {"id": provider_id, "status": "CANCELLED"}
        if self.behavior.get(stage) == "pending" or time.time()-job["created"] < self.delay:
            return {"id": provider_id, "status": "IN_PROGRESS"}
        payload = job["payload"]
        output = {key: payload[key] for key in ("schema_version", "stage", "run_id", "invocation_id")}
        output.update(ok=True, model={"id": MODELS[stage], "revision": "synthetic-fixture", "precision": "synthetic", "runtime": "fake-adapter-v1"}, data=None, usage={}, timings={"inference_ms": 0}, counters={"inference_attempts": 1, "inference_retries": 0, "dependency_retries": 0}, error=None)
        if stage == "qwen":
            output["data"] = {"prompt": "A textured editorial illustration inspired by " + payload["article"]["title"][:120] + ", warm light and a balanced composition, no text."}
            output["usage"] = {"input_tokens": None, "output_tokens": None, "total_tokens": None}
        else:
            image = Image.new("RGB", (payload["width"],payload["height"]), "#eee6d5")
            draw = ImageDraw.Draw(image)
            scale = payload["width"] / 1024
            draw.ellipse(tuple(int(x*scale) for x in (200,140,840,780)), fill="#92aa8b")
            draw.rectangle(tuple(int(x*scale) for x in (130,610,890,920)), fill="#ce745d")
            draw.text((40,40), "SYNTHETIC DEMO - NO MODEL INFERENCE", fill="#263c31")
            stream = io.BytesIO()
            image.save(stream, format=payload["output_format"].upper(), quality=85)
            raw = stream.getvalue()
            output["data"] = {"image_base64": base64.b64encode(raw).decode(), "mime_type": "image/" + payload["output_format"], "width":payload["width"],"height":payload["height"],"seed":payload["seed"],"image_bytes":len(raw)}
            output["usage"] = {"image_count": 1}
        if self.behavior.get(stage) == "failure":
            output.update(ok=False, data=None, error={"code":"INFERENCE_FAILED", "message":"Simulated permanent worker failure.","retryable":False})
            if stage == "flux":
                output["usage"] = {"image_count":0}
        elif self.behavior.get(stage) == "malformed":
            output["run_id"] = "wrong"
        return {"id": provider_id,"status":"COMPLETED", "delayTime":0,"executionTime":0,"output":{"result":output}}
    async def cancel(self, stage, provider_id):
        if provider_id in self.jobs:
            self.jobs[provider_id]["cancelled"] = True
        return {"status":"CANCELLED"}
    async def close(self):
        pass
