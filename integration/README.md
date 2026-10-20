# Integration and release checks

CPU suites verify contracts, app/worker boundaries, local gateways and browser behavior without paid inference. GPU smoke, cloud readiness and a connected deployment remain separate gates. [Architecture](../docs/architecture.md) · [deployment gates](../docs/deployment.md) · [release evaluation](RELEASE_EVALUATION.md)

## Isolated test environments

From the repository root, use Python 3.11 and Node 18+ (CI uses Node 22). The app and full Runpod SDK have different FastAPI constraints; **do not install every requirement into one environment**.

App, integration/local and browser checks:

```sh
python3.11 -m venv app/.venv
app/.venv/bin/python -m pip install -c app/requirements.lock \
  -r requirements-dev.txt -e 'app[test]'
PULSEFEED_PYTHON=app/.venv/bin/python bash scripts/check.sh app
PULSEFEED_PYTHON=app/.venv/bin/python bash scripts/check.sh integration
bash scripts/check.sh ui
```

Qwen CPU/SDK checks (`scripts/check.sh qwen` uses unittest discovery, not pytest):

```sh
python3.11 -m venv workers/qwen/.venv
workers/qwen/.venv/bin/python -m pip install -r workers/qwen/requirements-test.txt
PULSEFEED_PYTHON=workers/qwen/.venv/bin/python bash scripts/check.sh qwen
```

FLUX CPU/SDK checks:

```sh
python3.11 -m venv workers/flux/.venv
workers/flux/.venv/bin/python -m pip install -r workers/flux/requirements-test.txt
workers/flux/.venv/bin/python -m pip install --no-deps runpod==1.12.0
PULSEFEED_PYTHON=workers/flux/.venv/bin/python bash scripts/check.sh flux
```

FLUX's dependency-free SDK installation is solely for isolated module tests with mocked transports, not a production runtime recipe. Worker GPU dependencies belong in their pinned images. [`scripts/check.sh`](../scripts/check.sh) centralizes suite selection; the [GitHub Actions workflow](../.github/workflows/ci.yml) runs CPU suites without deployment credentials and does not build/publish GPU releases or invoke models.

CI also checks the exact app lock with `pip-audit==2.10.1` against current public package advisories. This is a network metadata query, not inference or an automatic dependency upgrade. GPU package/base-image advisories require separate compatibility and reachability review; a passing app audit does not cover those images.

## What each layer proves

| Layer | Coverage | Does not prove |
| --- | --- | --- |
| JSON Schemas + `contract_checks.py` | Required fields, normalization, bounds, token arithmetic, IDs/counters, actual image decoding, synthetic fixtures | Model quality or GPU support |
| Worker boundary tests | Actual core/SDK wrappers with injected engines, failures, retries, cancellation and bounds | Native CUDA cancellation or real memory behavior |
| App suite | Sessions/admin/CSRF/ownership, SSRF, admission/quota races, frozen views, restart/recovery, usage and templates | Railway proxy/volume configuration |
| Local gateway suite | Real temporary loopback HTTP, bearer auth, queue/status/cancel, shared lock, retention/capacity, process-stop behavior | Cloud queue semantics |
| `*.test.cjs` | Actual browser callbacks/modules with DOM/IndexedDB doubles; recovery, templates, chart/settings/limits and retained DOM state | Real browser layout, rendering or physical-device usability |
| Manual/browser evidence | Observed rendered flow, storage restore, layout and live usage under recorded conditions | General performance distribution or external cloud readiness |

Tests label synthetic metadata. Fixture deadlines must be replaced before deliberate live requests. A CPU fixture image is not an AI quality sample.

## Optional real-feed CPU probe

[`evaluate_feed.py`](evaluate_feed.py) fetches a real public publisher feed and checks its normalized article bounds, card descriptions and production Qwen input validation. It uses the network and CPU only: no GPU, model calls, provider credentials, app sessions, live database or allowance changes. Run explicitly from the repository root in the app/integration environment; publisher uptime/content is deliberately outside deterministic CI.

```sh
PYTHONPATH=.:app app/.venv/bin/python -m integration.evaluate_feed --source hacker-news
# Rotate to BBC England and New York Times in subsequent evaluation rounds.
PYTHONPATH=.:app app/.venv/bin/python -m integration.evaluate_feed --source bbc-england
PYTHONPATH=.:app app/.venv/bin/python -m integration.evaluate_feed --source nytimes
```

Only aggregate counts/lengths are printed. Add `--output /tmp/pulsefeed-feed-evaluation.json` to retain one result deliberately; keep each round's source/date and do not mistake a changing headline sample for a repeatable performance benchmark.

## Real-model and cloud validation

Use component [Qwen](../workers/qwen/README.md#required-real-gpu-smoke) and [FLUX](../workers/flux/README.md#explicit-real-gpu-smoke) smokes for the target images. For Spark use [local/control.sh smoke](../local/README.md#explicit-real-model-smoke-test), which explicitly generates real output and bypasses app quotas. For Railway/Runpod, follow the full [connected release smoke](../docs/deployment.md#5-connected-release-smoke), including fresh public visitors, isolation, admin, refresh and controlled restart/drain checks.

Real Spark GB10/ARM64 evidence does not validate AMD64 RTX PRO 4500 Blackwell images. A provider `COMPLETED` status is insufficient: inspect `output.result`, model identity, counters and decoded output. Never repeat a possibly accepted `/run` request simply to obtain a cleaner result.

## Browser layout probe

[`layout_probe.py`](layout_probe.py) is a local-only instrumented app factory. Start it with an isolated app environment/database using `PYTHONPATH=app` and `integration.layout_probe:create_probe` in place of the ordinary factory. `LAYOUT_PROBE_DELAY_MS=900` delays asynchronous assets/API reads. Never deploy this wrapper.

The hidden `#layout-probe-result` records layout shifts, element geometry and scroll position without article text, URLs, credentials or image data. Compare initial/final reader geometry during loading, later image arrivals, reload, scrolled reload and logo navigation. Verify no additional inference. Restore the ordinary factory after measurement. DOM tests complement this actual rendered check; they cannot measure CLS.

## Evidence records

- [Release evaluation](RELEASE_EVALUATION.md): current committed review/test summary and remaining gates.
- [Integration validation](VALIDATION.md): dated, chronological product/live/browser evidence; earlier checkpoints retain older policies and UI behavior.
- [Spark validation](../local/SPARK_VALIDATION.md) and [packaging validation](../local/PACKAGING_VALIDATION.md): hardware and image scope.
- [App](../app/VALIDATION.md), [Qwen](../workers/qwen/evidence/local-validation.md) and [FLUX](../workers/flux/VALIDATION.md): component checkpoints.

Raw `integration/artifacts/` files linked by older validation records are workspace-only and ignored by Git unless individually reviewed for publication. Their absence from GitHub does not justify inventing public screenshots or measurements; use the committed summaries and clearly state evidence scope.

For a new release, record commit, image digests, exact model revisions, GPU/driver/runtime, execution mode, tested commands, redacted configuration and each gate's result. Keep failures and uncertainty visible. Do not overwrite historical evidence to match the current implementation. [Case-study outline](../docs/case-study.md)
