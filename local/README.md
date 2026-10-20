# DGX Spark local testing

Run Qwen NVFP4 and FLUX in separate native ARM64 containers on DGX Spark, with FastAPI on the laptop. The final maintained October 20 native images passed a 27-image / 54-call application flow with zero retries on GB10 (SM 12.1), following two earlier remediation flows and cancellation checks. The corrected AMD64 Qwen image passed 87 CPU/SDK tests, strong runtime imports, inventory/provenance and independent scan review. Its scan retains 1,907 findings, including zero Critical and 22 High; these are scoped findings, not blanket vulnerability clearance.

Nothing from this remediation has been pushed to a registry or deployed to Runpod/Railway. Actual AMD64 target-GPU/native-queue and Railway deployment checks remain separate gates. See the [remediation checkpoint](../docs/dependency-remediation-2026-10-20.md), [integration evidence](../integration/VALIDATION.md) and [Spark record](SPARK_VALIDATION.md) for exact image identities and historical results.

[Repository overview](../README.md) · [architecture](../docs/architecture.md) · [source walkthrough](../docs/code-walkthrough.md) · [cloud deployment](../docs/deployment.md) · [maintenance and rollback](../docs/operations.md)

This guide runs real models. `smoke` and an explicit reader submission perform inference; build, config and preflight do not silently submit jobs. Keep private environment files out of shared output. Raw artifacts linked by historical validation records are workspace-only unless separately reviewed for publication.

The worker HTTP gateways preserve `/run`, `/status/{id}`, `/cancel/{id}` and `output.result`. Each worker accepts one active invocation. A shared filesystem lock serializes actual inference across the two containers on the same GPU. Both models stay loaded; this requires enough unified memory for both runtimes, their caches, and the OS. Do not increase Qwen memory utilization or concurrency before measuring combined residency.

## Bounded application pipeline

**Pipeline/reveal revision: implemented and locally validated on Spark.** See the [validation record](../integration/VALIDATION.md) for test scope and evidence.

`COORDINATOR_PIPELINE_SIZE` accepts `1` or `2`, default `2`. One global batch remains active; at most two stories may be started but unfinished, with at most one Qwen invocation and one FLUX invocation in flight. Prefer the oldest FLUX-ready story before starting another Qwen stage. Set `COORDINATOR_PIPELINE_SIZE=1` to roll back to serial story processing. Existing atomic admission, two-attempt reservations, quota limits, deadlines and uncertain-job capacity holds remain unchanged. On Spark, worker concurrency remains one and the shared GPU lock serializes actual inference: requests may overlap without simultaneous Qwen/FLUX GPU execution. No speedup is assumed; compare measured end-to-end timings before making a performance claim.

For the laptop launcher, pass this option in the process environment when restarting FastAPI: `COORDINATOR_PIPELINE_SIZE=1 PULSEFEED_PYTHON="$PWD/app/.venv/bin/python" ./local/control.sh laptop`. The default value of `2` needs no override.

Start the one-shot adaptive article reveal and spinner fade after `min(3, selected card count)` images have actually decoded in the browser. Provider completion or a fetched Blob alone does not meet that threshold. Remaining cards keep their own placeholders and fill independently without restarting the page reveal. Keep the existing error/recovery, reduced-motion and expiry behavior; browser persistence does not delay display or admit more inference.

## Prepare the Spark

The maintained Spark checkout is `/home/vacko/pulsefeed-dependency-remediation-20261020/src`; use it for future configuration, planned restarts and rebuilds. Its private `local/.env` is a mode-600 copy with the same values as the preserved original. Normal local image aliases `pulsefeed-spark-qwen:latest` and `pulsefeed-spark-flux:latest` identify the tested native images; these are local aliases, not published registry releases.

```sh
cd /home/vacko/pulsefeed-dependency-remediation-20261020/src
./local/control.sh config  # Quiet validation; do not print the private environment.
./local/control.sh status
```

For a planned restart, first drain accepted work as described below, then use `stop` / `start` from this maintained checkout. `start` reuses the existing images and caches; a routine restart does not require `build`. The older checkout remains for reference and should not supply new worker builds.

