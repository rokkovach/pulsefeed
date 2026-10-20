# PulseFeed guides

Start with the [reviewer guide](reviewer-guide.md), [six-page report](../output/pdf/pulsefeed-review.pdf) and [current release status](release-status.md). The [showcase sources](showcase/README.md) explain how the editable handoff is built. For the implementation, read the architecture, then follow one request through the code.

## Understand the system

| Guide | What to look for |
| --- | --- |
| [Architecture](architecture.md) | Topology, request sequence, limits, state transitions and trust/storage boundaries |
| [Code walkthrough](code-walkthrough.md) | Source entry points from RSS submission to browser image |
| [Architecture diagram](diagrams/architecture.svg) | The app, two workers and their data boundaries |
| [Request lifecycle](diagrams/request-lifecycle.svg) | Admission, model stages, polling and browser storage |
| [Local versus cloud](diagrams/local-vs-cloud.svg) | What each Spark, Runpod and Railway validation path covers |

## Run and operate it

| Guide | Use it for |
| --- | --- |
| [App](../app/README.md) | Synthetic quickstart, admin access and provider settings |
| [Spark](../local/README.md) | Real model inference through authenticated local gateways |
| Workers: [Qwen](../workers/qwen/README.md) / [FLUX](../workers/flux/README.md) | Separate images, model pins, build and smoke checks |
| [Models-first deployment](runpod-first-deployment.md) | Build, publish and verify the first two Runpod endpoints |
| [Full deployment](deployment.md) | Railway, persistent storage, secrets and release gates |
| [Model concurrency](model-concurrency.md) | Configure 1–3 jobs per model; admission, restart behavior and rollback |
| [Operations](operations.md) | Drain, restart, investigate, back up and roll back |
| [Integration checks](../integration/README.md) | Executable test commands and their scope |

## Check the evidence

- [Production Railway validation](production-validation-2026-10-20.md): nine real NYTimes images, token/image accounting, quota settlement and refresh reuse.
- [Local app with live Runpod](local-runpod-validation-2026-10-20.md): real RSS, 512 images, bounded concurrency and aggregate usage checks.
- [Compact FLUX images](flux-compact-images-2026-10-20.md): 512-square default, worker cleanup, deployment order and rollback.
- [Qwen startup fixes and cleanup](qwen-runpod-startup-fix-2026-10-20.md): storage, cache links, expired test deadlines, safeguards and rollback.
- [Public worker release](image-release-2026-10-20.md): current public tags, immutable registry digests and anonymous-access verification.
- [Railway cloud checkpoint](cloud-release-2026-10-20.md): production app, authentication, persistent storage and paused inference.

The controlled local-app/Runpod test completed ten images and twenty successful model calls, with bounded request overlap and verified usage accounting. The earlier maintained Spark pair completed 27 images and 54 calls across three feeds. The public Railway flow also completed nine NYTimes images and eighteen calls with matching usage and no extra calls on refresh. See the [release status](release-status.md) for current image references and rollback versions.

- [Dependency remediation, October 20](dependency-remediation-2026-10-20.md): current image identities, exact-image checks, final native GPU run and retained security findings. No remediation image was published or cloud-deployed at that checkpoint.
- [Integration validation](../integration/VALIDATION.md) and [release evaluation](../integration/RELEASE_EVALUATION.md): dated application/browser checks and release scope.
- [Dependency security](dependency-security.md): earlier dependency review and limitations.
- [Project audit, October 20](audit-2026-10-20.md): preceding source fixes and scans of the older October 16 images. Those images predate the later remediation.
- [Case-study evidence](case-study.md): established claims, historical measurements and fields to fill after cloud validation.

The [PRD](../PRD/README.md) preserves the original briefs. Chronological validation files preserve the policy and measurements at each checkpoint; current behavior is described in the guides above. Raw evidence under ignored artifact directories is local to the workspace unless deliberately reviewed and published.

[Back to the repository](../README.md)
