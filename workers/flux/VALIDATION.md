# FLUX validation record

Updated: 15 October 2026. This record separates the original CPU implementation
checks from completed Spark GPU validation. Runpod deployment remains pending.

## Initial CPU implementation checks — 14 October 2026

| Check | Observed result |
| --- | --- |
| `workers/flux/.venv/bin/python -m pytest -q workers/flux/tests` | 62 passed |
| Isolated Docker CPU target | 61 passed, 1 skipped; build completed |
| `docker buildx build --check --platform linux/amd64 --target runtime workers/flux` | Completed, no warnings |
| Python compilation of worker, handler, GPU smoke script | Passed |
| `git diff --check -- workers/flux` | Passed |

The Docker CPU target ran on the local `linux/aarch64` engine using its pinned
Python base. Its one skip is the shared-root schema check, because isolated
`workers/flux` build context intentionally excludes root contracts. That check
passes in the repository CPU suite. The GPU runtime base is explicitly amd64;
its configuration check only fetched public image metadata, not CUDA layers.

The SDK tests use the actual installed Runpod 1.12.0 `run_job` and logger source,
with transport helpers isolated. They verify `output.result` preserves both
success and typed failure, INFO logging omits image payloads, and SDK async
cancellation signals the inference thread and waits for cleanup. An
uncooperative native-call double verifies forced worker exit after the bounded
cleanup grace and rejects overlapping inference while the call remains active.
They do not
read local Runpod credentials, make provider calls, or simulate a deployed
endpoint's terminal status transitions.

The local suite also verifies huge numeric inputs fail before inference, fixed
generation argument/seed wiring, token-budget rejection, malformed inputs,
valid WebP/JPEG decoding, finite same-image compression, transport bounds,
startup retry limits, OOM/permanent/uncertain failures without automatic replay,
one explicit safe transient retry, deadlines, concurrency, and attempt counters.
All generated CPU images and injected engines are synthetic.

## Remaining Runpod release checks

- Build the complete amd64 GPU runtime image, retain its digest and dependency
  inventory, and verify startup on RTX PRO 4500 Blackwell 32 GB.
- Run `smoke_test.py` for WebP and JPEG; retain actual model/runtime/GPU metadata
  and observed initialization, first-generation, and warm timings.
- Run controlled retry/retry-exhaustion checks on that runtime and a direct
  native Runpod endpoint request, including cancellation/timeout reconciliation.
- Verify the authenticated Railway app consumes the real `output.result`,
  relays the image transiently, and saves it in browser IndexedDB.

These initial CPU checks did not download model weights or large CUDA image
layers, provision cloud services, or run paid cloud jobs. The subsequent Spark
build and real-GPU evidence is recorded separately below.

## DGX Spark profile preparation — 15 October 2026

Readiness corrections increased the CPU suite to **97 passed**. Separate review verified fatal runtime errors latch local readiness off while preserving known result envelopes. `INFERENCE_OUTCOME_UNKNOWN` also latches readiness off; the shared gateway terminates its process while holding the GPU lock before any terminal publication. A real spawned CPU-process regression confirms exit code 70, and loopback HTTP integration uses actual worker boundaries with synthetic engines. This establishes transport/lifecycle behavior only. The earlier 86-test and CPU Docker counts below describe the pre-correction checkpoint; later native validation follows this preparation record.

- Public NGC manifest/configuration lookup confirmed the pinned PyTorch 25.11
  Linux/ARM64 manifest, CUDA 13.0.2 and Torch build `2.10.0a0+b558c98`. Only
  small registry metadata was retrieved; no image layers or weights were pulled.
- Added explicit `dgx-spark` profile guards and a real BF16 kernel preflight
  before weight loading; retained the default strict Runpod Torch/CUDA profile.
- Added the root-context Spark Dockerfile and root-gateway factory contract,
  without implementing or modifying the shared gateway/compose files here.
- CPU suite after the new profile: **86 passed**. New cases use injected
  runtime evidence and mocked kernels; they are not Spark GPU evidence.
- Lightweight CPU Docker build: **85 passed, 1 expected root-contract skip**;
  image built successfully. The Spark Dockerfile's ARM64 `--check` completed
  without warnings using only public registry metadata. Compilation and
  whitespace checks passed. Neither command built/pulled the NGC GPU layers.
- At this preparation checkpoint, package-overlay resolution, the full ARM64
  build and GPU inference were still pending. The earlier Runpod CPU checks
  did not establish those gates.

## Native Spark validation — 15 October 2026

