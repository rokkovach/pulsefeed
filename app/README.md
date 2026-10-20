# PulseFeed app

FastAPI, Jinja and bundled Tailwind/JavaScript run the web UI, anonymous sessions, safe RSS fetches, SQLite admission/accounting and one background coordinator. GPU libraries are kept in the separate workers. [Architecture](../docs/architecture.md) · [source walkthrough](../docs/code-walkthrough.md) · [deployment](../docs/deployment.md) · [operations](../docs/operations.md)

## Synthetic demo

From the repository root, using Python 3.11:

```sh
python3.11 -m venv app/.venv
app/.venv/bin/python -m pip install -c app/requirements.lock -e 'app[test]'
export DATABASE_PATH="$PWD/app/demo.db"
export APP_BASE_URL=http://127.0.0.1:8000
export SESSION_SECRET="$(python3.11 -c 'import secrets; print(secrets.token_urlsafe(48))')"
export PULSEFEED_FAKE_MODE=true
export COOKIE_SECURE=false
export PUBLIC_ACCESS_ENABLED=true
app/.venv/bin/python -m uvicorn pulsefeed.main:create_app --factory \
  --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

Open `http://127.0.0.1:8000/`, enter `demo`, and click **View RSS**. The server creates a visitor session; no seeding is needed. The demo feed and inference are synthetic and establish no GPU/cloud capability. A fresh request key may create fresh runs; replaying the original key preserves its selection and expiry. Synthetic provider results disappear on process restart. Preserve the secret privately if you want sessions to survive restarts; the command above generates a new one each time it is run.

Do not reuse the demo database for a real provider. Fake mode is disabled by default and must always be explicit. Public deployment requires HTTPS and secure cookies.

## Reader and admin

The home form submits a bounded URL/idempotency key with session and CSRF protection. Accepted views contain frozen source-order snapshots and expire after one hour. GET, reload, polling and image reads never start generation. A new key represents a new view, including for the same URL. Five simultaneous unexpired views per visitor are allowed by default; this is an active-view cap rather than a lifetime saved-feed limit.

Images pass through the app and are saved only as IndexedDB Blobs, keyed by visitor:view:run and original expiry. Marking an image saved happens after a successful database commit. Storage failure or missing provider output cannot silently trigger inference. Empty excerpts are omitted; nonempty source descriptions are normalized, clamped to three display lines and passed to Qwen's article context within the combined 4,000-character model bound.

For local admin access, set `ADMIN_ACCESS_TOKEN` to a distinct random URL-safe secret before starting the app, then open `/admin` and sign in. The token must be 32–256 characters and differ from `SESSION_SECRET`. The protected login preserves visitor ownership while rotating the session. The dashboard provides aggregate usage, independently saved Qwen/FLUX/filter settings, policy edits and explicit allowance reset. Bare `?admin` is only an authorized diagnostic presentation flag.

The compatible root-query credential exchange redirects to clean `/admin` but can expose the bearer token in history/upstream logs. Prefer the form and verify upstream redaction for deployed links. Token rotation/removal revokes derived admin sessions.

Legacy private access is optional compatibility: set `PUBLIC_ACCESS_ENABLED=false` and leave `ADMIN_ACCESS_TOKEN` unset, then use:

```sh
app/.venv/bin/python -m pulsefeed.cli seed --name evaluator --role evaluator
app/.venv/bin/python -m pulsefeed.cli seed --name admin --role admin
app/.venv/bin/python -m pulsefeed.cli issue-link --account evaluator
app/.venv/bin/python -m pulsefeed.cli --help
```

An issued access URL contains a credential; handle its output privately. This CLI is not required for normal public visitor access and is not a deployment/provisioning tool.

## Provider profiles

| Mode | Settings | Scope |
| --- | --- | --- |
| Runpod (default) | `INFERENCE_PROVIDER=runpod`, `RUNPOD_API_KEY`, `RUNPOD_QWEN_ENDPOINT_ID`, `RUNPOD_FLUX_ENDPOINT_ID` | Two native queue endpoints |
| Real Spark | `INFERENCE_PROVIDER=local_http`, static loopback `LOCAL_QWEN_URL` / `LOCAL_FLUX_URL`, secret `LOCAL_WORKER_TOKEN` | Authenticated gateways through SSH; [local guide](../local/README.md) |
| Synthetic | `PULSEFEED_FAKE_MODE=true` | Injected demo results; no real inference |

