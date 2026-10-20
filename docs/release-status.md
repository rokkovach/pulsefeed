# Release status

Checkpoint: 20 October 2026. This is the current summary; dated audit and deployment records describe their earlier checkpoints.

## Verified

- The Railway web app has HTTPS, password-protected admin access and persistent SQLite at `/data/pulsefeed.db`.
- The latest app ran locally against both live Runpod queue endpoints. The controlled run completed ten 512-square images and twenty successful model calls, with no retries or duplicate invocations. Model usage and quota settlement matched durable records. [Evidence](local-runpod-validation-2026-10-20.md)
- The app supports up to three pending jobs per model and six started stories. Safe defaults remain one per model and two started stories. [Configuration](model-concurrency.md)
- Native Spark testing covered all three reference feeds. The model images remain separate and use the existing GPU/runtime profiles.

## Current public worker images

| Worker | Published candidate | Immutable digest |
| --- | --- | --- |
| Qwen cleanup | `docker.io/rokkovach/pulsefeed:qwen-v1.0.5-20261020` | `sha256:10916e788ebad05f2d0d5b6834941f75f5e4e2e615b7ed0086e4024f7c40f1d0` |
| FLUX compact | `docker.io/rokkovach/pulsefeed:flux-v1.0.2-20261020` | `sha256:431ec62c62e1c7e6e187d5f6b990ed14c2300cfdb9605c41834968dfff441187` |

Known-working rollback images are Qwen `qwen-v1.0.4-20261020` (`sha256:8429dfe6121b504feb11386703b7c995627ab078ecc50f359f260d132afde964`) and FLUX `flux-v1.0.1-20261020` (`sha256:e674b604f7b64e1e30a3636ef74b9c8bf9c7f4e01033a464ae6b028dd9883f0c`). Keep them intact.

The controlled cloud test verifies the returned model/runtime identity, native queue behavior and 512-square output. The application API key cannot read endpoint management settings, so it did not independently verify the configured image digest. Record the console's endpoint image references before the final release. The [Qwen startup record](qwen-runpod-startup-fix-2026-10-20.md) and [compact FLUX record](flux-compact-images-2026-10-20.md) explain tested scope and rollback order.

## Remaining release gates

The final CI advisory check identified [GHSA-6q48-6c7w-9gw2](https://github.com/advisories/GHSA-6q48-6c7w-9gw2), published on 20 October. The app pin is now `certifi==2026.10.19`, and its image-validation dependency is updated to `Pillow==13.0.0` for the current Pillow advisories. FLUX already has that version in both locks. The current Qwen images inherit `2026.7.22` from the pinned base: update the installed-aware overlay and provenance, rebuild, and validate a new Qwen image before reviewer deployment. The published working images are retained for rollback, not described as patched. No model or GPU profile needs to change.

The latest connected Railway generation flow still needs redeployment after the reported platform deployment issue is resolved. Do not treat an older working web deployment as proof of the latest app release.

After deploying, follow the [deployment guide](deployment.md): check health, HTTPS/session/CSRF behavior, admin access, the persistent volume and one complete real feed. Reconcile model usage and quota reservations. Rotate among Hacker News, BBC England and NYTimes for additional feed checks. Preserve the current nine-story admin setting unless deliberately changed; the fresh-database default is ten.

Current performance is dominated by provider wait time. A warm demo can keep one active worker on each endpoint; more active workers raise idle spend. The [report](../output/pdf/pulsefeed-review.pdf) records measured ranges, not a latency guarantee. Remaining upstream dependency findings are retained in the [remediation record](dependency-remediation-2026-10-20.md).

## Public handoff

The public repository and review documents exclude credentials, cookies, certificates, databases, raw logs, downloaded weights and private test artifacts. The report uses reviewed screenshots from existing local pages. Share admin access separately and rotate it after the review. No secrets belong in issue comments, screenshots, copied environment dumps or example requests.
