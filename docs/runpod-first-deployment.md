# Prepare the models before Railway


Qwen cleanup release: `rokkovach/pulsefeed:qwen-v1.0.5-20261020`. The working rollback release is `qwen-v1.0.4-20261020`; FLUX compact release is `flux-v1.0.2-20261020`, with `flux-v1.0.1-20261020` preserved for rollback. See the [startup fixes, cleanup validation and rollback record](qwen-runpod-startup-fix-2026-10-20.md).

Both cloud workers use `runpod.serverless.start` with separately pinned SDKs: Qwen uses the selective **1.10.0+pulsefeed.security.1** compatibility/security backport, while FLUX uses upstream **1.12.0**. The Qwen backport preserves compatible dependency declarations and selected official fixes; it is not full upstream 1.12 equivalence. See its [rebuild/provenance record](../workers/qwen/vendor/runpod/README.md). Deploy their custom images as two queue endpoints; the app calls those endpoints.

The earlier native Spark images passed a 27-image application flow; packaging and dependency scan evidence is in the [remediation report](dependency-remediation-2026-10-20.md). The operator subsequently confirmed real Qwen Runpod inference on v1.0.4. The v1.0.5 cleanup passed local SDK/contract tests and first/warm native Spark inference; its Runpod queue smoke is still pending. The original FLUX v1.0.1 also passed a real cloud image request; compact FLUX v1.0.2 and connected Railway generation remain deployment gates. The [Railway web app](cloud-release-2026-10-20.md) is deployed with real generation paused. See [dependency security](dependency-security.md) for retained findings.

## Current public release — October 20, 2026

