# FLUX Worker PRD

Owner: FLUX implementation agent using GPT-6.1-Sol\
Dependencies: [Product PRD](PRD.md) and [shared contracts](00-shared-contracts.md)

## Purpose and exclusive ownership

Implement the Runpod image worker. Given Railway's final content-filtered prompt and fixed settings, return one valid compressed illustration with accurate metadata and attempt counters. Cover **FR6**, the FLUX portion of **FR10**, and the worker portion of **FR12**.

Own **`workers/flux/**`**, including `handler.py`, Dockerfile, pinned dependencies, tests, fixtures, endpoint settings, environment example, and worker README. Other agents work concurrently. Do not edit the app, Qwen, root files, or shared contracts; propose shared changes through the coordinator.

## Boundary and contract

Use the common Runpod input fields, including the deadline and correlation IDs, plus `prompt`, `seed`, `width`, `height`, `num_inference_steps`, `guidance_scale`, and `output_format` exactly as specified in the shared contract.

Railway owns composition, blocked terms, fallback choice, submission/polling, account quotas, and explicit user retries. Treat `prompt` as final. Do not invoke Qwen, prepend styles, or choose another content fallback. Reject arbitrary URLs, checkpoint names, model paths, image inputs, and unapproved generation settings.

Return the shared envelope, with image data on success and typed error data on failure. Provider completion does not turn an `ok: false` result into a successful image.

## Generation and encoding

- Use `black-forest-labs/FLUX.2-klein-4B`, the distilled checkpoint, with a pinned revision.
- Generate one image at 1024 × 1024, four inference steps, guidance scale 1.0, and the validated seed.
- Validate input before GPU work. Load the model once per worker and reuse it.
- Record the actual revision, precision, runtime, settings, and inference timing.
- Return WebP or JPEG matching the requested output format, at most 2 MiB before base64 encoding.

Verify count, dimensions, MIME type, byte length, and image decodability. Use a finite compression-quality sequence on the same image if necessary; do not regenerate or reduce dimensions to satisfy output limits. Exhausted compression returns `OUTPUT_TOO_LARGE`. Enforce the contract's base64 and complete-envelope limits.

Return no permanent URL. Do not write generated images to object storage or persistent worker storage, and do not log their bytes/base64. FLUX usage reports image count and available timings; no fabricated token metrics.

## Failures and retries

Implement the shared error taxonomy, safe messages, correlation fields, and provider/payload mapping. Invalid input, unsupported settings, access denial, missing configuration, corrupt model configuration, and OOM do not trigger automatic inference retries. Incompatible startup prevents readiness.

Allow at most one classified recoverable transient retry in an invocation, within the shared deadline. Retry only when the previous operation is known not to remain in progress. Never retry cancellation, OOM, unsupported kernels, unchanged permanent failures, or an expired deadline; never submit another provider job from the handler.

Report actual inference and dependency retry counters according to the shared contract. Count initiated inference even if it fails; compression passes do not count as inference. Pre-inference validation reports zero attempts. Process crashes cannot fabricate successful accounting.

## Container and deployment

Pin compatible CUDA, PyTorch, Diffusers/Transformers or selected image runtime, image-encoding libraries, Runpod SDK, and model revision. Build dependencies into the image. Document baked weights or Runpod model caching; do not install dependencies per request.

Validate environment, model access, and GPU/runtime compatibility at startup. Transient startup downloads have at most three attempts, then exit clearly. Define initialization/execution limits, readiness, and termination cleanup. Align provider execution limits with the application deadline.

Target RTX PRO 4500 Blackwell, 32 GB VRAM, maximum one worker, one concurrent invocation, and scale-to-zero. Record the tested image tag/digest. This worker has its own GPU allocation and does not share Qwen's endpoint memory.

## Validation and handoff

CPU tests use injected inference doubles to verify input rejection, output contracts, deadlines, retry classification/caps, counters, image decoding/size enforcement, and compression exhaustion. Shared fixtures come from `contracts/**`; lane-specific fixtures belong here. CPU tests must not download the model or require a GPU.

A real GPU smoke test verifies startup, valid generation at the fixed preset, actual model identity/settings, and invalid-input rejection. Use controlled fixtures to verify transient recovery and exhausted retries. Record cold/warm timings and exact runtime details; mocked images are not GPU evidence.

Provide reproducible build/deployment commands using build context `workers/flux`, redacted environment examples, direct request examples, test instructions, and remaining blockers. The coordinator owns the integrated Railway demonstration and independent release review.
