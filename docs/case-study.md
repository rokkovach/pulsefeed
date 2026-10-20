# Case study evidence

The current reviewer deliverable is the [six-page report](../output/pdf/pulsefeed-review.pdf), with an [editable Word copy](../output/docx/pulsefeed-review.docx). Its source is [review.md](showcase/review.md). Read [release status](release-status.md) before describing the cloud setup.

## Current evidence

| Claim | Scope and record |
| --- | --- |
| Both models work through native Runpod queues | Controlled local FastAPI app to live Runpod: ten 512-square images, twenty successful calls, no retries or duplicate invocations |
| Aggregate usage reconciles | Same sample: 4,880 Qwen input tokens, 767 output tokens, ten FLUX images/attempts and zero final reservations |
| Bounded request overlap works | Three pending requests per model and six started stories observed; this is not proof of three simultaneous physical FLUX kernels |
| Current latency is dominated by provider waiting | Qwen wait 2.88–96.31 s / execution 1.51–3.90 s; FLUX wait 0.09–82.91 s / execution 0.49–1.26 s; nine-story page 344 s, one sample |
| Native Spark supports all reference feeds | Earlier maintained pair: 27 images / 54 calls / zero retries across Hacker News, BBC England and NYTimes; ARM64 evidence remains distinct from AMD64 Runpod |
| Railway web hosting works | HTTPS, authentication and persistent SQLite verified; latest connected Railway generation release remains pending redeployment |

The [Runpod validation record](local-runpod-validation-2026-10-20.md), [model concurrency record](model-concurrency.md), [worker cleanup](qwen-runpod-startup-fix-2026-10-20.md), [compact images](flux-compact-images-2026-10-20.md) and [dependency remediation record](dependency-remediation-2026-10-20.md) describe exact scope. Historical checkpoints preserve earlier policy, hardware and measurements. Raw artifacts are private and ignored; public claims use these reviewed summaries.

## Screenshots and limits

The report's screenshots are actual local browser captures using live Runpod results. The reader uses the nine-story operator override; fresh databases default to ten. Dashboard screenshots include later exploratory use and therefore show nineteen calls/images rather than the controlled ten-image sample. They contain no credentials or private session links.

The live HTTP test did not measure a browser's animation smoothness or verify physical mobile layout. Separate browser behavior harnesses cover storage/reveal and other UI paths. The report does not claim formal latency percentiles, cost per image, three simultaneous physical GPUs, complete dependency clearance or the final connected Railway release. The restricted application API key also could not read the deployed endpoint image digest; verify the console references before release.

## Finish before sending

After Railway redeployment, test the connected feed flow, usage settlement and persistent storage. Record the deployed app commit and endpoint image references, then update [release status](release-status.md) and rebuild the report if the findings change. Send the PDF with the public repository link; share admin credentials separately and rotate them after the review.
