# PulseFeed Parallel Implementation Plan

Use these briefs to run three GPT-6.1-Sol implementation agents concurrently. The product requirements remain in [PRD.md](PRD.md). Read the [shared contracts](00-shared-contracts.md) before writing code; then read only the owned lane and relevant parent sections.

| Track | Implementation brief | Exclusive implementation ownership |
| --- | --- | --- |
| App | [Railway application](01-app.md) | `app/**` |
| Qwen | [Qwen worker](02-qwen-worker.md) | `workers/qwen/**` |
| FLUX | [FLUX worker](03-flux-worker.md) | `workers/flux/**` |
| Coordination | [Contracts](00-shared-contracts.md) and [integration](04-integration-and-release.md) | `contracts/**`, `integration/**`, root documentation/configuration, and `PRD/**` |

## Container decision

Keep Qwen and FLUX in separate images and separate Runpod Serverless endpoints for the MVP. Qwen's NVFP4 serving runtime and FLUX's image pipeline can be developed, built, diagnosed, and released independently. The app can exercise their shared interface before either GPU deployment is ready.

A combined worker is technically an option, but it would require measured combined VRAM use, compatible runtime dependencies, and explicit model-loading/offloading behavior. It also couples startup and failure recovery. Do not spend the three day timebox on that optimization. Separate endpoints each allocate their own worker; they do not share a single GPU by choosing the same GPU type.

## Parallel execution rules

- Run at least the app, Qwen, and FLUX tracks concurrently with `gpt-6.1-sol`.
- Each agent owns only its listed directory, including its own tests, Dockerfile, dependency manifest, environment example, and README. Other agents are working in the same repository; do not revert their edits.
- The coordinator owns shared schemas/fixtures and integration. Workers request contract changes through the coordinator and cannot silently change fields, limits, or error meanings.
- Contract fixtures unblock app development and CPU worker tests. Mocked inference is not evidence that a model or Docker image runs on the target GPU.
- Each agent returns changed files, exact checks/results, remaining issues, and the relevant acceptance IDs. An independent reviewer checks material work before the combined release.
- Provisioning status and implementation status are separate. Do not describe a local scaffold or passing mock test as a deployed endpoint.

## Start order and delivery gates

1. Coordinator publishes contract version 1 and representative fixtures. Agents may begin their isolated skeletons immediately using the written contract.
2. Three implementation tracks proceed in parallel. App uses a fake Runpod adapter; workers use injected model doubles for CPU tests.
3. Verify each real model image on RTX PRO 4500 Blackwell early on day one. Resolve Qwen runtime compatibility before polishing the UI.
4. Connect real endpoints through application environment variables and run the [integration acceptance checks](04-integration-and-release.md).
5. Finish the Railway deployment, failure/refresh demonstration, and submission documentation by the end of day three.

The app is the largest track. If it becomes the bottleneck, the coordinator may explicitly split its backend and templates/static assets into separate ownership assignments after the app skeleton and browser API are stable. Do not introduce extra writers to `app/**` without that reassignment.

## Ready to use agent assignments

**App:** Read `PRD/PRD.md`, `PRD/00-shared-contracts.md`, and `PRD/01-app.md`. Implement only `app/**`, including tests and Railway configuration. Start against the shared fixtures. You are not alone in the repository; preserve others' changes. Report tests and remaining deployment gates.

**Qwen:** Read `PRD/PRD.md`, `PRD/00-shared-contracts.md`, and `PRD/02-qwen-worker.md`. Implement only `workers/qwen/**`. Preserve the selected NVFP4 checkpoint and validate the exact Blackwell runtime. You are not alone in the repository; preserve others' changes. Separate CPU test results from real GPU evidence.

**FLUX:** Read `PRD/PRD.md`, `PRD/00-shared-contracts.md`, and `PRD/03-flux-worker.md`. Implement only `workers/flux/**`. Return bounded image bytes through the shared envelope with no object storage. You are not alone in the repository; preserve others' changes. Separate CPU tests from real GPU evidence.
