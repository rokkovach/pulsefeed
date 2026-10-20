# Release evaluation — 16 October 2026

**October 20 follow-up:** see the [project/container audit](../docs/audit-2026-10-20.md) for additional cleanup, deadline/recovery fixes, worker lifecycle and model-cache hardening, current dependency scans and test evidence. The published images described below remain unchanged and do not include those source changes. Rebuild new versions before deployment.

Three GPT-6.1-Sol review rounds covered functionality, security boundaries, worker lifecycle, browser behavior and deployment documentation. Implementation and independent review were assigned separately. This record describes the local source and measured checks; it is not evidence of a Railway/Runpod deployment or a security certification.

## Review rounds

| Round | Scope | Outcome |
| --- | --- | --- |
| 1 — inspect and implement | RSS descriptions and resource bounds; visitor storage/admission; worker fatal outcomes; UI cleanup and explanatory comments | Added bounded, cancellable RSS subprocesses; deterministic description fallback; atomic live-view limits and snapshot compaction; FLUX invalidation/recycle handling; compact optional excerpts and removal of proven dead code. |
| 2 — independent regression review | Separate backend and worker/UI reviewers reran failure cases against the changed source | Found and fixed an RSS shutdown pipe-drain cancellation hang, malformed Unicode URL returning 500, and a FLUX canceled-handler/unknown-native-outcome race. Original reproductions passed after fixes; no remaining material findings. |
| 3 — release acceptance | Independent holistic source review, documentation/CI review, dependency advisory audit, complete CPU suites, real-feed rotation and rendered UI checks | Initial code review passed 146 focused tests. The advisory audit found an exposed Starlette Range DoS and other app package findings; patched dependencies and early image-format restriction passed complete suites and an independent 182-test/41-browser-check follow-up. Documentation and CI review passed with the deployment gates below. |

## What changed

- **RSS and model context:** prefer the first nonempty description, then summary, content or encoded content, independent of XML order. Normalize HTML/XHTML to plain text with structural spacing and hidden-script/style exclusion. Keep missing descriptions empty. `{{article_excerpt}}` supplies that source description to Qwen's separate article message; title plus excerpt stays within 4,000 characters.
- **Reader cards:** show meaningful descriptions with a three-line clamp; omit empty or standalone Comments boilerplate. Do not add empty paragraphs or reserve blank description height. Keep image geometry and loading/recovery state stable.
- **Untrusted fetches:** four nonqueued process slots, a ten-second overall deadline including DNS, bounded input/output, safe pinned destinations and explicit terminate/kill/reap behavior. Saturation returns `FEED_BUSY` with a retry delay; the form does not replay a rejected request automatically. Invalid Unicode URLs fail before child creation.
- **Persistence/admission:** service-layer bounds do not rely on the fetcher being trusted. Limit each visitor to five simultaneous unexpired pages by default (`LIVE_VIEW_LIMIT`, legacy `SAVED_FEED_LIMIT` fallback), including blocked pages; exact-key replay remains exempt. Check and reserve atomically. Compact expired view payloads in bounded batches, preserving identity, expiry, admissions and accounting history.
- **Worker lifecycle:** invalidated FLUX engines cannot accept another job. Known terminal fatal outcomes retain the result envelope while requesting SDK recycle. Unknown native outcomes force process exit without unsafe CUDA cleanup, including the cancellation-drain path. Current GPU profiles, model pins and separate worker architecture remain intact.
- **Maintainability:** removed unused viewer state, obsolete history template/CSS and redundant worker lifecycle policy. Added comments explaining trust boundaries, transactions, quota reset epochs, uncertain submissions, cancellation, image decoding and expiry. Added an isolated CPU runner and least-privilege GitHub Actions workflow pinned to action commits.

## Complete CPU suites

Run from the repository root using the separate environments in the [integration guide](README.md). These counts are distinct suite totals; focused agent reruns overlap them and are not added again.

| Command | Result |
| --- | --- |
| `PULSEFEED_PYTHON=app/.venv/bin/python bash scripts/check.sh app` | **532 passed** |
| `PULSEFEED_PYTHON=app/.venv/bin/python bash scripts/check.sh integration` | **151 passed** |
| `PULSEFEED_PYTHON=workers/qwen/.venv/bin/python bash scripts/check.sh qwen` | **75 passed**, unittest |
| `PULSEFEED_PYTHON=workers/flux/.venv/bin/python bash scripts/check.sh flux` | **140 passed** |
| `bash scripts/check.sh ui` | **9 suites / 172 checks passed** |

