# Runpod 1.10 selective security backport

This directory contains `runpod-1.10.0+pulsefeed.security.1-py3-none-any.whl`, its MIT license, three source patches, hash-pinned provenance and an offline rebuild/test recipe. This is a maintained local backport, **not upstream Runpod 1.12.0 or a claim of equivalent coverage**. Final target-image resolution, dependency audit and GPU behavior are separate release gates.

Runpod 1.10.0's dependency declarations are retained byte for byte, including its compatible `fastapi[all]>=0.94.0` declaration. This lets the Qwen build resolve vLLM's actual FastAPI range without falsifying SDK metadata or altering vLLM requirements. Retaining those declarations does not itself prove that every possible allowed dependency version is secure; the final image uses a separately reviewed exact lock.

The seven changed SDK source files are byte-identical to their corresponding files in the hash-verified official 1.12.0 wheel. Every other `runpod/` source file remains byte-identical to 1.10.0. The patches carry:

- [Job-state fix `6c2cd64`](https://github.com/runpod/runpod-python/commit/6c2cd64bb8efa2dd1c5294946ab809ba1d25f29b): remove filesystem pickle state loading/writing, keep job progress in memory, and pass a shared-memory snapshot to the heartbeat process. Worker and local FastAPI startup wire the same mirror.
- [Fitness shutdown fix `08e07cb`](https://github.com/runpod/runpod-python/commit/08e07cb5dded275b1bab0451940433fe2a1c8244) and [bounded host reporting `f95aef8`](https://github.com/runpod/runpod-python/commit/f95aef892f673875f2cc9801e440486c40b1bc48): an unhealthy worker force-exits even with non-daemon threads, after a best-effort report with a two-second network timeout.
- [Download SSRF fix `b510d2a`](https://github.com/runpod/runpod-python/commit/b510d2a989e6ce17c7316c333697754da6d99f5e): public-address validation, DNS pinning with TLS hostname/SNI preservation, redirect revalidation, proxy refusal and declared/streamed download caps. It retains upstream's explicit private-download escape hatch and default 5 GiB cap; production configuration must not enable that escape hatch. These generic SDK helpers are separate from the Qwen handler's text-only input contract.

VolumeCache, batch logging, dependency-floor upgrades and unrelated 1.12 changes are excluded. `rp_job.py`, `rp_scale.py` and `serverless/__init__.py` retain their exact 1.10 bytes. The existing `{result: envelope}` handler wrapper remains necessary: the SDK still consumes top-level `error` and `refresh_worker` control fields.

## Rebuild

Obtain the exact 1.10.0 upstream wheel identified by URL and SHA256 in `provenance.json`, then run from the repository root:

```sh
python3 workers/qwen/vendor/runpod/build_backport.py \
  --source-wheel /path/to/runpod-1.10.0-py3-none-any.whl \
  --output-dir /tmp/runpod-backport-rebuild
```

The command checks the upstream hash, each patch hash, every changed file's before/after hash and the final wheel hash. It does not download packages or install anything. It changes the distribution version honestly, preserves all other metadata, embeds the patch provenance, and rebuilds a complete wheel `RECORD`. Fixed path ordering, timestamps, file modes and uncompressed ZIP entries avoid filesystem- or zlib-dependent rebuild differences. `runpod.version.__version__` obtains the local version through distribution metadata.

## Offline CPU regression tests

Use an isolated Python environment with compatible Runpod CPU dependencies already present; no GPU packages, credentials or installed-environment mutation are required. Supply the hash-verified upstream 1.10.0 and reference 1.12.0 wheels:

```sh
/path/to/test-python workers/qwen/vendor/runpod/verify_backport.py \
  --base-wheel /path/to/runpod-1.10.0-py3-none-any.whl \
  --reference-wheel /path/to/runpod-1.12.0-py3-none-any.whl
```

The script imports the explicit candidate wheel directly from its ZIP. It suppresses optional operator credential-file lookup, removes Runpod service variables within the test process and uses synthetic transports. The 20 tests cover wheel/version/dependency/RECORD/license integrity, cross-Python deterministic rebuild, poisoned pickle rejection, mirror updates and a spawned reader process, heartbeat reporting, fitness force-exit/report failure, SSRF and resource caps, and the actual Qwen async handler's success/failure envelopes through the real candidate SDK with an injected CPU runtime. No cloud request or inference is performed.