The Spark image built successfully with the vendor Torch/CUDA stack retained,
the hash-pinned `pynvml` metadata repair, and full `pip check`. The final worker
CPU suite before deployment passed **99 tests**. A disposable CPU environment
also confirmed the repair preserved `nvidia-ml-py==13.580.82` and its import.

| Evidence | Observed result |
| --- | --- |
| Final Spark image | `sha256:1e2f0ce7345117dd569248f975649d3533a89da611811cf7fb3f4615e8871af3` |
| Device/platform | GB10, SM 12.1, Linux/aarch64, driver `580.178.04` |
| Runtime | Torch `2.10.0a0+b558c986e8.nv25.11`, CUDA `13.0`, Diffusers `0.37.1`, Transformers `4.57.6` |
| Model | Real BF16 `black-forest-labs/FLUX.2-klein-4B`, revision `e7b7dc27f91deacad38e78976d1f2b499d76a294` |
| Recorded HTTP smoke | 1024 × 1024 WebP; 3385.928 ms inference, 4297 ms wall; one attempt, zero retries |
| Prior standalone generation | 3890.256 ms inference, 5090 ms wall; 433,420-byte image |
| Laptop FastAPI/browser NASA feed | Three successful Qwen → FLUX runs, three images, zero retries |
| Quota reconciliation | Three image charges settled against the 100-image daily allowance |
| Browser refresh | Operator observed all three images restored from IndexedDB |

[Model smoke evidence](../../integration/artifacts/spark-2026-10-15/model-smoke.jsonl)
records the real model/runtime metadata and HTTP smoke counters.
[App checks](../../integration/artifacts/spark-2026-10-15/app-live-checks.jsonl)
record the three-run batch, usage reconciliation, ownership/privacy checks,
and replay/refresh without additional invocations. That automated file does not
itself establish browser Blob restoration; restoration was observed separately
in the laptop browser.

These runs establish native Spark build compatibility, real pinned-model BF16
inference, and the laptop browser flow with both model services running. They
do not establish native CUDA cancellation, controlled GPU failure/retry paths,
JPEG output, or peak memory under broader workloads. The amd64 Runpod image,
native Runpod endpoint, and Railway deployment remain unvalidated.

## Packaging, cache policy and Spark overlay lock — 15 October 2026

The packaging/cache CPU suite passed **134 tests**, with **one expected skip**
for the separate Runpod target lock that is still being generated. The new
cache tests use tiny synthetic files, not weights: complete cached-only startup,
managed/volume path selection, old/new offline flag compatibility, absent or
truncated shards, dangling blob links, other-revision-only caches, and rejection
before model deserialization. Compilation and scoped whitespace checks passed.

The Spark lock/provenance were copied without modification from the coordinator's
installed-aware resolver report on the exact native ARM64 NGC base. Seven
hashed binary overlay wheels are installed without dependency resolution;
the original NVML repair hash, vendor constraints, Torch/NVML guards and full
`pip check` remain required. CPU checks bind the lock to its base/platform/Python,
direct requirements, report wheel hashes and preserved vendor GPU packages.
The provenance is not copied into the runtime image.

BuildKit pip cache mounts and narrow runtime source copies are configured;
the isolated context excludes nested secrets, databases, artifacts and weights.
No GPU configuration, BF16 preset, allocator cleanup or NVIDIA entrypoint was
changed. This worker-side pass performed no Docker builds, remote commands,
model calls or large downloads. A fresh native build/inventory replay and the
separate amd64 Runpod lock remain coordinator-owned validation gates; earlier
Spark image/model evidence does not validate this newer packaging automatically.

## Separate amd64 lock and native Spark replay — 15 October 2026

The coordinator supplied the installed-aware report from the exact pinned
amd64 Runpod base (Python 3.11, x86_64; base `pip check` passed). Its lock and
provenance were copied unchanged to `requirements-runpod.lock` and
`requirements-runpod.provenance.json`: **95 overlay wheels**, including the
Runpod SDK dependency closure, with no protected GPU-stack changes.

The actual Runpod Dockerfile installs this overlay with
`--no-deps --require-hashes --only-binary=:all:`, retains its immutable GPU base
and startup command, explicitly uses the BuildKit pip cache, checks the base
Torch version and runs full `pip check`. Both profile-lock tests bind the base,
Python/platform, source model/dependency pins, wheel hashes and Docker install
flags to the target reports. The full worker CPU suite passed **135 tests with
zero skips**; compilation and scoped whitespace checks passed.

