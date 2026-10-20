# Handoff checks

Final local checks for the public reviewer handoff, 20 October 2026. No Railway redeployment or new inference was performed for this documentation pass.

| Check | Result |
| --- | --- |
| App, integration and local gateway suite | 737 passed, with 75 subtests; rerun with loopback socket access after the sandbox blocked transport fixtures |
| Added release-guard regression checks | 5 passed: private files, nested document token, encoded known value, URL fixture handling and no blanket fixture exemption |
| Qwen worker and SDK suite | 92 passed |
| FLUX worker suite | 173 passed |
| Browser behavior harnesses | All 9 passed |
| Six-page DOCX and exported PDF | All pages rendered and visually reviewed; text, images, tables and page count verified |
| One-page summary and technical diagrams | Rebuilt and visually reviewed |
| Documentation links | Current root/docs links resolve to local targets |
| Staged release guard | Common token formats, private runtime paths, nested archive contents and known private values checked |
| Decoded PDF text and metadata | Checked separately against token patterns and private values; screenshot PNG metadata also checked |
| Independent scan | Official Gitleaks 8.30.1 binary verified against the release checksum; scanned an export of the staged tree with archive and decoding depth 3 |

The independent scanner reported five reviewed `generic-api-key` false positives: three public FLUX tokenizer metadata hashes, one public Qwen tokenizer metadata hash, and a fixed request UUID in the browser regression harness. These are integrity pins and test data, not access credentials. No broad file exemption or removal of integrity checks was used to hide them.

Known-value comparison included the actual local and production secret values and private test sessions, without printing or publishing them. Ignored operator artifacts remain outside the public commit. This check is a release credential review, not a full penetration test or a clearance of upstream dependency findings. [Security boundaries](../SECURITY.md)

The current connected Runpod evidence is described in [the validation record](local-runpod-validation-2026-10-20.md); the latest Railway generation flow remains pending redeployment. The handoff is one initial commit, so no earlier development commits or local artifact history are included in the public repository.

## First hosted CI follow-up

The first GitHub run caught a stale SDK verifier constructor call and a newly published certifi CA-bundle advisory. The verifier now uses the cleaned handler interface; all 20 backport checks pass locally. The app lock now pins certifi 2026.10.19. The current Qwen image inherits an affected base version and needs a separately validated image rebuild before reviewer deployment; FLUX locks already use the patched version. [Advisory](https://github.com/advisories/GHSA-6q48-6c7w-9gw2) · [Release gates](release-status.md)

The clean hosted runner also exposed an HTMLParser behavior difference in CPython 3.11.17. RSS fields now pass only their inner content to the HTML normalizer, so an RSS title wrapper cannot trigger HTML title/RCDATA semantics. Malformed marked sections are rejected explicitly instead of relying on an older parser assertion. Existing feed rejection, script stripping and description tests cover these paths.

The advisory feed also reported eight Pillow 12.3 findings requiring 13.0.0. The app lock, project dependency and shared CPU test requirement now agree on Pillow 13.0.0; worker inventories already used 13.0.0. The advisory check stays strict, without ignored IDs. [Upstream Pillow release notes](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html)

After the final parser and dependency updates, the complete app/integration/local suite passed 742 checks with 75 subtests. The strict pip-audit scan of the app lock reported no known vulnerabilities. Pillow 13 decoded all ten retained real 512-square Runpod images. The 95 RSS rejection, sanitation and description checks also passed using the official CPython 3.11.17 HTMLParser and markup-base modules, independently of the older local interpreter. These app results do not clear the Qwen CA-bundle rebuild gate or retained worker/native findings.
