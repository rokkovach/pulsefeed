# Shared Worker Contracts

Status: Version 1 implementation agreement\
Owner: Coordinating agent

The parent [PRD](PRD.md) governs product scope. This file governs wire formats and cross-track behavior. Lane briefs specialize their own work without overriding either document. The coordinator resolves conflicts and owns `contracts/**`; agents must request any change that affects another track.

## Transport and identifiers

Railway calls Runpod's native queue API: submit with `/run`, poll `/status/{provider_job_id}`, and cancel through the provider cancellation operation. Authentication is server-side. A provider job contains its own `id` plus the application payload in `input`.

Every input has these required fields:

| Field | Type and meaning |
| --- | --- |
| `schema_version` | Integer, exactly `1`. |
| `stage` | Exactly `qwen` or `flux`, matching the endpoint. |
| `run_id` | Application generation-run UUID string. |
| `invocation_id` | UUID string for this model-stage invocation. |
| `deadline_unix_ms` | Integer UTC deadline, from 1 through JavaScript's maximum safe integer (`9007199254740991`), assigned by Railway initially no later than 10 minutes after the item starts. |

Railway persists the invocation before submitting and stores the returned provider ID separately. Correlation identifiers do not create provider-side idempotency. A deliberate user retry creates a linked new run and fresh invocation ID; a safe status-read retry retains all IDs. Do not retry an uncertain submission automatically.

Reject unknown input keys, unsupported versions/stages, wrong types, or expired deadlines before inference. Each handler checks remaining time again before a retry and observes the smaller of the application deadline and its configured execution limit. Provider timeouts bound operations that cannot be interrupted safely inside the process.

## Qwen input

The common fields plus:

| Field | Constraint |
| --- | --- |
| `article` | Object containing only `title` and `excerpt`, both strings. Title is nonempty; excerpt may be empty. Combined normalized length at most 4,000 characters. |
| `system_prompt` | Nonempty string, at most 4,000 characters, from authenticated admin configuration. |
| `article_template` | Optional user-message layout. Exactly one `{{article_title}}` and `{{article_excerpt}}` token, with at most 2,000 surrounding Unicode code points (2,036 total). Unsupported controls/surrogates are rejected; newline, carriage return and tab are allowed. |
| `max_new_tokens` | Integer, exactly `256` for the MVP preset. |

Railway normalizes article text once before recording and submitting it: extract plain text, normalize Unicode to NFC, collapse each run of Unicode whitespace to one ASCII space, and trim leading/trailing whitespace. The worker validates that supplied strings are already normalized; it does not rewrite or truncate them. Measure the limit as the sum of Unicode code points in the supplied `title` and `excerpt`, with no separator added to that count. Chat-template separators count toward the tokenizer budget separately. Apply character limits elsewhere in this contract using Unicode code points as well.

The worker also enforces its tokenizer context budget including template overhead and reserved output tokens. Reject excess with `INPUT_TOO_LARGE`; do not silently truncate a prompt differently from the app's recorded snapshot. No caller-provided model, tokenizer, URL, chat role, or runtime option is accepted.

Use the checkpoint's chat template with thinking disabled. Return one plain visual prompt, nonempty and at most 2,000 characters. Reject reasoning output, unexpected structured responses, and incomplete/truncated output rather than treating them as valid image prompts.

The admin editor keeps system instructions separate from article data. The optional article layout defaults to `Article data:` followed by `{"title": {{article_title}}, "excerpt": {{article_excerpt}}` on the next line. Each token expands once to its JSON-quoted field, with `<` and `>` escaped, inside the **user** message only. Do not add quotes around the markers. Replacement is non-recursive; article text containing markers remains literal. Unknown markers are literal text. The worker appends its fixed output/data guard to the system message as before. Existing payloads without the optional field retain the legacy article JSON message exactly. Current settings receive the default layout; historical snapshots and prepared invocations are not rewritten. Deploy the updated Qwen worker before enabling custom layouts in the app; the app omits the optional field for the exact default layout for compatibility.

## FLUX input

The common fields plus:

