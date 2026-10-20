# Deploy Railway and Runpod

This is the cloud deployment runbook, not evidence of an existing deployment. The ARM64 Spark path has real-model evidence; **AMD64 RTX PRO 4500 Blackwell runtime validation and connected cloud inference are still pending**. The [Railway web checkpoint](cloud-release-2026-10-20.md) records the production app, HTTPS/auth and persistence checks. Review [release evaluation](../integration/RELEASE_EVALUATION.md) and [operations](operations.md) before rollout.

For the first deployment, start with [models before Railway](runpod-first-deployment.md). It includes a command-preview/build/publish helper and a fresh Runpod request generator, using the same images and contracts documented here.

## Deployment inventory

| Resource | Configuration |
| --- | --- |
| Railway app | `app/` build context, one replica, one ASGI process/coordinator, `/data` persistent volume |
| Qwen queue endpoint | Independent AMD64 image; `nvidia/Qwen3-8B-NVFP4` revision `ccd10a893cbca613259517c3efe08e151ddf2b8e` |
| FLUX queue endpoint | Independent AMD64 image; `black-forest-labs/FLUX.2-klein-4B` revision `e7b7dc27f91deacad38e78976d1f2b499d76a294` |
| Each worker | One GPU, concurrency one, active/min workers 0, max workers 1 |
| Model cache | Exact pinned snapshots in a selected cache; no generated-image storage service |

