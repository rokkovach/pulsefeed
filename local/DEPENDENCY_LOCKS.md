# Base image and wheel overlay locks

`lock_dependencies.py` uses only Python's standard library. It records package metadata without importing PyTorch, loading models, or inspecting credentials. The first distribution discovered by `importlib.metadata` is the active provider. Later copies (such as OS packages shadowed by `/usr/local` installations) are recorded in the optional `shadowed_distributions` inventory, including every copy’s exact version and dependency metadata in discovery order; none are removed. Dependencies come from the public `Distribution.requires` API, covering both modern `Requires-Dist` metadata and legacy `.egg-info/requires.txt` files. It validates pip's resolver output; it is not another resolver.

Run these steps separately for each exact worker base and architecture. Execute inventory and resolution **inside the unmodified image identified by the same immutable digest**, using the Python interpreter the worker will run:

```sh
python local/lock_dependencies.py inventory base.json
python -m pip install --dry-run --only-binary=:all: --report report.json -r direct-requirements.txt
python local/lock_dependencies.py generate base.json report.json overlay.lock provenance.json \
  --base 'registry/image:tag@sha256:<actual-64-hex-digest>'
```

Keep dependency resolution enabled and installed-aware: do not use `--no-deps`, `--ignore-installed`, cross-target flags, or a different Python environment when producing the report. Existing compatible base dependencies stay satisfied. Protected GPU roots already satisfied by the base may appear in direct requirements but must produce **no install entry**. Every protected install is rejected, even if its version is unchanged. `pynvml` is an allowed wrapper; `nvidia-ml-py` is protected.

Generation rejects unpinned bases, non-wheel/editable artifacts, missing SHA-256 hashes, duplicate names, and architecture/Python target mismatches. It writes all report installs as sorted exact versions and hashes. Provenance contains the base inventory, a compact report summary without artifact URLs or README descriptions, the original report's SHA-256, and the complete expected package-version inventory. Retain the original inventory/report as local evidence. The helper cannot prove the report's invocation flags or image identity; those are part of the workflow above.

Install the resolved overlay in that same digest-pinned base. `--no-deps` here prevents a second resolution: the report already includes every required overlay dependency, while the recorded base supplies the rest. Hash mode verifies downloaded artifact bytes.

```sh
python -m pip install --require-hashes --no-deps --only-binary=:all: -r overlay.lock
python local/lock_dependencies.py verify provenance.json
python -m pip check
```

Verification requires the complete expected inventory: additions, removals, version changes, architecture changes, or Python major/minor changes fail. Shadowed distributions must remain identical to the base, including versions, requirement metadata, copy counts, and order. Older provenance without the optional field means no shadowed copies and remains valid on matching environments. A missing transitive dependency therefore fails verification. `pip check` remains a separate required dependency-consistency check. Do not upgrade pip or add unrelated packages between base inventory and verification. An empty overlay is valid.

## Full runtime on a fresh Python base

Use `--fresh-base` only when constructing a GPU runtime from an immutable Python
base without an existing protected GPU stack. It rejects every protected active
or shadowed base distribution, even when the report proposes the same version or
no GPU installation at all. It therefore cannot upgrade or replace an inherited
NGC/PyTorch/CUDA stack. The default overlay mode continues to reject all protected
wheel installs. Verification repeats this base guard; the provenance records
`installation_mode: "fresh-base"` and the same complete expected inventory.

Inside the unmodified digest-pinned Python image, using the worker interpreter:

```sh
python local/lock_dependencies.py inventory base.json
python -m pip install --dry-run --only-binary=:all: --report report.json -r direct-requirements.txt
python local/lock_dependencies.py generate base.json report.json runtime.lock provenance.json \
  --base 'python:3.12-slim@sha256:<actual-64-hex-digest>' --fresh-base
python -m pip install --require-hashes --no-deps --only-binary=:all: -r runtime.lock
python local/lock_dependencies.py verify provenance.json --lock-file runtime.lock
python -m pip check
```

