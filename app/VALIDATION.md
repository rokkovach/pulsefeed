# App lane local validation

Application executor: GPT-6.1-Sol. Python quota/orchestration/API changes, tests and documentation stayed in the assigned app files. The separate usage executor owns `usage.py`/`test_usage.py`; the UI executor owns templates/static.

## Optimization and local-provider checkpoint — 15 October 2026

The current app suite passes **217 tests**. Added checks cover loopback-only local worker origins and Bearer authentication, the narrowly scoped local HTTP cookie exception, immutable database provider identity, uncertain submission without replay, explicit SQLite connection cleanup, hashed static assets and conditional caching, protected-response `no-store`, and full own-article metrics in initial HTML. The six UI scripts pass **69 checks**. Separate GPT-6.1-Sol reviewers found no remaining actionable issues after corrections.

The updated app image is `pulsefeed-app:local-spark-ready`, Linux ARM64 digest `sha256:50167d7643d2115e52fc48bf6e13620c6079832c43597f777fd304a8b45bd3a4`. A network-disabled smoke with a throwaway database started the actual `local_http` adapter, rendered authenticated reader/admin routes, served seven hashed public assets and confirmed no invocation was submitted. No GPU endpoint was available or called. This supersedes the earlier app image identities below.

Actual browser reloads recorded CLS 0 with delayed fonts/JavaScript/API reads. Three saved images restored from IndexedDB. Expanding a full server-rendered article-details panel before hydration caused only the intentional disclosure expansion, with no later layout shift. The ordinary server was restored after measurement. See [coordinator evidence](../integration/VALIDATION.md).

## Reader refresh snapshot evidence

- `app/.venv/bin/python -m pytest -q app/tests/test_reader_bootstrap.py` → **17 passed**, one dependency deprecation warning, 0.36 seconds.
- `app/.venv/bin/python -m pytest -q app/tests` → **165 passed**, the same warning, 5.41 seconds.
- Initial authenticated HTML now receives an owner-scoped `initial_feed` snapshot. URL selection has deterministic owned fallback; an empty account never borrows another visitor's feed. Feed metadata, source membership/order, latest run/invocation metadata and active batch are read consistently in one SQLite snapshot.
- Bounded regressions cover selected/fallback feeds and equal-time ordering, foreign-ID privacy, empty accounts, source caps/order, admin/auth gates, completed-run restoration with no mutations/submissions, and a real admission committed between feed/run reads without producing mixed-version HTML.
- The read-only `source_hostname` template helper and item `display_source` labels share safe HTTP(S) hostname normalization for server/browser text: lowercase IDNA, omitted credentials/ports/leading `www.`, compressed bracketed IPv6, and empty malformed/unsupported links. Eleven parameterized cases cover these spellings and invalid input.

## Final SSR container and layout gate

- `docker build -t pulsefeed-app:local app` → passed with the frozen SSR/backend/UI assets.
- Final `docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' pulsefeed-app:local` → **`sha256:f92c0abb3cc6185f84f61db8bf3cd220caa912f559fe54e4df8752680de4e2f0 linux/arm64`**.
- BuildKit runtime config digest: `sha256:e7b6702867c22a295990b9e3b3c6e49580e2df506717e090de9c0e5f4c25940a`.
- A throwaway `docker run --rm --network none -i pulsefeed-app:local python -` smoke loaded the packaged FastAPI app, explicit synthetic mode and an injected 13-item feed. Its RSS fetcher rejected any invocation. The bounded check rendered the selected older owned feed immediately, ten SSR story cards and the reserved image region; a foreign feed ID fell back to the newest owned feed without revealing its private sentinel. Repeated page GETs left every database table unchanged and made zero provider submissions.
- Packaged `app.css`, `app.js`, `theme.js`, `settings.js`, `admin.js`, `htmx.min.js` and `Silkscreen-Regular.ttf` all returned HTTP 200 with nonempty content. Final app CSS SHA-256: `8a054bedea6a3bb831442bdea993359db856b9d0d4f03d74027a9890a54c6bfd`; app JS SHA-256: `5213befd15efcd6678baed3acc86ed39de52af81b18f660f0da7de982683e9a8`.
- The first smoke draft also polled `GET /api/feeds/{id}/items`, which correctly adds rate-limit bookkeeping and invalidated its all-table equality assertion. Removing that unrelated API poll made the intended page-GET immutability check pass; no application change was necessary.
- Coordinator browser evidence: actual PerformanceObserver **CLS 0** while fonts, app JS and APIs were delayed 900 ms on normal/scrolled reader reload, admin reader and admin dashboard. Scrolled reload retained `scrollY=650.833` and identical initial/final element rectangles. Logo navigation reused the same image Blob URL without reload and scrolled to the top. Final UI suite: **60 checks**, independently reviewed; final app suite: **165**.

The following quota/admin container record is the earlier feature checkpoint. The final SSR image above supersedes its image identity; quota/model behavior was unchanged and was not redundantly rerun.

## Quota/admin container evidence

