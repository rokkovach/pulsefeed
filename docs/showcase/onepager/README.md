# Build the one-page handout

`../one-pager.md` is the editable prose source. The builder uses ReportLab and its bundled Bitstream Vera fonts; it does not need external assets, the app, model workers or a GPU.

From the repository root, run with a Python environment containing `reportlab`:

```sh
python3 docs/showcase/onepager/build.py
```

The default output is `output/pdf/pulsefeed-one-pager.pdf`. Paths can be overridden with `--source`, `--output` and `--font-dir`. The default repository root is resolved relative to the script. The invariant PDF setting removes build-time timestamp variance. The builder fails if text falls below its A4 portrait layout boundary.

To inspect a rendered page with Poppler:

```sh
pdftoppm -r 150 -png -singlefile output/pdf/pulsefeed-one-pager.pdf /tmp/pulsefeed-one-pager
```

Keep generated previews and QA notes in the ignored `integration/artifacts/documentation-2026-10-20/onepager/` directory.