Use **queue-based Serverless endpoints** with native handlers, rather than load-balancing HTTP endpoints. PulseFeed submits `/run`, stores the job ID and polls `/status/{id}`. [Runpod overview](https://docs.runpod.io/serverless/overview), [queue operation reference](https://docs.runpod.io/serverless/endpoints/operation-reference)

## 1. Prepare a release

Run the [isolated CPU suites](../integration/README.md), inspect the resulting diff and build from the intended commit. Read the [dependency security review](dependency-security.md) and [October 20 audit](audit-2026-10-20.md): the app lock is remediated, but worker findings remain. The published October 16 worker images lack the latest source fixes; build new versions, scan their exact digests and complete target-GPU smoke before release. No Git remote or published repository URL is assumed here: publishing the repository and choosing its visibility are operator steps. Do not upload private environment files, databases, caches or unreviewed validation artifacts.

Build and publish versioned images to a registry reachable by Runpod. Replace `YOUR_REGISTRY` and `VERSION` first; these commands publish images and belong to an intentional release session:

```sh
bash scripts/runpod-images.sh --registry YOUR_REGISTRY \
  --repository pulsefeed --version VERSION --push

docker build -t pulsefeed-app:VERSION app
```

Record commit, immutable image digests, dependency inventories and build logs. Use versioned tags or digests for endpoint configuration; mutable `latest` complicates reproduction and rollback. [Runpod rolling releases](https://docs.runpod.io/serverless/endpoints/rolling-releases)

The helper prepares Qwen's authenticated dependency archives in a private, allowlisted temporary context, builds the two images separately and removes that temporary context on exit. It does not download model weights. Use `--plan` to review commands without downloads or Docker, or `--build` to load images locally without publishing. In the shared repository, tags are `qwen-VERSION` and `flux-VERSION`; verify the intended visibility before publishing. The [current release](image-release-2026-10-20.md) is public by owner choice.

Each worker has its own dependency lock. Do not combine app and GPU dependencies in one environment. `Dockerfile.dgx-spark` is ARM64-only and is not the cloud image. See the component [Qwen](../workers/qwen/README.md) and [FLUX](../workers/flux/README.md) guides for exact runtime requirements.

## 2. Prove the selected GPU

The intended target is **RTX PRO 4500 Blackwell, 32 GB**. Confirm the exact offered hardware/driver and GPU availability in the deployment console; a similarly named older RTX A4500 is a different device. Do not silently substitute GPU, revision, precision or runtime if the target fails.

Run each image on that target with its configured model cache and explicit real-GPU smoke:

```sh
# Inside the Qwen image on the selected GPU; performs real inference.
python3 /worker/smoke_test.py --real-gpu --evidence-path /tmp/qwen-gpu-evidence.json
nvidia-smi
cat /worker/build-packages.txt

# Inside the FLUX image on the selected GPU; performs real inference.
python /worker/preflight.py
python /worker/smoke_test.py --size 512 --controlled-failure > /tmp/flux-gpu-evidence.jsonl
nvidia-smi
python -m pip freeze
```

The smoke scripts make multiple controlled real calls and injected failure cases, not one free health probe. Save only redacted evidence: GPU/driver, image digest, model revision, package versions, initialization and invocation timings, counters, output format/dimensions and manual quality assessment. CPU checks and ARM-host cross-builds cannot establish kernel support or actual VRAM headroom.

## 3. Configure two Runpod endpoints

No marketplace template is required. In the Runpod console, open **Serverless → New Endpoint → Import from Docker Registry**, enter the published versioned image reference, and select **Queue** as the endpoint type. Repeat for the other model. These endpoints run our SDK handlers directly; the app connects through Runpod's queue API. [Custom Docker deployment](https://docs.runpod.io/serverless/workers/deploy#deploy-an-endpoint)

Create one queue endpoint per image and retain distinct endpoint IDs. Put a Hugging Face token in worker secrets only when model access requires it; `RUNPOD_API_KEY` belongs only in the app service. Do not pass credentials in job payloads.

| Setting | Qwen | FLUX |
| --- | --- | --- |
| Runtime profile | `QWEN_RUNTIME_PROFILE=runpod` (default) | `FLUX_RUNTIME_PROFILE=runpod` |
| Active/min / max workers | 0 / 1 | 0 / 1 |
| GPU / concurrency | One target GPU / one job | One target GPU / one job |
| Startup watchdog | `INITIALIZATION_TIMEOUT_SECONDS=480` | `FLUX_STARTUP_TIMEOUT_SECONDS=600` |
| Worker execution bound | `EXECUTION_TIMEOUT_SECONDS=240` | `FLUX_EXECUTION_TIMEOUT_SECONDS=540` |
| Provider execution timeout | Initial 600 seconds; validate under measured startup/queue behavior | Initial 600 seconds; validate under measured startup/queue behavior |
| Model revision | Fixed Qwen pin above | Fixed FLUX pin above |

The app's default started-story deadline is `ITEM_DEADLINE_SECONDS=600`, covering its stages and waiting after the story starts. A scale-to-zero cold start may consume that budget. Measure startup, queue delay and both stages before adjusting bounds together. Keep provider execution timeout enabled and choose job TTL to cover queueing plus execution; neither control extends async result retention. [Runpod endpoint settings](https://docs.runpod.io/serverless/endpoints/endpoint-configurations)

Keep concurrency at one. The handlers own one bounded safe retry; do not add a client loop that repeats `/run` after a timeout, generic SDK/engine inference replay, or a provider retry policy without reviewing its duplication/accounting consequences. The app cannot observe every platform-internal replay. Exercise the actual endpoint failure behavior as a release gate.

### Weight cache

Both workers support these policies:

| Mode | Qwen setting | FLUX setting |
| --- | --- | --- |
| Mounted/custom cache (default) | `MODEL_CACHE_SOURCE=volume`, `MODEL_CACHE_DIR=/runpod-volume/huggingface` | `MODEL_CACHE_SOURCE=volume`, `HF_HUB_CACHE=/runpod-volume/huggingface/hub` |
| Runpod managed cache | `MODEL_CACHE_SOURCE=runpod-managed` | `MODEL_CACHE_SOURCE=runpod-managed` |
| Require complete local pin | `MODEL_CACHE_OFFLINE=true` | `MODEL_CACHE_OFFLINE=true` |
| Permit bounded startup download | `MODEL_CACHE_OFFLINE=false` | `MODEL_CACHE_OFFLINE=false` |

Managed mode resolves `/runpod-volume/huggingface-cache/hub`; select the model in the endpoint configuration and verify the **exact pinned revision** exists. A cached `main` or another revision is insufficient. Custom network volumes and managed cache are different mechanisms. [Runpod cached-model layout](https://docs.runpod.io/serverless/endpoints/model-caching#where-cached-models-are-stored)

Prefetch/verify before enabling offline mode. Missing or incomplete snapshots fail readiness without fallback. Initial online download can exceed the startup budget, especially FLUX's roughly 14.9 GiB snapshot. Plan cache capacity, first-start transfer and region availability deliberately. The cache stores model files, never generated outputs.

Workers also verify the pinned routing-metadata hashes and reject Python files or unsafe cache links before loading. Standard Hugging Face blob links are supported. Do not modify metadata to make an incompatible snapshot pass; restore the reviewed revision and restrict cache writers. Full weight authenticity and concurrent cache mutation are outside these bounded startup checks.

### Readiness

Qwen checks package/device/precision metadata, tokenizer configuration and a one-token GPU probe before SDK registration; its Docker healthcheck verifies both parent and engine PIDs. FLUX validates runtime/device/BF16 kernels and loads the pipeline before SDK registration. A structured model failure may arrive inside a provider `COMPLETED` job: inspect `output.result.ok`, IDs, model metadata, counters and image decoding, not only the queue status.

Before connecting public traffic, perform an explicit native endpoint smoke with a fresh UUID and future deadline; submit once and retain the returned job ID. A stale contract fixture deadline is not a valid live request. Poll known jobs; never treat a lost submission response as authorization to submit again.

## 4. Configure Railway

Connect the intended published repository/branch, or use your chosen Railway source flow. Set the service **root directory to `/app`** (repository path) and Dockerfile path within the root to `Dockerfile`. Configure one replica, healthcheck `/health` with a 120-second timeout, and `ON_FAILURE` with three restart retries explicitly in the service settings. The current Railway API rejects setting `railwayConfigFile`: JSON/TOML Config as Code is deprecated in favor of Infrastructure as Code. `app/railway.json` remains a legacy reference; do not assume it applies those settings. [Railway migration guidance](https://docs.railway.com/infrastructure-as-code#migrating-from-config-as-code) The app Dockerfile uses a separately cached dependency layer without service-specific BuildKit cache mounts. It is self-contained and listens on `0.0.0.0:$PORT` with one Uvicorn worker. [Railway Dockerfile detection](https://docs.railway.com/builds/dockerfiles), [monorepo configuration](https://docs.railway.com/deployments/monorepo)

For a direct CLI upload containing only the contents of `app/`, use a clean source directory containing `Dockerfile`, `railway.json`, `pyproject.toml`, `requirements.lock`, `pulsefeed/` and a restrictive `.dockerignore`. Exclude all environment files, databases, caches and test artifacts. Upload that directory with `railway up <source-directory> --path-as-root` and explicit project/service/environment targeting; its service root is `/`, because there is no enclosing `app/` directory in that upload. Set the healthcheck, replica count and restart policy explicitly as above. CLI authentication alone does not choose a deployment target. [Railway CLI](https://docs.railway.com/cli)

Attach a retained volume at `/data` before serving real work. Use `DATABASE_PATH=/data/pulsefeed.db`; do not initialize the real database during build or pre-deploy. Railway volumes are mounted at runtime, not build time. Preserve the volume across releases and arrange private backups. [Railway volumes](https://docs.railway.com/volumes)

Set service variables through Railway's private Variables interface and deploy the staged changes. Seal the secrets. The placeholders below are descriptions, not literal values to paste. [Railway variables](https://docs.railway.com/variables)

| Variable | Value / purpose |
| --- | --- |
| `APP_BASE_URL` | Exact public HTTPS origin, without path/query/fragment |
| `DATABASE_PATH` | `/data/pulsefeed.db` |
| `SESSION_SECRET` | Secret: stable, random, ≥32 characters; generate independently of the admin token |
| `ADMIN_ACCESS_TOKEN` | Secret: distinct random URL-safe token, 32–256 characters; password for `/admin` |
| `ADMIN_ACCESS_PARAM` | `admin_key` unless deliberately changed |
| `ADMIN_SESSION_SECONDS` | `3600` |
| `PUBLIC_ACCESS_ENABLED` | `true` for anonymous visitors |
| `COOKIE_SECURE` | `true` |
| `PULSEFEED_FAKE_MODE` | `false` |
| `INFERENCE_ENABLED` | `true` after both endpoints are verified; `false` serves a production site with generation paused |
| `INFERENCE_PROVIDER` | `runpod` |
| `RUNPOD_API_KEY` | Secret: provider key with required endpoint permissions |
| `RUNPOD_QWEN_ENDPOINT_ID` | Qwen endpoint ID |
| `RUNPOD_FLUX_ENDPOINT_ID` | FLUX endpoint ID |
| `TRUSTED_PROXY_CIDRS` | Verified proxy hop CIDRs only; empty ignores forwarded headers |
| `COORDINATOR_STAGE_CONCURRENCY` | `1` by default; `1–3` jobs per model endpoint |
| `COORDINATOR_PIPELINE_SIZE` | Defaults to twice stage concurrency if unset; set `6` with stage concurrency `3`, or `1` for serial |
| `ITEM_DEADLINE_SECONDS` | `600` initially; validate against cold-start/queue measurements |
| `GLOBAL_DAILY_IMAGE_LIMIT` | `100` initial policy seed |
| `GLOBAL_HOURLY_GENERATION_LIMIT` | `30` initial policy seed |
| `FEED_ARTICLE_LIMIT` | `10` initial policy seed, range 1–10 |
| `IMAGE_QUOTA_PER_HOUR` | `30` visitor attempt limit |
| `LIVE_VIEW_LIMIT` | `5` simultaneous unexpired views/visitor; legacy `SAVED_FEED_LIMIT` fallback |

To bring up the production site before the model endpoints exist, set `INFERENCE_ENABLED=false` and keep `PULSEFEED_FAKE_MODE=false`. Home and admin remain available; the reader explains the pause, and admission, retry and provider-backed image routes return `503 INFERENCE_PAUSED` without fetching feeds or reserving quota. The coordinator stays stopped. A new database is not bound to empty endpoint IDs; an existing provider binding is preserved. `/health` reports `generation-paused`. Configure the real Runpod key and both endpoint IDs privately, complete endpoint verification, then set `INFERENCE_ENABLED=true` and deploy. Normal credential validation and provider identity checks apply when enabling. This mode generates no synthetic results.

Keep one Railway replica and one ASGI process/coordinator. Do not add Gunicorn/Uvicorn workers or a second service sharing the database. The checked-in legacy `railway.json` records those intended settings; verify they are explicitly present on the current service. `/health` reports app mode/liveness; it does **not** make an inference call or prove endpoint readiness. Railway's deployment healthcheck is not continuous monitoring, and volume-backed redeployment can have downtime. [Railway healthchecks](https://docs.railway.com/guides/healthchecks)

Keep `SESSION_SECRET` stable to preserve session lookup across restart. SQLite binds provider mode and endpoint IDs; use a separate database for Spark, fake mode and cloud. API-key rotation does not change that binding, but endpoint-ID replacement does. See [operations](operations.md#provider-and-secret-changes).

### Authentication and ingress

Fresh visitors need no CLI seeding. Open `/admin` and submit the configured password through its protected form. Login preserves the visitor owner while rotating the session. Token rotation/removal revokes derived admin sessions. Bare `?admin` only requests diagnostics for an already authorized admin.

The compatible `/?admin_key=<token>` exchange redirects to clean `/admin`, but the bearer credential can enter browser history and upstream logs. Prefer the form; if links are used, verify edge/proxy/tracing query redaction. The app disables access logging, sends private `no-store` responses and uses referrer controls, but cannot control upstream retention. Confirm HTTPS cookie flags, CSRF/same-origin behavior and proxy IP handling on the actual Railway route before external release.

## 5. Connected release smoke

Perform these gates deliberately; real image generation spends provider allowance/budget. Start with one safe public feed and a low page cap, then restore the intended policy through the admin UI.

1. Verify both endpoint workers pass their own target-GPU smoke and report the pinned metadata. Confirm images, cache settings and effective endpoint concurrency/timeouts.
2. In a fresh external browser, open the app without a private link, enter a feed and create a reader. Confirm normalized excerpts, original source links, labeled images and live provenance. No account seeding is required.
3. Confirm exact-key replay and reader reload add no provider jobs. Restore saved Blobs after refresh; test unavailable browser storage and unavailable provider results without automatic regeneration.
4. Use two independent visitor sessions to verify foreign, expired and missing views fail generically. Check CSRF, mutation throttles, global/hourly reservations and admin rejection.
5. Sign in at `/admin`; inspect Qwen tokens separately from FLUX images/attempts, live versus synthetic coverage, limits, prompt sections and reset behavior. Compare aggregates to known invocation history, not provider billing estimates.
6. Verify restart recovery for a **known** job under a controlled ingress write barrier, then test drain/rollback without overlapping admissions. Unknown acceptance and destructive GPU cancellation need controlled evidence; do not improvise them against public traffic.
7. Record public URL, commit, image digests, redacted settings, GPU/driver, measured timings and gate results in a new validation checkpoint. Record failures and unverified gates as such.

The one-hour-reader/30-minute-Runpod-result gap remains a product limitation. A working first visit does not prove a late uncached return can retrieve every image. No cloud latency, throughput, cost or availability target is established until measured on this topology.
