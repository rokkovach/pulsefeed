# FLUX image worker

Current compact image: `rokkovach/pulsefeed:flux-v1.0.2-20261020`. The original v1.0.1 completed real Runpod generation and remains the rollback image. The compact release passed all three sizes on native Spark; its cloud queue smoke is pending. See the [release/rollback guide](../../docs/flux-compact-images-2026-10-20.md).

An independent Runpod queue worker using `Flux2KleinPipeline` and `black-forest-labs/FLUX.2-klein-4B`, revision `e7b7dc27f91deacad38e78976d1f2b499d76a294`. It produces **one square 256, 512 or 1024 image, four steps, guidance 1.0, BF16**, with a caller-supplied seed. The app owns final prompt composition/filtering and user retry admission. Images remain in memory and native temporary results; the worker writes no image archive or object store.

[Cloud deployment](../../docs/deployment.md) · [contracts](../../contracts/README.md) · [architecture](../../docs/architecture.md) · [Spark setup](../../local/README.md)

## Runtime profiles and status

| Profile | Image / runtime | Evidence |
| --- | --- | --- |
| `runpod` | AMD64 `Dockerfile`; clean CPython 3.12.15 / PyTorch 2.13.0+cu129 / CUDA 12.9 | Packaging/CPU evidence; **real RTX PRO 4500 Blackwell 32 GB inference remains unverified** |
| `dgx-spark` | ARM64 `Dockerfile.dgx-spark`; the same clean Python/Torch/CUDA versions / GB10 | Real BF16 inference and app flows passed; separate profile, not cloud GPU proof |

Both use Diffusers 0.41.0, Transformers 5.20.2 and the same fixed model revision and bounded presets. Runpod SDK is 1.12.0; Spark uses the authenticated root gateway instead. The [Dockerfiles](Dockerfile) and full lock/provenance files define immutable CPython base digests and the complete wheel closure. Runtime metadata reports installed versions, not a claimed GPU-validation status.