Total: **898 Python tests plus 172 browser-module checks**. Final app/integration/UI checks ran first with `PULSEFEED_PYTHON=/private/tmp/pulsefeed-app-security-20261016/bin/python`; the exact verified 32-package environment was then synchronized into `app/.venv`. Node rendering helpers honor that interpreter override. Integration/local tests also reported 41 passing subtests, not included in the total. The integration/local suite used permission to bind temporary loopback servers. Nonfailing warnings remain for Starlette's test-client migration to httpx2, an AnyIO deprecation and an existing empty regular-expression match in a gateway test. Shell syntax checks passed. The workflow itself has not run on GitHub; there is no configured repository remote yet.

## Dependency security

The independent [dependency review](../docs/dependency-security.md) queried current public advisories and checked actual source reachability. The app now pins FastAPI 0.135.4, Starlette 1.3.1 and Pillow 12.3.0; its test environment uses pytest 9.0.3 and pytest-asyncio 1.3.0. Removed unused python-multipart. Provider image validation permits only JPEG/WebP decoder plugins before opening bytes, with a regression proving a PNG decoder never runs.

The final 26-package app lock returned **zero known advisories and zero skipped packages** with pip-audit 2.10.1. CI now audits that exact lock on normal runs and never performs automatic fixes. This is dated advisory coverage, not a guarantee against unknown vulnerabilities.

**GPU inventories retain known advisories and incomplete vendor/native-package coverage.** The bounded review established no public-request exploit in the current fixed-model, local-snapshot paths. It does not clear the complete images: trusted model caches, exact artifact inventories, native/OS scanning and reachability triage remain required before public cloud release. Worker versions were preserved to avoid an untested CUDA/quantization change. Read the linked report before deploying them.

## Real RSS rotation

The explicit [feed probe](evaluate_feed.py) used the production isolated fetcher and Qwen's actual input validator. It does not create app sessions/views, invoke models, change policy or reset allowance. Counts describe the first ten source entries fetched at that time, **not** an override of the live nine-story page limit.

