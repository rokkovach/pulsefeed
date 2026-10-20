# Qwen startup failures and handler cleanup — 20 October 2026

Qwen now runs the pinned NVFP4 model on Runpod. The failures we encountered were separate: insufficient download storage, cache validation rejecting externally linked files, and an expired test-request deadline. The generic initial error made these harder to distinguish.

This record describes what we observed, what changed, and what still needs verification. It contains no API keys or private endpoint identifiers.

## Runtime and deployment

- Model: `nvidia/Qwen3-8B-NVFP4`, revision `ccd10a893cbca613259517c3efe08e151ddf2b8e`.
- Engine: vLLM 0.32.0, PyTorch 2.13.0 / CUDA 12.9, ModelOpt FP4 with `flashinfer-cutlass`.
- Interface: Runpod Queue handler using `runpod.serverless.start()`. Railway submits and polls jobs; it does not host the model.
- Cloud GPU policy: one SM120 Blackwell GPU, at least 30 GiB reported VRAM (32 GB class or larger). This includes RTX 5090 and compatible RTX PRO Blackwell cards; it does not include RTX 4090, A40 or RTX A6000.
- Spark remains a separate ARM64/GB10 profile. A successful Spark test alone does not prove AMD64 cloud compatibility.

## 1. Download ran out of space

The original worker logs warned that one artifact needed about 4,987 MB while only about 3,925 MB remained. Startup exited during `snapshot_download`. A download `RuntimeError` was classified too generically as `UNSUPPORTED_RUNTIME`; this was not evidence of a GPU failure.

The operator increased the endpoint disk to 40 GB. Endpoint metadata reported no attached network or global volumes. Later logs showed all 15 files downloaded successfully without that warning. We did not directly inspect the cloud mount table, so this is evidence of successful download rather than a claim about the exact mount mapping.

The image explicitly supplies `cache_dir=` from `MODEL_CACHE_DIR`, defaulting to `/runpod-volume/huggingface`. Changing `HF_HOME` alone does not override this argument. An attached network volume would need its own capacity adjustment. The public model's unauthenticated-Hub warning was not the observed validation failure.

## 2. Completed download was rejected by our validator

After the storage change, logs reached 15/15 and then returned `MODEL_NOT_READY`. This happened in our checkpoint validator, before torch/vLLM engine initialization. Increasing the engine startup timeout would not repair this validation failure.

Release v1.0.3 added fixed validation check identifiers and cache capacity logging. The next attempt identified `blob_symlink_target`: a file resolved outside the snapshot and its repository blob directory. The precise external target path was not captured; we should not claim we inspected Runpod's internal store layout.

Release v1.0.4 accepts externally linked files only if their complete bytes match the pinned artifact identity:

1. The snapshot-relative filename must be in the exact revision's artifact allowlist.
2. The target must be a regular file with the exact pinned size.
3. A streaming SHA-256 must match the pinned digest. Shard/tokenizer LFS digests came from the immutable Hugging Face revision API; other artifact digests were computed from files fetched at that revision.
4. Unknown files, changed contents, extra/truncated bytes, FIFOs/devices, directory symlinks and executable Python files remain rejected.

Normal snapshot/blob links still work. We did not permit an arbitrary external directory or disable validation. External weight verification adds startup I/O, with bounded 4 MiB read chunks. Cache writers must still be trusted between checking and loading; this is not an immutable filesystem guarantee. Weight hashes are enforced on the external-link fallback, not retroactively claimed for every ordinary cache file.

Models, dependency pins and GPU allocation were unchanged. Our checks used synthetic rejection cases and actual pinned metadata/tokenizer files in a recreated external shared store; they did not download full weights to the laptop.

## 3. A ready worker rejected an expired test payload

The first response after startup had Runpod status `COMPLETED`, but the nested result had `ok: false`, `DEADLINE_EXCEEDED` and zero inference attempts. The copied example contained an old absolute deadline. This was a request validation failure, not another model startup failure.

A fresh request succeeded on v1.0.4 with:

| Measurement | Reported value |
| --- | --- |
| Handler outcome | `ok: true` |
| Model inference time | 3,637.3 ms |
| Runpod execution time | 3,876 ms |
| Queue/startup delay | 70,661 ms |
| Input / output tokens | 165 / 97 |
| Inference attempts / retries | 1 / 0 |

The operator supplied this real cloud result. Its runtime metadata confirms the cloud NVFP4 engine; the supplied response does not identify the exact GPU model. The worker logs reported architecture SM120. Queue delay must not be presented as model inference time.

In Postman, generate IDs and a future deadline at send time:

```javascript
pm.variables.set("run_id", pm.variables.replaceIn("{{$guid}}"));
pm.variables.set("invocation_id", pm.variables.replaceIn("{{$guid}}"));
pm.variables.set("deadline_ms", String(Date.now() + 600000));
```

