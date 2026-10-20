# Container packaging validation — 15 October 2026

**Historical checkpoint.** The measured images, package counts and accepted vendor diagnostics below remain unchanged. The [October 20 remediation](../docs/dependency-remediation-2026-10-20.md) supersedes these recipes: native Qwen now has an overlay and both workers have different dependency stacks. Its final maintained native pair passed a third 27-image flow; corrected AMD64 Qwen passed 87 exact-image CPU/SDK tests, strong imports, provenance and independent scan review. The rejected `466055…` checkpoint and its correction remain documented in that report. Do not apply these historical pip-check exceptions to a new image, infer cloud deployment, or treat the remaining findings as cleared.

This pass preserves the separate Qwen and FLUX services, NVIDIA base digests,
entrypoints, model revisions, precision, GPU allocation, inference serialization,
and startup/execution budgets. FastAPI remained running on the laptop at port 8017.

## Native Spark result

Both images rebuilt successfully on Linux ARM64. A second build reused the
dependency/runtime layers and produced the same image identities:

| Worker | Image ID |
| --- | --- |
| Qwen | `sha256:053ac4371ec8534c4baa99686622cc89a496def73081ac45f16b4015ff691ddd` |
| FLUX | `sha256:aee0b35aac528ac1ba57180e2b6e38dd1722d21d16e6fbcd421d31df3acbdb08` |

FLUX's seven-wheel overlay was resolved inside the exact native NVIDIA base,
installed with SHA-256 checking and no second dependency resolution, and passed
`pip check`. Its complete installed inventory matched the checked-in provenance.
Qwen installs no additional wheels; its immutable NVIDIA base and runtime lock
continue to supply the GPU stack. Complete base and final package inventories
were captured independently and compared byte-for-byte; they are identical.

Qwen's untouched NVIDIA base reports two `pip check` messages for one existing
metadata mismatch: `apache-tvm-ffi==0.1.7` versus the `0.1.9` requirements of vLLM
and xgrammar. The rebuilt image reports the identical mismatch. No vendor GPU
package was changed to suppress it. The real model checks below passed; this is
an acknowledged vendor packaging limitation, not a clean `pip check` claim.

Both new images resolved their exact existing Hugging Face checkpoint snapshots
with **networking disabled**, no GPU devices, and read-only cache mounts. Offline
startup is now enabled for both Spark services in their private environment.
Missing or incomplete pinned caches fail readiness; there is no revision fallback.
Model file presence/size checks are completeness checks, not cryptographic weight
integrity verification.

Both running containers are healthy. Effective Docker configuration confirms the
`local` log driver with `max-size=10m` and `max-file=3` on each. Previous images
remain tagged `before-packaging-20261015` for rollback. Existing cache volumes were
retained; no model cache or database was cleared.

## Live checks after activation

| Check | Result |
| --- | --- |
| Qwen invalid input | Rejected before inference |
| FLUX invalid input | Rejected before inference |
| Qwen real NVFP4 generation | 107 input + 55 output tokens, 1,754 ms inference, zero retries |
| FLUX real BF16 generation | Valid 1024 × 1024 WebP, 3,689 ms inference, zero retries |
| Laptop FastAPI RSS flow | Ten articles, three images, 793 Qwen tokens, zero retries |
| Reader lifetime | Exactly 3,600 seconds |
| Same-key replay and page refresh | Original batch reused; no new model calls |
| Another visitor's reader access | Generic HTTP 404 |
| Unauthenticated model API access | Both workers returned HTTP 401 |
| Final application state | Zero active batches; exactly three added runs and six invocations |

Timings describe individual smoke calls, not a benchmark. This pass used real HTTP
and image decoding; it did not repeat browser layout or IndexedDB visual checks.
Sanitized local evidence: [model smoke](../integration/artifacts/locks/spark-smoke.jsonl)
and [application flow](../integration/artifacts/locks/app-flow.json).

## Dependency maintenance

Local application/gateway checks passed (325 checks across their component
suites), as did the 71 integration/lock-helper checks and all seven JavaScript
test files. Worker CPU suites passed independently. The isolated FLUX CPU Docker
target passed 129 checks, with six expected skips for root/image metadata absent
from that deliberately small test image. Temporary loopback-server tests needed
host socket permission; rerunning those with that permission passed.

See [the lock workflow](DEPENDENCY_LOCKS.md). Model caches are separate from pip
build caches, and pip cache mounts do not become runtime image layers. Runtime
COPY lists are explicit; build exclusions cover credentials, development caches,
database files, logs, and model weights.

## AMD64 cloud image packaging

Both actual cloud Dockerfiles built successfully on the Spark using an isolated
BuildKit builder's bundled CPU emulation. No host emulation registration or GPU
configuration change was needed. Verification layers mounted the final lock
helper and provenance without copying test tooling into production images.

| Profile | Python | Locked overlay | Result |
| --- | --- | --- | --- |
| Qwen Runpod | 3.12 / x86_64 | 47 wheels | Hash install, strict baseline check, complete inventory and imports passed |
| FLUX Runpod | 3.11 / x86_64 | 95 wheels | Hash install, clean `pip check`, complete inventory, imports and WebP encoding passed |
| FLUX Spark | 3.12 / aarch64 | 7 wheels | Hash install, clean `pip check`, complete inventory and real GPU flow passed |
| Qwen Spark | Vendor base | No overlay | Full package inventory identical to immutable base; real GPU flow passed |

Imports ran with networking disabled and no GPU devices. FLUX imported the actual
`Flux2KleinPipeline`, Torch, handler and Runpod SDK and encoded a WebP. Qwen imported
Torch, Transformers, Tokenizers, Hugging Face Hub, handler/runtime and Runpod SDK.
Repeated builds demonstrated both cached layers and cached wheel downloads.

The untouched Qwen **cloud** base has a separate pre-existing diagnostic:
`pygobject 3.42.1 requires pycairo, which is not installed.` Its build permits only
that exact single diagnostic (exit 1, empty stderr), or a clean dependency check;
all other errors fail. This unused OS GUI package is preserved. Shadowed OS
package copies and legacy dependency metadata are recorded and checked, including
the missing `pycairo>=1.16.0` requirement. Both FLUX dependency closures are clean.

Final independent review found no remaining actionable findings. The shared lock
helper passed 21 tests; Qwen passed 64 worker tests; FLUX passed 135. All three
lock/provenance pairs regenerate exactly from the exact-base inventories and
original installed-aware pip reports, without changing any resolved wheel hash.

Sanitized build evidence: [Qwen](../integration/artifacts/locks/qwen-cloud-final.log)
and [FLUX](../integration/artifacts/locks/flux-cloud-final.log). Cloud builds remain
in the isolated builder cache; nothing was pushed or deployed. The helper builder
is stopped after validation, retaining its cache for later builds.

Real Runpod GPU inference, managed-cache mounting, and Railway deployment remain
cloud release gates; CPU image checks and local Spark evidence do not establish them.
