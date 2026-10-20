"""Fetch one real public feed and validate its model inputs without GPU work.

Run explicitly; CI does not depend on publisher uptime or changing headlines.
Rotate --source hacker-news, bbc-england, nytimes across evaluation rounds.
Only aggregate lengths/counts are printed. No app session, saved settings, live
database, allowance reset, or provider credentials are used by this probe.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import uuid4

from pulsefeed.assets import reader_excerpt
from pulsefeed.rss import fetch_feed
from workers.qwen.handler import validate

SOURCES = {
    "hacker-news": "https://news.ycombinator.com/rss",
    "bbc-england": "https://feeds.bbci.co.uk/news/england/rss.xml",
    "nytimes": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
}


async def evaluate(source):
    started = time.monotonic()
    feed = await fetch_feed(SOURCES[source])
    items = feed["items"]
    assert 1 <= len(items) <= 10
    lengths = []
    for item in items:
        article = item["article"]
        now = int(time.time() * 1000)
        # Use the production input validator, not a duplicate approximation.
        # Calling validate does not load a tokenizer, model or Runpod SDK.
        validate({
            "schema_version": 1, "stage": "qwen",
            "run_id": str(uuid4()), "invocation_id": str(uuid4()),
            "deadline_unix_ms": now + 600_000,
            "article": article,
            "system_prompt": "Describe the article as an editorial illustration.",
            "max_new_tokens": 256,
        }, now)
        assert len(item["title"]) <= 500 and len(item["excerpt"]) <= 4000
        assert article["excerpt"] == item["excerpt"][:4000-len(article["title"])].strip()
        lengths.append({"title": len(item["title"]), "description": len(item["excerpt"]),
                        "model_context": sum(map(len, article.values())),
                        "card_description": bool(reader_excerpt(item["excerpt"]))})
    return {
        "result": "PASS", "source": source, "url": SOURCES[source],
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "articles": len(items), "qwen_inputs_validated": len(items),
        "cards_with_description": sum(row["card_description"] for row in lengths),
        "lengths": lengths, "model_calls": 0, "live_app_mutated": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=SOURCES, required=True)
    parser.add_argument("--output", type=Path, help="Optional aggregate evidence JSON path")
    args = parser.parse_args()
    result = asyncio.run(evaluate(args.source))
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
