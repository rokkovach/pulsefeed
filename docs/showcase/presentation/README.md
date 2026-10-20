# Historical presentation source

This slide builder predates the latest Runpod validation and compact-image release. It is retained as an editable source, but its claims must be refreshed before reuse. Earlier generated slides are kept only in the ignored `output/archive/` folder. The current reviewer handoff is the [six-page report](../../../output/pdf/pulsefeed-review.pdf) and [Word document](../../../output/docx/pulsefeed-review.docx).

# PulseFeed case-study presentation

`build.mjs` creates nine editable 16:9 slides with native text, process diagrams, architecture boundaries and tables. It uses the existing pixel-garden repository asset on the cover. The final deliverables are `output/presentation/pulsefeed-case-study.pptx` and `output/pdf/pulsefeed-case-study.pdf`.

The narrative describes the final native Spark validation on 20 October 2026. It keeps the pending Runpod target-GPU and Railway deployment checks visible. Slide notes cite repository sources. No public repository address or app URL is assumed.

Use the supplied artifact runtime. No packages need installation. Set:

```sh
export WORKSPACE_DIR="$PWD"
export RUNTIME_NODE="/path/to/supplied/node"
export RUNTIME_NODE_MODULES="/path/to/supplied/node_modules"
export RUNTIME_PYTHON="/path/to/supplied/python3"
export PRESENTATIONS_SKILL_DIR="/path/to/presentations/skill"
export SOFFICE_BIN="/path/to/supplied/bundled/headless/soffice"
```

Immediately before the first create/edit run, invoke the presentation skill operation marker exactly once for that operation:

```sh
cd "$PRESENTATIONS_SKILL_DIR"
"$RUNTIME_NODE" container_tools/mark_artifact_operation_started.mjs --operation-kind create --expected-output-count 1 --output-format pptx
cd "$WORKSPACE_DIR"
"$RUNTIME_NODE" docs/showcase/presentation/build.mjs
```

The builder verifies Arial availability, exports a candidate to ignored workspace evidence, runs the skill finalizer, imports the final PPTX, renders all nine final slides, and tries a PDF export from the final object. If native PDF export is unavailable, `SOFFICE_BIN` selects the supplied bundled headless LibreOffice fallback. Do not point it at a desktop application or install substitute dependencies. The finalizer does not overwrite existing output or receipts. For another revision, set `FINAL_PPTX` to a new path and `BUILD_REVISION` to a fresh short label, inspect that result, then copy the accepted artifact to the desired final filename. Keep build previews and validation receipts private under `integration/artifacts/documentation-2026-10-20/presentation/`.

Before sharing a rebuilt deck, inspect every full-size final-slide render and check that the factual sources still match. Rendering is not a test in Microsoft PowerPoint itself.
