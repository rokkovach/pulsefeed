"""Independent contract checks for test evidence, not a shared runtime package."""

from __future__ import annotations

import base64
import io
import json
import unicodedata
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
MAX_IMAGE_BYTES = 2_097_152
MAX_OUTPUT_BYTES = 4 * 1024 * 1024


def check_schema(name: str, value: dict) -> None:
    schema = json.loads((CONTRACTS / f"{name}.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def check_input(stage: str, value: dict) -> None:
    check_schema(f"{stage}-input", value)
    if stage == "qwen":
        article = value["article"]
        if any(text != normalized(text) for text in article.values()):
            raise ValueError("Article strings must already be normalized")
        if len(article["title"]) + len(article["excerpt"]) > 4000:
            raise ValueError("Combined article length exceeds 4000 code points")
        if "article_template" in value:
            template = value["article_template"]
            for marker in ("{{article_title}}", "{{article_excerpt}}"):
                if template.count(marker) != 1:
                    raise ValueError("Article template requires exactly one of each article token")
                template = template.replace(marker, "")
            if len(template) > 2000:
                raise ValueError("Article template exceeds 2000 static code points")
            if any(unicodedata.category(char) in {"Cc", "Cs"} and char not in "\n\r\t" for char in template):
                raise ValueError("Article template contains unsupported control characters")
    for field in ("system_prompt",) if stage == "qwen" else ("prompt",):
        if not value[field].strip():
            raise ValueError(f"{field} must not be blank")


def check_output(stage: str, value: dict, request: dict | None = None) -> None:
    check_schema(f"{stage}-output", value)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise ValueError("Worker output exceeds 4 MiB")
    if request is not None:
        for field in ("run_id", "invocation_id", "stage", "schema_version"):
            if value[field] != request[field]:
                raise ValueError(f"Mismatched {field}")
    counters = value["counters"]
    attempts, retries = counters["inference_attempts"], counters["inference_retries"]
    dependency_retries = counters["dependency_retries"]
    if attempts is not None and retries is not None and retries != max(attempts - 1, 0):
        raise ValueError("Inconsistent inference retry count")
    if retries is not None and dependency_retries is not None and retries + dependency_retries > 1:
        raise ValueError("Handler exceeded one automatic retry")
    if stage == "qwen":
        usage = value["usage"]
        inp, out, total = (usage[k] for k in ("input_tokens", "output_tokens", "total_tokens"))
        expected = None if inp is None or out is None else inp + out
        if total != expected:
            raise ValueError("Inconsistent aggregate token usage")
        if attempts == 0 and (any(x != 0 for x in (inp, out, total)) or value["timings"]["inference_ms"] != 0):
            raise ValueError("Pre-inference failure must report zero usage")
        if value["ok"] and not value["data"]["prompt"].strip():
            raise ValueError("Blank output prompt")
    if not value["ok"] or stage != "flux":
        return
    data = value["data"]
    raw = base64.b64decode(data["image_base64"], validate=True)
    if not raw or len(raw) > MAX_IMAGE_BYTES or len(raw) != data["image_bytes"]:
        raise ValueError("Image byte count invalid")
    with Image.open(io.BytesIO(raw)) as image:
        image.verify()
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        mime = {"WEBP": "image/webp", "JPEG": "image/jpeg"}.get(image.format)
        if mime != data["mime_type"] or image.size != (data["width"], data["height"]):
            raise ValueError("Image MIME or dimensions invalid")
    if request is not None:
        for field in ("seed", "width", "height"):
            if data[field] != request[field]:
                raise ValueError(f"Image does not match requested {field}")
        if data["mime_type"] != {"webp": "image/webp", "jpeg": "image/jpeg"}[request["output_format"]]:
            raise ValueError("Image format differs from requested format")


def check_provider_output(stage: str, value: dict, request: dict | None = None) -> None:
    check_schema(f"{stage}-provider-output", value)
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_OUTPUT_BYTES:
        raise ValueError("Wrapped output exceeds 4 MiB")
    check_output(stage, value["result"], request)
