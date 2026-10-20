# Smaller FLUX images and worker cleanup — 20 October 2026

New feed jobs default to **512 × 512 WebP**, replacing 1024 × 1024 for new admissions. The model, BF16 precision, four diffusion steps, guidance 1.0 and seed behavior remain unchanged. This is a real lower-resolution render, not a 1024 image resized after inference.

The worker accepts bounded square presets **256, 512 and 1024**. We chose 512 for feed cards because 200-pixel images would lose detail when displayed at larger desktop sizes or on dense screens. The model's [official demo](https://huggingface.co/spaces/black-forest-labs/FLUX.2-klein-4B/blob/main/app.py) offers sizes starting at 256 with 32-pixel increments; our API deliberately exposes only three tested square presets rather than arbitrary dimensions.

A 512 image has one quarter of the pixels of 1024. This does not imply four times faster inference: text encoding, model initialization, kernel overhead and provider queueing also contribute. File-size and visual results are measured separately below.

## Handler cleanup

The FLUX entry point already mostly handles SDK lifecycle. The removable complexity was in `flux_worker.py`:

- Removed the generic inference retry loop and its test-only `SafeTransientFailure` exception. Each request now dispatches GPU inference once. Dependency errors are returned; inference is not automatically replayed.
- Removed unused request sleep/jitter injection. Startup download retries still use their own bounded retry/backoff policy.
- Simplified inference timing to one attempt, preserving unknown timing for uncertain completion.
- Replaced the broad module-import fallback with an explicit package/script import. Import errors in a dependency are no longer mistaken for a different launch mode.
- Removed duplicate startup-timer cancellation; the `finally` block always disarms it.

Kept deadlines, cancellation watchdogs, native-work hard exits, single-job locking, readiness invalidation, prompt-token limits, encoding verification, image/base64/response size caps and finite compression passes. These protect real failure cases. Unlike Qwen's text output, an image response still needs transport ceilings.

The wire response retains zero-valued retry counter fields for compatibility. No synthetic token equivalent is created for FLUX; it reports images, attempts and inference time.

## App and contracts

Set this on the app deployment:

```text
FLUX_IMAGE_SIZE=512
```

Only 256, 512 and 1024 are accepted. This is an operator environment setting, not an unbounded browser parameter.

The app writes width/height into each job row when admitting it. The coordinator uses those saved dimensions and passes them through to the image pipeline. Retries inherit their parent's dimensions. Changing the environment does not silently resize queued work, and old rows retain their existing 1024 values. No schema migration is needed: the columns already existed.

Worker input/output schemas now accept the three square presets. The worker verifies the generated image and decoded encoded bytes against the request. The app checks response metadata, decoded dimensions, MIME type, seed and byte count against its saved invocation. Rectangular, unsupported or mismatched dimensions fail. The fake adapter follows requested dimensions too; its output remains explicitly synthetic.

## Safe deployment order

Railway deployment is intentionally deferred while the operator reports deployment problems. No production app variables, live endpoints or running Spark services are changed by this work.

1. Deploy the new compact FLUX worker to Runpod first. It continues to accept the current app's 1024 requests.
2. Submit one fresh 512 request, poll its existing job ID and verify `ok: true`, decoded 512-square WebP, one attempt and no retries.
3. When Railway deployments are available, deploy the prepared app with `FLUX_IMAGE_SIZE=512` (also its default). Keep other credentials and limits unchanged.
4. Verify one real RSS page and image/attempt usage after the connected cloud flow is enabled. Local tests do not establish this production integration result.

The published compact worker is:

```text
docker.io/rokkovach/pulsefeed:flux-v1.0.2-20261020
docker.io/rokkovach/pulsefeed@sha256:431ec62c62e1c7e6e187d5f6b990ed14c2300cfdb9605c41834968dfff441187
```

Its public manifest and AMD64 configuration were verified anonymously. The rollback tag below was independently checked against its original digest and is unchanged.

The working rollback worker is:

```text
docker.io/rokkovach/pulsefeed:flux-v1.0.1-20261020
docker.io/rokkovach/pulsefeed@sha256:e674b604f7b64e1e30a3636ef74b9c8bf9c7f4e01033a464ae6b028dd9883f0c
```

Do not roll the worker back to the 1024-only version while the app is submitting 512/256 jobs. First set the app to 1024, apply that deployment, and drain previously admitted smaller jobs with the compact worker. Existing smaller jobs retain their size by design. If an immediate rollback is necessary, restore the previous app release as well and inspect existing job IDs rather than blindly resubmitting them.

## Validation scope

CPU suites cover preset bounds, exact output dimensions, compression exhaustion without regeneration, no inference replay, SDK cancellation/refresh behavior, app admission/configuration and retries, and authenticated HTTP transport through both handler cores. Browser layout remains square at every supported size, so image loading reserves the same aspect ratio.

Real inference on Spark tests the native ARM64/GB10 profile. It does not certify the AMD64 image on every Runpod GPU. Model quality, storage and timing results from that single prompt/device are examples, not a general benchmark.

## Measured Spark results

The same prompt and seed 42 were rendered in one process, in the order below, at WebP quality 95. Every response reported one inference attempt and no retries. Output bytes were decoded independently as WebP at the requested dimensions.

| Test | Image bytes | Inference time |
| --- | ---: | ---: |
| first-512 | 154,810 | 2.217 s |
| warm-512 | 154,810 | 1.130 s |
| small-256 | 42,248 | 0.646 s |
| legacy-1024 | 443,146 | 3.433 s |

Warm 512 was about 65% smaller than the 1024 result. Startup took 105.0 seconds in a disposable offline container with the existing read-only cache and shared GPU lock. Size-dependent composition can differ even with the same seed; these are separate renders, not resized copies. Resident Spark workers remained running. A native Runpod request on the new worker is still required before calling the compact cloud release verified.

## Regression checks

- 173 FLUX worker/SDK tests passed.
- 624 app/integration tests passed before the additional size-parameterized gateway cases; the targeted 72-case config/contract/gateway/boundary suite then passed, including the two new cases. This is 626 unique app/integration tests.
- All 92 Qwen worker/SDK tests passed unchanged.
- All 80 local gateway tests and nine browser suites passed.
- Historical 1024 fixtures and responses remain valid; smaller preset jobs preserve dimensions through admission and retry.
