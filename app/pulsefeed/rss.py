"""Bounded RSS/Atom retrieval: validated DNS targets are pinned to the connection."""
import asyncio
import hashlib
import html
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import socket
import ssl
import sys
import threading
import time
import unicodedata
import httpx
from urllib.parse import urlsplit, urljoin
from defusedxml import ElementTree
from .errors import AppError

MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_TITLE_CHARACTERS = 500
MAX_EXCERPT_CHARACTERS = 4000
MAX_ARTICLE_CHARACTERS = 4000
FETCH_DEADLINE_SECONDS = 10
MAX_FETCH_OUTPUT_BYTES = 512 * 1024
# Nonblocking admission bounds subprocesses across event loops in this app process.
# A request never queues behind a slow DNS lookup or an uncooperative feed server.
_FETCH_SLOTS = threading.BoundedSemaphore(4)
_FETCH_CLEANUPS = set()
_FETCH_ERRORS = {
    "INVALID_FEED": "This URL did not return a safe RSS or Atom feed.",
    "UNSAFE_FEED": "Private, local and metadata-service feed destinations are blocked.",
    "FEED_UNREACHABLE": "The feed could not be fetched securely.",
    "FEED_TIMEOUT": "The feed exceeded its 10-second deadline.",
    "FEED_REDIRECT": "The feed exceeded the redirect limit.",
    "FEED_TOO_LARGE": "Feed exceeds the 2 MiB limit.",
    "EMPTY_FEED": "This feed has no readable articles. Try another RSS or Atom feed.",
}


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0
    def handle_starttag(self, tag, attrs):
        tag = tag.rsplit(":", 1)[-1]
        if tag in ("script", "style"):
            self.hidden += 1
        elif not self.hidden and tag in ("p", "br", "div", "li", "section", "article", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.parts.append(" ")
    def handle_endtag(self, tag):
        tag = tag.rsplit(":", 1)[-1]
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        if not self.hidden:
            self.parts.append(" ")
    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def normalize(text):
    # Decode before stripping markup. Decoding afterward can turn nested escaped
    # HTML back into tags and make an otherwise valid feed fail worker validation.
    # A fixed nesting cap bounds work on hostile entity chains.
    for _ in range(8):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    else:
        if html.unescape(text) != text:
            raise AppError("INVALID_FEED", "The feed contains excessively encoded article text.")
    # Legitimate XML CDATA was already consumed by ElementTree. Reject marked
    # sections remaining in decoded article HTML explicitly: HTMLParser patch
    # versions differ in whether malformed sections raise or become comments.
    if "<![" in text:
        raise AppError("INVALID_FEED", "The feed contains malformed article text.")
    parser = PlainText()
    try:
        parser.feed(text)
        parser.close()
    except AssertionError as exc:
        # HTMLParser rejects malformed marked sections even when the outer XML
        # safely escaped them. They are invalid feed text, not a server failure.
        raise AppError("INVALID_FEED", "The feed contains malformed article text.") from exc
    plain = unicodedata.normalize("NFC", "".join(parser.parts))
    plain = "".join(char for char in plain if unicodedata.category(char) not in {"Cs", "Cc"} or char.isspace())
    return " ".join(plain.split())


def public_url(url):
    if not isinstance(url, str) or len(url) > 2048:
        raise AppError("INVALID_FEED", "Enter a public HTTP(S) feed URL.")
    try:
        url.encode("utf-8")
    except UnicodeError as exc:
        raise AppError("INVALID_FEED", "The feed URL contains invalid Unicode text.") from exc
    try:
        parsed = urlsplit(url)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError as exc:
        raise AppError("INVALID_FEED", "The feed URL is malformed.") from exc
    if parsed.scheme not in ("http", "https") or not hostname or parsed.username or parsed.password:
        raise AppError("INVALID_FEED", "Only public HTTP(S) URLs without credentials are supported.")
    if port not in (None, 80, 443):
        raise AppError("INVALID_FEED", "Only standard HTTP(S) ports are supported.")
    return parsed


def source_hostname(link):
    """A bounded display label only; this never resolves or fetches the source."""
    if not isinstance(link,str) or len(link)>2048:
        return ""
    try:
        split = urlsplit(link)
        if not split.hostname:
            return ""
        _ = split.port  # Reject malformed bracket/port syntax before display parsing.
        parsed = httpx.URL(link)
        if parsed.scheme not in ("http","https") or not parsed.raw_host:
            return ""
        host = parsed.raw_host.decode("ascii").lower()
        if ":" in host:
            host = "["+str(ipaddress.IPv6Address(host))+"]"
        return host.removeprefix("www.")
    except (ValueError,UnicodeError,httpx.InvalidURL):
        return ""



def canonical_feed_id(url):
    """Shared quota identity; preserve path/query spelling and never expose this digest."""
    try:
        parsed = public_url(url)
        host = parsed.hostname.lower()
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            host = host.encode("idna").decode("ascii")
        if ":" in host:
            host = "[" + host + "]"
        port = parsed.port
        default = 443 if parsed.scheme == "https" else 80
        authority = host + (":" + str(port) if port is not None and port != default else "")
        canonical = parsed.scheme.lower() + "://" + authority + (parsed.path or "/")
        if parsed.query:
            canonical += "?" + parsed.query
    except (AppError, ValueError, UnicodeError):
        # Explicit local demo and legacy test/source labels are not network destinations.
        canonical = "legacy:" + url.strip()
    return hashlib.sha256(canonical.encode()).hexdigest()


def validate_address(address):
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError as exc:
        raise AppError("UNSAFE_FEED", "Feed destination is invalid.") from exc
    if not ip.is_global or ip.is_multicast or ip.is_reserved or (getattr(ip, "ipv4_mapped", None) and not ip.ipv4_mapped.is_global):
        raise AppError("UNSAFE_FEED", "Private, local and metadata-service feed destinations are blocked.")
    return ip


def resolve_targets(host, port):
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise AppError("FEED_UNREACHABLE", "The feed hostname could not be resolved.") from exc
    addresses = list(dict.fromkeys(row[4][0] for row in rows))
    if not addresses:
        raise AppError("FEED_UNREACHABLE", "The feed hostname has no addresses.")
    for address in addresses:
        validate_address(address)
    return addresses


class PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host, port, address, secure, timeout):
        super().__init__(host, port=port, timeout=timeout)
        self.address, self.secure = address, secure
    def connect(self):
        validate_address(self.address)
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        try:
            validate_address(self.sock.getpeername()[0])
            if self.secure:
                self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)
        except BaseException:
            self.sock.close()
            raise


