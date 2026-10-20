# Qwen on DGX Spark

This opt-in local profile preserves `nvidia/Qwen3-8B-NVFP4` revision
`ccd10a893cbca613259517c3efe08e151ddf2b8e`, the shared worker envelope, thinking-off
chat template, 256-token preset, counters, retries, deadlines, and cancellation.
The existing Runpod Dockerfile and default `runpod` profile retain the RTX PRO 4500
Blackwell requirements. Selecting Spark is an explicit hardware/runtime change.

## Image and hardware

`Dockerfile.dgx-spark` uses the official vLLM 0.32.0 Ubuntu 24.04 ARM64 image at immutable digest `sha256:b1bea6490db94268e68577929ae4b800a662b91a3cd4a1fcba81cfb3946ddcff`: Torch 2.13.0+cu130, CUDA 13.0, FlashInfer 0.7.0.post1 and Transformers 5.19.0. Startup requires Linux ARM64 and one GB10 (SM12.1), checks the exact runtime inventory and executes real NVFP4 kernels before readiness. The v0.32 adapter explicitly selects `kernel_config.linear_backend=flashinfer_cutlass`, disables prompt embeddings, supplies token IDs and neutral sampling penalties, and preserves the limits below.

Prepare an allowlisted private context before building (or use `local/control.sh build`, which prepares and cleans it automatically):

```sh
python3 workers/qwen/prepare_build.py --platform arm64 --output-dir /absolute/private/qwen-build
docker build --platform linux/arm64 -f /absolute/private/qwen-build/workers/qwen/Dockerfile.dgx-spark -t pulsefeed-qwen:candidate /absolute/private/qwen-build
```

The reviewed corrections align NCCL with Torch's 2.29.7 requirement, repair only cuSPARSELt wheel metadata, remove unused LMCache and six apt administration packages, and apply 32 exact Ubuntu security updates. The complete corrected baseline and final Python closure are verified offline; no dependency-check warning is suppressed. EP/M2N NCCL compatibility is outside this single-GPU dense-worker configuration. See [the worker build instructions](README.md) and maintained vendor provenance. The final 32-update maintained image `sha256:e0b3e0957105818aa6888eb64c693d3ef15082d89ff5a6d4a6a6fe1fd676c05a` passed strict dependency/inventory checks, 72 supported CPU tests, imports, real first/warm NVFP4 inference, and a 27-image RSS flow with 54 model calls and zero retries on the normal operator path on 2026-10-20.

## Memory and execution settings

| Setting | Spark value |
| --- | --- |
| Attention / NVFP4 GEMM | `FLASHINFER` / `flashinfer-cutlass` |
| Model dtype / KV dtype | BF16 for unquantized operators / BF16 KV; checkpoint remains NVFP4 |
| Context / output | 4096 / 256 tokens |
| Concurrent sequences / batched tokens | 1 / 4096 |
| KV allocation | 1 GiB explicitly |
| GPU utilization argument | 0.25 |
| CUDA graphs / prefix cache | Eager execution / disabled |
| Startup / execution limits | 480 / 240 seconds; job deadline can shorten execution |

