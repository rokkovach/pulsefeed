# Worker dependency remediation — October 20, 2026

**Status: rebuilds and predeployment verification complete.** Both separate native Spark images passed real GPU smoke tests and a 27-image RSS flow. Both AMD64 images passed their CPU, import, dependency and full-image scan checks, with independent source and artifact review. Qwen's rejected AMD64 checkpoint exposed unused Mooncake advisories and a TorchCodec CUDA mismatch; the replacement fixes both and passes the stronger actual-engine gate. No registry push, Railway deployment or Runpod endpoint deployment occurred. Actual Runpod GPU/queue execution remains the deployment gate described below.

The laptop FastAPI service and its operator database/settings were preserved. RSS tests used a separate database. Initial isolated worker tests used temporary credentials; the final maintained-image flow used the restored normal authenticated worker connection. Existing model caches and previous image tags remain available.

## Fixed scope

| Worker | Unchanged model and revision | Unchanged GPU target |
| --- | --- | --- |
| Qwen | `nvidia/Qwen3-8B-NVFP4`, `ccd10a893cbca613259517c3efe08e151ddf2b8e`; NVFP4 | Runpod RTX PRO 4500 **Blackwell** 32 GB / SM120; separate Spark GB10 / SM121 profile |
| FLUX | `black-forest-labs/FLUX.2-klein-4B`, `e7b7dc27f91deacad38e78976d1f2b499d76a294`; BF16, 1024 × 1024, four steps, guidance 1 | The same respective GPU targets, in a separate container |

Runtime upgrades were tested without changing checkpoints, quantization, image presets, per-worker inference concurrency or the shared Spark GPU lock. Spark's verified driver was `580.178.04`. ARM64 success does not establish AMD64/SM120 kernel compatibility.

## What changed

### FLUX: a clean, fully locked runtime

Both profiles now use digest-pinned `python:3.12.15-slim-trixie` and official PyTorch `2.13.0+cu129` wheels. Their full closures contain **147 packages on AMD64** and **65 on ARM64**; native Spark uses the authenticated local gateway and does not install the Runpod SDK.

The principal pins are Diffusers `0.41.0`, Transformers `5.20.2`, Accelerate `1.15.0`, Hugging Face Hub `1.33.0`, Safetensors `0.8.0`, Pillow `13.0.0`, pip `26.2.1`, setuptools `84.2.0`, wheel `0.48.0`, and Runpod `1.12.0` on AMD64. The old inherited NGC/Conda runtime and its repair helper are no longer needed.

Canonical locks select the actual architecture-specific artifacts and bind their SHA-256 hashes to install-report provenance. Downloads use bounded retries; installations require hashes, binary wheels and a clean `pip check`. Build gates import the actual FLUX pipeline, Qwen text encoder and SDK where applicable. An unused, exact-hash Python ensurepip bootstrap archive is removed; active installed package metadata remains intact for scanning.

The first AMD64 resolver output contained Triton hashes whose origin could not be established against current official release metadata. It was rejected and regenerated from authenticated package metadata. The final lock selects the actual downloaded artifact; this was not waived as a platform difference.

### Qwen: compatible runtime, explicit base corrections

The pinned vLLM family is now `0.32.0`, with PyTorch `2.13.0`, Transformers `5.19.0` and FlashInfer `0.7.0.post1`. AMD64 retains CUDA 12.9; Spark uses the compatible official CUDA 13 ARM64 image. Base digests and full expected inventories are recorded with each architecture's lock.

The upstream base has dependency inconsistencies that an overlay must not hide. Reviewed correction layers:

- Align NCCL to PyTorch's declared `2.29.7` requirement. FlashInfer expert-parallel and vLLM M2N paths require a newer NCCL and are explicitly excluded from this dense, single-sequence worker. Guarded imports and actual loaded NCCL checks cover the supported path; no expert-parallel compatibility is claimed.
- Repair NVIDIA's ARM64 cuSPARSELt wheel tag as described below, without modifying native library bytes.
- Remove orphan LMCache only after reverse-dependency checks. This worker does not configure a KV-transfer connector. Remove six unused Ubuntu Python/Launchpad management packages after equivalent dependency checks; do not use broad autoremove or forced pip overwrites of OS packages.
- Apply **32 exact Ubuntu security updates** in the final plan, including OpenSSL `3.0.13-0ubuntu3.16` and GnuPG `2.4.4-2ubuntu17.6` and `python3-jwt 2.7.0-1ubuntu0.4`. The final PyJWT security update, published during this work, fixes four Medium CVEs; the 31-package GPU-tested checkpoint below predates it. Both expanded 32-package architecture plans were independently reviewed: the earlier 31 targets are unchanged, and PyJWT is the only addition, with zero new packages or removals. Both corrected baselines pass actual offline installation and complete OS inventory checks. SHA-verified archives, a closed upgrade allowlist, package-manager ordering and complete post-install inventory/audits prevent unrelated changes. This is not a claim that every available OS update was installed.