Separately, the coordinator reported a successful updated native Spark build,
complete inventory verification, network-disabled cached startup, real FLUX
inference and a three-image app flow. No further Spark runtime changes followed
that result. The new amd64 Dockerfile build/inventory replay is next; real
Runpod GPU/endpoint and Railway deployment gates remain pending. This worker
pass itself performed no Docker builds, remote commands or model calls.

## Final cloud packaging verification — 15 October 2026

The coordinator built the actual AMD64 runtime Dockerfile under isolated CPU
emulation, with all 95 wheel hashes enforced. Full `pip check`, final inventory
comparison, and network-disabled imports of the SDK, handler, Torch and actual
`Flux2KleinPipeline` passed, as did native WebP encoding. Refreshed provenance
includes legacy and shadowed distribution metadata; its wheel pins and hashes
remain identical. Independent review found no remaining findings. These are
image packaging checks, not cloud GPU/endpoint inference. See the consolidated
[packaging validation](../../local/PACKAGING_VALIDATION.md) for native Spark and
cloud build evidence and remaining deployment gates.


## Clean dependency runtime — 20 October 2026

This supersedes the earlier Conda/NGC runtime and overlay-lock descriptions. Both profiles now build from immutable official CPython 3.12.15 slim-trixie and install complete hash-locked wheel closures: Torch 2.13.0+cu129 / CUDA 12.9, Diffusers 0.41.0, Transformers 5.20.2, Accelerate 1.15.0, Hub 1.33.0 and Pillow 13.0.0. The model revision, BF16 components and 1024-square/four-step/guidance-one preset remain unchanged. Native has 65 distributions without the SDK; AMD64 has 147 with Runpod SDK 1.12.0.

The actual complete pip reports generated fresh-base provenance and canonical locks. External inventory checks verified every package/version and the exact Torch URL/hash on native ARM64 and AMD64 under explicit BuildKit QEMU, without host binfmt changes. The native ARM64 wheel required a transparent cuSPARSELt packaging repair; independent read-back reproduced the output and confirmed unchanged CUDA payloads and complete RECORD coverage. The source/output/recipe/payload hashes are committed in `cusparselt-repair.provenance.json`. The obsolete NGC constraint/NVML metadata workaround was removed.

| Checkpoint | Evidence |
| --- | --- |
| Native candidate | `sha256:ba77bd6a33584e0651432254ec9491747229112e61df97ec8e8b19d8f6e7e94e` |
| Maintained native Dockerfile rebuild | `sha256:310c2353d83f7b86bd360cbfb71ee6f9bd68634cc6d9d89146ba5323e4cb0234`; imports, pip check and external complete lock/inventory verification passed |
| AMD64 candidate | `sha256:1bbe6e18295c8ab78ac04b32b63deb6e4f4cbb7ec046da27d3b7290d76130043`; imports, pip check, 147-entry report and external QEMU verification passed |
| Maintained AMD64 Dockerfile rebuild | `sha256:4ac720be06ce786143441074b62d25c714cb009ee960e252b07483edff9d27d2`; SDK/pipeline imports, pip check and external complete 147-package URL/hash verification passed |
| CPU regression suite | 168 passed in the full workspace; maintained AMD64 CPU target 162 passed / 6 skipped because shared repository files are outside its isolated context |
| Native device probe | NVIDIA GB10 CC12.1, BF16 matmul and SDPA passed; Python 3.12.15 / OpenSSL 3.5.7 / pip 26.2.1 |
| Candidate native inference/app flow | First/warm BF16 image generation and 27-image RSS flow passed, decoded 1024-square WebP, correct accounting, no retries |

The maintained native image also passed exact-ID first/warm inference and a second 27-image full RSS flow, with decoded 1024-square WebP, correct accounting and zero retries. Final independent scan/release results are tracked in the [dependency remediation report](../../docs/dependency-remediation-2026-10-20.md). Actual RTX PRO 4500 cloud inference remains unverified.

Fresh final scans of both maintained images reported identical 168 advisory tuples (0 critical, 57 high, 54 medium, 10 low, 47 negligible), comprising 161 Debian and seven Python binary CPE matches, with no Python-package advisory matches. Remaining findings require the documented release triage; this is not a blanket clean-image claim. Accelerate 1.15.0 still contains the upstream unvalidated shard-index path join. Authentication of this worker's exact pinned index/template metadata protects the fixed snapshot before loading; it does not patch Accelerate itself or remove the trusted cache-writer boundary.
