# PulseFeed handoff

The main reviewer handoff is the [six-page PDF](../../output/pdf/pulsefeed-review.pdf), with an [editable DOCX](../../output/docx/pulsefeed-review.docx). It covers the architecture, two pages of app screenshots, a page for each model worker, troubleshooting, measured results and next steps. The [reviewer guide](../reviewer-guide.md) gives a quick route through the app and source.

The [review ZIP](../../output/pulsefeed-review-pack.zip) contains those documents, a one-page summary and technical diagrams. Its builder uses an explicit allowlist, never a workspace-wide archive. The older slide deck has been retained locally under ignored `output/archive/` and is not part of this release. Its builder under `presentation/` is a historical source, not the current report.

## Rebuild

Use Python with `python-docx` for `build_review.py`, then export and render with the document skill's `render_docx.py --emit_pdf`. Keep the DOCX and PDF in sync by exporting the PDF from the final Word document. The editable text is [review.md](review.md); screenshots and report diagrams are in [assets](assets/README.md).

```sh
python3 docs/showcase/build_review.py
# Render the DOCX with your documented LibreOffice renderer and inspect all pages.
# Copy the verified PDF to output/pdf/pulsefeed-review.pdf.
python3 docs/showcase/onepager/build.py
python3 docs/showcase/package_review.py
python3 scripts/check-release.py
```

Capturing existing pages uses `capture_report.cjs` with an ignored private session export and an explicit Chromium executable. It does not submit feeds, create inference or edit limits. Never commit session exports. The screenshots are from the actual local app using live Runpod results, not a UI mockup.

See [release status](../release-status.md) for current evidence and the remaining Railway gate. Before sending the email, complete that gate and share the admin password separately from the public materials.