Use the maintained repository checkout on the Spark. Requirements: Linux aarch64, Docker with Compose supporting `gpus: all` (Compose 2.30+), NVIDIA Container Toolkit/driver, Python 3, sufficient disk and model access. Do not build through x86 emulation. No script changes host drivers, installs host system packages, uses SSH, or provisions cloud resources. Reviewed OS corrections are confined to the worker image build.

The native Qwen recipe uses the pinned `vllm/vllm-openai:v0.32.0-ubuntu2404` base, PyTorch 2.13/CUDA 13.0 and FlashInfer 0.7.0.post1, with a reviewed dependency overlay. FLUX uses a pinned Python 3.12.15 slim Trixie base and a complete wheel runtime: PyTorch 2.13/CUDA 12.9, Diffusers 0.41.0 and Transformers 5.20.2. Both native images use the authenticated local gateway and omit the Runpod SDK. The separate AMD64 Qwen recipe uses CUDA 12.9 and the selective `1.10.0+pulsefeed.security.1` SDK backport; AMD64 FLUX uses SDK 1.12.0. Model revisions, NVFP4/BF16 precision and GPU targets are unchanged. Read the maintained `Dockerfile.dgx-spark` files, worker guides and [lock workflow](DEPENDENCY_LOCKS.md) for exact digests and artifacts. Confirm driver compatibility before building; the scripts do not upgrade the host driver.

**Validated build inputs:** Qwen's 32-update recipes, locks and prepared contexts are frozen. The maintained native pair passed real Spark GPU/application checks; both AMD64 images passed their CPU packaging and independent scan reviews. Residual findings and the real Runpod GPU/queue gate remain in the [remediation status](../docs/dependency-remediation-2026-10-20.md).

```sh
./local/control.sh init-env
# Edit local/.env privately; add HF_TOKEN only if model access requires it.
chmod 600 local/.env
./local/control.sh preflight
./local/control.sh config
./local/control.sh build
```

`init-env` creates random worker/session/admin secrets with mode 600 and refuses to overwrite an existing file. Use raw `KEY=value` entries without quotes, `export`, or shell expressions. Never paste the file into logs, commits, chat or Docker build arguments. Compose passes secrets at runtime; Docker-authorized users can inspect container environment variables. Avoid printing `docker compose config` or container inspection output containing environment values; the control script validates configuration quietly.

`build` prepares Qwen automatically with `workers/qwen/prepare_build.py --platform arm64` in a private temporary directory outside the checkout. It authenticates the exact dependency/OS archives and copies only allowlisted source inputs; Compose uses that generated context, then the wrapper removes it on success or failure. FLUX retains its separate root-context recipe and explicit copy list. Do not bypass preparation with a raw checkout-context Qwen build. Preparation/build can download large bases and dependencies, but neither starts models nor downloads model weights. Secrets, environments, databases and evidence are excluded from the build inputs.

After building, prefetch the pinned weights before starting either gateway:

```sh
./local/control.sh prefetch       # Both, sequentially; equivalent to prefetch all
./local/control.sh prefetch qwen  # Or just one worker
./local/control.sh prefetch flux
```

Both services explicitly use `MODEL_CACHE_SOURCE=volume`. After successful prefetch, set `QWEN_CACHE_OFFLINE=true` and/or `FLUX_CACHE_OFFLINE=true` in the Spark's private `local/.env` to require the corresponding complete pinned cache during worker startup. Both default to `false`, allowing startup downloads. Offline startup fails on a missing/incomplete snapshot instead of downloading or choosing another revision. The cache paths and existing named volumes remain unchanged. `prefetch` is always an explicit online cache-hydration operation, even when these worker offline settings are true; it does not start a gateway or perform inference.

Both model services use Docker's `local` logging driver with `max-size=10m` and `max-file=3`: approximately 30 MiB per container plus logging metadata/overhead. This bounds retained container logs without changing model cache retention.

This verifies the worker image already exists locally, runs it with `compose run --rm --no-deps -T --pull never`, overrides its gateway entrypoint with a read-only mounted download helper, and reuses its configured cache volumes and private `HF_TOKEN`. It exposes no service ports, starts no inference/model loader, and does not change startup budgets. The one-off download timeout is 300 seconds per network read; the overall prefetch has no gateway startup deadline. Only the helper's safe start/success/failure messages are shown; third-party download progress, retry logs and stderr are suppressed because they can contain signed URLs. Failures retain cached files and can be explicitly rerun after checking model access/network configuration. Do not print the private environment when diagnosing access failures.

