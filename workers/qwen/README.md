# Qwen prompt worker

An independent Runpod queue worker for `nvidia/Qwen3-8B-NVFP4`, pinned to revision `ccd10a893cbca613259517c3efe08e151ddf2b8e`. It converts bounded article context into one visual prompt. The app owns RSS retrieval, final prompt filtering/composition and FLUX submission; this worker never invokes FLUX.

[Cloud deployment](../../docs/deployment.md) · [contracts](../../contracts/README.md) · [architecture](../../docs/architecture.md) · [ARM64 Spark profile](DGX_SPARK.md)

## Status and runtime

The current **v1.0.6 CA update** passed 92 worker/SDK tests, 29 lock-helper tests, actual AMD64 dependency/import gates and first/warm real Spark inference. Its Runpod queue smoke awaits the endpoint rollout. The working cloud **v1.0.4** and cleanup **v1.0.5** images are retained with their older certificate bundles. Earlier maintained images passed a 27-image application flow. Hardware-specific results must not be generalized to untested GPUs. See the [CA update and rollout](../../docs/qwen-ca-bundle-update-2026-10-20.md), [startup/cleanup record](../../docs/qwen-runpod-startup-fix-2026-10-20.md) and [packaging evidence](../../local/PACKAGING_VALIDATION.md).

| Component | Cloud pin / behavior |
| --- | --- |
| Checkpoint | `nvidia/Qwen3-8B-NVFP4` at the fixed revision above |
| Precision / engine | ModelOpt NVFP4, vLLM offline `LLM.generate`, `flashinfer-cutlass` backend |
| Base | vLLM 0.32.0, CUDA 12.9.1; immutable digest in [`Dockerfile`](Dockerfile) |
| GPU stack | Torch 2.13.0, FlashInfer 0.7.0.post1, Transformers 5.19.0 |
| Runpod SDK | 1.10.0+pulsefeed.security.1; selected upstream security backports |
| Generation | Thinking disabled; maximum 256 output tokens; context bound 4,096 tokens |

