# PulseFeed AI Product Requirements Document

Status: Implementation scope for a three day MVP\
Updated: 15 October 2026\
Author: Rok Kovač

## Parallel implementation briefs

This document remains the product specification. Implementation is divided into three independently owned tracks in the [parallel execution plan](README.md): [Railway app](01-app.md), [Qwen worker](02-qwen-worker.md), and [FLUX worker](03-flux-worker.md). All tracks follow the [version 1 shared contracts](00-shared-contracts.md) and the [integration acceptance gates](04-integration-and-release.md). Keep the two model containers separate for the three day MVP.

## Objective

Build an authenticated RSS viewer with a form-only home and explicit **View RSS** action that creates an owner-scoped one-hour reader page, freezes at most ten source-order articles, and illustrates a leading source-order selection up to the configurable 1–10-story page cap, constrained by remaining attempt and start allowances. Railway/Runpod remain the cloud target; the verified local Spark/laptop profile remains available. Images stay browser-only in IndexedDB until the view expires; no S3/R2 image archive.

The application demonstrates a complete user workflow, model deployment, request orchestration, authentication, bounded resource use, and clear error handling within three days. Preserve the original RSS title, excerpt, and source link; do not generate replacement articles or summaries.

The recruiting follow-up permits any frontend and backend application with AI models on Runpod. PulseFeed uses that flexibility while retaining the original case study's working endpoint, handler, Docker, demonstration, and documentation deliverables.

## Selected architecture