The existing lock helper still rejects unintended changes to protected inherited GPU packages. Explicit base corrections are reviewed separately, then become the baseline for the locked application overlay. Other available maintenance, including CA trust-store updates and updates to protected CUDA packages, was deferred; this review does not classify every deferred update as non-security.

The final AMD64 review identified two additional base-image issues. The maintained correction now removes the entire unused `mooncake-transfer-engine` distribution after checking reverse dependencies, entry points and native-library ownership. It also replaces the inherited TorchCodec build, which required CUDA 13 despite this being the CUDA 12.9 profile, with the exact official `torchcodec==0.17.0+cu126` wheel. This retains the upstream release and uses the existing CUDA 12 libraries; no CUDA 13 libraries or arbitrary shared-library substitutions are added.

A disposable baseline verified that these are the only two Python distribution changes: OS packages, other GPU packages, shadowed distributions and the 32 overlay artifacts remain unchanged. The maintained AMD64 gate now imports the real TorchCodec decoder, Qwen engine classes and sampling/configuration classes while denying Mooncake imports, and checks that both KV and EC transfer configurations remain unset. This verifies the supported text-worker import path, not CUDA media operations or execution on the eventual Runpod GPU. The replacement maintained image passes this gate, all 87 CPU/SDK tests and the fresh scan comparison. All 93 files owned by the former Mooncake distribution are absent.

### Honest, reproducible compatibility backports

Upstream dependency constraints prevent blindly installing every newest package in Qwen. Each local artifact has an explicit local version, source/patch hashes, license, complete wheel RECORD, reproducible builder and behavioral regressions.

| Artifact | Why it exists | Verification |
| --- | --- | --- |
| `runpod==1.10.0+pulsefeed.security.1` | vLLM requires FastAPI `<0.137`; current upstream SDK floors exceed that range. Retains 1.10 dependency metadata and selectively backports official 1.12 security changes. | Seven changed SDK source files match official 1.12; all other SDK source files match 1.10. Twenty regressions cover job state, heartbeat, fitness shutdown, SSRF and the real handler wrapper. |
| `setuptools==80.10.2+pulsefeed.ghsah35f.3` | vLLM requires setuptools `<81`; carries the official Unicode-normalization fix plus the separately recorded vendored-distutils matcher correction. | Thirteen regressions, deterministic rebuild and independent RECORD/source review. Its 180 vendored files match official 80.10.2, including fixed jaraco.context 6.1.0 and wheel 0.46.3. |
| `nvidia-cusparselt-cu12/cu13==0.8.1+pulsefeed.arm64tag1` | The vendor's ARM64 wheel contains an unsupported internal `sbsa` tag. Corrects version/tag/RECORD metadata for the reviewed ARM64 platform. | Both repaired artifacts rebuilt independently to the exact recorded hashes; all four native payload files remain byte-identical. |

The SDK backport replaces filesystem pickle job state with memory/shared heartbeat state, hardens unhealthy-worker shutdown/reporting, and imports the official bounded download/SSRF protections. It is **not** a claim of full Runpod 1.12 equivalence. The existing `{result: envelope}` wrapper remains necessary. Keep the SDK's private-download escape hatch disabled.

See [Qwen SDK provenance](../workers/qwen/vendor/runpod/README.md), [setuptools provenance](../workers/qwen/vendor/setuptools/README.md) and the worker build guides for exact hashes, reproduction commands and limitations. CPU CI now exercises the compatible SDK and both backports in isolated environments; the laptop app environment is not modified.

### Model-cache boundaries

Qwen authenticates six small metadata files, including its exact weight index and chat template. It accepts only two reviewed shard names, rejects an alternate monolithic weight file and permits its 105,079-byte index under a separate 128 KiB limit. FLUX authenticates eleven metadata files, including its text-encoder index and template.

Regular-file, expected-size, nonblocking-open and descriptor checks reject special files before metadata interpretation. Tests include same-size tampering, unsafe indexes, FIFO substitution and legitimate Hugging Face snapshot-to-blob links. These checks constrain the fixed snapshots; they do not hash every weight byte or eliminate mutation between validation and loading. Cache writers must remain trusted. Isolated live worker tests reused the existing caches read-only and offline; restored operator services retain their configured cache policy.