def fetch_bytes(url, resolver=resolve_targets, connection_factory=PinnedConnection):
    started = time.monotonic()
    for redirect in range(4):
        parsed = public_url(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = resolver(parsed.hostname, port)
        for address in addresses:
            validate_address(address)
        if not addresses:
            raise AppError("FEED_UNREACHABLE", "The feed has no public connection target.")
        remaining = 10 - (time.monotonic() - started)
        if remaining <= 0:
            raise AppError("FEED_TIMEOUT", "The feed exceeded its 10-second deadline.")
        conn = connection_factory(parsed.hostname, port, addresses[0], parsed.scheme == "https", remaining)
        try:
            conn.request("GET", (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""), headers={"User-Agent": "PulseFeed/0.1 RSS preview", "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml", "Accept-Encoding": "identity"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                if redirect == 3 or not response.getheader("Location"):
                    raise AppError("FEED_REDIRECT", "The feed exceeded the redirect limit.")
                try:
                    url = urljoin(url, response.getheader("Location"))
                except ValueError as exc:
                    raise AppError("INVALID_FEED", "The feed redirect URL is malformed.") from exc
                continue
            if response.status != 200:
                raise AppError("FEED_UNREACHABLE", "The feed server did not return a successful response.")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise AppError("INVALID_FEED", "Compressed feed transport is unsupported.")
            length = response.getheader("Content-Length")
            if length:
                try:
                    if int(length) > MAX_FEED_BYTES:
                        raise AppError("FEED_TOO_LARGE", "Feed exceeds the 2 MiB limit.")
                except ValueError as exc:
                    raise AppError("INVALID_FEED", "Invalid feed response length.") from exc
            content = bytearray()
            while True:
                remaining = 10 - (time.monotonic() - started)
                if remaining <= 0:
                    raise AppError("FEED_TIMEOUT", "The feed exceeded its 10-second deadline.")
                if conn.sock:
                    conn.sock.settimeout(remaining)
                chunk = response.read(min(65536, MAX_FEED_BYTES + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > MAX_FEED_BYTES:
                    raise AppError("FEED_TOO_LARGE", "Feed exceeds the 2 MiB limit.")
            return bytes(content), url
        except (TimeoutError, socket.timeout) as exc:
            raise AppError("FEED_TIMEOUT", "The feed exceeded its 10-second deadline.") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise AppError("FEED_UNREACHABLE", "The feed could not be fetched securely.") from exc
        finally:
            conn.close()
    raise AppError("FEED_REDIRECT", "The feed exceeded the redirect limit.")


def safe_link(value, base):
    try:
        url = urljoin(base, value)
        public_url(url)
        return url
    except (AppError, ValueError):
        return ""


def parse_feed(content, base):
    if len(content) > MAX_FEED_BYTES:
        raise AppError("FEED_TOO_LARGE", "Feed exceeds the 2 MiB limit.")
    try:
        root = ElementTree.fromstring(content, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except Exception as exc:
        raise AppError("INVALID_FEED", "This URL did not return a safe RSS or Atom feed.") from exc
    def qualified(element):
        if element.tag.startswith("{"):
            namespace, local = element.tag[1:].split("}", 1)
            return namespace, local
        return "", element.tag

    atom_namespaces = ("http://www.w3.org/2005/Atom", "http://purl.org/atom/ns#")
    rss1_namespace = "http://purl.org/rss/1.0/"
    root_namespace, root_name = qualified(root)
    if root_name == "rss" and not root_namespace:
        namespace, item_name = "", "item"
        channels = [node for node in root if qualified(node) == (namespace, "channel")]
        if len(channels) != 1:
            raise AppError("INVALID_FEED", "The RSS feed must contain one channel.")
        channel = channels[0]
        entries = [node for node in channel if qualified(node) == (namespace, item_name)][:10]
    elif root_name == "feed" and root_namespace in atom_namespaces:
        namespace, channel = root_namespace, root
        entries = [node for node in root if qualified(node) == (namespace, "entry")][:10]
    elif (root_namespace, root_name) == ("http://www.w3.org/1999/02/22-rdf-syntax-ns#", "RDF"):
        namespace = rss1_namespace
        channels = [node for node in root if qualified(node) == (namespace, "channel")]
        if len(channels) != 1:
            raise AppError("INVALID_FEED", "The RSS feed must contain one channel.")
        channel = channels[0]
        entries = [node for node in root if qualified(node) == (namespace, "item")][:10]
    else:
        raise AppError("INVALID_FEED", "This URL did not return a supported RSS or Atom feed.")

    def tag(element):
        child_namespace, name = qualified(element)
        # RSS content:encoded is a common, intentional extension. Foreign
        # elements with familiar core names cannot masquerade as feed content.
        if child_namespace == namespace or (child_namespace == "http://purl.org/rss/1.0/modules/content/" and name == "encoded"):
            return name
        return ""
    def text(node, names):
        for child in node:
            if tag(child) in names:
                return "".join(child.itertext())
        return ""
    def plain_text(node, names):
        # Prefer a description/summary over full content regardless of XML order,
        # but let an empty or script-only description fall through to useful text.
        # Serializing the subtree retains XHTML block boundaries and hidden tags;
        # itertext() would merge paragraphs and expose script/style contents.
        for name in names:
            for child in node:
                if tag(child) == name:
                    # Parse the field's contents, not the RSS wrapper. Recent
                    # HTMLParser releases treat <title> as HTML RCDATA, which
                    # would preserve nested escaped script/style as literal text.
                    # Element serialization retains child tails and XHTML blocks.
                    contents = html.escape(child.text or "", quote=False)
                    contents += "".join(ElementTree.tostring(part, encoding="unicode") for part in child)
                    value = normalize(contents)
                    if value:
                        return value
        return ""
    items = []
    for node in entries:
        source_title = plain_text(node, ("title",))
        title = source_title[:MAX_TITLE_CHARACTERS].strip()
        if not title:
            continue
        excerpt = plain_text(node, ("description", "summary", "content", "encoded"))[:MAX_EXCERPT_CHARACTERS].strip()
        link = text(node, ("link",))
        for child in node:
            if tag(child) == "link" and child.attrib.get("rel", "alternate") == "alternate":
                link = child.attrib.get("href", link)
                break
        link = safe_link(link, base)
        article_title = title
        identity = text(node, ("guid", "id")) or link or source_title
        items.append({"guid": hashlib.sha256(identity.encode()).hexdigest(), "title": title, "excerpt": excerpt,
                      "link": link, "published": text(node, ("pubDate", "published", "updated"))[:200],
                      "article": {"title": article_title, "excerpt": excerpt[:max(0, MAX_ARTICLE_CHARACTERS-len(article_title))].strip()}})
    if not items:
        raise AppError("EMPTY_FEED", "This feed has no readable articles. Try another RSS or Atom feed.")
    return {"title": plain_text(channel, ("title",))[:200] or urlsplit(base).hostname, "items": items}


def _fetch_command():
    # Start a fresh interpreter instead of forking a multithreaded web process.
    # The URL is delivered over stdin, never in process arguments or logs.
    return sys.executable, "-m", "pulsefeed.rss", "--fetch"


async def _fetch_output(spawn, url):
    process = await asyncio.shield(spawn)
    process.stdin.write(url.encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()
    output = bytearray()
    while True:
        chunk = await process.stdout.read(min(65536, MAX_FETCH_OUTPUT_BYTES + 1 - len(output)))
        if not chunk:
            break
        output.extend(chunk)
        if len(output) > MAX_FETCH_OUTPUT_BYTES:
            raise AppError("INVALID_FEED", "The feed response could not be processed safely.")
    await process.wait()
    try:
        result = json.loads(output)
    except (ValueError, UnicodeError) as exc:
        raise AppError("FEED_UNREACHABLE", _FETCH_ERRORS["FEED_UNREACHABLE"]) from exc
    if process.returncode != 0 or not isinstance(result, dict):
        raise AppError("FEED_UNREACHABLE", _FETCH_ERRORS["FEED_UNREACHABLE"])
    if result.get("error"):
        code = result["error"] if result["error"] in _FETCH_ERRORS else "FEED_UNREACHABLE"
        raise AppError(code, _FETCH_ERRORS[code])
    if set(result) != {"feed"} or not isinstance(result["feed"], dict):
        raise AppError("FEED_UNREACHABLE", _FETCH_ERRORS["FEED_UNREACHABLE"])
    return result["feed"]


async def _reap_fetch(spawn):
    process = None
    drain = None
    reaped = False
    try:
        try:
            process = await asyncio.shield(spawn)
        except OSError:
            reaped = True  # The interpreter never started.
            return
        async def discard_output():
            # A child blocked on a full stdout pipe must still be reapable after
            # termination. Discard in fixed chunks instead of accumulating data.
            while await process.stdout.read(65536):
                pass
        drain = asyncio.create_task(discard_output())
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        await drain
        reaped = True
    except asyncio.CancelledError:
        # Application shutdown can cancel tracked cleanup tasks too. Finish the
        # OS cleanup before propagating it, rather than abandoning a live child.
        process = process or await asyncio.shield(spawn)
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        if drain is not None:
            # asyncio.run cancels *all* outstanding tasks at shutdown, including
            # the drain above. Recreate its work inline after stopping it; waiting
            # on the canceled drain or on a full PIPE would strand the child wait.
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
        while await process.stdout.read(65536):
            pass
        await process.wait()
        reaped = True
        raise
    finally:
        # Keep admission held even after caller cancellation until the OS child
        # has been reaped. Killing DNS/read work cannot strand executor threads.
        if reaped:
            _FETCH_SLOTS.release()


async def fetch_feed(url):
    public_url(url)
    if not _FETCH_SLOTS.acquire(blocking=False):
        raise AppError("FEED_BUSY", "Feed retrieval is busy. Try again shortly.", 429, retry_after=1)
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        *_fetch_command(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=65536))
    try:
        # Includes process startup, DNS, redirects, response headers/body and XML
        # parsing. Socket idle timeouts alone do not stop a trickling response.
        return await asyncio.wait_for(_fetch_output(spawn, url), timeout=FETCH_DEADLINE_SECONDS)
    except asyncio.TimeoutError as exc:
        raise AppError("FEED_TIMEOUT", _FETCH_ERRORS["FEED_TIMEOUT"]) from exc
    except (OSError, BrokenPipeError, ConnectionResetError) as exc:
        raise AppError("FEED_UNREACHABLE", _FETCH_ERRORS["FEED_UNREACHABLE"]) from exc
    finally:
        cleanup = asyncio.create_task(_reap_fetch(spawn))
        _FETCH_CLEANUPS.add(cleanup)
        cleanup.add_done_callback(_FETCH_CLEANUPS.discard)
        try:
            # Bound request cleanup latency; an exceptionally slow OS reap keeps
            # its slot and continues in the tracked cleanup task after this grace.
            await asyncio.wait_for(asyncio.shield(cleanup), timeout=1.5)
        except asyncio.TimeoutError:
            pass


def _fetch_child():
    try:
        # 2,048 Unicode URL characters fit in 8,192 UTF-8 bytes. One sentinel
        # byte lets public_url reject overlong input without an unbounded read.
        raw = sys.stdin.buffer.read(8193)
        url = raw.decode("utf-8")
        public_url(url)
        content, final_url = fetch_bytes(url)
        result = {"feed": parse_feed(content, final_url)}
    except AppError as exc:
        result = {"error": exc.code if exc.code in _FETCH_ERRORS else "FEED_UNREACHABLE"}
    except Exception:
        # Never transport exception strings, URLs, DNS answers or local paths.
        result = {"error": "FEED_UNREACHABLE"}
    output = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(output) > MAX_FETCH_OUTPUT_BYTES:
        output = b'{"error":"FEED_TOO_LARGE"}'
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()


def demo_feed():
    return parse_feed(b'<rss version="2.0"><channel><title>PulseFeed demo feed (synthetic)</title><item><guid>demo-1</guid><title>A new community garden opens</title><description>Neighbors plant a colorful garden on an unused city lot.</description><link>https://example.com/garden</link></item><item><guid>demo-2</guid><title>Scientists explore ocean habitats</title><description>A research team maps the sea floor and observes marine wildlife.</description><link>https://example.com/ocean</link></item><item><guid>demo-3</guid><title>A library welcomes its readers</title><description>A restored library opens bright reading spaces to the community.</description><link>https://example.com/library</link></item></channel></rss>', 'https://example.com/feed')


if __name__ == "__main__" and sys.argv[1:] == ["--fetch"]:
    _fetch_child()