Qwen downloads exactly its full pinned snapshot (`nvidia/Qwen3-8B-NVFP4`, revision `ccd10a893cbca613259517c3efe08e151ddf2b8e`) into `MODEL_CACHE_DIR=/runpod-volume/huggingface`, directly under `models--nvidia--Qwen3-8B-NVFP4/snapshots/`. FLUX downloads only the loader's `model_index.json`, `scheduler/*`, `tokenizer/*`, `text_encoder/*`, `transformer/*` and `vae/*` patterns from `black-forest-labs/FLUX.2-klein-4B` revision `e7b7dc27f91deacad38e78976d1f2b499d76a294`, under `HF_HUB_CACHE=/runpod-volume/huggingface/hub/models--black-forest-labs--FLUX.2-klein-4B/snapshots/`. No separate checkpoint/text-encoder repository is needed. Default named volumes are `pulsefeed-spark_qwen-cache` and `pulsefeed-spark_flux-cache`; configured host cache paths work identically. Successful prefetch proves only cached downloads; startup must still validate kernels, load both models and complete GPU readiness probes.

```sh
./local/control.sh start
./local/control.sh status
```

`start` uses existing images only, without building/pulling them. Qwen starts first; Compose waits for its healthy status before starting FLUX, so model initialization and GPU startup probes do not run concurrently. The start command can remain blocked while Qwen initializes, and an unhealthy Qwen prevents FLUX from starting. Without prefetch, first startup downloads pinned weights and loads both models; later starts reuse weights. To use existing Spark cache directories, set `QWEN_CACHE_PATH` and `FLUX_CACHE_PATH` to absolute host paths in `local/.env` before starting. Blank values create separate named cache volumes. Weight caches are distinct from generated-image storage.

Readiness becomes successful only after the factory loads its model. A running container is not proof of readiness or valid GPU inference. `status` prints container/health state without secrets. Startup failures require private worker diagnostics; do not repeatedly restart to hide unsupported kernels or failed model access.

The gateway has a 660-second overall startup watchdog. Qwen's 480-second and FLUX's 600-second model initialization budgets may expire first. Prefer prefetch before cold startup so downloads do not consume these budgets. If startup still fails, inspect the cause rather than increasing timeouts; its cache survives failure. A native execution watchdog forces the container to exit if GPU work cannot be stopped safely. Containers have no automatic restart/replay policy: inspect the stopped process and unresolved jobs before starting manually. If a container remains running with an unhealthy runtime, stop it and then start it explicitly after correcting the cause.

## Connect the laptop

The Spark ports are bound to **Spark loopback**: Qwen `127.0.0.1:8181`, FLUX `127.0.0.1:8182`. They require `Authorization: Bearer <LOCAL_WORKER_TOKEN>` for every route. The browser never receives that token.

On the laptop, create a private `local/.env` with `./local/control.sh init-env`. Transfer **only the worker token** securely from your Spark file into the laptop file; keep the laptop session secret independent. A tunnel is recommended; run this yourself in another laptop terminal, replacing `spark-host` with your configured SSH host:

```sh
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:8181:127.0.0.1:8181 \
  -L 127.0.0.1:8182:127.0.0.1:8182 spark-host
```

No SSH command is run by the scripts. If local ports are occupied, choose different forwarding ports and update `LOCAL_QWEN_URL`/`LOCAL_FLUX_URL` in the laptop file. No Spark port needs to be exposed on the LAN. Both hosts' clocks should be synchronized because worker deadlines use UTC milliseconds.

Install the lightweight app dependencies into a laptop virtual environment (no GPU packages):

```sh
python3.11 -m venv .venv-local
.venv-local/bin/python -m pip install -c app/requirements.lock -e './app[test]'
export PULSEFEED_PYTHON="$PWD/.venv-local/bin/python"
./local/control.sh laptop
```