## Actual-image checks

| Profile | Final local image ID | State |
| --- | --- | --- |
| FLUX Spark ARM64 | `sha256:310c2353d83f7b86bd360cbfb71ee6f9bd68634cc6d9d89146ba5323e4cb0234` | Maintained Dockerfile built; full 65-package inventory, lock/artifact binding, imports and pip check pass. Independent full-image rescan matches the tested candidate. |
| Qwen Spark ARM64 | `sha256:e0b3e0957105818aa6888eb64c693d3ef15082d89ff5a6d4a6a6fe1fd676c05a` | Maintained prepared-context build includes all 32 OS updates; exact-image 72 worker tests, guarded imports, complete baseline/lock verification and strict pip check pass. Independent scan, guarded imports and operator-path first/warm GPU smoke pass; both normal worker endpoints are healthy. |
| FLUX Runpod AMD64 | `sha256:4ac720be06ce786143441074b62d25c714cb009ee960e252b07483edff9d27d2` | Maintained Dockerfile built; full 147-package inventory, imports, pip check and authenticated artifact binding pass under explicit CPU emulation. Independent full-image scan passes artifact verification and has exactly the same 168 advisory tuples as ARM64. |
| Qwen Runpod AMD64 | `sha256:9c1173ac7ef0ee093a3ca0709bf4fcf42888428b7e4d4cb2ef394e5c052208be` | Corrected maintained build passed all 32 OS updates, inventories, strict pip check and the real decoder/LLM/SamplingParams/EngineArgs gate. All 87 exact-image CPU/SDK tests passed. Mooncake import attempts, all 93 owned files and loaded native mappings are absent; KV/EC transfer remains unset. Fresh scan: 1,907 matches, zero Critical and 22 High; all 37 Go advisory keys removed, zero new keys. |

These are local Docker image identities, not published registry manifest digests. Old October 16 published images do not contain these changes. Do not deploy those tags as if they were the new release.

The prior AMD64 `466055…` image is a rejected checkpoint. Its 86 CPU/SDK tests and basic imports passed, but the stronger `from vllm import LLM` gate failed because TorchCodec required `libnvrtc.so.13` in the CUDA 12.9 profile. Its scan also exposed the unused Mooncake library. The replacement image above includes both reviewed corrections and enforces the stronger gate during its build.

On Spark, the local `pulsefeed-qwen-remediation:20261020-amd64-maintained-final` alias now points to the verified `9c1173…` image. The old image remains explicitly labeled `20261020-amd64-rejected-466055` for evidence; neither alias is a published registry release.

The AMD64 checks use BuildKit and an explicit hash-verified QEMU executable, without installing a global emulator. They can prove package/SDK compatibility, not GPU kernel execution. Builders maintain a 40 GiB free-space floor; model volumes and previous images are preserved.

## Test evidence

| Check | Result and scope |
| --- | --- |
| App Python suite | 542 passed; application unchanged during this dependency follow-up |
| Integration/gateway/lock suite | 165 passed, 75 subtests; repeated after lock and local/cloud build-launcher changes |
| Browser behavior harnesses | 188 checks across nine suites; CPU behavioral evidence, not a new rendered desktop/mobile session |
| FLUX full CPU suite | 168 passed; isolated AMD64 Docker target: 162 passed and six repository-context-only skips |
| Qwen final native image | 72 worker tests and 13 setuptools regressions passed; final source CPU suite: 87 worker tests; isolated backport CI reproduction: 20 SDK + 13 setuptools + eight installer tests passed |
| Qwen corrected AMD64 image | 87 exact-image CPU/SDK tests, the strong actual-engine import gate, strict inventories/pip check and archive cleanup passed under verified QEMU. Prior `466055…` basic-import results do not establish final acceptance. |
| Shared lock helper | 29 maintained tests and 14 independent adversarial checks |
| Package backports | SDK 20 and setuptools 13 regressions; independent deterministic artifact review |
| Ubuntu installer | Eight offline adversarial tests for archive hashes, closed plans and audits |
| Candidate real GPU smoke | Both fixed models pass invalid-input rejection, first/warm inference, output decode, usage and bounded retry accounting |
| Candidate RSS pipeline | **27 images: nine each from Hacker News, BBC England and NYTimes; 54 successful model calls, zero retries** |
| Real cancellation | Qwen terminal cancellation in about 0.3 s; FLUX in about 1.5 s; no resubmission |
| 31-update native checkpoint | First/warm smoke and **27 images / 54 successful calls / zero retries** on Qwen `f699…` and FLUX `310c…`; all app invariants repeated |
| Final maintained native images | First/warm smoke and **27 images / 54 successful calls / zero retries** on Qwen `e0b3…` and FLUX `310c…` through the restored normal ports; all app invariants repeated |

