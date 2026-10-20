# Local app with live Runpod endpoints — 20 October 2026

This run exercises the updated local FastAPI app against the two real Runpod Queue endpoints. It is separate from the earlier Spark model tests and synthetic provider tests. Railway deployment was intentionally left alone while the operator reported blocked deployments.

## Setup

- A separate Uvicorn process listens only on loopback, port 8020, with a development HTTPS certificate for localhost.
- The test HTTP client trusts that certificate explicitly. Secure/HttpOnly cookies, CSRF protection and the real admin sign-in form remain enabled. No production cookie guard was weakened for local testing.
- The existing laptop app on port 8017 and the Railway service are unchanged.
- A new SQLite database holds only this test's admissions and aggregate usage. Production allowances were not reset or modified.
- Existing `RUNPOD_API_KEY` and the two endpoint IDs were read privately from the PulseFeed Railway service. Credentials and session jars live only in ignored private evidence files; no key is included here.
- Endpoint health and inference calls succeeded with this key. Management configuration reads returned HTTP 403, so the test did not read or change live endpoint configuration.
- `PULSEFEED_FAKE_MODE=false`, provider `runpod`, inference enabled, 512-square output, stage concurrency 3, pipeline size 6. The endpoint health responses reported pools of three workers.

## Test sequence

1. Start with the local per-page policy at one story. Fetch Hacker News through the real RSS route and generate one image as a compatibility gate for the deployed 512-capable FLUX worker.
2. After that passes, use the local admin limits API to set nine stories per page. Submit BBC England and let the coordinator generate the nine illustrations.
3. Poll existing app batch IDs, never blindly resubmit model jobs. Monitor persisted invocations to enforce at most three pending requests per model and six started stories.
4. Retrieve every completed image through its authenticated owner-scoped app route; decode and check WebP/512-square dimensions.
5. Compare the authenticated admin usage API with durable invocation counts and quota settlement. Read existing Runpod results for worker assignment evidence, without starting another inference.

The test is bounded at ten images and twenty successful model calls if both phases pass. Fixed admission keys and saved local sessions permit recovery without creating a new feed admission. A failed or uncertain submission stops the driver; reservations remain for reconciliation.

## Limits of the evidence

This is actual local HTTPS/API traffic, real RSS retrieval and real cloud inference. The driver does not automate browser IndexedDB or physical mobile layout; the nine browser behavior harnesses remain separate synthetic checks. An in-flight app request can still be queued by Runpod, so three pending jobs alone do not prove three simultaneous GPU kernels.

The API key cannot read endpoint management settings. Runtime metadata and returned model revisions can be checked, but this test cannot independently certify the deployed image digest from those permissions. Image deployment references remain in the release documents.

The local certificate and secrets are for this isolated development process, not Railway production. Its usage table includes only this test database; it must not be presented as production-wide historical usage.

## Results

Both phases passed. Every completed image was retrieved through the local app and decoded as 512 × 512 WebP.

| Check | Result |
| --- | --- |
| Hacker News gate | 1 image; 2 successful model calls |
| BBC England page | All 9 images; 18 successful model calls |
| Total calls | Qwen 10 + FLUX 10, all succeeded |
| Duplicate invocations / inference retries | 0 / 0 |
| Maximum pending requests | Qwen 3, FLUX 3 |
| Maximum started unfinished stories | 6 |
| Qwen input / output / total tokens | 4,880 / 767 / 5,647 |
| FLUX reported images / attempts | 10 / 10 |
| Local daily allowance | 10 used, 0 reserved, 90 remaining of 100 |
| Local rolling-hour starts | 10 used, 20 remaining of 30 |
| Today versus All time | Model totals match in this fresh database |
| Admin privacy check | No article, feed URL or prompt fields in aggregate reports |
| Description context | All 10 Qwen inputs had nonempty RSS excerpts |

The two batches took 175.5 seconds (one-story gate) and 344.0 seconds (nine-story BBC page). Overall admission-to-final-image elapsed time was 522.5 seconds. These are not warm-performance measurements: Runpod reported substantial repeated queue/startup delays.

Across completed jobs, reported provider delay ranged from 2.88 to 96.31 seconds for Qwen and 0.09 to 82.91 seconds for FLUX. Provider execution time was much smaller: 1.51–3.90 seconds for Qwen and 0.49–1.26 seconds for FLUX. Delay includes queueing/provisioning; it cannot be attributed solely to a GPU kernel or solely to container loading.

Read-only result checks found four distinct Qwen worker IDs and two FLUX worker IDs across the whole run, including replacements over time. This is not a claim that four Qwen workers ran simultaneously. App-level concurrency reached three for both stages, but queued provider requests are not proof of three simultaneous physical FLUX workers.

For a smoother demonstration, keep workers warm across the stage gap: consider a 120-second idle timeout, or one active worker per endpoint during the session, with max workers 3. A fully warm three-per-model demonstration would require three active workers on each endpoint and incurs continuous billing for all six. The test did not change those settings because its key lacks management access.

The server remains on loopback HTTPS port 8020 with a development certificate. Its admin secret is separate from production. Production app variables, SQLite usage, Runpod endpoint configuration and the existing laptop app process were not changed by this validation run.
