# Operations and recovery

PulseFeed deliberately trades high availability for a small auditable deployment: one app process and SQLite volume, with one worker per model endpoint. Treat inference, accounting and provider-result lifetime together when restarting. A green `/health` alone does not establish safe maintenance or healthy GPU inference.

## Observe without admitting work

Use the app's `/health` for process liveness; its status/mode response does not establish database or provider readiness. Use `/admin` for aggregate usage, reservations and model outcomes. The reports separate real, synthetic and unknown provenance and distinguish missing measurements from zero. Compare known starts, attempts and reservation state; FLUX has images/attempts, not a fabricated token equivalent.

On Spark, authenticated gateway `/health` reports aggregate `active_jobs`, `retained_jobs` and `result_capacity` without identifiers or payloads. `./local/control.sh status` reports containers. A full result store rejects new jobs rather than evicting unexpired output. Default capacity is 128 per worker (range 1–256); configure it before startup, accounting for bounded output plus Python/transient memory.

Do not print private environment files, expanded Compose configuration, full job payloads, credential URLs or base64 images into shared logs. Model caches and image results are separate: clearing weights cannot fix a lost image, and restarting a local gateway discards its in-memory results.

## Drain before changing a running deployment

**There is no built-in maintenance/admission switch or automated drain command.** An idle observation races with the next visitor POST. `PUBLIC_ACCESS_ENABLED=false` prevents public bootstrap behavior; it does not revoke every existing session or provide a write barrier. Lowering limits also does not guarantee zero admission.

For planned app/worker maintenance:

1. Establish an external ingress barrier that rejects new generation writes, including `POST /api/view-feed` and `POST /api/runs/{run_id}/retry`, while allowing the running coordinator and read/status/image requests to continue. Apply it to all reachable ingress paths; removing a navigation button is insufficient. If no such barrier exists, restrict all ingress externally and accept reader downtime while the coordinator drains.
2. Under that barrier, confirm no active batches or nonterminal runs remain, no unresolved provider IDs/acceptance gaps remain, and both endpoint queues have no accepted work. Repeat the observation while the barrier stays active. Reservations must be explained; do not zero them to make the dashboard look idle.
3. On Spark, let clients retrieve results before replacing a gateway: a restart destroys unexpired provider results even after all jobs have finished. Browser Blobs survive only in browsers that saved them. Plan the loss or wait out the view/result lifetime where required.
4. Take a consistent private SQLite backup and record commit, app/worker image digests, endpoint IDs, model pins, saved policy/settings and aggregate accounting. Keep the ingress barrier active during deployment and rollback checks.
5. Replace one intended component, wait for actual model readiness, and verify app configuration, old known job retrieval where applicable, liveness and read-only accounting. Perform an explicitly budgeted smoke only when the endpoints and app are ready. Reopen writes after validation.

