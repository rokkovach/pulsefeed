# Live DGX Spark validation

## Current remediation checkpoint — 20 October 2026

The final maintained native images completed a third isolated application flow: nine stories each from Hacker News, BBC England and NYTimes, **27 decoded images / 54 successful model calls / zero retries**. Qwen reported **13,140 input + 2,016 output = 15,156 tokens**. Page durations were **64.963 / 64.200 / 61.949 seconds**, respectively; these are observed Spark timings, not cloud guarantees or a benchmark. Evidence is `live/flow-3-maintained/summary.json` under the ignored remediation artifact directory.

| Maintained native profile | Exact local image ID |
| --- | --- |
| Qwen | `sha256:e0b3e0957105818aa6888eb64c693d3ef15082d89ff5a6d4a6a6fe1fd676c05a` |
| FLUX | `sha256:310c2353d83f7b86bd360cbfb71ee6f9bd68634cc6d9d89146ba5323e4cb0234` |

These images are restored on the normal Spark loopback ports. The maintained checkout is `/home/vacko/pulsefeed-dependency-remediation-20261020/src`; its private mode-600 `local/.env` copy preserves the original values, and quiet `control.sh config` passes. Normal image aliases identify the tested native pair; the original environment file and older checkout are retained. The original laptop FastAPI remains healthy on port 8017, with its settings, credentials, database and allowance state preserved. The maintained native Qwen image passed 72 worker tests and 13 setuptools backport tests; current source checks passed 87 Qwen tests and 165 integration tests with 75 subtests. FLUX's separate AMD64 artifact has passed CPU packaging and independent scan gates. No real cloud GPU execution, remediation registry push, Runpod deployment or Railway deployment is claimed. See the [remediation report](../docs/dependency-remediation-2026-10-20.md) and [integration record](../integration/VALIDATION.md) for exact scope and subsequent identities.

AMD64 Qwen `sha256:466055136e035686afd5cf957d9c377213a1f06d622e9292b44703522e89a1ca` is retained as a rejected historical checkpoint. Its 86 CPU/SDK tests, guarded imports, full inventories and strict pip check passed under verified QEMU, but its scan found 1,944 matches including three Critical and 49 High. Of these, 37 Go findings were in optional Mooncake `libetcd_wrapper.so`.

The independently reviewed replacement removes complete unused Mooncake and selects official TorchCodec `0.17.0+cu126` using existing CUDA 12.9 libraries, without added CUDA libraries. All other inventories and all 32 overlay artifact hashes are unchanged. Native inputs remain byte-identical, so the final native flow above remains valid. The replacement passed network-disabled real decoder / `LLM` / `SamplingParams` / `EngineArgs` imports with Mooncake absent/denied, KV/EC transfer unset and expert parallel disabled.

The replacement local image is `sha256:9c1173ac7ef0ee093a3ca0709bf4fcf42888428b7e4d4cb2ef394e5c052208be`; its 87 exact-image CPU/SDK tests, strict pip check, full provenance and absence of staged archives passed. Independent source/package review passed.

Its fresh scan reports 1,907 findings: zero Critical, 22 High, 1,690 Medium, 176 Low and 19 Negligible. Exactly 37 Go advisory keys are removed versus rejected `466055…`, with zero new keys; independent raw comparison passed. All 22 High findings exactly match native FFmpeg 8.1.1 findings. Independent review verifies the two-distribution change, 281 active distributions, unchanged shadows, all 93 Mooncake-owned files and metadata/executable leftovers absent, and all 156 installed TorchCodec payload files matching the official wheel. FFmpeg/native, OS and source-specific findings remain open as scoped in the remediation report; zero Critical scan matches is not blanket vulnerability clearance. CPU emulation proves packaging contracts, not AMD64 GPU kernels or native Runpod queue behavior.

Earlier remediation checkpoints remain distinct: the first candidates passed a 27-image flow and cancellation checks; a second flow on Qwen `f699a90f…` and FLUX `310c2353…` passed another 27 images / 54 calls with zero retries. That Qwen image contained 31 OS updates. The third maintained flow above covers the final 32-update native recipe, including `python3-jwt 2.7.0-1ubuntu0.4`; it does not establish AMD64 compatibility.

The maintained native profiles use Qwen vLLM 0.32/PyTorch 2.13 CUDA 13.0 with FlashInfer 0.7.0.post1, and FLUX Python 3.12.15/PyTorch 2.13 CUDA 12.9/Diffusers 0.41.0/Transformers 5.20.2. Native workers use the authenticated gateway without the Runpod SDK. Models, pinned revisions, NVFP4/BF16 precision and the shared GPU execution boundary remain fixed. These are distinct from the AMD64 CUDA 12.9 queue images and their separate target-GPU gates. Targeted repairs and scans do not establish complete vulnerability clearance.

## Historical check — 16 October 2026 (Europe/Vienna)

The native images were rebuilt with configurable retained-result capacity (default 128, validated range 1–256) and safe aggregate health counts. Model weights, runtime dependencies, GPU settings, separate workers and shared execution lock were preserved. Image identities at that checkpoint:

