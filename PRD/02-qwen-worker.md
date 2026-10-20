# Qwen Worker PRD

Owner: Qwen implementation agent using GPT-6.1-Sol\
Dependencies: [Product PRD](PRD.md) and [shared contracts](00-shared-contracts.md)

## Purpose and exclusive ownership

Implement the dedicated Runpod prompt-generation endpoint. Own **`workers/qwen/**`**, including `handler.py`, Dockerfile, pinned dependencies, tests, fixtures, deployment instructions/settings, and `.env.example`.

Other agents implement the application and FLUX concurrently. Preserve their work and do not change their files or shared contracts. The app owns authentication, quotas, orchestration, prompt settings, content filtering, FLUX composition, and user retry decisions.

## Fixed deployment

- Checkpoint: `nvidia/Qwen3-8B-NVFP4`; pin and report its revision.
- Target: RTX PRO 4500 Blackwell, 32 GB VRAM.
- Dedicated endpoint, maximum one worker, concurrency one, scale to zero when idle.
- Thinking disabled; maximum generated output 256 tokens.
- Load the model once per worker and reuse it.

Select and pin a serving runtime that actually supports this checkpoint on the target Blackwell card. A thin handler may wrap a local serving engine. Preserve the shared Runpod interface regardless of internal runtime choice. Pin compatible base image, CUDA, PyTorch, serving libraries, tokenizer, and Runpod SDK; the app's Python version does not force an incompatible GPU image.

A real GPU compatibility spike is an early deliverable. Do not silently change checkpoint, precision, GPU, or runtime behavior to make a test pass. Report required changes to the coordinator with evidence.

## Input and output

Implement the exact shared contract, including `deadline_unix_ms`, identifiers, `stage: qwen`, `article`, `system_prompt`, and `max_new_tokens`. Reject unsupported keys/types/limits before GPU work. Enforce the 4,000-character article limit, system-prompt limit, tokenizer context budget, and output bounds defined by the contract.

Use the checkpoint's supported chat template with thinking explicitly disabled. Keep article data separate from system instructions. Return one concise plain visual prompt. Reject empty, incomplete, malformed, over-limit, or reasoning-bearing output. Do not generate summaries, invoke FLUX, or decide the app's fallback policy.

Return the shared envelope with `data.prompt`, actual model metadata, observed token usage/timing, counters, and typed errors. Unknown metrics remain null. An `ok: false` payload is a failure even if the provider envelope says completed.

## Startup and request reliability

Validate configuration, model access, tokenizer/template support, and GPU/kernel compatibility before accepting jobs. Record initialization/execution limits. At most three startup attempts are permitted for explicitly transient download/dependency failures. Access denial, corrupt configuration, incompatible hardware, or unsupported kernels fail startup; do not advertise readiness or restart endlessly internally.

Use at most one classified transient retry per invocation, within its remaining deadline. Retry only when the previous operation is known not to remain in progress. In particular, never replay a local inference-server request with uncertain acceptance. No automatic retries for inference timeouts with unknown outcome, cancellation, OOM, kernel errors, invalid output, or permanent failures.

Configure serving-engine retries so they cannot silently multiply handler attempts. Apply shared accounting: pre-inference failures have zero inference attempts; safe dependency retries are counted separately; every initiated model call counts even if it fails. Never invent reliable counters after a crash.

Clean up request resources on all exit paths. Keep credentials, unnecessary article text, and stack traces out of user-visible errors. Stop additional work after deadline/cancellation, and expose failures through the common contract.

## Deliverables and validation

Provide build/start instructions, redacted environment examples, endpoint settings, weight-cache strategy, deadlines, direct endpoint smoke-test instructions, and the tested image tag/digest. Include local test commands in the worker README and support a root build context of `workers/qwen`.

CPU tests with deterministic runtime doubles must cover schema rejection, zero-attempt validation, prompt validation, retry classification/caps, deadline/cancellation, uncertain local inference acceptance, token accounting, and shared envelope compatibility. Tests must run without loading model weights or requiring a GPU.

Real GPU evidence identifies the image, model revision, GPU, precision, and runtime versions, and demonstrates a real bounded prompt with thinking off. Also verify invalid-input rejection and controlled transient/exhausted-retry paths. Record cold/warm timings. CPU mocks alone do not validate NVFP4 or container compatibility.

## Acceptance

- **FR4:** The deployed checkpoint returns one valid visual prompt within contract bounds and without thinking content.
- **FR10:** Responses expose actual model identity, measured or unavailable token usage/timing, and correct attempts.
- **FR12:** Failure tests show bounded recovery and no replay after uncertain acceptance.
- The app consumes the worker through the published contract without worker-specific parsing or retry logic.

Keep this image independent of FLUX for the MVP. Integrated provisioning and the end-to-end demonstration remain coordinator responsibilities.
