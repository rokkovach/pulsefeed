"""Content-versioned URLs for public, locally bundled static assets."""
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
import re
import httpx

_STATIC = Path(__file__).with_name("static")


def reader_date(value: str) -> str:
    """A short, deterministic UTC label; never expose an unparsed source date."""
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        return ""
    try:
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            parsed = parsedate_to_datetime(value.strip())
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        month = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")[parsed.month - 1]
        return f"{parsed.day} {month} {parsed.year}"
    except (ValueError, TypeError, OverflowError):
        return ""


def source_origin(value: str) -> str:
    """A safe website link for display, without fetching or resolving its host."""
    if not isinstance(value, str) or len(value) > 2048 or any(char.isspace() or ord(char) < 32 or char == "\\" for char in value):
        return ""
    try:
        split = urlsplit(value)
        if split.username is not None or split.password is not None:
            return ""
        _ = split.port
        parsed = httpx.URL(value)
        if parsed.scheme not in ("http", "https") or not parsed.raw_host:
            return ""
        return parsed.scheme + "://" + parsed.netloc.decode("ascii")
    except (ValueError, UnicodeError, httpx.InvalidURL):
        return ""


def reader_created(value: float) -> str:
    """Human-readable view creation time with an explicit, stable timezone."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    try:
        parsed = datetime.fromtimestamp(value, timezone.utc)
        return f"{reader_date(parsed.isoformat())} at {parsed:%H:%M} UTC"
    except (ValueError, OverflowError, OSError):
        return ""


def reader_excerpt(value: str) -> str:
    """Remove standalone terminal RSS comment labels, preserving ordinary prose."""
    if not isinstance(value, str):
        return ""
    label = r"comments(?:[\t ]+https?://[^\s]+)?[\t ]*"
    if re.fullmatch(r"\s*" + label, value, re.IGNORECASE):
        return ""
    return re.sub(r"(?:(?<=[.!?])[\t ]+|[\t ]*[\r\n]+[\t ]*)" + label + r"$", "", value, flags=re.IGNORECASE).rstrip()


def utc_timestamp(value: float | str) -> str:
    """Stable server/browser expiry label without a locale-dependent hydration swap."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return value


@lru_cache(maxsize=64)
def _fingerprint(name: str, modified: int, size: int) -> str:
    return sha256((_STATIC / name).read_bytes()).hexdigest()[:16]


def _version(name: str) -> str | None:
    # Only direct public files are used; never resolve a supplied path outside static.
    if not name or Path(name).name != name or "\\" in name:
        return None
    file = _STATIC / name
    try:
        stat = file.stat()
        if not file.is_file():
            return None
        return _fingerprint(name, stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def asset_url(name: str) -> str:
    version = _version(name)
    if version is None:
        raise ValueError("Unknown public static asset")
    return f"/static/{name}?v={version}"


def static_cache_control(path: str, version: str | None) -> str:
    """Call only for /static requests; protected responses must remain no-store."""
    current = _version(path.removeprefix("/static/")) if path.startswith("/static/") else None
    if current is None:
        return "no-store"
    if version == current:
        return "public, max-age=31536000, immutable"
    return "public, max-age=0, must-revalidate"


def stamp_css_assets() -> None:
    """Run before compiling Tailwind after changing the bundled font or illustration."""
    source = _STATIC / "tailwind.input.css"
    css = source.read_text()
    for name in ("Silkscreen-Regular.ttf", "pixel-garden.svg"):
        css = re.sub(r"/static/" + re.escape(name) + r"(?:\?v=[a-f0-9]+)?", asset_url(name), css)
    source.write_text(css)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp-css", action="store_true", required=True)
    parser.parse_args()
    stamp_css_assets()