| Field | Constraint |
| --- | --- |
| `prompt` | Nonempty final content-filtered prompt, at most 4,000 characters. |
| `seed` | Integer from `0` through `4294967295`, assigned by Railway and echoed in output. |
| `width`, `height` | Integers, both exactly `1024`. |
| `num_inference_steps` | Integer, exactly `4`. |
| `guidance_scale` | Number, exactly `1.0`. |
| `output_format` | `webp` or `jpeg`; application default `webp`. |

Railway substitutes the Qwen output at exactly one literal `{{qwen_prompt}}` token in the configured FLUX template, then filters the complete prompt including surrounding text. The admin UI permits moving the token to wrap text around the Qwen output. New settings require one token and at most 2,000 characters of surrounding text. An explicit internal format version distinguishes templates from legacy prefixes; unversioned historical snapshots always retain prefix composition, even if their text contains a literal marker. The internal settings key `flux_prefix` remains for compatibility; clients cannot set the internal version. The fallback remains a complete independently validated prompt. The FLUX worker does not add styles, choose policy fallbacks, or call Qwen. No arbitrary model/checkpoint paths, URLs, image inputs, or generation overrides are accepted.

## Handler result envelope

Both workers' core execution functions produce the following envelope. The SDK-registered handler returns `{ "result": envelope }`, so the provider response contains `output.result`. This wrapper preserves the required `error` field: the pinned Runpod SDK consumes a top-level handler `error` as a provider error. Require exactly one `result` key in the wrapper and validate the nested envelope; never accept an unwrapped result as a fallback. Required envelope fields:

| Field | Contract |
| --- | --- |
| `schema_version`, `stage`, `run_id`, `invocation_id` | Echo the accepted version and correlation fields. For malformed input, return version 1 and the endpoint stage; invalid/missing IDs are null. |
| `ok` | Boolean indicating worker-level success. |
| `model` | Object with `id`, `revision`, `precision`, and `runtime` strings; actual loaded values. Revision/precision/runtime may be null before successful startup, never invented. |
| `data` | Stage-specific success object, or null on error. |
| `usage` | Stage-specific measured metrics, using null where unavailable. |
| `timings` | `{ "inference_ms": number-or-null }`, nonnegative when known. |
| `counters` | `inference_attempts`, `inference_retries`, `dependency_retries`, each a nonnegative integer or null if genuinely unknown. |
| `error` | Null on success; otherwise `{ "code": string, "message": string, "retryable": boolean }`. No secrets or raw stack traces. |

Qwen success `data` is `{ "prompt": "..." }`; `usage` contains `input_tokens`, `output_tokens`, and `total_tokens`, each an integer or null. A known total must equal known input plus output. Count actual model tokens, including chat-template input; do not estimate from characters.

Qwen token usage aggregates all initiated inference attempts in the invocation, including failed attempts. For each component, return null if any attempt's measurement is unavailable; do not report a partial sum as a complete total. `total_tokens` is null unless both aggregate input and output counts are known. `timings.inference_ms` likewise sums measured inference durations across all attempts, excluding startup, provider queue time, retry backoff, and image encoding; return null when any attempt's duration is unknown. With no inference attempts, token counts and inference duration are zero. Attempt counters remain independent of whether usage measurements are available.

FLUX success `data` contains `image_base64`, `mime_type`, `width`, `height`, `seed`, and `image_bytes`. `usage` is `{ "image_count": 1 }` on success and zero when no valid image was produced. FLUX has no token metrics. Image metadata must match the actual bytes and requested preset.

The model IDs are fixed to `nvidia/Qwen3-8B-NVFP4` and `black-forest-labs/FLUX.2-klein-4B`. Pin and report actual revisions/runtime versions. Synthetic contract fixtures must be marked as such and never used as live-model evidence.

## Image bounds

- One decodable WebP or JPEG, at most 2 MiB (`2,097,152` bytes) before encoding.
- `mime_type` is `image/webp` or `image/jpeg`, matching `output_format` and decoded bytes.
- Base64 is raw RFC 4648 data without a `data:` prefix; maximum encoded length is `2,796,204` characters.
- The complete UTF-8 serialized worker output, including the `result` wrapper, must be at most 4 MiB. Both worker and application validate bounds before returning/decoding the payload.
- FLUX may use a finite compression-quality sequence on the same generated image. This does not rerun inference or change image dimensions. Exhaustion returns `OUTPUT_TOO_LARGE`.
- Railway relays authorized image data transiently; it does not persist bytes/base64. The browser stores a Blob, keyed by account, reader view ID and run ID, with the immutable view expiry; expired entries are purged while running or before reuse on the next visit. Do not store temporary object URLs as image content.

