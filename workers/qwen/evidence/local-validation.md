# Local Qwen validation — 14 October 2026

This is CPU/build-attempt evidence, not real-model or GPU evidence.

- Interpreter: isolated Python 3.11 environment at `workers/qwen/.venv`.
- Command: `workers/qwen/.venv/bin/python -m unittest discover -s workers/qwen/tests -v`.
- Result: 31 tests passed, zero skips; JSON Schema validation used jsonschema 4.25.1.
- Handler/runtime/smoke-test Python compilation passed.
- Tests included the actual runtime adapter control flow with mocked GPU libraries,
  plus shared output schema, startup retry caps, deadlines/cancellation, uncertain
  acceptance, complete usage accounting, and script-entrypoint exception identity.
- Actual Runpod SDK 1.12.0 `run_job` serialization preserved success/error envelopes
  through `output.result`. Native task cancellation stopped an actual local CPU
  child process, released the lock, and dispatched no replacement request.
- The SDK's actual signal-registration path was tested with registered callbacks
  and bounded runtime cleanup delegation. Both parent and engine health are checked.

Docker command attempted against local Colima on linux/aarch64:

```sh
docker build --platform linux/amd64 -t pulsefeed-qwen:candidate workers/qwen
```

The build read the Dockerfile/context, resolved official vLLM 0.12.0 at pinned
multiarch digest `sha256:6766ce0c459e24b76f3e9ba14ffc0442131ef4248c904efdcbf0d89e38be01fe`,
and began pulling amd64 base layers. The bounded attempt was cancelled after
30 seconds (`Canceled: context canceled`) before dependency installation/image
completion. Therefore there is no completed worker image tag/digest or passing
Docker build result to report. No model weights were downloaded.

Public model metadata identified revision
`ccd10a893cbca613259517c3efe08e151ddf2b8e`; public Docker registry metadata identified
the base digest. Neither operation was inference or a paid cloud call.

Pending: completed amd64 image build/pip check, exact GPU/package/kernel startup,
real prompt quality/usage, and cold/warm GPU timings. Follow the worker README's
explicit `smoke_test.py --real-gpu` instructions on RTX PRO 4500 Blackwell 32 GB.
