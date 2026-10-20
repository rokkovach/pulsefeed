# Dependency security review — 2026-10-16

**Latest remediation:** the [October 20 dependency follow-up](dependency-remediation-2026-10-20.md) supersedes the runtime inventories below. It records stronger metadata validation, fully locked FLUX runtimes, Qwen base corrections and compatible security backports, actual-image SBOMs and real Spark GPU/RSS results. Consult that report for final build identities and pending release checks. Published October 16 tags remain unchanged.

**October 20 follow-up:** the [project/container audit](audit-2026-10-20.md#dependency-and-container-findings) refreshes the advisory results and adds actual published-image SBOM scans. The app lock still has zero reported findings; Qwen now has 138 distinct expected-inventory findings and FLUX 51, with eight skipped packages each, including vendor GPU and Conda/OS distributions. Source now verifies pinned small metadata and rejects `.py` files and unsafe cache links, but published October 16 images remain unchanged. The historical review below records the earlier checkpoint.

The remediated Python 3.11 app lock passed a live PyPI advisory check with **zero known vulnerabilities across all 26 pinned packages, with no skipped packages**. The GPU worker inventories retain known advisories and incomplete vendor-package coverage. This is a dated dependency review, not approval to deploy an unreviewed image or a claim that the full GPU stack is vulnerability-free.

## Method and evidence

Used `pip-audit==2.10.1` in an isolated temporary uv tool environment, with `--disable-pip --no-deps`. Only public package names and versions were queried; target packages were neither installed nor resolved by the audit, and `--fix` was not used. Reviewed upstream maintainer advisories and current public PyPI metadata for the app fixes. No production services, databases, model files, GPU settings, or worker locks were changed by this audit.

Repeat the app check from the repository root:

```sh
uv tool run --isolated --from pip-audit==2.10.1 pip-audit \
  --disable-pip --no-deps --progress-spinner off \
  -r app/requirements.lock
```

An advisory-service/network failure is an incomplete check. A zero finding count is limited to the queried database and exact supplied inventory. This check does not cover OS libraries, CUDA drivers, image layers, model contents, or dependencies omitted from an inventory.

Redacted local evidence is saved under ignored `integration/artifacts/release-2026-10-16/dependency-audit/`: original and remediated app JSON, worker JSON, public package/version inputs, deduplicated app results, aggregate counts, and an input-hash manifest. The remediated app lock SHA-256 is `e9fed58ed456aca3fcb4086276ba851a1cdf7e40006c1b920549f8e44bf38cf7`.

## App findings and remediation

The original app lock produced 61 advisory records in four packages. Deduplication by CVE within each package gives **31 distinct findings**; the raw number includes duplicate advisory records. The rebuilt lock has zero reported findings.

| Package | Original pin | Relevant fixed versions | Final app treatment |
| --- | --- | --- | --- |
| Starlette | 0.48.0 | 0.49.1 for Range DoS; 1.0.1, 1.1.0, 1.3.0, and 1.3.1 for later findings | 1.3.1, with compatible FastAPI 0.135.4 replacing 0.119.0 |
| Pillow | 11.3.0 | 12.1.1 and 12.2.0 for earlier findings; 12.3.0 covers the complete returned set | 12.3.0; provider decoding restricts formats to JPEG/WEBP before plugin selection |
| python-multipart | 0.0.20 | 0.0.22, 0.0.26, 0.0.27, 0.0.30, and 0.0.31 | Removed: app uses bounded manual request parsing and does not call multipart/form APIs |
| pytest | 8.4.2 | 9.0.3 | 9.0.3, with pytest-asyncio 1.3.0 for compatibility |

**Confirmed public-request exposure:** Starlette's [CVE-2025-62727 / GHSA-7f5h-v6xp-fcq8](https://github.com/Kludex/starlette/security/advisories/GHSA-7f5h-v6xp-fcq8) is a High, CVSS 7.5 CPU denial of service through `FileResponse` Range processing. The app's unauthenticated `/static` mount made the old pin reachable. Later Starlette CVEs returned were CVE-2026-48710, CVE-2026-48817, CVE-2026-48818, CVE-2026-54282, and [CVE-2026-54283](https://github.com/Kludex/starlette/security/advisories/GHSA-82w8-qh3p-5jfq). The Windows static-file issue does not apply to the Linux deployment; form-parser issues require APIs the app does not use. All are absent from the final lock audit.

Pillow's returned set includes [PSD out-of-bounds writing, CVE-2026-25990](https://github.com/python-pillow/Pillow/security/advisories/GHSA-cfh3-3jmp-rvhc), and decoder, font, drawing, PDF, and encoder findings through the [12.3.0 security fixes](https://pillow.readthedocs.io/en/stable/releasenotes/12.3.0.html). Checking `image.format` after an unrestricted `Image.open()` is weaker than restricting the permitted plugins at open time. Provider bytes now enter only JPEG/WEBP plugins; this also reduces exposure to unused formats. The 18 returned CVEs were CVE-2026-25990, 40192, 42308, 42309, 42310, 42311, 54058, 54059, 54060, 55379, 55380, 55798, 59197, 59198, 59199, 59200, 59204, and 59205 (all latter numeric suffixes have the `CVE-2026-` prefix). Several require APIs or formats the app never uses; they are not 18 demonstrated app exploits.

The six multipart CVEs were CVE-2026-24486, 40347, 42561, 53538, 53539, and 53540. The app does not expose their upload-directory, multipart, query-string parser, or direct `parse_form()` paths. Upstream describes [multipart preamble/epilogue DoS](https://github.com/Kludex/python-multipart/security/advisories/GHSA-mj87-hwqh-73pj), while the [negative Content-Length issue](https://github.com/Kludex/python-multipart/security/advisories/GHSA-v9pg-7xvm-68hf) explicitly excludes normal Starlette/FastAPI parsing. Removing the unused dependency eliminates these lock findings without introducing a parser change.

The [pytest changelog](https://docs.pytest.org/en/stable/changelog.html) documents CVE-2025-71176's insecure temporary-directory fix in 9.0.3. This was a local test-runner issue, not a public web endpoint.

## Worker scope and retained findings

Runpod and Flux Spark locks are installed-aware overlays, so querying only their overlay lines would miss inherited packages. The audit used the corresponding provenance files' complete `expected_inventory` maps. Qwen Spark has four guarded public package versions in `runtime.py` but no equivalent complete checked-in inventory; its row below is deliberately narrower.

| Recorded target | Packages queried | Packages with findings | Distinct package/CVE findings | Skipped packages |
| --- | ---: | ---: | ---: | ---: |
| Qwen Runpod expected inventory | 249 | 28 | 136 | 8 |
| Flux Runpod expected inventory | 204 | 16 | 51 | 8 |
| Flux Spark expected inventory | 247 | 34 | 164 | 13 |
| Qwen Spark guarded public pins only | 4 | 2 | 42 | 1 |

These counts are inventory matches, not established remotely exploitable paths. A shared CVE appearing in two target inventories is counted once per target. Skips include CUDA/vendor PyTorch variants, compiler/kernel packages, and Conda/OS distributions absent from PyPI. No substitution of a plain PyPI torch release was made to pretend those vendor builds were covered.

Material retained findings and their conditions:

| Finding | Recorded version / upstream fix | Current-path assessment and release condition |
| --- | --- | --- |
| vLLM [CVE-2026-22807: model `auto_map` code execution](https://github.com/vllm-project/vllm/security/advisories/GHSA-2pc9-4j83-qjmr), High 8.8 | Qwen Runpod 0.12.0 / 0.14.0 | Requires attacker-controlled model artifacts. `trust_remote_code=False` alone is insufficient. Keep the exact reviewed model snapshot and cache writable only by trusted operators; do not accept request-selected model paths. |
| vLLM [CVE-2026-47155: artifact pin decay](https://github.com/vllm-project/vllm/security/advisories/GHSA-3ww4-5jv9-j5gm), Moderate | Qwen Runpod 0.12.0 and Spark 0.20.1 / advisory lists 0.22.0 patched | Qwen passes a local snapshot path and enables Hub offline mode before engine initialization. Nested artifact resolution and the actual vendor image still require review. Upstream's affected-range text and patched version overlap at 0.22.0; do not choose a replacement GPU version from this table alone. |
| Diffusers model-loading code execution advisories | Flux 0.37.1 / advisory feed lists 0.38.0 | [TOCTOU advisory](https://github.com/huggingface/diffusers/security/advisories/GHSA-7wx4-6vff-v64p) explicitly excludes commit-pinned/local-directory loading, as used here. [Custom-component advisory](https://github.com/huggingface/diffusers/security/advisories/GHSA-98h9-4798-4q5v) requires untrusted executable components. Its primary affected range says `<0.37.1` while the feed flags 0.37.1 and lists 0.38.0 fixed: reconcile this metadata before any upgrade decision. **October 20 correction:** a fixed pipeline class alone does not prevent all local dynamic-module loading. New source additionally checks exact metadata hashes and rejects Python files/unsafe links; the published images lack those checks. |
| Transformers/model/checkpoint, cache, notebook, Ray, and vLLM serving findings | Versions and advisory IDs retained in local JSON | Worker contracts accept bounded text and fixed presets, not user-selected checkpoints, pickles, URLs, embedding tensors, regex grammars, or API-server options. Qwen uses the offline `LLM` API with prefix caching disabled; it does not launch the OpenAI API server. The Qwen3 text-only model does not exercise the Qwen-VL image/video grid paths. No request-reachable exploit from these groups was established by this bounded review. |

## Cloud release gates

Before enabling live cloud workers, record the exact image digest and full built-image inventory/SBOM, scan OS/native layers and vendor builds with a source that supports them, and triage retained findings against that artifact. A pristine Python app audit does not clear a worker image.

Keep model caches and deployment configuration under trusted operator control. Pin and prefetch the reviewed checkpoint; enforce the existing offline/local loading boundaries. After the dependency follow-up, source authenticates seventeen small metadata files across both workers, including their reviewed weight indexes and chat templates. It does not authenticate all weight/tokenizer bytes or eliminate concurrent cache mutation between check and load. Do not expose inherited Jupyter, notebook, Ray dashboards, or a vLLM API server; verify actual image processes and published ports before release. Keep the documented worker authentication/network boundary intact.

The dependency follow-up changes runtime packages while preserving model revisions, precision and GPU targets. It records new-image inventories/scans and Spark inference checks. Retained upstream findings and native Runpod SM120 GPU/queue validation remain release considerations; old published-image evidence cannot validate the new artifacts.