Runpod documents rolling replacement and a scale-to-zero procedure for strict version consistency, but an endpoint update is not an application admission barrier. Preserve known job IDs and inspect queue/workers while draining. [Runpod rolling releases](https://docs.runpod.io/serverless/endpoints/rolling-releases)

If termination is forced, record interruption as uncertainty. A cancellation acknowledgement does not prove the native process stopped. Do not start replacement work against an unresolved prior execution merely because an HTTP request returned.

## Failure guide

| Symptom | Meaning / next step |
| --- | --- |
| App cannot start after endpoint/provider edit | SQLite provider-profile guard rejected changed routing. Restore the original profile; do not alter the fingerprint manually. |
| Run is `submission_unknown` | Provider acceptance/execution cannot be proved. New dispatch freezes; inspect the original endpoint and private invocation metadata. Do not repeat `/run`. |
| Provider says `COMPLETED` but story failed | Inspect structured `output.result.ok` and its stable error code. Successful delivery is not successful inference. |
| Status read is unavailable | Bounded GET retries may recover; no new submission is warranted. Preserve the known provider ID. |
| Provider result is missing/expired | A terminal image may be unretrievable. Use an existing browser Blob if present; retrieval failure alone must not generate another image. |
| Deadline or cancellation | Poll for confirmed terminal status. Hold unknown reservations until the original execution is reconciled. |
| Qwen engine/PID fails | Readiness is removed, the child process is killed, and the invalid worker must be replaced. No inference replay. |
| FLUX native execution is unknown | SDK process exits before publishing a terminal result, preventing more work on an uncertain CUDA context. Inspect and replace the worker deliberately. |
| FLUX known fatal model/runtime failure | Readiness latches false and the SDK requests worker refresh after delivering the known result. Ordinary invalid input does not poison the worker. |
| Spark result capacity reached | Wait for one-hour expiry or plan a coordinated restart with acknowledged result loss; increasing capacity requires container recreation. Do not resubmit uncertain jobs. |
| Empty or sparse illustrations | Check current policy, active batch, hourly starts, per-visitor attempts and two-attempt reservations. A blocked view remains readable. |

There is no general automatic unknown-outcome reconciliation command. Operator resolution requires evidence from the original worker/provider that execution is terminal or never accepted, plus an accounting review. Do not paste ad-hoc SQL from an old incident into a new database. Preserve model usage, run history, audit events, request-key tombstones and conservative charges whenever certainty cannot be established.

## Backups and restore

The app uses SQLite WAL with explicit transactions. Back up using SQLite's online backup API or a platform-consistent snapshot; copying only the main `.db` file during writes can miss WAL data. Store backups privately because the database contains article context, prompts and session/account records. Test restoration on an isolated copy using the same provider profile, with inference dispatch disabled by the test harness and no public ingress.

Take a snapshot before code that changes the schema. A code rollback does not reverse an automatic schema migration. Verify that the previous release can read the current schema; otherwise restore a compatible snapshot under a write barrier, after accounting for every job admitted since that snapshot. Restoring an older database can lose charged usage and provider IDs, so it is not a safe generic cure for uncertainty.

## Provider and secret changes

- Keep `SESSION_SECRET` stable for session continuity. Rotating it invalidates existing session/token lookup; plan reauthentication and retain the database.
- Rotate `ADMIN_ACCESS_TOKEN` through private service variables. Derived admin sessions are revoked when the configured credential changes or is removed. Restoring an old token does not revive its revoked sessions.
- A Runpod API-key or local bearer-token rotation does not change provider identity. Rotate the app and gateways coherently; verify reads before reopening admissions.
- Changing endpoint IDs, transport mode or local worker origins changes the provider fingerprint. Finish/reconcile old work on the original configuration first. Provision a separate database for a new profile; the app has no automatic job-routing migration between profiles.
- If switching providers while retaining history is required, design an explicit migration that preserves original per-invocation routing and accounting. Do not bypass the current guard or replace the database with an empty file to hide history.

## Rollback

Keep the last known good app commit/image, each immutable worker image digest and a compatible database backup. Under the drain/write barrier, restore the intended code/image on the **same endpoint IDs**, wait for readiness and validate the existing database. Endpoint image rollback does not require inventing new endpoint IDs. Keep model/runtime/contract compatibility across the transition.

For Spark, `./local/control.sh stop` preserves cache/coordination volumes but loses in-memory jobs/results. Do not use `down -v` during routine rollback. Recreate workers from the known good native images, check both are ready, then start/reconnect the laptop app with the original provider profile. [Local guide](../local/README.md)

## Known operational limits

Anonymous session identity can be replaced by a new visitor; global caps remain the shared cost guard. One replica is a single point of failure. Runpod async output retention is shorter than reader expiry, and Spark restart loses all retained outputs. Native GPU cancellation, unknown acceptance and target-GPU behavior need controlled live evidence. There is no production drain switch, permanent image archive, automated multi-region failover or established cloud performance/cost baseline.