The checkpoint's published B200 support and vLLM's build targets guide the candidate runtime; they do not prove workstation GPU compatibility. [NVIDIA model card](https://huggingface.co/nvidia/Qwen3-8B-NVFP4), [vLLM 0.32 ModelOpt implementation](https://github.com/vllm-project/vllm/blob/v0.32.0/vllm/model_executor/layers/quantization/modelopt.py), [Qwen thinking control](https://huggingface.co/Qwen/Qwen3-8B)

## CPU checks

From the repository root, in a **separate** Python 3.11 environment:

```sh
python3.11 -m venv workers/qwen/.venv
workers/qwen/.venv/bin/python -m pip install -r workers/qwen/requirements-test.txt workers/qwen/vendor/runpod/runpod-1.10.0+pulsefeed.security.1-py3-none-any.whl
workers/qwen/.venv/bin/python -m unittest discover -s workers/qwen/tests -v
```

This environment pins direct test dependencies and the reviewed local Runpod SDK; do not merge it with the app environment. Tests inject synthetic runtimes, import no GPU libraries and make no model/network calls. They cover input/output bounds, tokenizer preflight, templates, deadlines, single-attempt accounting, uncertain outcomes, SDK serialization/cancellation, process termination and cache behavior. [All suites](../../integration/README.md)

The core seam is `QwenWorker(runtime).handle(job)`. The runtime supplies `metadata`, `ready`, `prepare(...)` and `generate(...)`; `Result` carries text, finish reason, tokens and inference time. Synthetic metadata must remain labeled.

## Build and endpoint configuration

Prepare a new private directory outside the repository, then build its allowlisted offline context:

```sh
python3 workers/qwen/prepare_build.py --platform amd64 --output-dir /absolute/private/qwen-build
docker buildx build --platform linux/amd64 --load -t pulsefeed-qwen:candidate /absolute/private/qwen-build
```

The preparer verifies exact public artifact hashes, architecture and full installed-aware lock provenance. The immutable base receives the reviewed NCCL correction, optional LMCache/tool removals and 32 exact Ubuntu security updates before the locked Python overlay. The correction layer preserves the new pinned base GPU packages except the explicit NCCL correction and ARM64 cuSPARSELt metadata repair; model precision stays NVFP4. Native ARM64 additionally repairs the official cuSPARSELt wheel packaging tag without changing native payload bytes. AMD64 removes the unused Mooncake distribution and selects the official same-release TorchCodec CUDA12 wheel; an actual decoder/LLM/sampling/engine-argument import gate verifies this configuration and rejects Mooncake imports without loading a model. See [base corrections](vendor/base-corrections/README.md), [OS updates](vendor/os-security/README.md), [setuptools backport](vendor/setuptools/README.md), and [SDK backport](vendor/runpod/README.md).

[`requirements-runpod.lock`](requirements-runpod.lock) and its [provenance](requirements-runpod.provenance.json) cover every inherited distribution and exact overlay wheel. `check_dependencies.py` requires a completely clean pip check; there are no inherited-warning exceptions. Builds verify the corrected Python and OS inventories before installing the overlay, and verify the full final Python inventory afterward. This does not claim all upstream vulnerabilities are fixed; consult the current dependency security report.

Use a dedicated **queue-based** Runpod endpoint: compatible GPU architecture **Blackwell SM120 with at least 30 GiB reported VRAM** (32 GB class or larger), including RTX 5090 and RTX PRO Blackwell cards. These additional GPUs are accepted by preflight but still require real inference validation; the original target remains RTX PRO 4500 **Blackwell** 32 GB, one GPU, active/min 0, max 1, concurrency 1. Defaults are `INITIALIZATION_TIMEOUT_SECONDS=480`, `EXECUTION_TIMEOUT_SECONDS=240`; initial provider execution timeout is 600 seconds and must be validated against startup/queue/app deadlines. No client or engine inference replay is allowed after uncertain acceptance. [Runpod queue overview](https://docs.runpod.io/serverless/overview)

The worker needs no Runpod API key. Configure `HF_TOKEN` privately only if model access requires it; keep it out of jobs, images and saved commands. No model weights are fetched during Docker build.

## Cache and readiness

| Variable | Behavior |
| --- | --- |
| `MODEL_CACHE_SOURCE=volume` | Default; use `MODEL_CACHE_DIR`, default `/runpod-volume/huggingface` |
| `MODEL_CACHE_SOURCE=runpod-managed` | Override the directory with `/runpod-volume/huggingface-cache/hub` |
| `MODEL_CACHE_OFFLINE=false` | Default; up to three classified transient startup download attempts for the exact pin |
| `MODEL_CACHE_OFFLINE=true` | Require a complete local pinned snapshot, no network/auth or revision fallback |
| `MODEL_REVISION` | Must equal the fixed reviewed revision |

Managed mode does not provision the cache or prove the exact commit is present; select and verify it in the endpoint. [Managed cache layout](https://docs.runpod.io/serverless/endpoints/model-caching#where-cached-models-are-stored)

Startup checks config/tokenizer/thinking template and all referenced safetensors shards, runtime pins, GPU name/capability/memory and NVFP4 metadata. The loaded model uses offline local files. A one-token GPU probe runs before SDK registration; its cost/timing is initialization, separate from invocation usage. Model weights may persist in the cache; generated outputs are never written there.

Before GPU imports, startup verifies SHA-256 hashes for six metadata files from the pinned commit, including the exact weight index and chat template. The index must select the two reviewed safetensors shards; alternate weight files are rejected. Metadata reads reject nonregular files and enforce size limits, including a separate 128 KiB bound for the reviewed 105,079-byte index. Python files, symlinked directories and links outside the snapshot/Hub blobs are rejected; normal Hub file links remain supported. These checks apply to online and offline loading. Keep cache writers trusted and restore the exact upstream snapshot if validation fails. This authenticates the selected metadata, not model weights or all tokenizer files, and does not eliminate concurrent cache mutation during loading. See the [October 20 audit](../../docs/audit-2026-10-20.md).

The model runs in one child process. Parent IPC observes the earlier worker/application deadline and kills the engine process group on cancellation/deadline. Invalidated runtimes remove readiness and request SDK worker refresh after known terminal delivery. The Docker healthcheck verifies parent/engine PIDs, including zombie detection. Unknown dispatch cannot replay. [Source walkthrough](../../docs/code-walkthrough.md#worker-entry-points)

## Input and output

Input is Runpod `{"input": {...}}` containing schema version, stage, UUID run/invocation IDs, future UTC-millisecond deadline, bounded plain article text and system instruction. Optional `article_template` must contain `{{article_title}}` and `{{article_excerpt}}` exactly once with at most 2,000 static Unicode code points. Markers expand once to JSON-quoted values in the user message; do not surround markers with additional quotes. System instructions remain separate. Omission preserves legacy rendering; deploy a compatible worker before sending custom layouts.

The async SDK callback returns `{"result": envelope}`; the application reads provider `output.result`. The core test seam returns the raw envelope. This wrapper avoids Runpod SDK consumption of a top-level `error`; the four-MiB transport bound includes it. A provider `COMPLETED` job can contain `ok:false` and must not be treated as inference success.

Invalid input/preflight has zero inference attempts. Attempts increment before dispatch; unknown token measurements remain null. Each request prepares and generates once; neither dependency errors nor inference failures are replayed by this handler. Retry counter fields remain zero for response compatibility. Bounded transient model-download retries remain separate. SDK INFO logging avoids default DEBUG payload exposure.

## Required real-GPU smoke

Inside the built image on the intended GPU, explicitly run:

```sh
python3 /worker/smoke_test.py --real-gpu --evidence-path /tmp/qwen-gpu-evidence.json
nvidia-smi
cat /worker/build-packages.txt
```

This performs multiple real prompt generations plus a labeled pre-inference failure check that verifies no replay. Record image digest, exact GPU/driver, metadata, token usage, timings and manual prompt quality. Then verify a native endpoint request with fresh IDs/future deadline, submit once and poll the saved job ID. Do not replay `/run` after an uncertain response.

Read [deployment gates](../../docs/deployment.md), [maintenance/unknown recovery](../../docs/operations.md) and [Spark instructions](DGX_SPARK.md) before changing a running worker. Historical raw evidence links may be workspace-only; committed summaries establish their scope.

The final maintained AMD64 build is `sha256:9c1173ac7ef0ee093a3ca0709bf4fcf42888428b7e4d4cb2ef394e5c052208be`; the native ARM64 build is `sha256:e0b3e0957105818aa6888eb64c693d3ef15082d89ff5a6d4a6a6fe1fd676c05a`. Both use reviewed private generated contexts, exact 32-package OS corrections, clean dependency checks and complete installed-inventory verification. CPU construction/import evidence does not establish cloud GPU compatibility; vulnerability findings and source-specific mitigations remain recorded separately.

### Shared-cache symlinks

Qwen v1.0.4-20261020 supports external shared-cache file links by checking exact artifact size and streaming SHA-256 against the pinned model revision. Unknown external files, altered bytes, devices and FIFOs remain rejected. Original snapshot/blob links continue to work. Hashing external weight shards adds startup I/O; memory use is bounded to 4 MiB chunks. Cache writers must still be trusted between validation and model load. Validation failures identify a fixed check name without logging credentials or file contents. This release was locally tested; real Runpod GPU inference remains a deployment gate.

See the [startup incident and cleanup record](../../docs/qwen-runpod-startup-fix-2026-10-20.md) for the storage/cache/deadline fixes, test evidence and v1.0.4 rollback reference.