## Errors and attempt accounting

Use these stable codes: `INVALID_INPUT`, `INPUT_TOO_LARGE`, `UNSUPPORTED_SCHEMA`, `MODEL_NOT_READY`, `MODEL_ACCESS_DENIED`, `UNSUPPORTED_RUNTIME`, `DEPENDENCY_UNAVAILABLE`, `GPU_OOM`, `INFERENCE_FAILED`, `INFERENCE_OUTCOME_UNKNOWN`, `INVALID_MODEL_OUTPUT`, `OUTPUT_TOO_LARGE`, `DEADLINE_EXCEEDED`, and `CANCELLED`.

An initiated inference call increments `inference_attempts` before dispatch, even if it fails. `inference_retries` counts only additional inference calls within that invocation, so it equals `max(inference_attempts - 1, 0)` when known. `dependency_retries` counts safe pre-inference dependency retries separately. Validation failure reports zero inference attempts. Compression passes and status reads are not inference attempts.

At most one classified transient retry is permitted per handler invocation, and never more than two inference attempts. Retry only when the preceding operation is known not to remain in progress. Qwen must not replay a local serving-engine request with uncertain acceptance. Invalid input, OOM, unsupported kernels, permanent configuration errors, cancellation, invalid output, and expired deadlines do not trigger automatic inference retries.

The coordinator never automatically resubmits a failed inference job. `error.retryable` describes eligibility for an explicit retry after any necessary correction; it is not an instruction to loop. The app reserves the maximum possible image attempts before submission and reconciles reported counters. Missing output after a crash leaves the reserved allowance charged conservatively.

Startup retries are separate: at most three attempts for transient weight-download/dependency availability failures. Failed initialization prevents worker readiness. Report startup evidence in deployment logs rather than pretending it was a successful invocation.

## Provider status and recovery

- Provider `IN_QUEUE` and `IN_PROGRESS` remain pending.
- Provider `COMPLETED` is a successful stage only with the exact `output.result` wrapper, a valid matching envelope, `ok: true`, and valid stage data.
- Provider `COMPLETED` with `ok: false` is a worker failure. Malformed output is an integration failure. Never send either to the next stage.
- Provider failure, cancellation, or timeout is terminal failure regardless of any partial data. Keep raw diagnostic details server-side.
- Provider `delayTime`/`executionTime` are milliseconds recorded separately from worker `inference_ms`. Missing metrics stay null.
- Safe reads may use three transport retries with backoff/jitter and `Retry-After`; they never create a new inference job.
- An overall deadline triggers cancellation of known jobs and reconciliation. Hold capacity while execution is uncertain; release it only after terminal confirmation or explicit operator resolution.

Backend completion means observed successful generation, not browser persistence. Browser availability is independently saved locally, retrievable, or unavailable. Provider result retention is temporary; use an explicit provider expiry if supplied, otherwise label any derived expiry as an estimate and handle actual retrieval failure. Do not treat first observation of completion as the exact provider completion timestamp.

## Reader view authorization and application admission

**Pipeline/reveal revision: implemented and locally validated on Spark.** See the [validation record](../integration/VALIDATION.md) for test scope and evidence.

`COORDINATOR_PIPELINE_SIZE` accepts `1` or `2`, default `2`. One global batch remains active; at most two stories may be started but unfinished, with at most one Qwen invocation and one FLUX invocation in flight. Prefer the oldest FLUX-ready story before starting another Qwen stage. Set `COORDINATOR_PIPELINE_SIZE=1` to roll back to serial story processing. Existing atomic admission, two-attempt reservations, quota limits, deadlines and uncertain-job capacity holds remain unchanged. On Spark, worker concurrency remains one and the shared GPU lock serializes actual inference: requests may overlap without simultaneous Qwen/FLUX GPU execution. No speedup is assumed; compare measured end-to-end timings before making a performance claim.