Open the configured `APP_BASE_URL` in any browser. The public home creates an isolated visitor session automatically; no account or access link is required. The server disables access logs to keep query credentials out of request logs. The launcher binds only to laptop loopback and sets `INFERENCE_PROVIDER=local_http`, `PULSEFEED_FAKE_MODE=false`, and `COOKIE_SECURE=false` for local HTTP. It uses the configured worker URLs/token and a **separate database** at `local/data/pulsefeed.db`. Never reuse a database holding Runpod invocations: provider job identifiers belong to the backend that created them. The laptop file's `APP_BASE_URL` and `APP_PORT` must agree if changing port 8000.

Use a real public RSS URL. Each new view selects the policy-limited affordable source-order prefix (default ten stories; the recorded live local override is nine). Session ownership, CSRF, quotas, conservative attempt accounting and browser IndexedDB persistence still apply. Cookies and browser images remain separate in Chrome, Codex and other browsers.

For admin access, run this in a **private laptop terminal**, with the same `PULSEFEED_PYTHON` configured above:

```sh
./local/control.sh admin-url
```

For everyday access, bookmark `/admin` and enter the value of `ADMIN_ACCESS_TOKEN` from your private `local/.env` as the admin password. The dashboard contains prompts, usage and limits; Qwen, FLUX and content filters each save independently. The command above remains a one-click private-link shortcut.

Open the printed URL in the browser you want to use. It is assembled from `APP_BASE_URL`, `ADMIN_ACCESS_PARAM` (default `admin_key`) and `ADMIN_ACCESS_TOKEN` in the laptop's private `local/.env`. It exchanges the reusable credential for an admin session and redirects to `/admin`; `/?admin` is only a protected reader display mode. The admin grant lasts `ADMIN_SESSION_SECONDS` (default 3,600 seconds). Existing visitor feed ownership is preserved on elevation, and other browsers' feeds remain inaccessible.

For an older local environment, add `ADMIN_ACCESS_PARAM=admin_key` and a privately generated `ADMIN_ACCESS_TOKEN` (at least 32 URL-safe random characters) to `local/.env`, then restart the laptop app. `init-env` intentionally does not overwrite existing secrets. `PUBLIC_ACCESS_ENABLED=true` is the default; `VISITOR_LIMIT_PER_MINUTE=20` caps new visitor sessions per IP. Public RSS fetches also have an IP limiter, so clearing cookies does not bypass it.