Use the UUID variables as JSON strings and `{{deadline_ms}}` as an unquoted JSON number. Literal Postman placeholders are not valid JSON outside Postman's variable substitution. Submit once and poll the returned job ID; never repeatedly submit because a job remains queued.

## 4. Handler cleanup in v1.0.5

The request path is now: validate, acquire the single-job lock, prepare, generate once, record measurements, validate the output, return.

Removed:

- Generic request-level retry/backoff machinery. Production inference is not automatically replayed. Bounded transient startup-download retries remain in the runtime.
- Usage accumulation across hypothetical retries; one observation is recorded directly, and missing measurements remain null.
- The redundant 4 MiB JSON check. Successful prompt text is already capped at 2,000 characters, with bounded envelope fields.
- Sentence-opening heuristics that guessed whether text was a caption, summary or reasoning. Empty text, truncation, reasoning/control tags, JSON/code fences, multiline output, invalid control characters and size violations are still rejected.

Retained:

- Strict schema, identifiers, article/plain-text limits and prompt-template validation.
- Wall-clock request expiry plus monotonic execution limits.
- Single-job locking and cancellation propagation into the disposable engine process.
- Token/timing accounting even when completed output is rejected.
- Zero attempts for pre-inference rejections and unknown measurements for uncertain failures.
- SDK worker refresh when the engine becomes unavailable; bounded process-group cleanup.
- Signal handling both during startup and after the SDK takes ownership. The two phases have different owners, so blindly deleting the startup handlers would leave a cleanup gap.

Retry counter fields remain present and zero to preserve the app/worker response contract. The local and cloud GPU smoke scripts were updated to assert no replay after a simulated prepare failure. This cleanup does not claim the text validator can determine semantic prompt quality; that remains a prompt/evaluation concern.

## Release and rollback

The known-working cloud release is unchanged:

```text
docker.io/rokkovach/pulsefeed:qwen-v1.0.4-20261020
docker.io/rokkovach/pulsefeed@sha256:8429dfe6121b504feb11386703b7c995627ab078ecc50f359f260d132afde964
```

The published cleanup image is:

```text
docker.io/rokkovach/pulsefeed:qwen-v1.0.5-20261020
docker.io/rokkovach/pulsefeed@sha256:10916e788ebad05f2d0d5b6834941f75f5e4e2e615b7ed0086e4024f7c40f1d0
```

Its public manifest and AMD64 configuration were verified anonymously; the v1.0.4 rollback tag was independently checked and retains its original digest. The cleanup is a separate v1.0.5 image, built as a small code layer on that exact release. No old tags are overwritten and no endpoint is automatically switched.

To roll back, edit the Qwen endpoint's container image to the v1.0.4 digest above, save, and inspect the new worker's logs. Preserve the same model/cache/GPU settings. Check existing submitted job IDs before making another request; changing images does not prove an already submitted job was cancelled.

## Cleanup validation

| Suite | Result |
| --- | --- |
| Qwen worker and actual installed SDK tests | 92 passed |
| App and integration tests | 614 passed (612 in the initial scoped run; two loopback HTTP tests passed with socket access) |
| FLUX regression tests | 168 passed |
| Local gateway/coordination tests | 80 passed |
| Browser behavior harnesses | All nine passed |
| Native Spark/GB10 real model | First and warm prompts passed; invalid input and injected prepare failure rejected without inference |
| Changed documentation relative links | Passed |

The first broad integration directory invocation accidentally collected ignored dependency source artifacts; the corrected command explicitly selected `integration/test_*.py`. Initial loopback socket restrictions were resolved by running only the affected tests with network permission. These were test-runner/environment issues, not hidden passing results.

Native Spark startup took 114.8 seconds in a disposable offline container using the existing read-only model cache and shared GPU lock. First/warm generation took 1,339.9 / 1,486.0 ms and reported 111 input tokens each, with 51 / 58 output tokens. Resident model services were left running. This validates the cleaned handler on GB10; it is not a 5090 benchmark.

The AMD64 cleanup image is an incremental code layer on the immutable v1.0.4 image. Spark currently has no AMD64 emulation registered, so actual AMD64 image execution and a v1.0.5 Runpod queue smoke remain separate gates; local SDK tests and native Spark tests do not replace them. No live endpoint has been changed by this cleanup.

## References

- [Qwen source and operation](../workers/qwen/README.md)
- [Runpod first deployment](runpod-first-deployment.md)
- [Runpod cached-model layout](https://docs.runpod.io/serverless/endpoints/model-caching)
- [Pinned model files](https://huggingface.co/nvidia/Qwen3-8B-NVFP4/tree/ccd10a893cbca613259517c3efe08e151ddf2b8e)
