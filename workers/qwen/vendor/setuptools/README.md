# Candidate setuptools security backport

This directory retains the candidate `80.10.2+pulsefeed.ghsah35f.3` wheel, its MIT license, source patches and deterministic rebuild recipe. It is a candidate input; the production Dockerfiles and locks do not select it yet.

The upstream patch backports [GHSA-h35f-9h28-mq5c](https://github.com/pypa/setuptools/commit/dd9f436a36486b4cb8a4c70a2321548b0be09b8f) to `egg_info` matching. The separate local patch covers vendored distutils `FileList` glob comparisons. Both compare NFC forms while retaining original stored filenames; explicitly requested regular expressions retain their previous behavior. The wheel keeps truthful local version metadata and every dependency declaration. Its public version can still match advisory scanners; the patches and tests provide the remediation evidence.

The upstream `80.10.2` base includes fixed vendored `jaraco.context 6.1.0` and `wheel 0.46.3`. Those vendored payloads remain byte-identical to the official wheel; no separate vendor patches are applied.

Rebuild offline from the upstream wheel identified in `provenance.json`:

```sh
python3 workers/qwen/vendor/setuptools/build_backport.py \
  --source-wheel /path/to/setuptools-80.10.2-py3-none-any.whl \
  --output-dir /tmp/setuptools-backport
```

Use `--download-source` instead of `--source-wheel` to explicitly fetch the small public upstream wheel. Both paths verify its hash, both patch hashes and the exact rebuilt wheel hash. No installed environment is modified by the build script.

Run the regression suite inside an isolated environment containing the candidate wheel:

```sh
python3 -m venv /tmp/setuptools-backport-tests
/tmp/setuptools-backport-tests/bin/python -m pip install --no-index --no-deps \
  workers/qwen/vendor/setuptools/setuptools-80.10.2+pulsefeed.ghsah35f.3-py3-none-any.whl
/tmp/setuptools-backport-tests/bin/python workers/qwen/vendor/setuptools/verify_backport.py
```

The thirteen tests cover NFC/NFD exclusions through both implementations, ASCII behavior, stored filename preservation, explicit regex behavior and actual sdist creation. They also exercise the unmodified upstream vendored `jaraco.context 6.1.0` tar path/symlink filtering and `wheel 0.46.3` unpack permission handling using harmless local archives; no network is used. Full target-image dependency, import and GPU validation remain separate promotion gates.