[vLLM's pinned cache configuration](https://github.com/vllm-project/vllm/blob/v0.32.0/vllm/config/cache.py)
states that explicit KV bytes override utilization-based cache sizing. **Neither
setting is a hard cap on total model/workspace/system memory.** The pinned checkpoint's
36 layers, 8 KV heads, and 128 head dimension need approximately 576 MiB for BF16
K+V at 4096 tokens before alignment, so the cache budget is 1 GiB rather than 512 MiB.

[NVIDIA warns about aggressive allocation on unified-memory systems](https://docs.nvidia.com/deeplearning/frameworks/vllm-release-notes/rel-26-05.html).
Start Qwen before FLUX, serialize inference through the shared gateway lock, and
measure combined memory pressure. Co-resident Qwen and FLUX passed the combined smoke
and three-story app flow below; this does not establish headroom for larger workloads.
Do not raise memory settings automatically or substitute quantization to hide OOM.
Conflicting emulation/FBGEMM/batch-invariant kernel overrides fail startup.

## Build and gateway integration

Use the private prepared-context commands above, or `local/control.sh build` for shared Compose integration. The allowlisted generated context supplies the stdlib gateway and excludes virtual environments, secrets, weights and evidence. The official public vLLM base starts the Python gateway directly; it has no NVIDIA framework entrypoint wrapper. The gateway imports `LOCAL_WORKER_FACTORY=local_factory:create_worker` from `/worker`. No Runpod SDK lifecycle is required in this image.

Use `.env.dgx-spark.example` as configuration reference. The shared deployment must
provide a random `LOCAL_WORKER_TOKEN`, port/tunnel routing to the laptop, a persistent
model-cache mount at `/runpod-volume/huggingface`, and the same `/coordination` volume for both
models. `LOCAL_GPU_LOCK=/coordination/gpu.lock` serializes gateway calls; separate
unshared directories do not coordinate anything. Never expose a privileged worker
directly to the browser. Shared Compose, gateway, laptop FastAPI settings, and launch
scripts are maintained outside this lane.

The default `MODEL_CACHE_SOURCE=volume` keeps the current `MODEL_CACHE_DIR` and
prefetch-volume layout. After successful prefetch, optionally set the container's
`MODEL_CACHE_OFFLINE=true` to remove Hub access from startup. The worker resolves
only the pinned revision with `local_files_only=True` and verifies the configuration,
tokenizer/template, and all indexed safetensors shards before loading the model.
Missing or partial cache fails readiness with `MODEL_NOT_READY`; there is no online
or revision fallback. Leave offline mode false during an initial download. The
alternative `runpod-managed` source selects the provider's cloud cache path and
overrides `MODEL_CACHE_DIR`; it is not the existing Spark volume. See the
[cache deployment settings](README.md#cache-source-and-offline-startup).

`LocalQwenWorker.handle(job,cancelled=threading.Event())` returns the raw envelope;
the gateway wraps it as `output.result`. `ready` reflects the actual child process,
and `close()` kills its process group with a bounded join. Deadline/cancellation
invalidates the engine rather than replaying work. After such a failure, retrieve
the current failure result and explicitly restart the container if the gateway has
not recycled it. Docker does not automatically restart a merely unhealthy container.

## Spark validation: 2026-10-15

The combined gateway smoke passed with both models resident after the tokenizer
return-type fix. The final Qwen image ID was
`sha256:b27a6f9bf7d0eed12ae6f6586ddd23601c5a0f3532ec177532132fb39a4f05d5`.
The tested machine was Linux `aarch64`, NVIDIA GB10 SM12.1, kernel driver
`580.178.04`. NVIDIA's entrypoint enabled compatibility driver libraries
`595.58.03`; `cuDriverGetVersion` reported `13020`.

| Runtime component | Observed version |
| --- | --- |
| vLLM | `0.20.1+7124b12a.nv26.5.50625635` |
| Torch | `2.12.0a0+5aff3928d8.nv26.5.50603568` |
| Transformers | `5.6.0` |
| FlashInfer | `0.6.10+cf494fca.nv26.5.cu132.50619265` |
| CUDA / attention / NVFP4 GEMM | `13.2` / `FLASHINFER` / `flashinfer-cutlass` |

[The real model smoke](../../integration/artifacts/spark-2026-10-15/model-smoke.jsonl)
records 1,815.103 ms Qwen inference and 2,076 ms wall time: 107 input tokens,
55 output tokens, 162 total, one inference attempt and zero retries. It also records
successful FLUX inference and input-validation rejection checks for both workers.

[The laptop app checks](../../integration/artifacts/spark-2026-10-15/app-live-checks.jsonl)
record three completed NASA story runs: three Qwen invocations, 582 input tokens,
286 output tokens, 868 total, zero retries, and three generated images. Replay and
reader refresh created no new invocations. The operator also confirmed the browser
displayed all three images and restored them from IndexedDB after refresh; the
[reader screenshot](../../integration/artifacts/spark-2026-10-15/reader-live.png)
captures the live reader. These are observed results for this pinned Spark setup,
not a latency or memory guarantee. The separate x86 Runpod deployment and its
RTX PRO 4500 Blackwell GPU validation remain pending.

## Repeating verification

CPU tests, including both profiles, require no weights:

```sh
workers/qwen/.venv/bin/python -m unittest discover -s workers/qwen/tests -v
```

For standalone GPU evidence, stop resident model containers first. Override the
image entrypoint with `python3` and pass
`/worker/smoke_dgx.py --real-gpu --evidence-path /evidence/qwen.json` as its command,
mount a writable `/evidence` directory and the model cache, and grant GPU access.
The script tests real first/warm prompts and labeled dependency-failure injection.
Then run the shared gateway smoke test with both models resident, capture raw
`nvidia-smi`, host memory pressure, driver/package versions and image digest, and
inspect prompt quality. CPU doubles and successful image construction alone do not
prove GPU compatibility; retain real inference evidence when changing the image,
host driver, model, or memory settings. Larger workloads and GPU fault/cancellation
scenarios still need their own validation.
