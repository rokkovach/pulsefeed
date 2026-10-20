"""Bundle only the reviewed handouts, slides and diagrams for sharing."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "output/pulsefeed-review-pack.zip"

# An explicit allowlist keeps local logs, private configuration and application
# data out of the handoff, regardless of what else is in the workspace.
FILES = {
    "START-HERE.txt": "docs/showcase/share-notes.txt",
    "pulsefeed-one-pager.pdf": "output/pdf/pulsefeed-one-pager.pdf",
    "pulsefeed-review.pdf": "output/pdf/pulsefeed-review.pdf",
    "pulsefeed-review.docx": "output/docx/pulsefeed-review.docx",
}
for diagram in ("architecture", "request-lifecycle", "local-vs-cloud"):
    for extension in ("svg", "png", "mmd"):
        filename = f"{diagram}.{extension}"
        FILES[f"diagrams/{filename}"] = f"docs/diagrams/{filename}"

for filename in ("review-architecture.svg", "review-architecture.png", "reader.png", "admin-overview.png", "admin-models.png"):
    FILES[f"assets/{filename}"] = f"docs/showcase/assets/{filename}"


def main() -> None:
    missing = [source for source in FILES.values() if not (ROOT / source).is_file()]
    if missing:
        raise SystemExit("Missing reviewed output: " + ", ".join(missing))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".zip.tmp")
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
            for destination, source in FILES.items():
                archive.write(ROOT / source, destination)
        with ZipFile(temporary) as archive:
            bad_member = archive.testzip()
            if bad_member:
                raise SystemExit(f"Archive integrity check failed: {bad_member}")
        # Replace the previous pack only after every reviewed file is included
        # and its stored checksum passes.
        temporary.replace(OUTPUT)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Created {OUTPUT.name}: {len(FILES)} files")


if __name__ == "__main__":
    main()