Use `docker.io/rokkovach/pulsefeed:qwen-v1.0.5-20261020` and `docker.io/rokkovach/pulsefeed:flux-v1.0.2-20261020`, using the [Qwen cleanup digest/rollback record](qwen-runpod-startup-fix-2026-10-20.md#release-and-rollback) and [compact FLUX release/rollback guide](flux-compact-images-2026-10-20.md#safe-deployment-order). Both are public and verified for anonymous registry access. **These images are already built and published: continue at step 2.** Step 1 is for subsequent releases.

## Historical candidate — October 16, 2026

The two AMD64 images below were published in the **private** [Docker Hub repository](https://hub.docker.com/r/rokkovach/pulsefeed). **They predate the [October 20 hardening](audit-2026-10-20.md) and must be rebuilt under new version tags before deployment. Start at step 1.** They remain historical references for the completed packaging and dependency scans. The separate images share one repository, with no model weights or credentials baked in; handlers fetch the pinned model snapshots into their configured cache.

| Worker | Published image tag |
| --- | --- |
| Qwen | `docker.io/rokkovach/pulsefeed:qwen-v1.0.0-20261016-204232` |
| FLUX | `docker.io/rokkovach/pulsefeed:flux-v1.0.0-20261016-204232` |

The historical digest references are:

```text
Qwen: docker.io/rokkovach/pulsefeed@sha256:20a2a6e793bbb0766bf88f57da2f4977df61f28ce567132c08528ff9edf80639
FLUX: docker.io/rokkovach/pulsefeed@sha256:eb3536675ced65492ae6d9eaff4a9bfd453ca29a1807e1a36d011bdc35ee5785
```

These are OCI index digests containing an AMD64 runtime and its SBOM/build provenance. Packaging and CPU checks passed at that checkpoint; no Runpod endpoint has been created or tested. October 20 scans found retained worker dependency issues. Rebuilt images need fresh inventories/scans and target-GPU validation. See the [audit](audit-2026-10-20.md) and [historical release evidence](../integration/RELEASE_EVALUATION.md#cloud-container-packaging-follow-up).

## 1. Build and publish separate AMD64 images

Use Docker Hub repository `rokkovach/pulsefeed` for both images, distinguished by `qwen-` and `flux-` version tags. They remain independent images and endpoints. The owner chose public visibility for the current release. Verify intended visibility before any future push; the script does not create repositories or change visibility.

Run these from the repository root and choose a new version for every changed image. The example version below is illustrative and has not been published. The default is a local command preview; no Docker daemon, registry or model is contacted.

```sh
bash scripts/runpod-images.sh --registry docker.io/rokkovach --repository pulsefeed --version v1.0.2-20261020 --plan
```

When ready, build and load both images locally:

```sh
bash scripts/runpod-images.sh --registry docker.io/rokkovach --repository pulsefeed --version v1.0.2-20261020 --build
```

Configure push-capable registry authentication separately through Docker's normal login flow. An explicit publication command builds and pushes both images:

```sh
bash scripts/runpod-images.sh --registry docker.io/rokkovach --repository pulsefeed --version v1.0.2-20261020 --push
```

The example produces `docker.io/rokkovach/pulsefeed:qwen-v1.0.2-20261020` and `docker.io/rokkovach/pulsefeed:flux-v1.0.2-20261020`. Optional `--repository pulsefeed` selects this single-repository layout. Omitting it preserves the default two repositories, `REGISTRY/NAMESPACE/pulsefeed-qwen:VERSION` and `REGISTRY/NAMESPACE/pulsefeed-flux:VERSION`; create each with the intended visibility before pushing if using that layout. A shared-repository version is limited to 123 characters because the model prefix consumes five of Docker's 128 tag characters.

Never overwrite a released version; record and deploy its registry digest. The script cannot enforce registry tag immutability. It uses the current Docker context/builder and fixed `linux/amd64`. For Qwen it automatically invokes `workers/qwen/prepare_build.py --platform amd64` into a private temporary directory outside the checkout, authenticates the pinned dependency/OS artifacts and builds only that allowlisted context. FLUX keeps its independent worker context and `runtime` target. The wrapper cleans up preparation files after success or failure; do not bypass preparation by building Qwen from its source directory. The default plan prints the complete preparation/build/cleanup recipe without running it. Preparation/build does not fetch model weights. Apple Silicon needs AMD64 build support; a successful Mac build is packaging evidence, while the ARM64 Spark images are a separate runtime. [Runpod custom-image deployment](https://docs.runpod.io/serverless/workers/deploy)

Before enabling live cloud workers, scan the actual published artifacts' OS/native/vendor layers and triage the retained GPU advisories against their digests. Existing Python lock evidence does not clear those images.

## 2. Create two custom queue endpoints

For each published image, follow **Runpod Console → Serverless → New Endpoint → Import from Docker Registry → Container Image → Next → Endpoint Type: Queue**. Enter that image's version/digest and configure its settings before deploying. The current images are public, so private registry credentials are unnecessary. If a future release uses a private repository, configure a separate read-only pull credential in Runpod; keep it outside images and job payloads. [Runpod deployment flow](https://docs.runpod.io/serverless/workers/deploy)

| Setting | Qwen | FLUX |
| --- | --- | --- |
| Image | `rokkovach/pulsefeed:qwen-VERSION` | `rokkovach/pulsefeed:flux-VERSION` |
| Active / max workers | 0 / 1 | 0 / 1 |
| GPUs / worker concurrency | 1 / 1 job | 1 / 1 job |
| Intended GPU | Blackwell SM120, 32 GB class or larger (RTX 5090 / RTX PRO Blackwell) | RTX PRO 4500 **Blackwell** 32 GB or other BF16-capable GPU, subject to inference validation |
| Runtime profile | `QWEN_RUNTIME_PROFILE=runpod` | `FLUX_RUNTIME_PROFILE=runpod` |
| Worker startup bound | `INITIALIZATION_TIMEOUT_SECONDS=480` | `FLUX_STARTUP_TIMEOUT_SECONDS=600` |
| Worker execution bound | `EXECUTION_TIMEOUT_SECONDS=240` | `FLUX_EXECUTION_TIMEOUT_SECONDS=540` |
| Initial provider execution timeout | 600 seconds | 600 seconds |

Qwen v1.0.2-20261020 accepts SM120 Blackwell GPUs with at least 30 GiB reported memory, including the RTX 5090. Earlier Qwen images require the RTX PRO 4500 Blackwell specifically. Ada/A4500 and Ampere cards do not satisfy the Qwen runtime. FLUX has no GPU-name restriction and checks BF16 kernel support at startup. Newly accepted hardware still needs actual inference validation. Validate availability and drivers before provisioning. Preserve one-job concurrency in the SDK handler and effective endpoint configuration. Keep provider execution timeout enabled, and choose job TTL to cover queueing plus execution. The app's 600-second started-story deadline covers both stages, so cold-start and queue timings must be measured. [Endpoint settings](https://docs.runpod.io/serverless/endpoints/endpoint-configurations)

Use worker secret `HF_TOKEN` only if model access requires it. Workers do not need `RUNPOD_API_KEY`. Keep secrets out of images, payloads and saved commands.

## 3. Prepare the exact model cache

| Worker | Checkpoint | Required revision |
| --- | --- | --- |
| Qwen | `nvidia/Qwen3-8B-NVFP4` | `ccd10a893cbca613259517c3efe08e151ddf2b8e` |
| FLUX | `black-forest-labs/FLUX.2-klein-4B` | `e7b7dc27f91deacad38e78976d1f2b499d76a294` |

Use a custom mounted cache with `MODEL_CACHE_SOURCE=volume`: Qwen's `MODEL_CACHE_DIR=/runpod-volume/huggingface`, FLUX's `HF_HUB_CACHE=/runpod-volume/huggingface/hub`. Alternatively configure the provider's cached model and set `MODEL_CACHE_SOURCE=runpod-managed`, which resolves `/runpod-volume/huggingface-cache/hub`. Verify the complete **exact commit** is present; a cached `main` is insufficient. Set `MODEL_CACHE_OFFLINE=true` only after verification. Online mode allows bounded startup downloads, which may exceed the startup budget, especially for FLUX's roughly 14.9 GiB snapshot. These preparation scripts do not download or prefetch models. [Runpod model cache](https://docs.runpod.io/serverless/endpoints/model-caching)

Current source verifies exact hashes of reviewed small metadata and refuses Python files, symlinked component directories and links outside the snapshot/Hub blobs. If readiness reports an incomplete or unreviewed cache, restore the exact upstream snapshot instead of bypassing validation. Keep cache writers trusted throughout startup; these checks do not authenticate all weight files.

## 4. Validate the GPU, then native queue jobs

Run the existing [Qwen real-GPU smoke](../workers/qwen/README.md#required-real-gpu-smoke) and [FLUX real-GPU smoke](../workers/flux/README.md#explicit-real-gpu-smoke) inside their built images on the intended GPU. Save image digests, GPU/driver, model revision/precision, decoded output checks, timings and counters. Spark success does not establish this cloud GPU result.

After worker readiness, generate a request immediately before submission:

```sh
python3 scripts/runpod-input.py qwen > /tmp/pulsefeed-qwen-input.json
```

This emits `{"input": {...}}`, a fixed safe synthetic article, fresh run/invocation UUIDs and a deadline 600 seconds ahead. Submit the body once using the native endpoint request UI or authenticated `/run` API, and retain the returned job ID. Poll that ID with `/status/{job_id}`; never automatically resubmit after an ambiguous submission response.

For a linked FLUX check, save the successful Qwen `output.result.data.prompt` as a UTF-8 text file and replace the UUID below with that request's `run_id`:

```sh
python3 scripts/runpod-input.py flux --prompt-file /tmp/pulsefeed-final-prompt.txt \
  --run-id 123e4567-e89b-12d3-a456-426614174000 > /tmp/pulsefeed-flux-input.json
```

For an independent image smoke, explicitly choose the standalone synthetic illustration prompt:

```sh
python3 scripts/runpod-input.py flux --standalone-illustration > /tmp/pulsefeed-flux-input.json
```

Each call generates a fresh invocation UUID and deadline; the standalone mode does not validate Qwen linkage. The helpers print JSON only and never submit jobs or handle credentials. FLUX prompt files must be nonblank UTF-8 text of at most 4,000 characters; the worker separately enforces its 512-token budget with the real tokenizer. Refresh a payload that expired before submission; do not resubmit an already accepted invocation under a new ID. Native provider `COMPLETED` is insufficient: check `output.result.ok`, matching IDs, exact model revision/precision, non-synthetic runtime metadata, bounded counters and the decoded 1024×1024 image. The full app smoke must still validate its final prompt composition and both stages within one shared deadline. [Contract checks](../contracts/README.md)

## 5. Carry three values to Railway

Once both endpoints pass, retain the **Qwen endpoint ID**, **FLUX endpoint ID**, and **Runpod API key**. Set Railway's private variables `RUNPOD_QWEN_ENDPOINT_ID`, `RUNPOD_FLUX_ENDPOINT_ID`, and `RUNPOD_API_KEY`, with `INFERENCE_PROVIDER=runpod` and `PULSEFEED_FAKE_MODE=false`. Follow the [Railway setup](deployment.md#4-configure-railway) for `/app`, the `/data` volume, app secrets and the connected release smoke. These three provider values supplement the required app configuration.
