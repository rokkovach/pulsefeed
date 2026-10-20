# Model concurrency

The app can submit up to three jobs to each model endpoint. This is an app scheduler setting; each GPU worker still processes one job at a time. No worker-image changes are needed for concurrency.

## Configuration

For three workers per endpoint, configure Railway as follows:

```text
COORDINATOR_STAGE_CONCURRENCY=3
COORDINATOR_PIPELINE_SIZE=6
```

`COORDINATOR_STAGE_CONCURRENCY` is a strict integer from 1 to 3. `COORDINATOR_PIPELINE_SIZE` caps started, unfinished stories, from 1 up to twice the stage limit. If the pipeline variable is absent, it defaults to twice stage concurrency. If it already exists as `2`, change it explicitly to `6` to use the full three-per-model mode.

| Mode | Stage concurrency | Started-story cap |
| --- | ---: | ---: |
| Serial | 1 | 1 |
| Original bounded pipeline (default) | 1 | 2 |
| Two workers per model | 2 | 4 |
| Three workers per model | 3 | 6 |

On **each Runpod endpoint** set max workers to **3**, GPUs per worker to **1**, and keep per-worker handler concurrency at **1**. Active workers can stay at 0 for scaling to zero, or be raised for a planned warm demonstration. Three active workers per endpoint means up to six continuously billed workers across the two models. Queueing and cold starts still affect elapsed time; increasing a maximum does not guarantee immediate GPU supply.

Keep one Railway process and one replica with the existing persistent SQLite volume. Adding Uvicorn processes is not how this concurrency is implemented: it would create multiple coordinators, which this single-process scheduling lock does not coordinate.

## What gets faster

The coordinator fills up to three Qwen slots, then overlaps later prompts with up to three FLUX jobs. At most six stories are started in the 3/6 mode; remaining stories stay queued and receive a deadline when started. This avoids starting every story's timeout while it still waits for local capacity.

The admission policy still permits one active global feed batch. This change speeds up that feed's work; it does not admit three simultaneous visitor batches. Existing per-page limits, daily/hourly allowances and upfront reservations remain unchanged. Broader multi-visitor admission would be a separate policy change.

A shared-GPU Spark gateway still serializes physical GPU operations through its cross-container lock. Concurrent HTTP submissions to Spark are not proof of multiple GPUs running simultaneously. Separate Runpod workers provide that physical capacity when available.

## Safety and rollback

- Every submission intent is persisted before sending the request. A lost response never causes automatic resubmission.
- Ambiguous acceptance or missing results freeze new dispatch. Already accepted jobs keep being polled and settled.
- Each task has its own durable invocation ID; concurrent ticks remain protected by the existing coordinator lock.
- Each dispatch wave is joined before releasing that lock, even on exceptions or cancellation.
- Reducing the limit does not abandon accepted jobs. All saved provider IDs continue to drain, even when their number temporarily exceeds the new limit. New submissions respect available stage capacity.
- Reservations remain held until known results or confirmed termination. Cancellation requests are not repeatedly sent after restart.
- Global quotas are not multiplied by concurrency. Nine successful one-attempt images settle as nine used attempts and zero remaining reservations.

To return to the original app mode, set stage concurrency to 1 and pipeline size to 2. For fully serial scheduling, use 1 and 1. Let accepted jobs drain; reducing a config value is not evidence that provider jobs stopped.

## Verification

The original 18 pipeline tests continue to pass. Fifteen additional checks exercise same-stage concurrency, six overlapping model requests, nine-story completion under limits 1/2/3, exact-once accounting, cancellation, uncertainty, restart/downgrade behavior and environment validation.

The full app/integration run passed 640 tests, then the newly added six-job deadline/cancellation case passed in the targeted 15-test concurrency suite (641 unique tests total). All nine browser harnesses passed. These automated provider tests use explicit synthetic doubles; they do not claim cloud inference proof.

The [real local-app/Runpod run](local-runpod-validation-2026-10-20.md) then completed ten images and twenty model calls with no duplicate invocations or retries. It observed three pending requests per model and six started stories, and verified aggregate token/image totals. Queue/cold-start delays dominated elapsed time; this does not establish three simultaneous physical FLUX workers. Production Railway deployment remains separate from local testing.
