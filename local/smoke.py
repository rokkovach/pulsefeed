"""Operator-invoked real Spark HTTP smoke test. Never prints credentials or image bytes."""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import time
import uuid
from http.client import RemoteDisconnected
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MODELS = {"qwen": "nvidia/Qwen3-8B-NVFP4", "flux": "black-forest-labs/FLUX.2-klein-4B"}
REVISIONS = {"qwen": "ccd10a893cbca613259517c3efe08e151ddf2b8e", "flux": "e7b7dc27f91deacad38e78976d1f2b499d76a294"}
MAX_OUTPUT = 4 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class Endpoint:
    def __init__(self, url, token):
        parsed = urlsplit(url)
        require(parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username
                and not parsed.password and not parsed.query and not parsed.fragment,
                "Worker URL must be a configured HTTP(S) origin without credentials/query/fragment.")
        self.url, self.token = url.rstrip("/"), token
        # Explicit endpoint traffic must not use an ambient proxy or follow redirects.
        from urllib.request import ProxyHandler
        self.client = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        request = Request(self.url + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with self.client.open(request, timeout=10) as response:
            raw = response.read(MAX_OUTPUT + 65_537)
        require(len(raw) <= MAX_OUTPUT + 65_536, "Provider response exceeds transport limit.")
        value = json.loads(raw)
        require(isinstance(value, dict), "Expected a JSON object.")
        return value

    def ready(self, seconds):
        end = time.monotonic() + seconds
        while True:
            try:
                response = self.request("GET", "/health")
                require(response.get("status") == "ready", "Unexpected health response.")
                return
            except HTTPError as exc:
                if exc.code not in {503}:
                    raise RuntimeError(f"Readiness rejected with HTTP {exc.code}.") from None
            except (URLError, TimeoutError, RemoteDisconnected, ConnectionResetError):
                pass
            require(time.monotonic() < end, "Timed out waiting for worker readiness.")
            time.sleep(3)

    def invoke(self, payload):
        # One POST only. Ambiguous acceptance must never be automatically resubmitted.
        submitted = self.request("POST", "/run", {"input": payload})
        job_id = submitted.get("id")
        require(isinstance(job_id, str) and 0 < len(job_id) < 256, "Submission returned no valid job ID; do not resubmit blindly.")
        path = "/status/" + quote(job_id, safe="")
        end = time.monotonic() + max(1, (payload["deadline_unix_ms"] / 1000 - time.time()))
        try:
            while True:
                status = self.request("GET", path)
                require(status.get("id") == job_id, "Provider job ID mismatch.")
                if status.get("status") == "COMPLETED":
                    wrapper = status.get("output")
                    require(isinstance(wrapper, dict) and set(wrapper) == {"result"}, "Expected output.result wrapper.")
                    require(len(json.dumps(wrapper, ensure_ascii=False, allow_nan=False).encode()) <= MAX_OUTPUT,
                            "Wrapped worker output exceeds 4 MiB.")
                    return wrapper["result"]
                require(status.get("status") in {"IN_QUEUE", "IN_PROGRESS"}, "Provider job ended without a valid completed envelope.")
                require(time.monotonic() < end, "Job deadline elapsed; cancellation requested without assuming termination.")
                time.sleep(1)
        except BaseException:
            try:
                self.request("POST", "/cancel/" + quote(job_id, safe=""))
            except Exception:
                pass
            raise


def payload(stage, run_id, prompt=None):
    value = {"schema_version": 1, "stage": stage, "run_id": run_id,
             "invocation_id": str(uuid.uuid4()), "deadline_unix_ms": int((time.time() + 600) * 1000)}
    if stage == "qwen":
        value.update(article={"title": "Neighbors open a community garden", "excerpt": "Residents plant trees beside a library."},
                     system_prompt="Return one concise plain visual prompt for an editorial illustration. No thinking or captions.",
                     max_new_tokens=256)
    else:
        value.update(prompt=prompt or "An editorial illustration of a community garden beside a library.",
                     seed=42, width=1024, height=1024, num_inference_steps=4, guidance_scale=1.0, output_format="webp")
    return value


def check(envelope, request, success):
    require(isinstance(envelope, dict), "Missing worker envelope.")
    for key in ("schema_version", "stage", "run_id", "invocation_id"):
        require(envelope.get(key) == request[key], f"Envelope {key} mismatch.")
    require(envelope.get("ok") is success, "Unexpected worker outcome; inspect private worker diagnostics.")
    counters = envelope.get("counters", {})
    attempts, retries, dependency = (counters.get(k) for k in ("inference_attempts", "inference_retries", "dependency_retries"))
    require(all(type(v) is int and v >= 0 for v in (attempts, retries, dependency)), "Smoke requires known attempt counters.")
    require(attempts <= 2 and retries == max(attempts - 1, 0) and retries + dependency <= 1, "Invalid retry accounting.")
    model = envelope.get("model", {})
    require(model.get("id") == MODELS[request["stage"]], "Unexpected checkpoint.")
    if not success:
        require(attempts == 0 and envelope.get("data") is None and isinstance(envelope.get("error"), dict),
                "Invalid-input test must fail before inference.")
        require(envelope["error"].get("code") == "INVALID_INPUT", "Invalid-input test returned an unexpected error code.")
        return
    require(attempts > 0 and envelope.get("error") is None, "Success must include inference attempts and no error.")
    require(model.get("revision") == REVISIONS[request["stage"]], "Unexpected model revision; synthetic output is not GPU evidence.")
    require(isinstance(model.get("runtime"), str) and "synthetic" not in model["runtime"].lower(), "Missing real runtime identity.")
    if request["stage"] == "qwen":
        require(model.get("precision") == "NVFP4", "Qwen precision changed from NVFP4.")
        prompt = envelope["data"].get("prompt")
        require(isinstance(prompt, str) and 0 < len(prompt.strip()) <= 2000 and "<think" not in prompt.lower()
                and not prompt.lstrip().startswith(("{", "[")), "Invalid Qwen visual prompt.")
    else:
        from PIL import Image
        image = envelope["data"]
        raw = base64.b64decode(image["image_base64"], validate=True)
        require(0 < len(raw) == image["image_bytes"] <= 2_097_152, "Invalid image byte count.")
        require((image["width"], image["height"], image["seed"], image["mime_type"]) == (1024, 1024, 42, "image/webp"),
                "Image preset mismatch.")
        with Image.open(io.BytesIO(raw)) as decoded:
            decoded.load()
            require(decoded.size == (1024, 1024) and decoded.format == "WEBP", "Image decoding failed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-gpu", action="store_true", required=True, help="Allow actual model inference.")
    parser.add_argument("--warm", action="store_true", help="Run the pipeline twice to compare first/warm request timing.")
    parser.add_argument("--ready-timeout", type=int, default=1200)
    args = parser.parse_args()
    require(1 <= args.ready_timeout <= 3600, "Readiness timeout must be 1..3600 seconds.")
    token = os.environ.get("LOCAL_WORKER_TOKEN", "")
    require(len(token) >= 32 and "\r" not in token and "\n" not in token, "Configure a private worker token of at least 32 characters.")
    # Fail before any inference if the image-decoding dependency is unavailable.
    from PIL import Image  # noqa: F401
    endpoints = {stage: Endpoint(os.environ.get("LOCAL_" + stage.upper() + "_URL", f"http://127.0.0.1:{8181 if stage == 'qwen' else 8182}"), token)
                 for stage in MODELS}
    for stage, endpoint in endpoints.items():
        endpoint.ready(args.ready_timeout)
        invalid = payload(stage, str(uuid.uuid4()))
        invalid["max_new_tokens" if stage == "qwen" else "width"] = 512
        check(endpoint.invoke(invalid), invalid, False)
        print(json.dumps({"stage": stage, "validation_rejection": "passed"}), flush=True)
    for iteration in range(2 if args.warm else 1):
        run_id, prompt = str(uuid.uuid4()), None
        for stage, endpoint in endpoints.items():
            request = payload(stage, run_id, prompt)
            started = time.monotonic()
            envelope = endpoint.invoke(request)
            check(envelope, request, True)
            if stage == "qwen":
                prompt = envelope["data"]["prompt"]
            print(json.dumps({"stage": stage, "request": "first" if iteration == 0 else "warm", "model": envelope["model"],
                              "wall_ms": round((time.monotonic() - started) * 1000), "timings": envelope.get("timings"),
                              "counters": envelope["counters"], "usage": envelope.get("usage")}), flush=True)
    print(json.dumps({"result": "PASS", "evidence": "operator-run real local HTTP inference"}))


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        # HTTP exception strings can include server-controlled URLs; keep errors bounded and safe.
        if type(error) is RuntimeError:
            raise SystemExit(str(error)) from None
        raise SystemExit(f"Smoke test failed ({type(error).__name__}); inspect private worker status. No automatic resubmission occurred.") from None