Start the one-shot adaptive article reveal and spinner fade after `min(3, selected card count)` images have actually decoded in the browser. Provider completion or a fetched Blob alone does not meet that threshold. Remaining cards keep their own placeholders and fill independently without restarting the page reveal. Keep the existing error/recovery, reduced-motion and expiry behavior; browser persistence does not delay display or admit more inference.

The home page contains only the centered RSS URL form and **View RSS** action, with no saved-feed picker or reader history. Clicking View RSS keeps the home form visible while the server fetches and validates the RSS/Atom response. The reserved inline status shows “Checking feed…” and the URL field is locked during the request. Only an accepted response with a validated reader URL and a durably saved result navigates directly to `/feeds/{view_id}`. Invalid, unsafe, oversized or empty feeds remain on the home form with the entered URL and a bounded error beneath the input. A ten-minute, account-scoped sessionStorage record holds the URL, idempotency key, creation time and submission state, plus a validated result or allowlisted rejection code; it contains no credentials. The posting state is verified before the authenticated POST. Reload never automatically submits or opens an old result: an uncertain request requires an explicit same-key retry, and a completed request can be opened explicitly without another POST. Verified reader arrival consumes its matching completed record so Load a new feed returns to a blank form. Correcting a definitively rejected URL creates a fresh request key. The error area reserves its space so messages do not move the form or footer. Blocked browser storage fails before submission. `/feeds/new` remains a read-only compatibility shell for requests already in progress; new form submissions do not navigate through it. A visitor-session-authenticated, CSRF-protected `POST /api/view-feed` with `{url, idempotency_key}` creates a random, owner-scoped `/feeds/{view_id}` reader page containing a frozen snapshot of at most the first ten source-order articles. `expires_at` is immutable `created_at + 3600 seconds`. Each fresh explicit view, including the same RSS URL with a fresh key, selects the largest affordable source-order prefix up to the configured per-page story cap (default 10, integer 1–10). Selection is atomic under the remaining global daily FLUX-attempt budget, account hourly attempt budget and global hourly start budget; each selected story reserves two FLUX attempts, including retry capacity. The story cap applies independently to each new page; a repeated URL has no separate shrinking daily allowance. Prior views do not suppress fresh runs. Never backfill or drain another batch automatically after admission; an existing view and its request-key replay retain their original selection. Busy/quota-blocked views show a clear no-illustrations notice and a link to the source website.

Persist account-scoped request-key idempotency durably. An exact replay returns the same view ID, original snapshot/admission and original expiry, without extending its lifetime or creating work; conflicting key/URL reuse is rejected. Concurrent distinct keys create distinct views and may create distinct selected runs only within atomic capacity/rate/quota bounds. Page GET, reload, navigation, polling and browser-storage failures never admit inference. The logo and **Load a new feed** return to the empty home form.

A reader view is accessible only to its authenticated owner while unexpired. Missing, foreign and expired view pages return the same generic funny 404 without leaking which condition applied. Reader item, image, batch/status and retry/regeneration calls must all validate unexpired view membership as well as session, ownership and CSRF where applicable. Expiry prevents further reader access/admission but does not erase durable usage/quota history or prematurely release active/uncertain reservations.

Store image Blobs in IndexedDB under account:view:run keys with the immutable view expiry. Restore only within a valid view. Delete expired view images and revoke their object URLs while the app is running; if the browser was closed, purge expired entries before reading/reusing them on the next visit. Keep theme preferences and authentication intact. The reader has no image-download action. Provider-result availability may end earlier; storage/retrieval failures never trigger automatic inference.

Add `GLOBAL_HOURLY_GENERATION_LIMIT` with default **30 accepted per-article generation starts in a rolling 60-minute window across all visitors**. Count admission once, independently of eventual outcome, including Qwen failure before FLUX; no refund on failure, no extra start for same-key replay/status reads, and no multiplication by internal worker retries. Enforce it atomically in SQLite on every generation/retry admission alongside five submissions/minute/account, 30 image attempts/hour/account, the shared 100 FLUX attempts/day global budget and the per-new-page story cap (default 10). Expose the global rolling-hour usage read-only to actual admins; editing limits does not clear it. Explicit Reset allowances clears spent global rolling-hour starts and visitor hourly attempts as well as daily charges, while preserving active/uncertain reservations and all model history. This is an application limiter, with no new worker hourly limiter.