| Round / source | Articles / validated Qwen inputs | Meaningful card descriptions | Fetch + validation |
| --- | --- | --- | --- |
| 1 — [Hacker News](https://news.ycombinator.com/rss) | 10 / 10 | 0; Comments-only source excerpts omitted from cards | 1.189 s |
| 2 — [BBC England](https://feeds.bbci.co.uk/news/england/rss.xml) | 10 / 10 | 10 | 0.334 s |
| 3 — [New York Times](https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml) | 10 / 10 | 10 | 0.720 s |

Measured on 15 October 2026 at 22:23–22:27 UTC, which is 16 October in Europe/Vienna. These are individual fetch observations, not latency benchmarks. **Zero model calls** were made by these checks. Missing/malformed descriptions, HTML safety and maximum lengths also have deterministic regression coverage.

## Rendered browser evidence

A disposable app on port 8021 used synthetic inference and three explicit source fixtures: a long description, a missing description and a Comments-only description. It used its own temporary database and random session secret; it did not access live feeds, private operator credentials or Spark.

- Desktop 1280 × 900: all three images decoded; first-three reveal completed; **CLS 0**, unchanged card/image geometry and no horizontal overflow.
- Mobile 390 × 844 fresh generation and cached refresh: all three images decoded/restored; **CLS 0**, no horizontal overflow in both flows. The only description paragraph measured 60 px versus 540 px of full text, with a three-line clamp. Missing/Comments-only descriptions produced no paragraph.
- Mobile invalid source: validation remained on the home page; form and footer rectangles were unchanged, and the error appeared above the footer in its reserved slot. A subsequent valid submission remained possible.

These are synthetic rendering measurements, not fresh AI quality evidence or guarantees for every browser/device. The earlier real Spark flow remains documented in the [chronological validation record](VALIDATION.md).

## Applied laptop update

Restarted only FastAPI on `127.0.0.1:8017` after the suite/review pass, then repeated the controlled stop/sync/start for the independently tested dependency fixes. Before and after both changes, checks reported a live healthy app, ready idle Spark workers and no active batches. Hash comparisons preserved current settings, quota policy, reset epoch, **126 runs**, **244 invocations**, nine quota audit records and 119 view/run links exactly. The actual policy remained **120 daily / 30 hourly / nine stories**, reset epoch five; this pass did not reset it.

The new expiry cleanup compacted payloads for 30 already-expired views while retaining their metadata and history. Both Spark containers stayed running, with their retained results intact. Worker source lifecycle changes were CPU-tested here; the existing GPU images were **not rebuilt or redeployed** during this review.

After the dependency update, live static-file normal/Range responses and security headers passed. Read-only retrieval of one already-completed result from each Spark worker passed the patched app's full envelope validation; the retained FLUX image decoded successfully (228,278 bytes). This checked existing real output compatibility without new inference or database mutations.

## Cloud container packaging follow-up

Prepared release candidate `v1.0.0-20261016-204232` from the current source as **two separate `linux/amd64` images**, using the existing Spark BuildKit builder and its bundled CPU emulator. This is distinct from the running ARM64 Spark services. The laptop had insufficient comfortable disk headroom for these large images, so packaging used Spark's storage. No GPU inference or model download was performed.

Both builds passed. Temporary verification stages then imported the actual image's handler and Runpod SDK (`1.12.0`), compiled the copied Python sources, accepted a fresh valid request through the real validator, and repeated dependency checks. Both reported `x86_64`; none loaded Torch, vLLM, Diffusers or Transformers. These inspection stages are not part of the deployable Dockerfiles.

Independent comparison found all **18 source-file hashes** unchanged, all **nine Qwen and five FLUX copied-file hashes** matching, and all **249 Qwen / 204 FLUX expected packages** matching their provenance. Qwen also contains the three documented shadowed distribution records. Its only dependency warning remains the exact inherited PyGObject/Pycairo baseline; FLUX reported no broken requirements. Full Python and OS package inventories were exported, but inventory collection does not replace advisory scanning or GPU verification.

Published both candidates in the verified **private** Docker Hub repository `rokkovach/pulsefeed`, using `qwen-v1.0.0-20261016-204232` and `flux-v1.0.0-20261016-204232`. Their immutable OCI index digests are:

| Worker | Published index digest |
| --- | --- |
| Qwen | `sha256:20a2a6e793bbb0766bf88f57da2f4977df61f28ce567132c08528ff9edf80639` |
| FLUX | `sha256:eb3536675ced65492ae6d9eaff4a9bfd453ca29a1807e1a36d011bdc35ee5785` |

Independent remote inspection passed 16 checks per image: one Linux AMD64 runtime, matching tested runtime/config digests, intended handler and model/cache settings, and attached SPDX SBOM/SLSA build-provenance descriptors. A separate registry check verified SHA-256 manifests and all **41 referenced blobs** with matching sizes/digests. Docker Hub reported `is_private=true`; anonymous clients with properly scoped registry tokens received HTTP 401 for both tags. SBOM generation is inventory evidence, not vulnerability clearance. Native GPU checks and OS/vendor advisory scanning/triage remain pending.

Private evidence, including source hashes, build logs, verification scripts and inventories, is under ignored `integration/artifacts/runpod-release/v1.0.0-20261016-204232/`. Runpod endpoints and Railway deployment remain pending. The packaging builder was returned to its original stopped state; the existing Spark model services were not restarted. The stopped laptop app and SSH tunnels were restored using the same configuration and database; FastAPI reported live/healthy and both authenticated workers reported ready with zero active jobs. The [first deployment guide](../docs/runpod-first-deployment.md#published-candidate--october-16-2026) contains the exact published references and the next configuration steps.

## Deployment gates and limits

- Follow [deployment](../docs/deployment.md), [architecture](../docs/architecture.md) and [operations](../docs/operations.md). Root/component READMEs and the [code walkthrough](../docs/code-walkthrough.md) provide a review order for tomorrow.
- Native ARM64 Spark validation does not establish AMD64 RTX PRO 4500 Blackwell compatibility. Build the current worker sources and run both explicit real-GPU smoke suites on the actual cloud target before public use.
- Railway deployment, HTTPS/proxy/cookie behavior, volume recovery and native Runpod queue failure behavior remain unverified. Keep one app/coordinator and one worker per model endpoint initially.
- There is no built-in admission maintenance barrier. Use a real ingress write barrier and drain known jobs before planned worker replacement. A momentary idle reading does not prevent new admissions.
- Runpod async results have a shorter retention period than the one-hour reader. A late visitor without the saved browser Blob can lose an image; no automatic regeneration occurs.
- Snapshot compaction bounds expired view payloads, not the lifetime size of accounting/history tables or the physical SQLite file. Anonymous visitor identity is not a strong identity boundary against a determined attacker; global limits remain the cost guardrail.
- No GitHub publication, cloud deployment, new GPU quality comparison or cost benchmark was performed. Raw JSON/screenshots under `integration/artifacts/release-2026-10-16/` remain ignored workspace evidence. This committed summary is readable without those files.

The [case-study outline](../docs/case-study.md) separates established evidence from fields to fill after cloud deployment. It is preparation for the later joint write-up, not a completed case study.
