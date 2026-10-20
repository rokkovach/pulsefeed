#!/usr/bin/env python3
"""Print a fresh Runpod queue request as JSON; never submit a job or load a model."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import uuid

# Reuse the existing contract presets without importing either GPU runtime.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local.smoke import payload  # noqa: E402


def run_uuid(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--run-id must be a canonical hyphenated UUID") from None
    if len(value) != 36 or str(parsed) != value.lower():
        raise argparse.ArgumentTypeError("--run-id must be a canonical hyphenated UUID")
    return str(parsed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("qwen", "flux"))
    parser.add_argument("--run-id", type=run_uuid, help="Reuse a run UUID to link the two stages; invocation UUIDs are always fresh.")
    prompt_source = parser.add_mutually_exclusive_group()
    prompt_source.add_argument("--prompt-file", type=Path, help="FLUX only: UTF-8 final visual prompt, preserved verbatim (1–4,000 characters).")
    prompt_source.add_argument("--standalone-illustration", action="store_true", help="FLUX only: use the safe synthetic garden illustration prompt; this does not test Qwen linkage.")
    args = parser.parse_args()

    prompt = None
    if args.stage == "qwen":
        if args.prompt_file is not None or args.standalone_illustration:
            parser.error("Qwen uses a fixed synthetic article; FLUX prompt options are not accepted.")
    elif args.prompt_file is None and not args.standalone_illustration:
        parser.error("FLUX requires --prompt-file or --standalone-illustration.")
    elif args.prompt_file is not None:
        try:
            with args.prompt_file.open("rb") as source:
                raw = source.read(16_001)
            prompt = raw.decode("utf-8")
        except (OSError, UnicodeError):
            parser.error("--prompt-file must be a readable UTF-8 file.")
        if not prompt.strip() or len(prompt) > 4000:
            parser.error("--prompt-file must contain 1–4,000 characters of nonblank text.")

    request = payload(args.stage, args.run_id or str(uuid.uuid4()), prompt)
    json.dump({"input": request}, sys.stdout, ensure_ascii=False, allow_nan=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