- `app/.venv/bin/python -m pytest -q app/tests` → **148 passed**, one Starlette/AnyIO dependency deprecation warning, 5.08 seconds. The independent backend reviewer also passed all 148.
- `app/.venv/bin/python -m pytest -q app/tests/test_daily_limits.py app/tests/test_usage.py` → **52 passed**, one identical warning, 1.61 seconds on the frozen backend.
- `docker build -t pulsefeed-app:local app` → passed with final quota/admin Python and frozen reader/admin UI assets.
- `docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' pulsefeed-app:local` → `sha256:275488bedebea87acb8d8f2360bda43244a420ad800a9a675e36edce28847d0d linux/arm64`.
- BuildKit runtime config digest: `sha256:a0b15c22922b0f71976938bf2fe0578a5bcf368ea1f35645b0eb44a8060d5a98`.
- Base: `python:3.11.12-slim-bookworm`, resolved to `sha256:dbf1de478a55d6763afaa39c2f3d7b54b25230614980276de5cacdde79529d0c`.
- Pinned dependencies are recorded in `pyproject.toml` and `requirements.lock`.

The final `docker run --rm --network none -i ... pulsefeed-app:local python -` smoke used explicit synthetic mode, a throwaway database, injected 13-item RSS data, and the packaged FastAPI application. It made no external network/model calls and removed its container automatically.

Observed result:

```json
{
  "source_items_after_cap": 10,
  "automatic_runs": 3,
  "reserved_before_and_after_active_reset": 6,
  "known_used_after_late_completion": 3,
  "remaining_after_completion": 9,
  "shared_feed_alias_denied": true,
  "terminal_reset_charge": 0,
  "history_runs_preserved": 3,
  "history_invocations_preserved": 6,
  "model_usage_history_preserved": true,
  "policy_edits_persist": true,
  "aggregate_private": true,
  "mode": "synthetic",
  "retry_metric_present": true,
  "provider_submissions": 6,
  "audit_events": 3,
  "durable_image_payloads": false
}
```

The smoke verified default 100/10 limits, an authenticated admin edit to temporary 12/6 limits, ten-item truncation before persistence, three automatic runs, active reservations retained through an API reset, and known charges retained when those runs finalized afterward. A second visitor's host/default-port/fragment alias was denied by the shared feed allowance. A second reset cleared terminal allowance without deleting model usage, runs or invocations. Policy edits survived reopening with default seed values. API and rendered admin HTML excluded private sentinel content and visitor/run/feed identifiers. New invocation provenance and retry metrics were present; SQLite held no base64.

The unchanged Docker CMD was previously checked with `PORT=8093` inside a temporary network-disabled container and returned `{"status":"ok","mode":"synthetic-demo"}`. It runs one process and disables access logs containing link tokens.

## Accounting and access guarantees

Schema version 3 stores canonical feed quota identity, invocation execution provenance, persisted daily policy/audit, finalization time and reset epochs. Defaults are 100 shared FLUX inference attempts per UTC day and 10 per canonical feed, additive to 30/hour/account and five submissions/minute. Every admission route reserves two attempts per run atomically, checks persisted policy, reconciles reliable worker counts and retains uncertain accounting conservatively. Replays/GETs/browser saves consume no allowance.

UTC day boundaries and manual resets retain active/uncertain reservations. Finalization adopts the current reset epoch and time so late completion stays charged today. Epochs make same-timestamp and transaction-order resets safe. Confirmed-terminal usage can be reset without deleting history; unreported counters remain distinct from known usage.

Only the first ten raw source items are persisted/current/displayed, including injected fetchers and old cached/legacy reads. Automatic admission caps raw positions at three before GUID collapse or previous-run filtering; later items never backfill. Any prior run prevents automatic replacement. Latest current-item runs come from the complete history.

Admin HTML and APIs enforce the actual role. Policy/reset writes require CSRF, strict bounded inputs, a shared write rate limit and private audit. Usage aggregates expose model/stage/mode counts and reported/missing metrics only, never other visitors' feeds, articles, prompts, account IDs or provider IDs. Current quota allowance is separate from immutable model-usage reporting history.

Tests cover global/per-feed cross-account boundaries, concurrency, canonical aliases, direct/manual/retry/idempotent paths, restart persistence, actual/unknown counters, midnight, equal-timestamp reset, active reservations, CLI settlement, migration/provenance, role/CSRF/rate/audit/privacy, ten-item read/write caps, and all prior auth/SSRF/orchestration/image/recovery behavior. Usage tests cover modes, retries without multiplication, null/partial metrics, UTC reporting windows, private sentinels and read-only aggregation.

The coordinator owns actual browser evidence. It reported successful admin save/reset/period controls, UTC/local reset display, preserved unsaved drafts, mode-separated usage/retries, light/dark UI and unchanged own-feed prompt/image restoration. UI suites pass **51 checks**; root integration passes **53 checks**. See coordinator-owned `integration/artifacts/pulsefeed-admin-usage.jpg` and associated captures.

## Remaining live gates

No cloud provisioning, credentials, model weights, GPU work or paid inference were used. The tested image is native Linux ARM64. Railway build/deployment, HTTPS/proxy/volume configuration, retained-volume redeployment, actual Runpod Qwen/FLUX inference, GPU compatibility/timings and the connected live demonstration remain release gates.

Synthetic model identities/images are fixtures, not live-model evidence. Setup, admin APIs, quota semantics and operator commands are documented in `README.md`.