| Component | Decision |
| --- | --- |
| Application hosting | One Railway service for the frontend, FastAPI backend, and bounded background job coordinator. |
| Application stack | Python 3.11, FastAPI, Jinja2, Tailwind CSS, and focused JavaScript modules for interactions and IndexedDB. |
| Visual language | Simple centered RSS URL field and View RSS action, referencing [rssviewer.app](https://rssviewer.app/), within the cozy pastel 8-bit interface: pixel headings, blocky cards, stepped borders, chunky controls, and readable body text. Light and dark themes follow the system preference initially; an accessible toggle remembers the user's choice locally. |
| Application database | SQLite on a persistent Railway volume, mounted at `/data`; proposed database path `/data/pulsefeed.db`. |
| Prompt model | `nvidia/Qwen3-8B-NVFP4`, used only to generate an image prompt, with thinking disabled. |
| Image model | `black-forest-labs/FLUX.2-klein-4B`, the distilled four-step variant. |
| Inference hosting | Two separate Runpod Serverless endpoints, one per model. |
| Target GPU | RTX PRO 4500 Blackwell with 32 GB VRAM; this means the Blackwell card, not RTX 4500 Ada or RTX A4500. |
| Image persistence | Image blobs in the current browser's IndexedDB. No durable image files in Railway, SQLite, or an object store. |
| Access | Public fresh-browser home with isolated visitor sessions by default; configured root-query admin bearer exchange into a short-lived privileged session. Optional private CLI flow only when explicitly disabled public mode has no environment admin token. |
| Resource controls | Global daily allowance of 100 FLUX attempts across all visitors and a separate 1–10-story cap per new page (default 10), admin-editable in SQLite; the global allowance is resettable; existing account limits, one active batch, and bounded worker attempts remain additive. |

Each Runpod endpoint has its own worker allocation. Hosting both models simultaneously on one GPU is outside the MVP. Verify the Qwen quantization, Blackwell kernels, and pinned serving runtime on day one; hardware compatibility alone does not establish that a particular container works. Any required precision or hardware substitution must be documented.

Railway volumes provide persistence and currently do not support service replicas. Use one application replica and one ASGI process, with a single background coordinator. The application must bind to Railway's provided `PORT` on `0.0.0.0`, expose a health endpoint, and initialize the database on the mounted volume at runtime. [Railway volumes](https://docs.railway.com/volumes/reference), [Railway networking](https://docs.railway.com/networking/troubleshooting/application-failed-to-respond)

## Three day scope

### Required MVP

- Public fresh-browser access with isolated visitor ownership and a protected configurable admin exchange; no signup or email delivery service.
- Enter an RSS URL and explicitly click View RSS to fetch the current feed and retain its first ten source-order articles for display. Create a random one-hour reader view and fresh selected runs for each fresh explicit request, subject to all admission limits; exact request-key replay reuses only that view and never extends expiry.
- Generate a visual prompt with Qwen, apply the configured content filter, and generate an image with FLUX on Runpod.
- Display the selected articles in source order, with original feed content and source links; retain at most the first ten source items in the immutable snapshot. Combine each article and its corresponding image in one card, with an AI-generated label and simple progress. Keep technical details out of the normal reader.
- Save completed images in account:view:run IndexedDB entries and restore only within the one-hour view; purge expired view images while open or before reuse on the next visit.
- Persist immutable view snapshots/order/expiry and durable request replay, jobs, prompts, settings, usage and rate-limit accounting; no saved-feed/history picker.
- Provide the Settings chip, protected settings form, model-pipeline controls, and per-image technical details in the query-enabled admin view only. The form retains the Qwen template, FLUX prompt template, restricted fallback prompt, and blocked terms.
- Provide an actual-admin-only aggregate usage overview at `/admin` and `/api/admin/usage`, plus shared daily limit editing/reset controls with used/reserved/remaining/reset-time visibility. Do not expose other visitors' feed/article/prompt/record data there.
- Implement worker error handling, bounded retries, backend rate limits, and explicit recovery actions.
- Deliver reproducible deployments and a working demonstration.

### Deferred features

Self-service account management/signup, teams, account-specific prompt settings, detailed feed/account dashboards, scheduled generation, full article scraping, summaries, social publishing, multiple image models or styles in the UI, cross-device image synchronization, object storage, Redis/Celery, and production-scale availability are outside the three day scope. Per-run telemetry and the privacy-limited all-visitor aggregate admin overview are required; richer dashboards remain deferred.

## User workflow

1. Open the public URL in a fresh browser; the server creates an isolated visitor session/owner and displays the form-only home without an access-link prerequisite.
2. The home page contains only the centered RSS URL form and **View RSS** action, with no saved-feed picker or reader history. A visitor-session-authenticated, CSRF-protected `POST /api/view-feed` with `{url, idempotency_key}` creates a random, owner-scoped `/feeds/{view_id}` reader page containing a frozen snapshot of at most the first ten source-order articles. `expires_at` is immutable `created_at + 3600 seconds`. Each fresh explicit view, including the same RSS URL with a fresh key, selects the largest affordable source-order prefix up to the configured per-page story cap (default 10, integer 1–10). Selection is atomic under the remaining global daily FLUX-attempt budget, account hourly attempt budget and global hourly start budget; each selected story reserves two FLUX attempts, including retry capacity. The story cap applies independently to each new page; a repeated URL has no separate shrinking daily allowance. Prior views do not suppress fresh runs. Never backfill or drain another batch automatically after admission; an existing view and its request-key replay retain their original selection. Busy/quota-blocked views remain readable.
3. Persist account-scoped request-key idempotency durably. An exact replay returns the same view ID, original snapshot/admission and original expiry, without extending its lifetime or creating work; conflicting key/URL reuse is rejected. Concurrent distinct keys create distinct views and may create distinct selected runs only within atomic capacity/rate/quota bounds. Page GET, reload, navigation, polling and browser-storage failures never admit inference. The logo and **Check a new feed** return to the empty home form.
4. Qwen generates the prompt; the coordinator composes/filters it, then FLUX generates the image. Show a fun reduced-motion-aware loader and simple progress.
5. Store image Blobs in IndexedDB under account:view:run keys with the immutable view expiry. Restore only within a valid view. Delete expired view images and revoke their object URLs while the app is running; if the browser was closed, purge expired entries before reading/reusing them on the next visit. Keep theme preferences and authentication intact. Files already downloaded outside the app cannot be purged by the app. Provider-result availability may end earlier; storage/retrieval failures never trigger automatic inference.
6. A reader view is accessible only to its authenticated owner while unexpired. Missing, foreign and expired view pages return the same generic funny 404 without leaking which condition applied. Reader item, image, batch/status and retry/regeneration calls must all validate unexpired view membership as well as session, ownership and CSRF where applicable. Expiry prevents further reader access/admission but does not erase durable usage/quota history or prematurely release active/uncertain reservations.
7. Actual admins may enable their own-view settings/technical controls through the supported admin query flag and follow the aggregate `/admin` link. Bare admin display flags never grant authority; public visitor sessions, configured credential exchange, role checks and CSRF govern access.

## Functional requirements

| ID | Requirement | Acceptance criterion |
| --- | --- | --- |
| FR1 | Establish isolated visitor access and protected admin authority. | Fresh public browsers receive visitor sessions automatically. APIs retain session/CSRF/owner checks; only valid configured admin exchange grants temporary session authority, revoked on token rotation/removal. |
| FR2 | Create a bounded one-hour reader. | Explicit View RSS POST freezes at most the first ten source-order articles in random `/feeds/{view_id}` with immutable created_at+3600 expiry. Invalid feeds make no model calls; home has no history picker. |
| FR3 | Bound automatic generation. | Each fresh view may admit the largest affordable source-order prefix of its first ten articles, without later backfill/draining. Atomically enforce rolling-hour starts, the daily global FLUX-attempt budget and configurable per-page story cap, account limits, reservations and capacity on all admission routes. |
| FR4 | Generate prompts. | Qwen returns one nonempty, bounded visual prompt; it does not emit article summaries, captions, or thinking content to FLUX. |
| FR5 | Apply content filtering. | The final composed image prompt is checked; a match selects the configured fallback and records matched terms. |
| FR6 | Generate illustrations. | FLUX returns one valid, size-bounded image per successful item, with seed, dimensions, and timing metadata. |
| FR7 | Preserve source content. | Selected articles remain readable in source order, within the configured 1–10-story cap per new page. Each card combines original title/excerpt/source link with its corresponding image when available and an explicit AI-generated label. |
| FR8 | Recover live-view progress. | Within TTL, refresh resumes existing runs; restart reconciles provider jobs without renewing the view. Expiry blocks reader access while durable usage/reservations remain. |
| FR9 | Restore and expire local images. | Restore account:view:run Blobs only within valid views; purge at expiry while open or before reuse after reopening. Preserve theme/auth and state downloaded files cannot be purged. |
| FR10 | Retain accurate private telemetry. | Persist per-run measurements/mode. Own-feed technical UI stays admin-only; `/admin` and `/api/admin/usage` aggregate all visitors by model/stage/mode with known subtotals/missing coverage and no other visitor's content or record IDs. |
| FR11 | Configure prompts and shared daily allowance as admin. | Actual admins can edit the four prompt fields and SQLite daily limits, reset allowance with CSRF/audit, and inspect used/reserved/remaining/reset time. Reset preserves history and active/uncertain reservations; lowering limits blocks new work only. |
| FR12 | Retry safely. | Transient failures follow the bounded retry policy; image-stage retry reuses the saved prompt and does not automatically rerun Qwen. |
| FR13 | Enforce shared and account limits without discarding readable feeds. | Shared daily limits reset at 00:00 UTC; global rolling-hour starts and account/hour/minute controls remain additive. Generation-blocked views stay readable until expiry with no model calls. |
| FR14 | Support explicit new views and safe replay. | Same-key replay returns the same ID/snapshot/expiry and no new work. Fresh keys create independent views/runs under limits; GET/reload/poll/navigation/storage failure never starts work. |

## Model inputs and outputs

Qwen receives a system template and normalized RSS title/excerpt as untrusted article data. It returns one concise visual prompt describing subject, setting, composition, and editorial style. Disable thinking, cap output at 256 tokens, and use a bounded context window appropriate to the input limit. Reject empty, malformed, or over-limit output with a typed error.

FLUX receives the composed image prompt, seed, dimensions, and a fixed generation preset. Use the distilled `FLUX.2-klein-4B` checkpoint with four inference steps and guidance consistent with its model card. The FLUX prompt template inserts the Qwen output at one movable `{{qwen_prompt}}` token. Text may appear before and after the token; it is ordinary image-prompt text, not a chat system role. Record the actual settings used. [FLUX model card](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B)

The image worker returns a compressed WebP or JPEG encoded for transport, plus metadata. FastAPI proxies the result to the authenticated browser, which decodes and stores a Blob. Enforce the encoded response-size limit before returning the worker result. Do not log or persist image base64 in application records.

## Persistence and refresh behavior

Persist account-scoped request-key idempotency durably. An exact replay returns the same view ID, original snapshot/admission and original expiry, without extending its lifetime or creating work; conflicting key/URL reuse is rejected. Concurrent distinct keys create distinct views and may create distinct selected runs only within atomic capacity/rate/quota bounds. Page GET, reload, navigation, polling and browser-storage failures never admit inference. The logo and **Check a new feed** return to the empty home form.

A reader view is accessible only to its authenticated owner while unexpired. Missing, foreign and expired view pages return the same generic funny 404 without leaking which condition applied. Reader item, image, batch/status and retry/regeneration calls must all validate unexpired view membership as well as session, ownership and CSRF where applicable. Expiry prevents further reader access/admission but does not erase durable usage/quota history or prematurely release active/uncertain reservations.

Store image Blobs in IndexedDB under account:view:run keys with the immutable view expiry. Restore only within a valid view. Delete expired view images and revoke their object URLs while the app is running; if the browser was closed, purge expired entries before reading/reusing them on the next visit. Keep theme preferences and authentication intact. Files already downloaded outside the app cannot be purged by the app. Provider-result availability may end earlier; storage/retrieval failures never trigger automatic inference.

SQLite retains view snapshots, request-key associations, runs/invocations, prompts/settings, durable usage, daily/hourly ledgers and audit/reservations across restart. Reader expiry is an access/retention boundary, not deletion of accounting or cancellation confirmation. There is no saved-feed/history UI or selected-feed preference. Theme alone remains a small local preference; authentication remains server session plus secure cookie. Never store credentials/image bytes in localStorage or persist object URLs.

Within a live view, missing local images can retrieve an existing provider result without inference while available. If neither cache nor provider has the image, offer only explicit quota-checked recovery using the saved prompt. Cache failure is not permission to generate; expiry blocks all image/retry reader calls. Provider output retention may be shorter than the reader TTL and is not an image archive.

## Job orchestration and lifecycle

**Pipeline/reveal revision: implemented and locally validated on Spark.** See the [validation record](../integration/VALIDATION.md) for test scope and evidence.

`COORDINATOR_PIPELINE_SIZE` accepts `1` or `2`, default `2`. One global batch remains active; at most two stories may be started but unfinished, with at most one Qwen invocation and one FLUX invocation in flight. Prefer the oldest FLUX-ready story before starting another Qwen stage. Set `COORDINATOR_PIPELINE_SIZE=1` to roll back to serial story processing. Existing atomic admission, two-attempt reservations, quota limits, deadlines and uncertain-job capacity holds remain unchanged. On Spark, worker concurrency remains one and the shared GPU lock serializes actual inference: requests may overlap without simultaneous Qwen/FLUX GPU execution. No speedup is assumed; compare measured end-to-end timings before making a performance claim.

Start the one-shot adaptive article reveal and spinner fade after `min(3, selected card count)` images have actually decoded in the browser. Provider completion or a fetched Blob alone does not meet that threshold. Remaining cards keep their own placeholders and fill independently without restarting the page reveal. Keep the existing error/recovery, reduced-motion and expiry behavior; browser persistence does not delay display or admit more inference.

Use SQLite-backed job records and one bounded background loop in the Railway process. An in-memory task alone is not the record of work. On startup, reconcile existing nonterminal runs using saved provider job IDs before accepting work beyond the configured capacity.

Execution states are `queued`, `creating_prompt`, `generating_image`, `completed`, and `failed`; an unresolved submission can use `submission_unknown` while it awaits reconciliation or operator action. Content-filter outcome and `used_fallback` are independent fields, so a fallback illustration can complete successfully.

Mark a run `completed` only after observing a valid successful image result. Browser saving is independent and must not hold the server run open. If an already-confirmed result expires, keep its successful generation history and show image unavailability. If the provider result expires before success can be verified, record an unknown/retrieval failure rather than inventing completion.

Persist each model invocation and provider job ID separately. Persist each account-scoped View RSS request key with its immutable reader snapshot/admission/expiry. Serialize key replay, selected view membership, batch admission, rolling-hour starts and quota reservations in SQLite. Distinct fresh keys intentionally allow distinct runs of the same articles, subject to limits; only exact replay reuses the original view. Completion never admits another automatic batch.

When the overall item deadline expires, stop advancing its pipeline, record `deadline_exceeded`, and request cancellation of known provider jobs. Confirm a terminal provider state before marking the run stopped and releasing its batch capacity. If cancellation or submission remains uncertain, retain the capacity reservation until reconciliation or explicit operator resolution; a local timeout must not imply that GPU work stopped or trigger a replacement job.

## Handler and Docker reliability

Every custom Runpod `handler.py` must validate inputs before GPU work, enforce the fixed model and allowed settings, load the model once per worker, and return a documented success or error contract. An error must never become a successful image result merely because it was returned inside a JSON object. The Railway client must inspect both provider status and the handler payload. Runpod supports failed jobs and explicit error responses; document the chosen mapping. [Runpod handler documentation](https://docs.runpod.io/serverless/workers/handler-functions)

Failures include a stable error code, stage, retryability, safe user message, and correlation/run ID. Keep detailed diagnostic logs server-side; omit credentials, access-link tokens, image payloads, and internal stack traces from the UI.

Success and error payloads must report the actual inference-attempt and retry counts, including zero attempts for pre-inference validation failures. The coordinator uses these counts to reconcile quota reservations. If a crash or timeout prevents reliable accounting, retain the full reserved allowance conservatively.

| Failure | Required handling |
| --- | --- |
| Invalid input, unsupported settings, missing credentials, model access denied | Fail promptly with an actionable error; no automatic inference retry. |
| GPU out of memory, unsupported NVFP4 kernel, corrupted model/configuration | Fail clearly; do not repeatedly rerun unchanged work. Startup incompatibility prevents worker readiness. |
| Explicitly classified recoverable dependency failure inside a custom handler | At most one automatic retry, with backoff and jitter, inside the same invocation and deadline. |
| Safe status/result reads fail with timeout, 429, or transient 5xx | Retry transport up to three times with backoff/jitter and respect `Retry-After`; resume bounded polling without submitting another inference job. |
| Submission response is lost or acceptance is uncertain | Persist `submission_unknown`; reconcile if possible. Never blindly repeat `/run` or assume a correlation ID guarantees provider deduplication. Require an explicit retry decision if unrecoverable. |
| Qwen fails | Stop that item before FLUX and expose a retry action. |
| FLUX fails | Preserve Qwen output and settings; an explicit image retry creates a linked attempt using the saved prompt. |
| Worker timeout or cancellation | Release request resources where possible, preserve failure state, and do not hide cancellation inside a retry loop. |

Automatic inference retries belong to the custom handler only. The coordinator must not multiply these by automatically resubmitting failed model jobs; user retries remain bounded by quotas. Configure any serving-runtime/provider retry behavior explicitly. Safe transport retries do not count as new inference, but any additional inference attempt does.

Docker requirements:

- Pin compatible CUDA, PyTorch, model-serving libraries, Runpod SDK, and model revisions; record the tested image tag or digest.
- Build dependencies into the image. Make model weights available through a documented baked-in or Runpod model-cache strategy; do not download dependencies on every request.
- Validate GPU/runtime compatibility, model access, and required environment variables at startup. Bound transient startup download retries to three attempts and exit with a clear diagnostic on failure.
- Define initialization and execution deadlines, readiness checks, and normal termination behavior. A broken worker must not appear healthy or spin forever in an internal restart loop.
- Limit worker concurrency to one initially; clean up per-request resources in success and failure paths.
- Include handler fixtures and a real GPU smoke test for valid input, invalid input, a simulated transient failure, and exhausted retries.

Model-weight caching is a deployment concern separate from generated-image storage. Removing S3 does not require downloading model weights for every request.

## Public visitor access and admin authorization

`PUBLIC_ACCESS_ENABLED=true` is the default: a fresh external browser opens the home form directly, without signup, CLI seeding or an access link. The server issues an isolated anonymous visitor session/cookie and owner identity. Every reader API still requires that session, CSRF for mutations, owner/unexpired-view membership, and all existing global/account limits. Public reading never grants admin access or exposes another visitor's view.

Open `/admin` (also linked from the reader footer) to sign in with the admin password stored in `ADMIN_ACCESS_TOKEN`. The password travels only in a bounded POST form, protected by an expiring signed cookie/nonce, same-origin validation and the existing IP attempt limit. Successful sign-in preserves the visitor owner and rotates into the existing expiring admin session. Failed sign-in does not echo the password or change visitor ownership. Login forms use a same-origin referrer policy for native form compatibility; private credential links and other pages retain no-referrer. Existing configured private links remain compatible.

The admin dashboard is ordered as Usage overview, Prompt settings, Limits, then separate Qwen and FLUX usage reports. Reset allowance is an action inside Usage overview with inline confirmation, not a separate collapsible section. Prompt editing lives on `/admin` in separate collapsible Qwen, FLUX and content-filter sections, each with its own save button and feedback. Qwen uses one prompt editor with an explicit `--- ARTICLE CONTEXT ---` divider: system instructions above, article layout below. Article title/excerpt tokens stay movable; Add-token buttons appear only for missing tokens. The two roles remain separate in the saved API fields and worker messages. FLUX retains the movable Qwen token. The content section saves blocked terms and the fallback prompt together. Each `POST /api/admin/settings/{section}` accepts only that section’s fields, requires admin authority and CSRF, uses the shared admin write limit, and merges atomically with current settings. Saving one section never submits or overwrites another section’s unsaved draft. Qwen/content saves preserve an existing legacy FLUX prefix; only an explicit FLUX save migrates its template. Changes affect future runs, preserving saved run settings.

Configure `ADMIN_ACCESS_PARAM=admin_key` (a safe ASCII identifier), a long random URL-safe `ADMIN_ACCESS_TOKEN` in server environment/secrets, and `ADMIN_SESSION_SECONDS=3600`. Opening the root URL `/?<configured-param>=<private-token>` validates the bearer credential, issues a fresh HttpOnly/SameSite=Lax session cookie (Secure in production), and returns `303` to clean `/admin`. If a visitor session already exists, preserve its owner and view ownership while elevating only the session; never make that visitor identity globally admin. Keep the credential out of navigation, bootstrap JSON, page content and subsequent URLs. The configured credential is reusable until rotated or removed. Rotation/removal revokes derived sessions; restoring an earlier token does not revive those revoked sessions. Bare `?admin`/`?admin=1` remains only a protected presentation toggle and cannot authenticate an admin.

The admin link is a bearer URL. Its credential can appear in browser history or ingress/proxy logs; a clean redirect cannot promise zero leakage. Application access logging is disabled and exchange responses use `Referrer-Policy: no-referrer` and `Cache-Control: no-store`; also redact the credential query parameter from upstream/edge logging and tracing before external release. Keep links out of screenshots, shared logs and documentation. Verify the deployed proxy/logging configuration rather than assuming the app controls upstream retention.

Configure variables in the Railway service's Variables settings and deploy the staged changes. Seal `ADMIN_ACCESS_TOKEN`, `SESSION_SECRET` and `RUNPOD_API_KEY`; keep endpoint IDs/configuration as appropriate non-secret variables. Set `APP_BASE_URL` to the exact public HTTPS origin, `COOKIE_SECURE=true`, and retain one replica/ASGI process/coordinator plus the `/data` volume and SQLite database. This is setup guidance, not evidence that variables or cloud resources have been provisioned. [Railway variables](https://docs.railway.com/variables)

Legacy CLI-issued private access is compatibility-only: enable it with `PUBLIC_ACCESS_ENABLED=false` and no configured `ADMIN_ACCESS_TOKEN`. It may seed evaluator/admin accounts and issue/revoke single-use links (30-minute default; legacy sessions 24 hours). It is not a prerequisite for public access. Existing session expiry, logout/revocation, owner checks and CSRF remain enforced; no public signup is introduced.

Normal reading hides model/settings/per-image technical controls. An already authorized admin may use the protected `?admin`/`?admin=1` presentation flag on their own unexpired view; it does not perform credential exchange. Actual admin authority is required by `/admin`, aggregate usage and every settings/policy write; mutations additionally require CSRF and audit. Missing/expired API sessions reject access; foreign/missing/expired reader views retain the generic funny 404. Runpod and model-access credentials remain server-side.

Public-access/admin-link validation is a new release gate: verify a fresh external browser can view/generate without CLI setup, separate visitor cookies cannot read each other's views, CSRF/global caps remain enforced, and a valid configurable root-query credential exchanges to clean `/admin` with the correct cookie flags. Test invalid credentials, bare display flags, preservation of the existing visitor owner, session expiry, token rotation/removal invalidating derived admin access, and private-mode compatibility. Inspect navigation/bootstrap/referrer/cache behavior and upstream query-log redaction without exposing real credentials. Local access validation passed: 287 backend tests (including 50 new auth cases, counted once), six additional independent temporary-database/ASGI probe groups, focused UI checks (18 admin and 13 theme/settings), two synthetic local-transport checks, and nine real local HTTP-cookie-jar checks. The live access checks used independent HTTP cookie jars; external browser/cloud validation remains pending. No new model calls occurred. Railway deployment, external fresh-browser/production HTTPS behavior, trusted proxy hops and upstream query-log redaction remain pending. See the current integration validation checkpoint for evidence.

## Rate limits and operating bounds

The following are initial application defaults. Shared daily policy lives in SQLite and is admin-editable/resettable; existing operational/account defaults remain configurable as before. Enforce fetch limits before network access and generation limits before model admission. Generation-only limits do not discard retained readable feed content.

| Control | Initial limit |
| --- | --- |
| Admin/private token exchange | 10 attempts per minute per client IP; public home session issuance never grants admin authority. |
| Explicit View RSS feed fetch | 10 requests per minute per authenticated account; page reload reads persisted state. |
| Generation or explicit retry submissions | 5 requests per minute per authenticated account. |
| Global rolling-hour generation starts | `GLOBAL_HOURLY_GENERATION_LIMIT=30` accepted per-article starts per rolling 60 minutes, outcome-independent; read-only admin usage. |
| Image inference allowance | 30 inference attempts per hour per account, including retries. |
| Shared global daily allowance | 100 FLUX inference attempts per UTC day across all visitors, including retries; admin-editable/resettable. |
| Per-page story cap | `FEED_ARTICLE_LIMIT=10` initially; admin-editable integer 1–10 stories per new page, independent of prior views of the URL. No per-URL daily allowance. |
| Daily boundary | 00:00 UTC; show next reset time and preserve held in-flight/uncertain reservations. |
| Status/result polling | 120 requests per minute per account; use one batch-status request for the UI. |
| Reader lifetime | Exactly 3,600 seconds from creation; immutable, no renewal on replay/read/reload. No saved-feed picker/history. |
| Feed display | At most first ten source-order items at parse/service/API/cached-read boundaries; preserve historical articles/runs. |
| Automatic illustration window | Up to the configured 1–10 source-order stories per fresh view, bounded by remaining global daily/hourly allowances and two reserved attempts per story; one image per selected item, no later backfill/draining. Only selected illustration cards are displayed. |
| Application concurrency | One active batch globally; `COORDINATOR_PIPELINE_SIZE=2` initially, bounded to two started unfinished stories and one invocation per stage; `1` restores serial processing. Spark GPU inference remains serialized by the shared lock. Blocked View RSS remains readable. |
| Runpod workers | Maximum one worker per endpoint initially; scale to zero when idle. |
| Feed fetch | 10 second timeout, at most 2 MiB response, at most 3 validated redirects. |
| Model input | At most 4,000 characters of normalized article text; at most 256 Qwen output tokens. |
| Image preset | 1024 × 1024, four FLUX steps; maximum 2 MiB compressed image before base64 encoding. |
| Job deadline | Initial 10 minute overall item deadline including queue/startup time; tune from day-one measurements and align provider timeouts. |

Use atomic SQLite accounting for request-key replay, global rolling-hour starts, account limits, and concurrency so restarts or simultaneous requests do not reset/bypass them. Reserve enough image quota before submission to cover the permitted handler retry, then reconcile known attempts; charge uncertain submissions conservatively until resolved. Use HTTP 429 with `Retry-After` for rejected fetch/standalone generation requests. For a successfully fetched View RSS response, generation-only rejection returns all readable items plus `blocked`, a reason, and retry guidance; it does not launch model work. Derive IPs only from trusted Railway proxy metadata. Quotas and worker caps constrain workload; they are not an exact monetary spending cap.

Read the current SQLite global daily budget and per-page story cap inside the same transaction that admits every View RSS/manual/retry/regeneration request and reserves allowance. Shared budgets count **FLUX inference attempts**, including failed/retried attempts, rather than Qwen calls, completed cards, image retrievals, or browser writes. Reserve **two attempts per new image** in global and applicable account ledgers; reconcile reliable actual zero/one/two counts once, and preserve the full reservation when outcome/count is unknown. Reusing a run consumes no new allowance.

Add `GLOBAL_HOURLY_GENERATION_LIMIT` with default **30 accepted per-article generation starts in a rolling 60-minute window across all visitors**. Count admission once, independently of eventual outcome, including Qwen failure before FLUX; no refund on failure, no extra start for same-key replay/status reads, and no multiplication by internal worker retries. Enforce it atomically in SQLite on every generation/retry admission alongside five submissions/minute/account, 30 image attempts/hour/account, the shared 100 FLUX attempts/day global budget and the per-new-page story cap (default 10). Expose the global rolling-hour usage read-only to actual admins; editing limits does not clear it. Explicit Reset allowances clears spent global rolling-hour starts and visitor hourly attempts as well as daily charges, while preserving active/uncertain reservations and all model history. This is an application limiter, with no new worker hourly limiter. Automatic midnight rollover affects only the daily budget; explicit Reset allowances also clears spent hourly budgets. Immutable admission epochs prevent late settlement from restoring pre-reset starts. Recent active/unknown starts remain counted until completion or rolling-hour expiry; daily and visitor image reservations stay held regardless of age.

Apply the current story cap independently to each fresh reader page, including repeated URLs. Existing view membership remains frozen when policy changes; never backfill or automatically admit later articles. Keep account ownership and reader authorization unchanged.

Actual-admin-only edits/resets show used/reserved/remaining/next reset time, retain internal audit, and take effect atomically for new admissions. A lower limit never cancels admitted work; display remaining allowance bounded at zero. Reset changes shared allowance accounting without deleting usage/history or releasing in-flight/uncertain reservations, and also clears spent account/hour attempts while leaving submission/minute and other request throttles intact. Preserve pending reservations at UTC rollover and settle late results once without double charging.

`POST /api/admin/limits` accepts exactly `{global_daily_limit, feed_article_limit}`: the global daily attempt limit is an integer 1–10,000 and the per-new-page story cap is an integer 1–10. `GET /api/admin/usage` reports the cap as `quotas.page: {article_limit, max_articles: 10}`; there is no per-URL daily allowance. `GLOBAL_DAILY_IMAGE_LIMIT=100` and `FEED_ARTICLE_LIMIT=10` only seed a missing SQLite policy; admin edits remain authoritative. Migration replaces the obsolete feed-daily policy without deleting historical usage, global charges or active/uncertain reservations. Changing the cap affects only new pages; it never changes an existing view’s selection.

## RSS handling and content controls

Allow HTTP(S) feeds only; block loopback, private, link-local, and metadata-service destinations, including redirects and resolved connection targets. Bound response size and parsing, disable external XML entities, and escape or sanitize feed HTML. Treat article text as data rather than instructions to the LLM. Preserve source attribution.

Freeze at most the first ten parsed source-order items into each reader view. Enforce the same cap at service/API and legacy-read boundaries without deleting durable historical usage/runs. A later explicit view fetches a new snapshot; an existing view never changes membership/order or suppresses fresh runs because an older view illustrated the same articles.

The admin form stores a Qwen prompt template, FLUX prompt template, restricted fallback prompt, and newline-separated blocked terms. The template editor shows a movable Qwen prompt token and requires exactly one `{{qwen_prompt}}` placeholder. Substitute the model output literally, without evaluating a formatting language. Limit surrounding template text to 2,000 characters and the final composed prompt to 4,000 characters. Apply normalized matching to the complete result, including text before and after the token. If blocked, substitute and validate the complete fallback prompt; reject invalid fallback configuration rather than recursively retrying it.

Record matched terms and fallback usage. Describe this feature as a configurable content filter; keyword matching does not guarantee safe generated imagery. Use a consistent editorial illustration style and label every generated image. Retain prompts and telemetry under existing account/authorization controls; expose their technical UI in the enabled admin view rather than ordinary article cards.

## Minimal data model and telemetry

| Record | Required data |
| --- | --- |
| Account/session | Account ID, role, token/session hashes, expiry, revocation, timestamps. |
| Feed/item | Owner, feed URL, item GUID/link hash, original title/excerpt/link, publication time, generation input snapshot; freeze source membership/position per reader view, separately from durable usage retention. |
| Reader view/request | Random view ID, owner, account-scoped request key, requested URL/fingerprint, frozen first-ten snapshot/source order, resulting runs/batch or blocked/idle outcome, immutable created_at and expires_at=created_at+3600. |
| Generation run | Owner, item ID, batch ID, idempotency key, lifecycle state, prompt/settings snapshot, filter outcome, seed, dimensions, error code, timestamps. |
| Model invocation | Run ID, stage, attempt, provider job ID, model/revision/precision, execution mode and provenance markers, status, timings, output expiry, measured usage/counters or missing coverage, actual/reserved quota usage. |
| Global settings | Qwen template, FLUX prompt template, fallback prompt, blocked terms, update timestamp. |
| Rate-limit accounting | Account/IP/global scope as applicable, rolling-hour accepted-start timestamps, windows, reservations and consumed attempts. |
| Admission policy/allowance | Global daily attempt limit, per-new-page story cap, UTC window/reset state, shared used/reserved allowance, linked settlement and unresolved reservations; SQLite-authoritative across visitors. |
| Admin audit | Internal actor/action/time and policy/reset change record; retain independently of allowance reset and do not expose cross-visitor record IDs in aggregate payloads. |
| IndexedDB image | Account ID plus view ID plus run ID, Blob, MIME/dimensions/display metadata, immutable view expiry and expiry-purge metadata. |

In the query-enabled admin UI, show Qwen input/output/total tokens and image count, dimensions, steps, seed, queue delay, and execution time. Store unavailable metrics as null and display them as unavailable. Normal cards retain only reading/image/progress/recovery information. Token usage is an LLM metric; do not manufacture an equivalent FLUX token count. Operational telemetry must not be labeled exact billing. Do not store a permanent image URL or a global `image_saved` flag in SQLite: availability is browser-specific.

`/admin` and `GET /api/admin/usage` provide all-visitor **aggregates only**, grouped by fixed model, stage and persisted execution mode. The admin dashboard separates **Qwen token usage** from **FLUX image usage**. A shared **Today · UTC / All time** control and explicit `live`, `synthetic` or `unknown` execution-mode selection apply to both reports. Qwen overlays input and output tokens; FLUX overlays generated images and inference attempts, without token columns or invented token equivalents.

Charts retain accessible data tables for exact bucket values; there are no draggable time-bucket controls. Bucket invocations by their recorded start time, not the time consumption or completion occurred. Today uses 24 UTC hourly buckets; mark the current bucket as partial and future buckets as unelapsed. All time covers valid recorded history in at most 120 buckets of whole UTC days, widening buckets instead of dropping older history; empty history uses one current UTC day. All-time totals retain invocations with invalid timestamps, but the chart excludes them and reports `timeline.unbucketed_invocations` with a note.

Keep actual zero distinct from missing measurements and label known subtotals with reporting coverage. Attempts include worker retries; sum each invocation’s already retry-inclusive usage once, without multiplying tokens, images or attempts. Reports aggregate across visitors without feed content or identities; execution modes remain separate and allowance resets preserve history. These are operational measurements, not exact billing. Known total tokens come from measured per-invocation totals rather than mismatched partial component sums.

**Usage dashboard revision: implemented and verified locally on 15 October 2026. See the integration validation record for automated, browser and recorded live-usage evidence.**

Persist `live`, `synthetic`, or `unknown` execution mode when admitting work. For older history use trustworthy recorded mode/markers; missing provenance stays unknown, never inferred from the current environment. Keep these modes separate in aggregates. Resets preserve all model-usage history.

Neither overview HTML nor API/export/drill-down payloads may expose another visitor's feed title/URL, article text, prompt, or account/feed/item/run/invocation ID. Model/stage/mode labels and aggregate counts are allowed. Keep audit records internal and own-feed per-article details under existing ownership checks.

## Acceptance and demonstration

The MVP is complete when the following are demonstrated against the deployed Railway app and actual Runpod endpoints:

1. Fresh public browser access works without CLI/link setup; visitors remain isolated and CSRF/limits hold. Valid configurable admin-query exchange preserves visitor ownership, sets a fresh secure session and redirects to clean /admin. Invalid/expired/rotated/removed credentials cannot authorize admin actions.
2. View RSS creates a random owner-only one-hour page containing the frozen first ten source-order articles and fresh selected runs under limits; no later backfill. Source content/links and AI labels remain intact.
3. Prompt generation, filtering, final prompts, model identity, and usage/timings remain inspectable in authorized query-enabled admin mode; normal reading hides technical controls/details.
4. A configured blocked term triggers a fallback illustration without conflating restriction with job failure.
5. Refresh within TTL restores the same view/runs and IndexedDB images without inference or expiry renewal. At expiry, page/item/image/batch/retry reader access is denied; missing/foreign/expired pages share a generic funny 404.
6. Cleared/unavailable browser storage and expired provider output produce an explicit unavailable state; IndexedDB write failure does not falsely claim a saved image.
7. Invalid worker input fails without inference; a simulated transient failure retries within the cap; permanent failure and exhausted retries surface cleanly.
8. Image-stage retry reuses the saved prompt. Duplicate submissions and uncertain submission responses do not silently create additional jobs.
9. Rejected fetch/standalone generation requests receive 429 before prohibited downstream work. Generation-busy/quota/rate-blocked View RSS responses still return the retained first ten readable articles and no new model calls; concurrent submissions cannot exceed shared/account/capacity limits.
10. Railway restart/redeployment preserves database records and reconciles known jobs. A real GPU smoke test confirms the chosen NVFP4 runtime.
11. Source, worker code, Docker/deployment configuration, environment examples, test instructions, and a recorded successful walkthrough are ready to share.
12. Exact key replay returns the original ID/snapshot/TTL without jobs. Fresh keys (including concurrent tabs and unchanged RSS) create separate views/fresh selected runs within atomic limits. Home has no history picker; logo/Check a new feed return home.
13. Verify the role/query matrix for no flag, `/?admin`, and `/?admin=1`: authenticated non-admins see the normal reader without a flag and receive 403 with either flag; admins see the normal reader without a flag and the Settings chip/settings/technical view with either flag. Fresh home access creates an ordinary visitor session, while APIs retain session/CSRF/backend-role checks; bare admin flags never grant privilege.
14. Concurrent visitors and canonical URL aliases cannot exceed the global daily FLUX-attempt allowance and per-page story cap, account limits or the default 30 rolling-hour accepted starts. Include Qwen failures and explicit retries in start accounting; exact replay/reads add none. Admin hourly usage is read-only.
15. Admin policy edits/resets enforce role and CSRF, retain audit/model-usage history, show used/reserved/remaining/reset time, and preserve active/uncertain reservations. Lowering limits stops new work only. Test reset while active, UTC midnight, restart, and late settlement without lost/double-counted allowance.
16. `/admin` and `/api/admin/usage` reject non-admins and expose only grouped cross-visitor aggregates without other visitors' content/URLs/prompts/record IDs. Verify separate Qwen/FLUX reports, UTC hourly Today and at most 120 whole-day All-time buckets, current/future labels, invalid-timestamp totals with an unbucketed chart note, retry-inclusive usage counted once, known subtotals with missing coverage, and synthetic/live/unknown historical separation. FLUX has no token columns.

Use focused automated tests for access control, quota enforcement, validation, retry decisions, and state transitions; use a browser walkthrough for IndexedDB and refresh behavior. Record observed cold and warm timings without promising unmeasured performance.

Before the public-access change, the one-hour reader implementation and independent reviews passed: 237 backend tests, 76 JavaScript behavioral checks and 20 gateway tests. Controlled synthetic browser checks passed expiry and restoration; the real Spark/laptop NASA flow passed with a 3,600-second view, ten articles, three real images, same-URL Blob restoration after refresh, and Check a new feed returning to empty home without extra jobs. Final browser layout checks also passed: loading through three finished images and hard-reload restoration of three 1024-square Blob images each recorded CLS 0 with no shifts under 900 ms script/font/API delays. Runpod/Railway deployment remains pending. See the current integration validation checkpoint for measured usage and evidence. [Current validation](../integration/VALIDATION.md).

Browser checks must exercise expiry while open and while closed, purge before reuse, account:view:run partitioning, retained theme/auth, downloaded-file limitation, reduced-motion loader, and generic 404 equality. Preserve durable usage/quota/audit history through expiry.

## Delivery plan

| Day | Work | Exit condition |
| --- | --- | --- |
| 1 | Deploy the Railway skeleton with volume and authentication; deploy and smoke-test Qwen NVFP4 and FLUX workers; verify GPU compatibility and output transport. | An authenticated application can obtain a real prompt and image from Runpod; deployment/runtime choices are fixed. |
| 2 | Implement View RSS/ten-item persistence, immutable one-hour views/request replay and shared daily/rolling-hour budgets, bounded orchestration, handler failures/retries, IndexedDB, and the main UI. | The workflow survives refresh without inference and enforces shared/account limits while keeping the retained articles readable. |
| 3 | Finish admin settings/shared allowance controls and aggregate telemetry; run budget/reset/privacy/failure/retry/storage tests, verify redeployment, polish the UI, and prepare documentation/video. | Acceptance checks pass and the evaluator has a working demonstration. |

If time becomes tight, defer dashboard aggregation and visual polish first. Preserve authentication, rate limits, worker error handling, refresh recovery, and the real Runpod demonstration.

## Submission and setup checklist

- Railway application source and Dockerfile/deployment configuration, public HTTPS URL, health check, and volume/database setup.
- Runpod handler code, Dockerfiles or reproducible base-image configuration, pinned dependencies/model revisions, endpoint settings, GPU selection, and initialization/execution limits.
- Redacted environment template covering Runpod endpoint IDs/key, database path, session configuration, and configurable limits; secrets are issued separately.
- Public visitor and Railway admin-variable setup, credential rotation/removal and upstream query-log redaction instructions; optional private CLI setup documented separately.
- Direct endpoint test script, focused test results, sample feed/input/output, and a short demonstration video including one failure/retry path.
- README explaining architecture, model choice, cost controls, browser-only image retention, and the absence of cross-device image history.
- Confirm available Runpod credits and Railway resources before provisioning. Obtain the actual recruiting contact; `XXXXX@runpod.io` in the reference is a placeholder.

## Model references

- [Qwen3-8B NVFP4](https://huggingface.co/nvidia/Qwen3-8B-NVFP4)
- [Qwen3-8B thinking control](https://huggingface.co/Qwen/Qwen3-8B)
- [FLUX.2-klein-4B](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B)
- [RTX PRO 4500 Blackwell](https://www.nvidia.com/en-us/products/workstations/professional-desktop-gpus/rtx-pro-4500/)
- [Railway FastAPI deployment](https://docs.railway.com/guides/fastapi)
- [Runpod Serverless](https://docs.runpod.io/serverless/get-started)