Use a separate SQLite database per profile. Startup binds provider mode and both endpoints into a fingerprint, excluding credentials. Changed endpoint IDs/origins are rejected before polling old jobs; key/token rotation is allowed. Local worker origins must be static HTTP(S) loopback origins without path, credentials, query or fragment. Insecure cookies are permitted only for explicit fake/local mode with a loopback app origin.

## Railway

Use repository root `/app` and its `Dockerfile`. Configure the healthcheck, replica count and restart policy explicitly on Railway; `railway.json` is a legacy reference, and the current API deprecates JSON/TOML Config as Code. Mount `/data`; set `DATABASE_PATH=/data/pulsefeed.db`, exact HTTPS `APP_BASE_URL`, stable `SESSION_SECRET`, private `ADMIN_ACCESS_TOKEN`, Runpod credentials, secure cookies and fake mode false. Keep **one replica, one ASGI process and one coordinator**. The Dockerfile expands Railway's `$PORT`, disables access logging and exposes `/health`. Complete variables, cache/GPU setup, proxy/auth checks and cloud smoke gates are in the [deployment guide](../docs/deployment.md).

SQLite policy values from the admin UI survive restarts. Environment defaults seed missing policy rather than overwrite it. Defaults are 100 global FLUX attempts/day, 30 global starts/rolling hour and ten selected stories/new page; current local test overrides are not deployment defaults. Read [admission semantics](../docs/architecture.md#admission-and-state) before changing limits.

## Tests and assets

```sh
app/.venv/bin/python -m pytest app/tests -q
```

Tests inject provider/network adapters and make no paid calls. For app/integration dependencies and all isolated suites, see [integration checks](../integration/README.md). Do not install the full worker/Runpod runtime into this app environment; SDK and app dependency constraints differ.

Tailwind 3.4.17 and Silkscreen are bundled with their licenses; Node is not needed to serve the app. To rebuild CSS from the repository root:

```sh
npm install --prefix /tmp/pulsefeed-ui-build --no-audit --no-fund tailwindcss@3.4.17
PYTHONPATH=app app/.venv/bin/python -m pulsefeed.assets --stamp-css
/tmp/pulsefeed-ui-build/node_modules/.bin/tailwindcss \
  -i app/pulsefeed/static/tailwind.input.css \
  -o app/pulsefeed/static/app.css \
  --content 'app/pulsefeed/templates/**/*.html,app/pulsefeed/static/*.js' --minify
```

Stamp before compiling so font/illustration dependencies have current hashes. Matching content-hash assets receive one-year immutable caching; stale/unversioned assets revalidate. Authenticated pages, APIs, redirects and errors remain `no-store`. Theme preference is nonsensitive localStorage; inference images live separately in IndexedDB.

[Historical app validation](VALIDATION.md) · [current integration evidence](../integration/VALIDATION.md) · [repository overview](../README.md)

To launch the production UI before connecting the models, set `INFERENCE_ENABLED=false` and leave fake mode false. The site and admin remain available, generation controls stay disabled, and generation/image routes return `503 INFERENCE_PAUSED` without provider calls or quota admission. After verifying both real endpoints and setting their private configuration, set `INFERENCE_ENABLED=true` and deploy. `/health` distinguishes `generation-paused` from `live`.

New image jobs default to 512 × 512 WebP. Set `FLUX_IMAGE_SIZE=256|512|1024` to select a bounded square preset. Dimensions are saved at admission, so queued jobs and retries retain their original size. Deploy FLUX `v1.0.2-20261020` or later before deploying this app change; older workers only accept 1024. Existing 1024 jobs remain supported. See the [compact-image release and rollback guide](../docs/flux-compact-images-2026-10-20.md).

The scheduler supports up to three jobs per model endpoint. For three-worker mode set `COORDINATOR_STAGE_CONCURRENCY=3` and `COORDINATOR_PIPELINE_SIZE=6`, with Runpod max workers 3 on each endpoint and one GPU/job per worker. The default remains 1/2; serial fallback is 1/1. This speeds up one feed batch rather than allowing multiple global batches. See [concurrency settings](../docs/model-concurrency.md).
