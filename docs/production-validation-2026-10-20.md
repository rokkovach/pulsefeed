# Railway production validation

Checkpoint: 20 October 2026 UTC (21 October in Vienna). I tested the public Railway app against both real Runpod queue endpoints after the Qwen CA-bundle rollout.

## Deployment

The operator confirmed the latest Qwen v1.0.6 workers were ready. A fresh request completed on a worker outside the previously recorded outdated set: one attempt, zero retries, 110 input tokens and 67 output tokens. The application key cannot read endpoint release settings, so image selection is operator-confirmed rather than independently verified through the management API.

I enabled `INFERENCE_ENABLED=true` and deployed the app-only Dockerfile bundle from commit `0ff4add`. Railway deployment `bbc9d1a0-63d4-4a4d-8eb0-6f1ed5ed2ac9` reached `SUCCESS`; `/health` returned live mode. Visitor identity, admin access and the saved nine-story policy survived deployment. Models, GPU profiles and worker images remain separate.

## Full feed test

The [NYTimes HomePage feed](https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml) admitted nine stories, respecting the saved page limit. All nine supplied descriptions in the saved article context. Every story completed one Qwen call and one FLUX call, with one inference attempt per call and no retries or duplicate invocation IDs.

| Measurement | Result |
| --- | --- |
| Retrieved and decoded images | 9 WebP images, each 512 × 512 |
| Successful model calls | 18: 9 Qwen and 9 FLUX |
| Qwen input / output / total tokens | 4,528 / 618 / 5,146 |
| FLUX images / inference attempts | 9 / 9 |
| Daily image allowance | 0 → 9 used of 100 |
| Rolling hourly story starts | 0 → 9 used of 30 |
| Outstanding daily reservations | 0 |
| Saved per-page policy | 9, unchanged |
| Full verification elapsed | 308.8 seconds, including polling and follow-up checks |

Aggregate admin totals matched the completed invocations. The usage response contained no article titles, feed URLs or prompts. An unauthenticated visitor received HTTP 403 from the admin usage API. Prompt settings were unchanged.

Refreshing the reader, replaying the exact submission key and retrieving an image again returned the existing page, batch and identical image bytes. Model totals and quota usage did not increase. No allowance reset was performed.

## Provider timing

| Model | Queue delay | Provider execution |
| --- | --- | --- |
| Qwen | 0.135 to 69.648 s | 1.535 to 2.281 s |
| FLUX | 0.128 to 72.909 s | 0.511 to 1.259 s |

These ranges describe nine calls per model in one run, not latency percentiles or a warm-worker benchmark. Provider waiting remained much longer than inference.

This pass exercised the real HTTPS application API, image decoding, sessions, admission, refresh reuse and admin accounting. It did not repeat earlier desktop/mobile animation checks. Report screenshots remain labeled as local captures. Private cookies, credentials, job IDs, visitor pages and raw evidence stay in ignored operator artifacts.

The two next improvements are wait time and image availability. Current image references and retained dependency findings are in [release status](release-status.md).
