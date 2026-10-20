"""TEST ONLY: isolated synthetic browser QA on 127.0.0.1:8018.

Run with app/.venv/bin/python integration/ephemeral_probe.py. The default fixture
starts empty: enter FEED_URL to observe the four-second RSS loader. Add
--near-expiry to preseed a completed view with an exact one-hour lifetime and
sixty seconds remaining. Login/view links are written only to a private file.
Nothing is created, seeded, or served when this module is imported. Never deploy
this launcher; it deliberately uses fake inference and a test-only RSS fetcher.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time

BASE_URL = "http://127.0.0.1:8018"
FEED_URL = "https://example.com/pulsefeed-probe.xml"
RSS_DELAY_SECONDS = 4


async def prepare_fixture(state_dir: Path, *, near_expiry=False, expires_in=60, layout_delay_ms=0):
    """Construct one private, disposable fixture; never read operator credentials."""
    # Late imports keep importing this test launcher inert and independent of cwd.
    root = Path(__file__).resolve().parents[1]
    if str(root / "app") not in sys.path:
        sys.path.insert(0, str(root / "app"))
    from pulsefeed.auth import issue_link, seed_account
    from pulsefeed.config import Config
    from pulsefeed.errors import AppError
    from pulsefeed.main import create_app
    from pulsefeed.provider import FakeRunpodAdapter
    from pulsefeed.rss import demo_feed

    fixed_feed = demo_feed()

    async def delayed_feed(url):
        if url != FEED_URL:
            raise AppError("INVALID_FEED", "This test fixture accepts only its example.com feed URL.")
        await asyncio.sleep(RSS_DELAY_SECONDS)
        return copy.deepcopy(fixed_feed)

    config = Config(database_path=str(state_dir / "fixture.sqlite3"), base_url=BASE_URL,
                    session_secret=secrets.token_urlsafe(48), fake_mode=True,
                    cookie_secure=False, coordinator_interval=0.25,
                    public_access_enabled=False)
    provider = FakeRunpodAdapter()
    app = create_app(config=config, provider=provider, feed_fetcher=delayed_feed)
    db, service = app.state.db, app.state.service
    owner = seed_account(db, "Synthetic browser QA", "admin")
    view_url = None
    view_expires_at = None
    if near_expiry:
        provider.delay = 0
        data = service.view_feed(owner, FEED_URL, "synthetic-expiry-fixture", copy.deepcopy(fixed_feed))
        for _ in range(64):
            await app.state.coordinator.tick()
            if not db.one("SELECT id FROM batches WHERE state='active'"):
                break
        runs = db.all("SELECT state FROM runs")
        if len(runs) != 3 or any(run["state"] != "completed" for run in runs):
            raise RuntimeError("Synthetic expiry fixture failed to complete its three runs.")
        if len(provider.submissions) != 6:
            raise RuntimeError("Synthetic expiry fixture has inconsistent provider jobs.")
        # Age only this disposable row. Production clocks and app routes stay intact.
        view_expires_at = time.time() + expires_in
        db.execute("UPDATE feed_views SET created=?,expires_at=? WHERE id=?",
                   (view_expires_at - 3600, view_expires_at, data["view"]["id"]))
        row = db.one("SELECT created,expires_at FROM feed_views WHERE id=?", (data["view"]["id"],))
        if row["expires_at"] - row["created"] != 3600:
            raise RuntimeError("Expiry fixture must retain an exact one-hour lifetime.")
        view_url = BASE_URL + data["view"]["url"]
    provider.delay = 0.15

    if layout_delay_ms:
        # Reuse only the test observer; do not call its environment-owned app factory.
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from fastapi import Request
        from fastapi.responses import Response
        from integration.layout_probe import PROBE

        @app.get("/__layout_probe.js")
        async def probe_script():
            return Response(PROBE, media_type="application/javascript", headers={"Cache-Control": "no-store"})

        @app.middleware("http")
        async def layout_probe(request: Request, call_next):
            path = request.url.path
            if request.method == "GET" and (path.startswith("/api/") or
                                             path.endswith(("app.js", ".ttf", ".woff2"))):
                await asyncio.sleep(layout_delay_ms / 1000)
            response = await call_next(request)
            if response.headers.get("content-type", "").startswith("text/html"):
                body = b"".join([chunk async for chunk in response.body_iterator])
                body = body.replace(b"<head>", b'<head><script src="/__layout_probe.js"></script>', 1)
                headers = dict(response.headers)
                headers["content-length"] = str(len(body))
                return Response(body, status_code=response.status_code, headers=headers)
            return response

    private_links = state_dir / "private-links.json"
    links = {"test_only": True, "login_url": issue_link(db, config, owner),
             "reader_url": BASE_URL + "/", "feed_url": FEED_URL,
             "view_url": view_url, "admin_url": BASE_URL + "/admin"}
    fd = os.open(private_links, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(links, stream, indent=2)
        stream.write("\n")
    metadata = {"test_only": True, "mode": "synthetic-demo", "base_url": BASE_URL,
                "health_url": BASE_URL + "/health", "feed_url": FEED_URL,
                "rss_delay_seconds": RSS_DELAY_SECONDS, "private_links_file": str(private_links),
                "layout_delay_ms": layout_delay_ms,
                "fixture": "completed-near-expiry" if near_expiry else "empty-reader",
                "view_ttl_seconds": 3600 if near_expiry else None,
                "view_expires_at": view_expires_at}
    return app, metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--near-expiry", action="store_true",
                        help="Preseed three completed synthetic images in a nearly expired view.")
    parser.add_argument("--expires-in", type=int, default=60,
                        help="Seconds remaining for --near-expiry (default 60; 1..120). TTL stays 3600.")
    parser.add_argument("--layout-delay-ms", type=int, default=0,
                        help="Enable the test CLS observer and delay scripts/fonts/GET API reads (1..3000ms).")
    parser.add_argument("--state-dir", type=Path,
                        help="New private fixture directory; refuses any existing directory.")
    parser.add_argument("--prepare-only", action="store_true",
                        help="Validate and seed the fixture without opening a listening socket.")
    args = parser.parse_args(argv)
    if not 1 <= args.expires_in <= 120:
        parser.error("--expires-in must be between 1 and 120 seconds")
    if not 0 <= args.layout_delay_ms <= 3000:
        parser.error("--layout-delay-ms must be between 0 and 3000")
    if args.state_dir:
        state_dir = args.state_dir.resolve()
        state_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    else:
        state_dir = Path(tempfile.mkdtemp(prefix="pulsefeed-browser-qa-"))
    app, metadata = asyncio.run(prepare_fixture(state_dir, near_expiry=args.near_expiry,
                                               expires_in=args.expires_in,
                                               layout_delay_ms=args.layout_delay_ms))
    print(json.dumps(metadata), flush=True)  # No login token or session secret in stdout.
    if not args.prepare_only:
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=8018, workers=1, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
