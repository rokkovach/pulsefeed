# Reviewer guide

Start with the [six-page report](../output/pdf/pulsefeed-review.pdf). It shows the architecture, the actual reader and admin screens, the two containers, measured results and two next steps. An [editable Word copy](../output/docx/pulsefeed-review.docx) and [PNG assets](showcase/assets/README.md) are included.

## Try the app

The web app is at [pulsefeed-web-production.up.railway.app](https://pulsefeed-web-production.up.railway.app). The complete Railway-to-Runpod flow passed with nine NYTimes images and matching usage counters. [Production validation](production-validation-2026-10-20.md) records the checks; [release status](release-status.md) lists the current images.

1. Paste a public RSS URL and select **View RSS**. Hacker News (`https://news.ycombinator.com/rss`) is a convenient starting point.
2. Wait for the first illustrations. Provider cold starts can take a minute or more; GPU execution itself was only a few seconds in the controlled run.
3. Read the cards and follow **Read original story**. Images are AI illustrations, not documentary photographs.
4. Refresh the page. Existing jobs and browser images are reused; a refresh does not admit new inference. The visitor-scoped page expires after one hour.
5. Open [the admin dashboard](https://pulsefeed-web-production.up.railway.app/admin) with the password shared separately. Inspect hourly/daily allowance, expand prompt settings and compare the Qwen and FLUX usage reports. Saving settings or resetting allowance changes the shared demo, so leave them unchanged for a read-only review.

## Read the implementation

- [Architecture](architecture.md) and [code walkthrough](code-walkthrough.md) explain the request flow and tradeoffs.
- [`app/pulsefeed/service.py`](../app/pulsefeed/service.py) handles admission, ownership and quota reservations; [`coordinator.py`](../app/pulsefeed/coordinator.py) persists submission intent and advances saved jobs.
- [Qwen](../workers/qwen/README.md) and [FLUX](../workers/flux/README.md) have independent images, locks, runtime checks and smoke commands. [Contracts](../contracts/README.md) define the messages between app and workers.
- [Deployment](deployment.md), [operations](operations.md) and [release status](release-status.md) cover secrets, storage, draining, rollback and the remaining gates.
- [Validation](local-runpod-validation-2026-10-20.md) separates real Runpod results from synthetic CPU/browser checks. [Test commands](../integration/README.md) are reproducible without provider credentials.

This is deliberately one app process with SQLite and a bounded active feed batch. I would next reduce wait time with warm workers and improve image availability with expiring private object storage. It does not claim production-scale multi-tenant scheduling or complete upstream vulnerability clearance.