The RSS harness runs laptop FastAPI through its real authenticated HTTP provider to Spark with a fresh isolated SQLite database. It validates 1024 × 1024 WebP decoding, exact request replay, refresh without inference, daily/hourly quota accounting and reset, preserved model history, admin authentication/CSRF/logout, invalid feed rejection before inference, one-hour expiry and absence of stored image bytes in SQLite. Missing/invalid direct worker credentials return 401.

Candidate Qwen usage was 13,145 input + 1,955 output = **15,100 tokens**; FLUX reported **27 images / 27 attempts**. BBC and NYTimes descriptions were available and exercised; Hacker News used graceful title-only fallback. The three pages completed in roughly 63–66 seconds each in this run. First/warm FLUX inference took approximately 4.7/3.5 seconds. These are observations on Spark, not cloud latency guarantees or a benchmark. The requested operator limit of nine stories per page was respected.

The second run against Qwen `f699…` and FLUX `310c…` repeated all checks successfully. It reported 13,138 input + 2,024 output = **15,162 Qwen tokens**, and **27 FLUX images / 27 attempts**, with no retries. Page durations were 63.776 s (Hacker News), 68.577 s (BBC England) and 64.335 s (NYTimes). Final-image first/warm FLUX inference measured 5.01/3.58 s. Evidence: `live/smoke-final.log` and `live/flow-2/summary.json`.

The final maintained-image run used Qwen `e0b3…` with all 32 OS updates and FLUX `310c…` through the restored operator connection. All 27 images and 54 model calls succeeded with zero retries. Qwen reported **13,140 input + 2,016 output = 15,156 tokens**; FLUX reported **27 images / 27 attempts**. Page durations were 64.963 s (Hacker News), 64.200 s (BBC England) and 61.949 s (NYTimes). The isolated quota counters correctly reached 27 daily and hourly starts, then both reset to zero while model history remained intact. Evidence: `live/flow-3-maintained/summary.json`. The running operator app's database and quotas were not changed.

Cancellation deliberately terminates Qwen's engine and marks that worker unhealthy. That is the expected lifecycle behavior; the test containers were replaced before subsequent verification.

## Security findings that remain

Full-image SBOMs and scans include OS packages, native libraries, vendored Python copies and shadowed distributions. Active `pip list` alone is insufficient. Syft/Grype tool hashes and the advisory database provenance are retained with each scan. Counts are artifact/advisory matches, not demonstrated remotely exploitable paths; repeated packages can multiply a single CVE.

**FLUX final ARM64 and AMD64:** each has the same 168 package/version/advisory matches, including 57 High, 54 Medium, 10 Low and 47 Negligible; zero Critical. There are no active Python-package findings in this scan. The 161 Debian and seven CPython-binary matches were reviewed against upstream evidence. All 67 distinct Debian CVEs lacked an available fixed stable Trixie package at review time. Several involve privileged utility paths not exposed by the worker, but they remain open package findings. The reviewed CPython temporary-directory issue remains unresolved in this actual source; private worker-owned scratch limits its attacker-write prerequisite. No blanket dismissal or unstable distribution upgrade was made.