- Qwen: `sha256:cb6f536361d02068945f45b029ec7e1ada76701fd901da9bb114986a60465e0b`.
- FLUX: `sha256:83a49b76b3ba1fc60764087b12f51b64cd34b0b9fd2da0c3898851821a3dc776`.

After both workers became ready, a fresh Hacker News page completed ten real 1024 × 1024 images and twenty successful model calls in 93.034 seconds. Qwen measured 4,695 input / 733 output tokens; FLUX measured ten images / ten attempts, with no retries. Refresh made no extra calls. Both workers ended healthy, idle and retaining ten results each. See the [full validation record](../integration/VALIDATION.md) for the first rejected batch, the separate batch interrupted by the restart, conservative accounting and the final nine-story setting. The final success does not erase those earlier outcomes.

## Original check — 15 October 2026

The coordinator ran the actual models on NVIDIA GB10 (SM 12.1), Linux aarch64, kernel driver **580.178.04**. Both authenticated worker containers were healthy and resident together. FastAPI ran on the laptop at `http://127.0.0.1:8017` in explicit non-synthetic `local_http` mode, through SSH loopback forwards 8181/8182. The final post-run host observation showed approximately **85 GiB available** and unused swap; this is a point-in-time observation, not a capacity benchmark.

## Real model smoke

| Model | Precision | Model inference | HTTP wall time | Measured output |
| --- | --- | --- | --- | --- |
| `nvidia/Qwen3-8B-NVFP4` | NVFP4 | 1,815.103 ms | 2,076 ms | 107 input + 55 output = 162 tokens; one attempt, zero retries. |
| `black-forest-labs/FLUX.2-klein-4B` | BF16 | 3,385.928 ms | 4,297 ms | One decoded 1024 × 1024 WebP; one attempt, zero retries. |

Both invalid-input checks rejected requests before inference. The smoke validated pinned revisions, correlation IDs, output envelopes and decoded image data, then reported `PASS`. Evidence: [model-smoke.jsonl](../integration/artifacts/spark-2026-10-15/model-smoke.jsonl). These direct operator calls bypass app quotas and are separate from the browser batch. Timings are individual measured calls, not a throughput or cold-start benchmark.

Final native image identities:

- Qwen: `sha256:b27a6f9bf7d0eed12ae6f6586ddd23601c5a0f3532ec177532132fb39a4f05d5`.
- FLUX: `sha256:1e2f0ce7345117dd569248f975649d3533a89da611811cf7fb3f4615e8871af3`.

Model revisions: Qwen `ccd10a893cbca613259517c3efe08e151ddf2b8e`; FLUX `e7b7dc27f91deacad38e78976d1f2b499d76a294`. Qwen used the vendor vLLM 26.05/CUDA 13.2 runtime with explicit FlashInfer attention/NVFP4 backend and a 1 GiB BF16 KV cache. FLUX used diffusers 0.37.1, transformers 4.57.6 and the preserved NVIDIA PyTorch 25.11/CUDA 13.0 stack. Runtime metadata is in the smoke evidence.

## Connected browser/application flow

The real NASA feed displayed **ten stories and three completed illustrations**. Its batch recorded exactly three Qwen and three FLUX invocations, 582 input + 286 output = **868 Qwen tokens**, three images, and zero inference/dependency retries. The actual browser reload restored all three fully loaded 1024 × 1024 Blob images with “saved in this browser” status.

The authenticated admin dashboard showed **3 used, 0 reserved, 97 remaining, global daily limit 100**, shared feed limit 10/day and zero feeds at their limit. Its two live model rows each had three successful calls and zero failed, pending or unknown calls. FLUX tokens were N/A rather than invented zero.

Owning-account feed/batch access passed. Cross-account feed, batch and run access returned 403; evaluator access to aggregate admin usage was denied. Aggregate reports excluded feed/article content. Exact-key replay returned the original batch; replay and reader refresh created no new feed/view/batch/run/invocation records. Evidence: [app-live-checks.jsonl](../integration/artifacts/spark-2026-10-15/app-live-checks.jsonl), supplemented by the coordinator's actual browser observations above.

The live correction pass retained NVIDIA's entrypoint/CUDA compatibility wrapper, repaired `pynvml` using a hash-pinned compatibility package without replacing the NVIDIA NVML binding, made Qwen chat-template output explicitly `return_dict=False`, added model prefetch outside startup deadlines, and limited transient disconnect tolerance to readiness GETs. No automatic `/run` replay was introduced.

## Scope of proof

This establishes the local Spark hardware/runtime and laptop-to-Spark full flow. It does not establish Runpod AMD64/RTX PRO 4500 compatibility, Railway deployment/retained volume, cloud HTTPS/proxy behavior, repeated benchmark statistics, or destructive live cancellation/deadline stress. Those remain separate gates. Secrets and generated image bytes are absent from the JSONL evidence; images remain browser Blobs or explicitly saved operator artifacts.