The model card supplies the fixed BF16 recipe. CUDA/Blackwell support in a library release does not establish this exact GPU/image combination. [Model card](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B), [pinned pipeline](https://github.com/huggingface/diffusers/blob/v0.41.0/src/diffusers/pipelines/flux2/pipeline_flux2_klein.py), [official PyTorch CUDA wheels](https://pytorch.org/get-started/previous-versions/)

Current measured results are in [integration validation](../../integration/VALIDATION.md) and [Spark validation](../../local/SPARK_VALIDATION.md). Do not present a one-call timing as a cloud latency or throughput benchmark.

## CPU checks

From the repository root, in a separate Python 3.11 environment:

```sh
python3.11 -m venv workers/flux/.venv
workers/flux/.venv/bin/python -m pip install -r workers/flux/requirements-test.txt
workers/flux/.venv/bin/python -m pip install --no-deps runpod==1.12.0
workers/flux/.venv/bin/python -m pytest -q workers/flux/tests

# Isolated CPU Docker target: no Torch/CUDA/weights.
docker build --target cpu-tests -t pulsefeed-flux-cpu:local workers/flux
```

The SDK is deliberately installed without dependencies only for isolated SDK-boundary tests with mocked transport. This is not a production SDK installation recipe. Do not combine this environment with the app or install GPU runtime requirements into it. [All suites](../../integration/README.md)

Tests cover input/contracts, prompt preservation, tokenizer bounds, bounded preset wiring, deadlines, cancellation, OOM/unknown/fatal behavior, single-attempt accounting, cache completeness, finite encoding and wrapper size. They use labeled synthetic images/injected engines and make no model/network calls.

Core test seam: `Worker(engine).handle(job, cancelled=event)`. Engines expose `metadata`, `validate_prompt`, `generate` (one-image list) and `cleanup`. Production uses `DiffusersEngine`; no job field enables synthetic inference/failure injection.

## Build and Runpod settings

```sh
docker build --platform linux/amd64 --target runtime \
  -t pulsefeed-flux:candidate workers/flux

# Intentional registry publication; replace registry/version first.
docker buildx build --platform linux/amd64 --target runtime \
  -t YOUR_REGISTRY/pulsefeed-flux:VERSION --push workers/flux
```

Build from the independent `workers/flux` context. No weights download during build. [`requirements-runpod.lock`](requirements-runpod.lock) contains the complete 147-wheel AMD64/Python 3.12 runtime, including the SDK; [`requirements-dgx.lock`](requirements-dgx.lock) contains the distinct 65-wheel ARM64/Python 3.12 runtime without the SDK. Torch uses an exact official CUDA wheel URL and SHA-256; other upstream packages come from PyPI. Builds enforce `--no-deps --require-hashes --only-binary=:all:`, imports and full `pip check`. Fresh-base provenance requires a clean base without protected GPU distributions and verifies the complete installed inventory with the shared external helper. Refresh the matching report/provenance and inventory with any dependency change. [Dependency maintenance](../../local/DEPENDENCY_LOCKS.md)

The image removes the pinned base's unused `ensurepip` pip 25.0.1 archive after installing active pip 26.2.1. Active pip metadata remains intact. These runtime images do not bootstrap new virtual environments; dependency changes go through image rebuilds. [Remediation evidence and residual findings](../../docs/dependency-remediation-2026-10-20.md)

Use a dedicated **queue-based** endpoint: target RTX PRO 4500 **Blackwell** 32 GB, one GPU, active/min 0, max 1 and concurrency 1. Initial provider execution timeout is 600 seconds; worker bounds are `FLUX_STARTUP_TIMEOUT_SECONDS=600` and `FLUX_EXECUTION_TIMEOUT_SECONDS=540`. The earlier application deadline also applies. Verify exact hardware and effective endpoint settings before release. [Runpod queue overview](https://docs.runpod.io/serverless/overview), [endpoint settings](https://docs.runpod.io/serverless/endpoints/endpoint-configurations)

Put `HF_TOKEN` in worker secrets only if access requires it; the Runpod API key stays in the app. Keep logs at INFO. Each request dispatches inference once; do not add blind replay around it.

## Cache and readiness

| Variable | Behavior |
| --- | --- |
| `MODEL_CACHE_SOURCE=volume` | Default; `HF_HUB_CACHE`, fallback `/runpod-volume/huggingface/hub` |
| `MODEL_CACHE_SOURCE=runpod-managed` | Override with `/runpod-volume/huggingface-cache/hub` |
| `MODEL_CACHE_OFFLINE=false` | Default; bounded startup downloads of the exact pin |
| `MODEL_CACHE_OFFLINE=true` | Require complete fixed snapshot; no network/revision fallback |
| `MODEL_REVISION` | Must match the pin above |

Managed mode requires endpoint cache configuration and verification of the exact commit, not `main`. [Runpod cache path](https://docs.runpod.io/serverless/endpoints/model-caching#where-cached-models-are-stored)

The worker verifies required component files/sizes and SHA-256 hashes for eleven small routing/index/template metadata files from the pinned commit before loading. Python files, symlinked directories and links outside the snapshot/Hub blobs are rejected. The fixed text-encoder shard index is authenticated before Accelerate can interpret its weight_map; tokenizer chat/config/special-token metadata is authenticated before template loading. Special files such as FIFOs are rejected before metadata reads. These checks protect this fixed snapshot; they do not patch Accelerate itself, authenticate all weights/tokenizer assets, or eliminate concurrent cache mutation during loading. Keep cache writers trusted and restore the exact upstream snapshot if validation fails. Online mode gets up to three classified transient snapshot attempts plus the startup watchdog. The roughly 14.9 GiB snapshot can take longer than startup permits, so prefetch/verify before offline rollout. See the [October 20 audit](../../docs/audit-2026-10-20.md).

Legacy `FLUX_LOCAL_FILES_ONLY=0|1` is used only when `MODEL_CACHE_OFFLINE` is unset; explicit conflicting values fail startup. Spark Compose uses its own `FLUX_CACHE_OFFLINE` launcher setting. No browser/job can choose a model/cache path.

Runtime/device checks, a BF16 matrix probe and model loading finish before SDK registration. Unsupported runtime/model/access failures latch readiness false; a known terminal result requests SDK worker refresh. Unknown native outcomes hard-exit before terminal delivery, and cancellation that cannot finish within its grace period recycles the process. Ordinary invalid input or bounded memory pressure does not permanently poison readiness. A cancellation acknowledgement alone is not proof of GPU termination. [Operations](../../docs/operations.md)

## Input, output and accounting

Version 1 input validates exact fields, UUID IDs, future deadline, prompt, seed and fixed preset before inference. Prompt text is preserved. The text-encoder budget is 512 tokens including template overhead; excess is rejected instead of silently truncated.

The SDK callback returns `{"result": envelope}`, appearing at provider `output.result`. Core tests receive the raw envelope. This protects the contract's `error` key from SDK consumption; a provider `COMPLETED` status can still contain `ok:false`.

Output encoding is a bounded quality search at fixed dimensions, capped at 2 MiB binary / 2,796,204 base64 characters and 4 MiB wrapped JSON. Only WebP/JPEG is accepted. The handler dispatches inference once. Download retries remain bounded at startup; request-level inference is not replayed. Attempts increment before inference and retry counters stay zero for contract compatibility. Unknown measurements stay unavailable; no fabricated token equivalent is reported for FLUX.

## Explicit real-GPU smoke

Inside the GPU image, deliberately run:

```sh
python /worker/preflight.py
python /worker/smoke_test.py --controlled-failure > /tmp/flux-gpu-evidence.jsonl
# Optional separate JPEG evidence; performs additional real inference.
python /worker/smoke_test.py --output-format jpeg > /tmp/flux-jpeg-evidence.jsonl
nvidia-smi
python -m pip freeze
```

The smoke script performs first/warm real generations, invalid-input rejection and, with `--controlled-failure`, a labeled simulated dependency failure that is returned without replay. It has no `--real-gpu` or `--evidence-path` flag. `preflight.py --config-only` is CPU metadata and explicitly not GPU proof. Record image digest, metadata, driver, timings, attempts and actual output decoding/quality; then verify a native endpoint job with fresh IDs/future deadline, submitted once.

## Native Spark profile

Build from the **repository root**, since the image includes the shared gateway:

```sh
docker build --platform linux/arm64 -f workers/flux/Dockerfile.dgx-spark \
  -t pulsefeed-flux:dgx-spark .
```

The profile requires ARM64 GB10 SM 12.1, the exact official Torch 2.13.0+cu129/CUDA 12.9 wheel and BF16 kernel support. It starts the shared gateway directly on clean CPython and retains the same model/cache paths, startup/execution bounds and shared GPU lock.

The upstream ARM64 cuSPARSELt 0.8.1 wheel has an incorrect internal `manylinux2014_sbsa` tag. A separate build stage authenticates its exact public source SHA-256 and deterministically rebuilds only packaging metadata as `0.8.1+pulsefeed.arm64tag1`, with an `aarch64` tag and regenerated RECORD. Every CUDA/library payload byte remains unchanged; source/output/payload hashes are in [`cusparselt-repair.provenance.json`](cusparselt-repair.provenance.json). Installation uses a read-only stage mount and the full hash lock, so the source/rebuilt wheel archives are absent from runtime layers. This corrects packaging, not an upstream CUDA or advisory issue. See [native dependency details](../../local/DEPENDENCY_LOCKS.md).

`local_factory.py` loads the real engine; `local/worker_gateway.py` owns bearer auth, queue/status/cancel, bounded in-memory results, startup watchdog and the shared cross-container GPU lock. Persistent caches contain weights only. Startup order, prefetch, private environment creation, SSH forwarding and laptop connection are documented in the [local guide](../../local/README.md). Coordinate replacements: even an idle gateway restart destroys retained images. Historical raw evidence links may be workspace-only; use committed summaries for GitHub review.

The app defaults to 512 × 512; legacy 1024 requests remain valid. Unsupported sizes and rectangular requests fail before GPU work, and encoded output must match the requested dimensions. The working `flux-v1.0.1-20261020` image remains available for rollback. See the [compact-image release](../../docs/flux-compact-images-2026-10-20.md) for deployment order and validation.