`POST /api/admin/limits` accepts exactly `{global_daily_limit, feed_article_limit}`: the global daily attempt limit is an integer 1–10,000 and the per-new-page story cap is an integer 1–10. `GET /api/admin/usage` reports the cap as `quotas.page: {article_limit, max_articles: 10}`; there is no per-URL daily allowance. `GLOBAL_DAILY_IMAGE_LIMIT=100` and `FEED_ARTICLE_LIMIT=10` only seed a missing SQLite policy; admin edits remain authoritative. Migration replaces the obsolete feed-daily policy without deleting historical usage, global charges or active/uncertain reservations. Changing the cap affects only new pages; it never changes an existing view’s selection.

These are app-side contracts; existing schema-version-1 worker inputs/envelopes, fixed models, token-authenticated loopback/SSH local containers, concurrency and deadlines remain unchanged. Worker endpoints receive no reader authority from a view ID and acquire no new hourly limiter. Usage/quota/audit records remain durable and aggregate-only admin reporting remains private.

Before the public-access change, the one-hour reader implementation and independent reviews passed: 237 backend tests, 76 JavaScript behavioral checks and 20 gateway tests. Controlled synthetic browser checks passed expiry and restoration; the real Spark/laptop NASA flow passed with a 3,600-second view, ten articles, three real images, same-URL Blob restoration after refresh, and Check a new feed returning to empty home without extra jobs. Final browser layout checks also passed: loading through three finished images and hard-reload restoration of three 1024-square Blob images each recorded CLS 0 with no shifts under 900 ms script/font/API delays. Runpod/Railway deployment remains pending. See the current integration validation checkpoint for measured usage and evidence. [Current validation](../integration/VALIDATION.md).

## Visitor and admin session boundary

`PUBLIC_ACCESS_ENABLED=true` is the default: a fresh external browser opens the home form directly, without signup, CLI seeding or an access link. The server issues an isolated anonymous visitor session/cookie and owner identity. Every reader API still requires that session, CSRF for mutations, owner/unexpired-view membership, and all existing global/account limits. Public reading never grants admin access or exposes another visitor's view.

Open `/admin` (also linked from the reader footer) to sign in with the admin password stored in `ADMIN_ACCESS_TOKEN`. The password travels only in a bounded POST form, protected by an expiring signed cookie/nonce, same-origin validation and the existing IP attempt limit. Successful sign-in preserves the visitor owner and rotates into the existing expiring admin session. Failed sign-in does not echo the password or change visitor ownership. Login forms use a same-origin referrer policy for native form compatibility; private credential links and other pages retain no-referrer. Existing configured private links remain compatible.

The admin dashboard is ordered as Usage overview, Prompt settings, Limits, then Usage by model. Reset allowance is an action inside Usage overview with inline confirmation, not a separate collapsible section. Prompt editing lives on `/admin` in separate collapsible Qwen, FLUX and content-filter sections, each with its own save button and feedback. Qwen uses one prompt editor with an explicit `--- ARTICLE CONTEXT ---` divider: system instructions above, article layout below. Article title/excerpt tokens stay movable; Add-token buttons appear only for missing tokens. The two roles remain separate in the saved API fields and worker messages. FLUX retains the movable Qwen token. The content section saves blocked terms and the fallback prompt together. Each `POST /api/admin/settings/{section}` accepts only that section’s fields, requires admin authority and CSRF, uses the shared admin write limit, and merges atomically with current settings. Saving one section never submits or overwrites another section’s unsaved draft. Qwen/content saves preserve an existing legacy FLUX prefix; only an explicit FLUX save migrates its template. Changes affect future runs, preserving saved run settings.

