# Qwen certificate bundle update — 20 October 2026

Qwen's pinned base image contains `certifi==2026.7.22`. The [certifi advisory](https://github.com/advisories/GHSA-6q48-6c7w-9gw2) describes a compromised intermediate CA chain and requires `2026.10.19` or later. Python HTTPS clients that use certifi need the corrected trust store inside their container.

Published AMD64 image, verified for anonymous registry access:

```text
docker.io/rokkovach/pulsefeed:qwen-v1.0.6-20261020
docker.io/rokkovach/pulsefeed@sha256:89be86ae6034ed929f3d3db6af8e7eb40c08cba72198bd79b43ca5b5d22c51df
```

## What changed

Both Qwen overlays now install `certifi==2026.10.19` from its exact, hash-verified public wheel. Their provenance was regenerated from actual pip reports for Python 3.12 on AMD64 and ARM64. The complete expected package inventories differ from the previous release only in certifi's version.

The immutable base inventories still record the older inherited package. This is intentional: the overlay records its replacement, and the final inventory gate verifies the installed result. Editing the base record to claim it already contains the fix would hide the dependency change.

The model revision, NVFP4 precision, GPU profiles, handler, runtime, health check and request schema are unchanged. The Railway app already contains the patched certifi version. FLUX's locks already pin that version; this release does not rebuild or switch FLUX.

## Validation

- All 92 Qwen tests and 29 dependency-lock helper tests passed.
- The AMD64 release was rebuilt from the maintained Dockerfile and allowlisted offline context on Spark using BuildKit's bundled emulator. `pip check`, complete installed-inventory verification and the real Torch/FlashInfer/TorchCodec/vLLM import gate passed. No model was loaded during those CPU checks.
- Both actual image variants load the corrected bundle, exclude `emSign ECC Root CA - G3`, and retain TLS chain and hostname verification. Both contain the reviewed handler, runtime and health-check bytes.
- Native GPU validation uses the existing Spark cleanup image plus the same locked certificate update. Its complete package inventory is checked before inference. The existing model cache is mounted read-only; a shared GPU lock protects resident services, which remain running.

The native smoke passed first and warm inference, token accounting, invalid-input rejection and a labeled pre-inference failure without replay. Startup took 111.7 seconds. First and warm generation took 1,684.2 and 1,615.1 ms; each request used 111 input tokens and produced 51 and 58 output tokens respectively. Both rejected cases recorded zero inference attempts. Native GB10 evidence does not replace a queue test on the deployed Runpod GPU.

## Deployment order

1. Use the new Qwen image reference in the [release summary](release-status.md). Update the existing Qwen Serverless endpoint's container image/template and save the configuration. Keep its endpoint ID, credentials, GPU selection, model cache and worker settings.
2. Wait for a fresh worker to become ready. Submit one new Qwen request with a fresh deadline and confirm a successful prompt plus input/output token usage. Record the selected container reference in the console; the restricted application key cannot read endpoint management settings.
3. Enable `INFERENCE_ENABLED=true` in Railway and deploy that variable change. The web app is already updated; this is the remaining generation switch.
4. Run one complete feed through the public app and reconcile generated images, Qwen tokens and allowance counters in admin. The current nine-story policy remains stored in SQLite.

No certificate needs to be uploaded through either console. Credentials remain runtime variables and are excluded from the build context and image configuration.

## Production follow-up

The operator confirmed the latest workers were ready. A fresh Qwen request passed on a new worker, Railway generation was enabled, and a complete nine-story feed passed. See [production validation](production-validation-2026-10-20.md) for aggregate usage, quota settlement and refresh checks. The deployment order above remains the procedure for future rollouts.

## Rebuilding later

Prepare into a new private directory outside the checkout, then build only Qwen:

```sh
python3 workers/qwen/prepare_build.py --platform amd64 --output-dir /absolute/private/qwen-build
docker buildx build --platform linux/amd64 --load -t pulsefeed-qwen:candidate /absolute/private/qwen-build
```

The preparer authenticates the public artifacts and copies an allowlist of maintained files. The build installs offline with exact hashes, checks the complete dependency inventory and runs its CPU import gate. Model weights and API credentials are not build inputs. Use a new release tag after validation; never overwrite a published tag.

## Rollback

The published `qwen-v1.0.4-20261020` and `qwen-v1.0.5-20261020` tags and digests are retained unchanged in the [startup and rollback record](qwen-runpod-startup-fix-2026-10-20.md#release-and-rollback). Both older versions retain the affected CA bundle. Check already-submitted job IDs before retrying requests after a rollout; replacing workers does not prove an earlier job was cancelled.