**Qwen final ARM64 `e0b3…` (32 Ubuntu updates):** 1,898 findings: 22 High, 1,684 Medium, 173 Low and 19 Negligible; zero Critical. This is 137 fewer matches than the earlier `42948004…` candidate; the count is unchanged from the `f699…` checkpoint. All 14 prior OS Highs and both vendored jaraco.context/wheel Highs are gone. The scanner also overstates some Ubuntu PyJWT findings: revision `.3` already backports five of its recorded CVEs, and the final `.4` update fixes four more. The final `.4` image retains the same thirteen scan matches: only their package-version tuples change from the `.3` checkpoint. The [official Ubuntu packaging source](https://launchpad.net/ubuntu/+archive/primary/+sourcefiles/pyjwt/2.7.0-1ubuntu0.4/pyjwt_2.7.0-1ubuntu0.4.debian.tar.xz) includes both changelog entries; this evidence is retained because the advisory database lags these updates. The single Python match is the version-range setuptools finding with independent patch and actual-image test evidence. The remaining 22 native Highs concern FFmpeg 8.1.1 bundled in vLLM-pinned `PyNvVideoCodec==2.0.4`; Ubuntu upgrades cannot fix that separate copy. The fixed text-only handler does not expose video decoding, but the package findings remain. Do not remove scan metadata or substitute arbitrary shared libraries to hide them.

**Qwen final AMD64 `9c1173…`:** 1,907 findings: 22 High, 1,690 Medium, 176 Low and 19 Negligible; zero Critical. Removing the entire unused Mooncake distribution resolves exactly **37 Go advisory keys**, including all three Critical and 27 additional High matches from the rejected `466055…` image. The corrected image introduces zero new advisory keys. The retained 22 High matches are the same bundled FFmpeg findings described above. Its nine additional OS matches relative to ARM64 are six Medium CUDA-compatibility-package matches and three Low `libvpl2` matches; the architecture-specific inventories remain explicit. The single Python finding retains the verified setuptools-backport evidence. These are scanner and source-review results, not a claim that every remaining path is safe.

Additional source-specific limits:

- [Accelerate 1.15.0](https://github.com/huggingface/accelerate/blob/v1.15.0/src/accelerate/utils/modeling.py) still joins index-provided paths in the reviewed loader. Exact metadata authentication constrains this model path; an advisory version range alone is not proof of an upstream fix.
- The reviewed vLLM 0.32 source still lacks the fix discussed in [PR 57762](https://github.com/vllm-project/vllm/pull/57762). Internal token IDs, disabled embeddings, neutral penalties and one sequence exclude the reviewed trigger. No package-wide clearance is claimed.
- DiskCache is absent from the inspected actual images; orphan LMCache removal is independently checked. This does not prove every retained framework component is safe.
- Version-only scanners may still flag the local setuptools backport. Retain exact patch/test evidence and honest version metadata rather than suppressing unrelated findings.

## Current local service state

The final native images are running on Spark's normal loopback ports 8181/8182, through the restored laptop SSH tunnel. Laptop FastAPI remains on `http://127.0.0.1:8017`; its original process (PID 13199) returned health HTTP 200 after restoration. Both authenticated worker health endpoints returned `ready`. Original operator credentials, SQLite state, admin settings and quotas were preserved. Temporary test credentials and the separate test tunnel were removed.

The exact maintained-image first/warm smoke passed through those normal ports. Qwen inference measured about 1.07/1.20 seconds; FLUX about 4.66/3.50 seconds, with correct model identities, output decoding and zero retries. These are individual measurements, not a service-level promise. Evidence: `live/smoke-maintained-operator.log` and `live/operator-readiness.json`.

The active Spark source checkout is `/home/vacko/pulsefeed-dependency-remediation-20261020/src`. Restoration used the original `/home/vacko/pulsefeed/local/.env` and the exact-image override at `/home/vacko/pulsefeed-dependency-remediation-20261020/validation/operator-images.yaml`. The existing private configuration was then copied locally, with identical values and mode `0600`, into the maintained checkout's `local/.env`; the original was preserved. Normal `./local/control.sh config` now passes from that checkout without restarting services. Use this maintained checkout for future start/status/build commands.

The normal local image aliases `pulsefeed-spark-qwen:latest` / `pulsefeed-spark-flux:latest` identify the tested native images. These are local aliases, not published cloud release tags. The older Spark checkout is retained for reference. A final read-only health check confirmed laptop FastAPI HTTP 200 and both exact native containers `running healthy`; see `live/final-service-health.json`.

## Release handoff

Follow the [models-first deployment guide](runpod-first-deployment.md) for publication and cloud verification. Use new immutable tags/digests for the two separate images in the private `rokkovach/pulsefeed` repository, configure secrets outside the images, and prefetch the exact reviewed model revisions. Never publish the rejected `466055…` checkpoint.

Before app traffic is routed to Runpod, run a native queue job on the **actual RTX PRO 4500 Blackwell target for each AMD64 image**. Verify cold startup, successful inference, output/usage, health, cancellation and bounded retries using one submission and the saved job ID. The Runpod SDK CPU checks and Spark GPU tests do not replace that gate. Railway HTTPS/proxy, persistent-volume and secret configuration also require deployment verification.

Ignored local evidence is under `integration/artifacts/dependency-remediation-2026-10-20/`: `validation/` contains artifact inventories/scans, `security/` contains source/advisory triage, and `live/` contains redacted smoke, cancellation and isolated RSS-flow results. Private credentials and model/image payloads must not be committed. Reproducible build inputs belong in the maintained worker directories, not in this evidence directory.

See the [preceding audit](audit-2026-10-20.md), [dependency history](dependency-security.md), [lock workflow](../local/DEPENDENCY_LOCKS.md) and [Spark guide](../local/README.md).
