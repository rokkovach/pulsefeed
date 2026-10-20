# PulseFeed contract version 1

The JSON Schemas describe the inputs inside Runpod's `input` and the handler envelopes inside `output.result`. The SDK callback returns `{ "result": envelope }` because Runpod consumes a top-level handler `error`. The `*-output` schemas validate the inner envelope; `*-provider-output` schemas validate the wrapper. They implement [the shared agreement](../PRD/00-shared-contracts.md). Docker build contexts remain independent: application and worker code enforce this agreement without importing files outside their context.

All fixtures are synthetic, including the image, model revisions, timings, and usage. They establish interoperability, not model quality or GPU compatibility. `manifest.json` identifies expected-valid and deliberately invalid examples. Fixture deadlines are illustrative; replace them with a current deadline before a real request.

JSON Schema validates structure. `integration/contract_checks.py` adds normalization, combined text length, token arithmetic, retry accounting, matching IDs, actual image decoding, and binary/transport bounds. The application and workers must independently enforce their relevant checks at runtime.

Qwen schema 1 accepts an optional `article_template` user-message layout containing one `{{article_title}}` and one `{{article_excerpt}}` marker. Semantic checks enforce their counts, a 2,000-code-point static budget, and permitted controls. Markers expand once to JSON-quoted article values in the user message; the system prompt remains separate. Missing layouts retain legacy rendering. Update workers before sending custom layouts; old workers reject unknown input fields.

## Run contract checks

Use the isolated app/integration environment in the [integration guide](../integration/README.md#isolated-test-environments), then run:

```sh
PULSEFEED_PYTHON=app/.venv/bin/python bash scripts/check.sh integration
```

The suite does not invoke real models. Never copy a historical fixture deadline unchanged into a live request. The [architecture](../docs/architecture.md), [code walkthrough](../docs/code-walkthrough.md) and [worker READMEs](../workers/qwen/README.md) explain the runtime boundary; the [FLUX guide](../workers/flux/README.md) covers image bounds.

FLUX schema 1 now accepts bounded square presets 256, 512 and 1024, retaining legacy 1024 compatibility. Width and height must match; the app verifies decoded dimensions against both metadata and its saved request. The compact worker must be deployed before an app sending 512/256 requests. Token/context limits, four inference steps and guidance 1.0 are unchanged.
