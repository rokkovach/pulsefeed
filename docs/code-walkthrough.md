# Code walkthrough

Start at [`main.py`](../app/pulsefeed/main.py), then follow the admission and coordinator paths below. The app uses plain FastAPI routes, Jinja templates, SQLite transactions and small JavaScript modules; there is no frontend framework or external application queue.

## Source map

| File / directory | Responsibility |
| --- | --- |
| [`app/pulsefeed/main.py`](../app/pulsefeed/main.py) | App factory, lifecycle/coordinator startup, routes, cookies, CSRF, response headers and view checks |
| [`config.py`](../app/pulsefeed/config.py) | Environment parsing, safe origins, limits and provider-profile fingerprint |
| [`auth.py`](../app/pulsefeed/auth.py) | Anonymous visitors, sessions, admin exchanges, challenge signing, IP and account throttles |
| [`rss.py`](../app/pulsefeed/rss.py) | SSRF-safe transport, bounded fetching, RSS/Atom parsing, HTML/XHTML normalization and article bounds |
| [`db.py`](../app/pulsefeed/db.py) | Schema/migrations, explicit transactions, defaults, expiry compaction and provider binding |
| [`service.py`](../app/pulsefeed/service.py) | View idempotency, snapshot ownership, atomic capacity/quota reservation, retries and prompt composition |
| [`coordinator.py`](../app/pulsefeed/coordinator.py) | Persist-before-submit state machine, per-stage pipeline, polling, deadlines, cancellation and settlement |
| [`provider.py`](../app/pulsefeed/provider.py) | Runpod/local/fake transports, conservative submission uncertainty, result and image validation |
| [`usage.py`](../app/pulsefeed/usage.py) | Aggregate usage, reporting coverage and UTC time buckets |
| [`prompt_defaults.py`](../app/pulsefeed/prompt_defaults.py) | New-database prompt/filter defaults; existing saved settings remain authoritative |
| [`static/app.js`](../app/pulsefeed/static/app.js) | Form submission/recovery, reader hydration, polling, retained card nodes and loader |
| [`static/image-store.js`](../app/pulsefeed/static/image-store.js) | IndexedDB commit/restore, owner/view scoping, expiry and Blob URL cleanup |
| [`static/admin.js`](../app/pulsefeed/static/admin.js), [`settings.js`](../app/pulsefeed/static/settings.js) | Aggregate charts, limits/reset, independently saved prompt/filter sections |
| [`templates/`](../app/pulsefeed/templates) | Initial server-rendered cards, reserved image spaces, reader and admin pages |
| [`assets.py`](../app/pulsefeed/assets.py) | Content-hash asset URLs and CSS dependency stamping |

## Follow a story

1. **Create a visitor.** A fresh `GET /` creates an isolated visitor/session through `auth.bootstrap_visitor`. The browser receives an HttpOnly/SameSite cookie; the database stores an HMAC hash, not the raw session token. `/api/session` exposes the bounded state the UI needs.
2. **Submit once.** The home UI saves a visitor-scoped idempotency record before `POST /api/view-feed`. The route checks session, CSRF, request shape and throttles, and checks replay before fetching. `rss.fetch_feed` validates and normalizes the feed. A bad feed keeps the entered URL on the form.
3. **Freeze admission.** `Service.view_feed` commits the view snapshot, fixed expiry, request-key relation and selected runs atomically with reservations. The same key/URL returns the original result; the same key with a different URL fails. Current policy chooses an affordable prefix once.
4. **Prepare Qwen.** `Coordinator.prepare` snapshots the schema-1 article, system instruction, optional article layout, IDs and deadline into an invocation. The coordinator persists `submitting` before the provider POST, then saves the returned job ID. A transport failure cannot safely imply rejection.
5. **Validate and compose.** The coordinator polls the known job. `provider.validate_envelope` checks identity/usage/output. `compose_prompt` expands the saved FLUX layout, applies filters and records fallback decisions before advancing the run.
6. **Generate and settle.** FLUX receives the fixed image preset and saved seed. Successful output is validated and decoded before metadata/accounting commit. SQLite retains the measured retry-inclusive counters once and releases only definitively unused reservations.
7. **Store in the browser.** The reader retrieves the existing image using run and view membership. `image-store.js` commits the Blob before labeling it saved. Refresh restores it without a new inference request. Expiry makes the reader inaccessible and triggers browser cleanup.

## Worker entry points

| Component | Read in this order |
| --- | --- |
| Qwen | [`handler.py`](../workers/qwen/handler.py): schema, counters, SDK callback → [`runtime.py`](../workers/qwen/runtime.py): pinned cache/profile, tokenizer, vLLM child process → [`healthcheck.py`](../workers/qwen/healthcheck.py): SDK/engine PID health |
| FLUX | [`handler.py`](../workers/flux/handler.py): SDK startup/cancellation/recycling → [`flux_worker.py`](../workers/flux/flux_worker.py): schema, retry budget, bounded encoding, Diffusers engine → [`preflight.py`](../workers/flux/preflight.py): runtime/device/BF16 probe |
| Local transport | [`worker_gateway.py`](../local/worker_gateway.py): bearer auth, native queue/status/cancel, shared GPU lock, retained results → worker `local_factory.py` |
| Wire contract | [`contracts/`](../contracts/README.md): input, inner envelope and SDK wrapper schemas → [`contract_checks.py`](../integration/contract_checks.py): cross-field/decoding checks |

A worker callback returns `{"result": envelope}`. The provider response contains `output.result`; the extra wrapper protects the contract's `error` field from the Runpod SDK's reserved top-level handling. CPU injection tests call the raw worker core and must not confuse that seam with provider serialization.

## Changes to make carefully

- Changing a schema or job field requires coordinated worker, app, schema/fixture and boundary-test updates. Deploy workers before the app sends optional fields that old workers reject.
- Changing model/runtime pins requires matching lock/provenance updates and real validation on the target architecture/GPU. Do not reuse Spark dependency locks for Runpod.
- Changing admission, reset or settlement needs transaction/race/restart checks. UI-only success is insufficient for quota correctness.
- Changing image storage needs commit-failure, corrupted-cache, expiry, refresh and owner-isolation checks. Retrieval failure must not become inference admission.
- Changing provider IDs on an existing database fails its provider-profile binding. Credential rotation is allowed because secrets are excluded from the fingerprint.

Use [integration checks](../integration/README.md) for the appropriate isolated suites. Record new observed results in validation evidence; preserve old measurements with their original scope.