The real resolver report must include the full missing runtime closure, including
PyTorch, CUDA wheels and Triton where applicable. Select the correct wheel for the
actual Python, architecture and CUDA profile during resolution. In fresh-base mode,
a direct PyTorch wheel selected by pip (`is_direct: true`) retains its exact URL
and report SHA-256 as `torch @ URL --hash=sha256:...`. Only HTTPS URLs on the exact
hosts `download.pytorch.org` and `download-r2.pytorch.org`, under `/whl/cuNNN/` or
`/whl/cpu/`, are accepted. User information, ports, query strings, fragments,
whitespace and unsafe path characters are rejected. The filename must match
`torch`, its exact version, the inventory's CPython tag, Linux and architecture.
Verification revalidates the URL, wheel and hash; `--lock-file runtime.lock` also
compares all actual requirement lines with provenance, detecting substituted
URLs or hashes. This narrowly retained official URL is public provenance.

Other artifact URLs remain omitted. If another selected artifact is outside the
default package index, provide its trusted source during installation (for
example an explicit `--find-links` location). Hash verification still selects the
recorded artifact bytes. Do not reuse architecture-specific evidence across
platforms. Keep the real report and
base inventory alongside provenance; this mode does not infer or audit a missing
resolver closure, so complete inventory verification and `pip check` remain
required.

Generate locks only from real reports; do not substitute sample hashes or fabricate a successful resolution. These files do not fetch models or test GPU execution.

## October 20 remediation profiles

Use each maintained Dockerfile with its matching architecture-specific lock and provenance; do not reuse a runtime inventory across these profiles:

| Profile | Recipe and lock/provenance pair | Runtime / SDK |
| --- | --- | --- |
| Qwen Runpod AMD64 | `workers/qwen/Dockerfile`; `requirements-runpod.lock` / `requirements-runpod.provenance.json` | vLLM 0.32, PyTorch 2.13 CUDA 12.9; Runpod `1.10.0+pulsefeed.security.1` |
| Qwen Spark ARM64 | `workers/qwen/Dockerfile.dgx-spark`; `requirements-dgx.lock` / `requirements-dgx.provenance.json` | vLLM 0.32, PyTorch 2.13 CUDA 13.0; no Runpod SDK |
| FLUX Runpod AMD64 | `workers/flux/Dockerfile`; `requirements-runpod.lock` / `requirements-runpod.provenance.json` | Python 3.12.15, PyTorch 2.13 CUDA 12.9, 147-package closure; Runpod 1.12.0 |
| FLUX Spark ARM64 | `workers/flux/Dockerfile.dgx-spark`; `requirements-dgx.lock` / `requirements-dgx.provenance.json` | Python 3.12.15, PyTorch 2.13 CUDA 12.9, 65-package closure; no Runpod SDK |

Qwen's maintained native recipe, preparer and actual 32-update corrected-baseline inventories are frozen. Its native lock contains 10 overlay wheels and its AMD64 lock contains 32; corrected Python/OS baselines live under `vendor/base-corrections/`, with architecture-specific manifests under `vendor/os-security/`. The final maintained native Qwen image passed its worker/backport tests and the third 27-image Spark application flow. FLUX's maintained native and AMD64 artifacts passed inventory/import/pip-check and independent scan verification; see the [remediation checkpoint](../docs/dependency-remediation-2026-10-20.md) for exact local image IDs and residual findings.

AMD64 Qwen `sha256:466055136e035686afd5cf957d9c377213a1f06d622e9292b44703522e89a1ca` is a **rejected historical checkpoint**. Its 86 CPU/SDK tests, guarded imports, inventories and pip check passed, but its scan found 1,944 matches including three Critical and 49 High, with 37 Go findings in optional Mooncake `libetcd_wrapper.so`. Its residuals must not be equated with the native scan.

The reviewed AMD64 correction removes the complete unused Mooncake package and uses the exact official TorchCodec `0.17.0+cu126` wheel, preserving the same release and using existing CUDA 12.9 libraries without adding CUDA libraries. All other Python/GPU/shadow/OS inventories and all 32 overlay artifact hashes are unchanged; native inputs are byte-identical, preserving the native application evidence. The replacement build passed a stronger network-disabled real decoder / `LLM` / `SamplingParams` / `EngineArgs` import gate with Mooncake absent/denied, KV and EC transfer unset, and expert parallel disabled. Source tests pass 87 cases.

The replacement local image is `sha256:9c1173ac7ef0ee093a3ca0709bf4fcf42888428b7e4d4cb2ef394e5c052208be`; its 87 exact-image CPU/SDK tests, strict pip check, full provenance and absence of staged archives passed. Independent source/package review passed.

