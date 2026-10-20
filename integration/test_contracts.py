import copy
import json

import pytest
from jsonschema import Draft202012Validator, ValidationError

from integration.contract_checks import CONTRACTS, check_input, check_output, check_provider_output


def fixture(name):
    return json.loads((CONTRACTS / "fixtures" / name).read_text())


@pytest.mark.parametrize("path", sorted(CONTRACTS.glob("*.schema.json")), ids=lambda p: p.stem)
def test_schema_is_valid(path):
    Draft202012Validator.check_schema(json.loads(path.read_text()))


@pytest.mark.parametrize("case", fixture("manifest.json")["fixtures"], ids=lambda case: case["file"])
def test_synthetic_contract_examples(case):
    value = fixture(case["file"])
    request = fixture(case["request"]) if case["request"] else None
    def check():
        if case["kind"] == "input":
            check_input(case["stage"], value)
        else:
            check_output(case["stage"], value, request)
    if case["valid"]:
        check()
    else:
        with pytest.raises((ValueError, ValidationError)):
            check()


def test_combined_length_and_normalization_are_not_separate_field_limits():
    request = fixture("qwen-input.json")
    request["article"] = {"title": "x" * 2001, "excerpt": "y" * 2000}
    with pytest.raises(ValueError, match="Combined"):
        check_input("qwen", request)
    request["article"] = {"title": "A  garden", "excerpt": ""}
    with pytest.raises(ValueError, match="normalized"):
        check_input("qwen", request)


def test_optional_qwen_article_template_keeps_legacy_requests_valid():
    request = fixture("qwen-input.json")
    check_input("qwen", request)
    request["article_template"] = 'Article data:\n{"title": {{article_title}}, "excerpt": {{article_excerpt}}}'
    check_input("qwen", request)
    request["article_template"] = "🌱" * 2000 + "{{article_excerpt}}{{article_title}}"
    check_input("qwen", request)


@pytest.mark.parametrize("template", [
    "{{article_title}}", "{{article_excerpt}}", "",
    "{{article_title}}{{article_title}}{{article_excerpt}}",
    "{{article_title}}{{article_excerpt}}{{article_excerpt}}",
    "x" * 2001 + "{{article_title}}{{article_excerpt}}",
    "{{article_title}}\x00{{article_excerpt}}", 12, None,
])
def test_invalid_qwen_article_templates_fail_contract(template):
    request = fixture("qwen-input.json")
    request["article_template"] = template
    with pytest.raises((ValueError, ValidationError)):
        check_input("qwen", request)


@pytest.mark.parametrize("field", ["max_new_tokens", "deadline_unix_ms"])
def test_boolean_is_not_a_wire_integer(field):
    request = fixture("qwen-input.json")
    request[field] = True
    with pytest.raises(ValidationError):
        check_input("qwen", request)


def test_unknown_partial_usage_cannot_be_a_claimed_total():
    result = fixture("qwen-success.json")
    result["usage"]["input_tokens"] = None
    with pytest.raises(ValueError, match="token"):
        check_output("qwen", result)
    result["usage"]["total_tokens"] = None
    check_output("qwen", result)


def test_handler_and_dependency_retries_share_one_budget():
    result = fixture("flux-success.json")
    result["counters"] = {"inference_attempts": 2, "inference_retries": 1, "dependency_retries": 1}
    with pytest.raises(ValueError, match="one automatic retry"):
        check_output("flux", result)


@pytest.mark.parametrize("stage", ["qwen", "flux"])
def test_success_cannot_refund_all_inference_attempts(stage):
    result = fixture(f"{stage}-success.json")
    result["counters"]["inference_attempts"] = 0
    result["timings"]["inference_ms"] = 0
    if stage == "qwen":
        result["usage"] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    with pytest.raises(ValidationError):
        check_output(stage, result)


@pytest.mark.parametrize("stage", ["qwen", "flux"])
@pytest.mark.parametrize("state", ["completed", "worker-failure"])
def test_sdk_wrapper_keeps_success_and_typed_failure_intact(stage, state):
    provider = fixture(f"{stage}-provider-{state}.json")
    check_provider_output(stage, provider["output"], fixture(f"{stage}-input.json"))
    assert "error" in provider["output"]["result"]
    with pytest.raises(ValidationError):
        check_provider_output(stage, provider["output"]["result"])
    with pytest.raises(ValidationError):
        check_provider_output(stage, {**provider["output"], "extra": True})


@pytest.mark.parametrize("mutation", ["mismatched-id", "wrong-mime", "wrong-size", "bad-base64", "wrong-seed"])
def test_bad_images_or_correlation_never_pass(mutation):
    request = fixture("flux-input.json")
    result = copy.deepcopy(fixture("flux-success.json"))
    if mutation == "mismatched-id":
        result["run_id"] = result["invocation_id"]
    elif mutation == "wrong-mime":
        result["data"]["mime_type"] = "image/jpeg"
    elif mutation == "wrong-size":
        result["data"]["image_bytes"] += 1
    elif mutation == "bad-base64":
        result["data"]["image_base64"] = "!!!!"
    else:
        result["data"]["seed"] += 1
    with pytest.raises((ValueError, ValidationError)):
        check_output("flux", result, request)
