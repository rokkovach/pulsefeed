# PulseFeed technical diagrams

Evidence checkpoint: **20 October 2026 UTC**. Native Spark GPU inference, laptop-to-Runpod queue inference and the full Railway generation flow are verified. The public Railway test completed nine NYTimes images and eighteen successful calls, with matching usage and refresh reuse. See [production validation](../production-validation-2026-10-20.md).

| Diagram | Standalone vector | PNG preview | Editable topology |
| --- | --- | --- | --- |
| Trust boundaries and cloud target | [architecture.svg](architecture.svg) | [architecture.png](architecture.png) | [architecture.mmd](architecture.mmd) |
| Admission, stages and image delivery | [request-lifecycle.svg](request-lifecycle.svg) | [request-lifecycle.png](request-lifecycle.png) | [request-lifecycle.mmd](request-lifecycle.mmd) |
| Local and cloud evidence | [local-vs-cloud.svg](local-vs-cloud.svg) | [local-vs-cloud.png](local-vs-cloud.png) | [local-vs-cloud.mmd](local-vs-cloud.mmd) |

The SVGs are hand-laid-out, accessible vector diagrams with live text, self-contained shapes and no external assets. The editable Mermaid files express the same technical relationships; Mermaid's automatic layout will differ. Keep both representations in sync. `render_diagrams.py` reproduces the SVG layouts; `render_png.cjs` exports 2× PNG previews through bundled sharp / librsvg. All three PNGs were rendered and visually inspected; labels, margins and connector placement were corrected before delivery. The sequence diagram combines submit / save-ID / status polling into one exchange in the SVG; its Mermaid source expands those operations.

Rebuild from the repository root using the supplied workspace runtime. Set `RUNTIME_PYTHON` and `RUNTIME_NODE` to its executable paths, and `RUNTIME_NODE_MODULES` to its package directory containing `sharp`:

```sh
"$RUNTIME_PYTHON" docs/diagrams/render_diagrams.py
NODE_PATH="$RUNTIME_NODE_MODULES" \
  "$RUNTIME_NODE" docs/diagrams/render_png.cjs
```

Sources: [architecture](../architecture.md), [cloud deployment](../deployment.md), [dependency remediation](../dependency-remediation-2026-10-20.md), [Spark topology](../../local/README.md), plus `main.py`, `service.py`, `coordinator.py` and browser `app.js`.

Review these facts when the deployment changes: actual cloud status, target hardware, worker concurrency and result retention. A browser Blob lasts only until the original one-hour view expiry; native gateway outputs last one hour after completion, Runpod async outputs 30 minutes after completion. SQLite persists sessions, policy, reservations, usage and metadata; it is not an image archive. Spark's shared lock serializes actual GPU execution even when transport overlaps. Admin usage is aggregate; visitor pages remain owner-scoped. The workers never call one another.