Its fresh scan reports 1,907 findings: zero Critical, 22 High, 1,690 Medium, 176 Low and 19 Negligible. Exactly 37 Go advisory keys are removed versus rejected `466055…`, with zero new keys; independent raw comparison passed. All 22 High findings exactly match the native FFmpeg 8.1.1 findings in vLLM-pinned PyNvVideoCodec 2.0.4. The independent review also verifies the two-distribution delta, 281 active distributions, unchanged shadows, all 93 Mooncake-owned files and metadata/executable leftovers absent, and all 156 installed TorchCodec payload files matching the official wheel. Retained FFmpeg/native, OS and source-specific findings remain open as scoped in the remediation report; zero Critical scan matches is not blanket vulnerability clearance. Local image IDs are not registry manifest digests; CPU emulation does not establish AMD64 GPU kernels or native Runpod queue compatibility.

Qwen now resolves an installed-aware overlay; the old Spark no-overlay policy no longer describes the remediation. FLUX installs a complete fresh-base closure, with bootstrap pip pinned separately in `requirements-bootstrap.lock`. Both FLUX profiles use Diffusers 0.41.0 and Transformers 5.20.2. Qwen retains FlashInfer 0.7.0.post1 and explicitly pins NCCL 2.29.7. Every final environment must pass a clean `pip check`; the older PyGObject/Pycairo and TVM exceptions are retained only in [historical packaging evidence](PACKAGING_VALIDATION.md), not as permission to accept a new mismatch.

Reviewed Qwen local wheels and rebuild inputs live in `workers/qwen/vendor/{setuptools,runpod,cusparselt}/`. The setuptools pin is `80.10.2+pulsefeed.ghsah35f.3`; the SDK backport keeps 1.10's compatible dependency declarations and selected official security/operational fixes, without claiming full 1.12 equivalence. Both native profiles repair the ARM cuSPARSELt wheel's platform tag with recorded source/output hashes and regenerated RECORD data; native payload bytes remain unchanged. FLUX retains `repair_vendor_wheel_tag.py` and `cusparselt-repair.provenance.json`; Qwen retains its separate `vendor/cusparselt/` provenance.

The Qwen remediation removes orphan LMCache and six reviewed APT tool packages separately from its 32 exact Ubuntu security updates. The final manifest adds `python3-jwt 2.7.0-1ubuntu0.4`; the earlier 31-package GPU/scan checkpoint remains historical evidence. The final maintained native 32-update recipe subsequently passed the third application flow; AMD64 validation remains separate. Use `workers/qwen/vendor/os-security/` and the maintained recipe's architecture-specific authenticated manifests; do not turn this into a general APT upgrade or remove GPU packages. Preserve the before/after inventories and verify the exact changes. FLUX's `cleanup_bootstrap.py` removes only the old ensurepip archive, preserving the active locked pip. These targeted changes do not clear every wheel-bundled/native or host-driver finding; retain final artifact scans and residual triage.

Qwen builds require an authenticated prepared context. The maintained wrappers handle this automatically: `./local/control.sh build` selects `arm64`, while `scripts/runpod-images.sh --build` or `--push` selects `amd64`. They create a private temporary parent, pass a nonexistent child to `prepare_build.py --platform arm64|amd64 --output-dir PATH`, then remove the generated context on success or failure. The Python 3.11+ standard-library preparer requires a new absolute output directory outside the repository; it authenticates the architecture-specific locked wheels and OS archives, verifies local backport hashes and copies an allowlisted source set. Docker installs the prepared dependency archives offline. Model snapshots and runtime credentials do not belong in this context.

For Qwen CPU/SDK checks, use a separate test environment and run from the repository root. Install the reviewed wheel explicitly; a relative wheel entry inside `-r` would depend on the caller's working directory. The test requirements select FastAPI 0.136.3 to match vLLM's `<0.137` range without installing vLLM in the CPU environment:

```sh
python -m pip install -r workers/qwen/requirements-test.txt \
  workers/qwen/vendor/runpod/runpod-1.10.0+pulsefeed.security.1-py3-none-any.whl
```

Offline lock-helper tests:

```sh
python -m unittest discover -s local -p test_lock_dependencies.py -v
```