Keep the admin URL out of shared logs, screenshots and source control. Changing its parameter name or token, or clearing its token, then restarting revokes existing grants. A clean redirect and `no-referrer` reduce propagation but cannot erase browser history or upstream proxy logs. Railway uses a separate runtime configuration: seal `ADMIN_ACCESS_TOKEN`, `SESSION_SECRET` and model credentials in its service variables; use HTTPS and `COOKIE_SECURE=true`. See the [app deployment guide](../app/README.md#railway).

## Explicit real-model smoke test

On the laptop through the tunnel, using the environment above:

```sh
./local/control.sh smoke
# Optional: generate a second prompt/image pair to compare first/warm request timing.
./local/control.sh smoke --warm
```

This waits up to 20 minutes for each gateway, submits invalid inputs to verify zero inference attempts, then makes **one real Qwen request and one real FLUX request**. `--warm` makes two of each. It requires Pillow (included in app dependencies), validates pinned model identity/revisions, bounded retry counters, correlation IDs, NVFP4 prompt output and a decodable 1024 × 1024 WebP. It prints model metadata, counters, usage and timings, never image bytes or credentials. Keep output as evidence only after an actual successful Spark run.

Direct smoke calls bypass application quota accounting and content filtering; they use a fixed harmless test article. The smoke command is an explicit operator action, never part of preflight/build/start. It never automatically repeats `/run`. After known-job errors it requests cancellation, but cancellation acknowledgement alone does not prove the GPU stopped. Check status/process termination before replacement work.

Gateway jobs and generated images are transient, in memory, with a one-hour result window measured from terminal completion. `LOCAL_RESULT_CAPACITY` sets the retained job slots per worker: default 128, integer range 1–256, validated before model loading. Both Compose services receive the same configured bound; existing environment files use 128 when the variable is absent. Completion occurs after reader-view creation, so retained results cover the remainder of a valid one-hour view even if the visitor left before an image reached IndexedDB. The gateway rejects additional submissions when full; it does not evict active jobs or unexpired results to make space. At the transport's 4 MiB + 8 KiB maximum response size, the default 128 retained responses use at most 513 MiB per worker (1,026 MiB at the maximum 256), plus Python objects and transient processing buffers. Authenticated `/health` reports only aggregate `active_jobs`, `retained_jobs` and `result_capacity` counts, pruning expired records first; it exposes no job identifiers or payloads. A full store still returns a definitive HTTP 422 rejection without inference, while exact invocation replay retrieves its existing job even when full. Changing capacity requires rebuilding the gateway image and recreating the worker container; an environment-only change requires recreation. Establish an external admission barrier, drain accepted work, and account for cached-result loss before restarting; observing idle once is not a maintenance barrier. The capacity change was deployed and verified in the 16 October integration checkpoint; it does not reset application allowance. See the [drain procedure](../docs/operations.md#drain-before-changing-a-running-deployment). Container restarts discard these records, so retrieval through a valid page is still subject to worker availability. The application preserves metadata; if a known job disappears it keeps uncertain capacity charged until reconciliation/operator resolution, rather than resubmitting. Browser images committed to IndexedDB remain available only within their original view lifetime and are purged on expiry. No generated image is written to a cache volume or S3.

This retention change applies only to the local gateway. Runpod documents asynchronous `/run` result retention as 30 minutes after completion; its separate job TTL does not establish one-hour result availability. Cloud retrieval for a visitor returning late without a browser copy remains a deployment limitation to resolve and validate. See [Runpod's operation reference](https://docs.runpod.io/serverless/endpoints/operation-reference) and [endpoint settings](https://docs.runpod.io/serverless/endpoints/endpoint-configurations#result-retention).

```sh
./local/control.sh stop
```

Stop does not remove cache/coordination volumes or laptop data. Stop only after jobs finish, or treat interrupted work as uncertain. Do not use `down -v` unless deliberately discarding the model caches. Later cloud deployment remains a separate task and needs its own real GPU/runtime verification.

If a native worker reports an unknown execution outcome, the gateway exits while holding the shared GPU lock, before publishing a terminal job result. This prevents the other model from starting over uncertain native work. The container stays stopped for operator inspection; the app retains uncertainty rather than automatically replaying the invocation.

## Validation gates

The [October 20 remediation](../docs/dependency-remediation-2026-10-20.md) records the maintained runtime inputs, completed FLUX artifact checks, the reviewed AMD64 Qwen correction and its independently verified final scan. [Packaging validation](PACKAGING_VALIDATION.md) preserves the older October 15 measurements; its no-overlay Qwen assumptions and accepted vendor diagnostics are historical, not permission to bypass current checks. See [dependency lock maintenance](DEPENDENCY_LOCKS.md) before changing runtime packages.

The early Spark run passed native builds, GPU probes, pinned real Qwen/FLUX inference with both models resident, the NASA RSS-to-three-images flow, browser restoration, usage/quota display, isolation and exact-key replay. Its dashboard/policy measurements are historical. Current selection is per new page, with no per-URL daily allowance; fresh-database defaults are 100 daily FLUX attempts, 30 global rolling-hour starts and ten selected stories/page. The recorded local 120/day and nine-story policy is an operator override. The October 16 ten-image result and retained failed/interrupted attempts are documented chronologically in [integration validation](../integration/VALIDATION.md). Direct model smoke requests bypass application accounting.

Earlier fixes preserved the then-selected NVIDIA entrypoint/CUDA initialization, repaired its `pynvml` wrapper, requested tokenizer `return_dict=False`, added prefetch outside startup deadlines and tolerated transient readiness-GET disconnects without submission retries. The October 20 candidate runtime changes and cancellation proof supersede those old packaging assumptions; they do not establish repeated cold/warm benchmark distributions, all failure/deadline scenarios or AMD64 RTX PRO 4500 kernel compatibility. AMD64 Runpod GPU/queue and Railway deployment gates remain open. Retained FFmpeg/native, OS and source-specific findings are scoped in the remediation report. Dependency scans and targeted mitigations are not blanket vulnerability clearance.