Configure `ADMIN_ACCESS_PARAM=admin_key` (a safe ASCII identifier), a long random URL-safe `ADMIN_ACCESS_TOKEN` in server environment/secrets, and `ADMIN_SESSION_SECONDS=3600`. Opening the root URL `/?<configured-param>=<private-token>` validates the bearer credential, issues a fresh HttpOnly/SameSite=Lax session cookie (Secure in production), and returns `303` to clean `/admin`. If a visitor session already exists, preserve its owner and view ownership while elevating only the session; never make that visitor identity globally admin. Keep the credential out of navigation, bootstrap JSON, page content and subsequent URLs. The configured credential is reusable until rotated or removed. Rotation/removal revokes derived sessions; restoring an earlier token does not revive those revoked sessions. Bare `?admin`/`?admin=1` remains only a protected presentation toggle and cannot authenticate an admin.

The admin link is a bearer URL. Its credential can appear in browser history or ingress/proxy logs; a clean redirect cannot promise zero leakage. Application access logging is disabled and exchange responses use `Referrer-Policy: no-referrer` and `Cache-Control: no-store`; also redact the credential query parameter from upstream/edge logging and tracing before external release. Keep links out of screenshots, shared logs and documentation. Verify the deployed proxy/logging configuration rather than assuming the app controls upstream retention.

Legacy CLI-issued private access is compatibility-only: enable it with `PUBLIC_ACCESS_ENABLED=false` and no configured `ADMIN_ACCESS_TOKEN`. It may seed evaluator/admin accounts and issue/revoke single-use links (30-minute default; legacy sessions 24 hours). It is not a prerequisite for public access. Existing session expiry, logout/revocation, owner checks and CSRF remain enforced; no public signup is introduced.

The visitor/admin access change does not alter worker input/envelope schemas, model pins, local bearer-token gateway transport or provider credentials. Browser query credentials never enter inference inputs or usage metadata. Public-access/admin-link validation is a new release gate: verify a fresh external browser can view/generate without CLI setup, separate visitor cookies cannot read each other's views, CSRF/global caps remain enforced, and a valid configurable root-query credential exchanges to clean `/admin` with the correct cookie flags. Test invalid credentials, bare display flags, preservation of the existing visitor owner, session expiry, token rotation/removal invalidating derived admin access, and private-mode compatibility. Inspect navigation/bootstrap/referrer/cache behavior and upstream query-log redaction without exposing real credentials. Local access validation passed: 287 backend tests (including 50 new auth cases, counted once), six additional independent temporary-database/ASGI probe groups, focused UI checks (18 admin and 13 theme/settings), two synthetic local-transport checks, and nine real local HTTP-cookie-jar checks. The live access checks used independent HTTP cookie jars; external browser/cloud validation remains pending. No new model calls occurred. Railway deployment, external fresh-browser/production HTTPS behavior, trusted proxy hops and upstream query-log redaction remain pending. See the current integration validation checkpoint for evidence.

## Configuration and fixtures

Canonical app configuration names: `RUNPOD_API_KEY`, `RUNPOD_QWEN_ENDPOINT_ID`, `RUNPOD_FLUX_ENDPOINT_ID`, `DATABASE_PATH`, `APP_BASE_URL`, `SESSION_SECRET`, `GLOBAL_DAILY_IMAGE_LIMIT` (seed default 100), `FEED_ARTICLE_LIMIT` (seed default 10, range 1–10), `GLOBAL_HOURLY_GENERATION_LIMIT` (default 30), `COORDINATOR_PIPELINE_SIZE` (1 or 2, default 2; serial rollback 1), `PUBLIC_ACCESS_ENABLED` (default true), `ADMIN_ACCESS_PARAM` (default admin_key), `ADMIN_ACCESS_TOKEN` (private server credential) and `ADMIN_SESSION_SECONDS` (default 3600). The app brief owns detailed non-secret limit/session options. Workers use `MODEL_REVISION` for the pinned revision and `HF_TOKEN` only if model access requires it; model IDs are fixed in worker configuration/code.

During implementation the coordinator creates `contracts/` JSON Schemas and fixtures for Qwen success, FLUX success with a highly compressible valid 1024 × 1024 WebP or JPEG test image, validation failure, worker failure, malformed output, provider failure, and missing result. Deliberately invalid fixtures, including wrong image dimensions, are labeled and used only for rejection tests. Neither worker nor app agents edit these shared files independently.
