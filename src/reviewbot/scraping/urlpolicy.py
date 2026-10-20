"""Which URLs the server is allowed to fetch, and in what canonical form.

`POST /api/brands` takes a `website` from whoever is using the product, and a
scraped page hands us more URLs to follow. Both are attacker-controlled input to
a server-side fetch, which is the textbook setup for SSRF: on Railway that
reaches the container's own loopback, the platform's internal `*.railway.internal`
service mesh, and (on providers that expose one) the cloud metadata endpoint at
169.254.169.254 that hands out credentials.

So every fetch in `reviewbot.scraping` goes through `check()` first, and — this
is the part that is easy to get wrong — **again after every redirect**. A host
that resolves to a public address on the first check can redirect to
`http://169.254.169.254/`, and a DNS name can resolve to a public address when
validated and a private one when connected (DNS rebinding). `check()` therefore
validates the *resolved addresses*, and the fetchers are configured to surface
redirects rather than follow them blindly.

Two deliberate design choices:

* **Deny by default on anything ambiguous.** A hostname that will not resolve, a
  scheme that is not http(s), an address family we do not recognise: blocked. A
  false negative costs one page of review data; a false positive is a credential
  leak.
* **No allowlist of sites.** ReviewBay's whole premise is "any brand, anywhere on
  the public web", so an allowlist would break the product. The guard is about
  *where the packet goes*, not which company owns the page.

`ALLOW_PRIVATE_FETCH=1` disables the private-range checks. That exists for
local development against a fixture server on 127.0.0.1 and is refused unless
also running with `REVIEWBAY_ENV=dev`.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

log = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("http", "https")

# Hostnames that are never a legitimate review source and are classic SSRF
# targets. Checked by name as well as by resolved address, because a name can
# resolve differently than it reads.
_BLOCKED_HOST_SUFFIXES = (
    "localhost",
    ".localhost",
    ".local",
    ".internal",          # covers Railway's *.railway.internal mesh
    ".localdomain",
    ".home.arpa",
    ".cluster.local",     # Kubernetes service DNS
    ".svc",
)

# Cloud instance-metadata services. These are link-local (169.254.0.0/16) and so
# already covered by the address checks, but naming them makes the intent
# explicit and catches a provider that moves one onto a routable address.
_METADATA_HOSTS = (
    "169.254.169.254",          # AWS / Azure / DigitalOcean / OpenStack
    "metadata.google.internal",  # GCP
    "metadata.goog",
    "100.100.100.200",          # Alibaba Cloud
    "192.0.0.192",              # Oracle Cloud
)

# Ports that are not web ports. Blocking these stops the fetcher being used as a
# generic port prober against the internal network.
_ALLOWED_PORTS = {80, 443, 8080, 8443, 3000, 8000}

# Query parameters that carry no page identity, only campaign tracking. Dropped
# during canonicalization so the same review page found via two marketing links
# produces one URL, one `stable_id`, and one row.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "utm_social", "utm_brand",
    "gclid", "gclsrc", "dclid", "fbclid", "msclkid", "twclid", "igshid",
    "mc_cid", "mc_eid", "_ga", "_gl", "yclid", "ref_src", "ref_url",
    "s_kwcid", "spm", "scm", "share_id", "si",
})


class UrlNotAllowed(ValueError):
    """Raised when a URL fails policy. The message is safe to log, not to echo."""


@dataclass(frozen=True)
class UrlVerdict:
    """The outcome of a policy check."""

    allowed: bool
    url: str
    reason: str = ""
    host: str = ""
    addresses: tuple[str, ...] = ()

    def __bool__(self) -> bool:  # lets callers write `if verdict:`
        return self.allowed


def _dev_private_allowed() -> bool:
    return (
        os.environ.get("ALLOW_PRIVATE_FETCH") == "1"
        and os.environ.get("REVIEWBAY_ENV", "").lower() in ("dev", "development", "local", "test")
    )


def _is_blocked_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """A reason string when this address must not be fetched, else ''."""
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address (cloud metadata range)"
    if ip.is_private:
        return "private address"
    if ip.is_reserved:
        return "reserved address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address"
    # IPv4-mapped and translated IPv6 (::ffff:127.0.0.1, 64:ff9b::/96) would
    # otherwise sail past the checks above while reaching an IPv4 private host.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        inner = _is_blocked_address(mapped)
        return f"IPv4-mapped {inner}" if inner else ""
    sixtofour = getattr(ip, "sixtofour", None)
    if sixtofour is not None:
        inner = _is_blocked_address(sixtofour)
        return f"6to4-embedded {inner}" if inner else ""
    teredo = getattr(ip, "teredo", None)
    if teredo:
        for part in teredo:
            inner = _is_blocked_address(part)
            if inner:
                return f"Teredo-embedded {inner}"
    if isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local:
        return "site-local IPv6 address"
    return ""


def _resolve(host: str) -> list[str]:
    """Every address `host` resolves to. Raises on failure (deny by default)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise UrlNotAllowed(f"host {host!r} does not resolve ({type(exc).__name__})") from exc
    seen: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.append(addr)
    if not seen:
        raise UrlNotAllowed(f"host {host!r} resolved to no addresses")
    return seen


def check(url: str, *, resolve: bool = True) -> UrlVerdict:
    """Decide whether the server may fetch `url`.

    `resolve=False` skips the DNS step, for callers that only want the cheap
    syntactic checks (a pre-filter over a large list of discovered links before
    paying for lookups). The real fetch path must always resolve.

    Never raises: returns a verdict. Use `require()` when a raise is wanted.
    """
    if not url or not isinstance(url, str):
        return UrlVerdict(False, str(url or ""), "empty url")

    raw = url.strip()
    # A control character or whitespace inside a URL is how header/request
    # splitting gets attempted; reject rather than sanitize.
    if any(ch.isspace() or ord(ch) < 0x20 for ch in raw):
        return UrlVerdict(False, raw, "url contains whitespace or control characters")
    # Defence in depth for the OTHER end of the pipeline. A stored source_url is
    # rendered as an href on the dashboard and in the chat, so a quote or angle
    # bracket in it is an HTML-injection primitive. These characters must be
    # percent-encoded in a real URL, so rejecting them literally costs nothing
    # and means malformed-but-renderable URLs never reach the database.
    if any(ch in raw for ch in '"\'<>`\\'):
        return UrlVerdict(False, raw, "url contains characters that must be percent-encoded")

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        return UrlVerdict(False, raw, f"unparseable url ({exc})")

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return UrlVerdict(False, raw, f"scheme {scheme or '(none)'!r} is not http(s)")

    # Credentials in a URL are a redirect-laundering trick and have no place in a
    # public review page.
    if parts.username or parts.password:
        return UrlVerdict(False, raw, "url carries embedded credentials")

    try:
        host = (parts.hostname or "").lower()
    except ValueError as exc:
        return UrlVerdict(False, raw, f"invalid host ({exc})")
    if not host:
        return UrlVerdict(False, raw, "url has no host")

    try:
        port = parts.port
    except ValueError:
        return UrlVerdict(False, raw, "invalid port")

    dev = _dev_private_allowed()
    # The port allowlist stops the fetcher being used as a port prober against
    # the internal network. In dev mode the private-range checks are already
    # off, so the allowlist would only block a fixture server on an ephemeral
    # port without adding any protection.
    if port is not None and port not in _ALLOWED_PORTS and not dev:
        return UrlVerdict(False, raw, f"port {port} is not an allowed web port", host=host)

    if host in _METADATA_HOSTS:
        return UrlVerdict(False, raw, "cloud metadata endpoint", host=host)
    if not dev:
        for suffix in _BLOCKED_HOST_SUFFIXES:
            if host == suffix.lstrip(".") or host.endswith(suffix):
                return UrlVerdict(False, raw, f"internal hostname ({suffix})", host=host)

    # A bare IP in the URL is checked directly; no DNS involved.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        reason = "" if dev else _is_blocked_address(literal)
        if reason:
            return UrlVerdict(False, raw, reason, host=host, addresses=(host,))
        return UrlVerdict(True, raw, host=host, addresses=(host,))

    if not resolve:
        return UrlVerdict(True, raw, "syntactic checks only (not resolved)", host=host)

    try:
        addresses = _resolve(host)
    except UrlNotAllowed as exc:
        return UrlVerdict(False, raw, str(exc), host=host)

    if not dev:
        for addr in addresses:
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                return UrlVerdict(
                    False, raw, f"unrecognised address {addr!r}", host=host,
                    addresses=tuple(addresses),
                )
            reason = _is_blocked_address(ip)
            if reason:
                # ALL resolved addresses must be public: a name that resolves to
                # one public and one private address is a rebinding attempt.
                return UrlVerdict(
                    False, raw, f"{host} resolves to a {reason}", host=host,
                    addresses=tuple(addresses),
                )

    return UrlVerdict(True, raw, host=host, addresses=tuple(addresses))


def require(url: str, *, resolve: bool = True) -> str:
    """`check()`, raising `UrlNotAllowed` instead of returning a verdict."""
    verdict = check(url, resolve=resolve)
    if not verdict.allowed:
        raise UrlNotAllowed(f"refusing to fetch {url!r}: {verdict.reason}")
    return verdict.url


def is_allowed(url: str, *, resolve: bool = True) -> bool:
    return bool(check(url, resolve=resolve))


def canonical(url: str) -> str:
    """A stable form of a URL, for deduplication and for citation links.

    Lowercases scheme and host, drops the default port, strips tracking
    parameters and fragments, sorts the remaining query, and removes a trailing
    slash on a path. This is what stops the same review page from becoming three
    rows because it was discovered via three marketing links.

    A fragment is dropped EXCEPT when it is the only thing distinguishing two
    reviews: `app_store.py` deliberately anchors each review as
    `...?see-all=reviews#review-<id>`, so a `#`-anchored id is preserved.

    >>> canonical("HTTPS://WWW.Example.com:443/review/1/?utm_source=x&b=2&a=1#frag")
    'https://www.example.com/review/1?a=1&b=2'
    >>> canonical("https://apps.apple.com/us/app/id1?see-all=reviews#review-9")
    'https://apps.apple.com/us/app/id1?see-all=reviews#review-9'
    """
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return url.strip()

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not host:
        return url.strip()
    netloc = host
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    query = urlencode(
        sorted(
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS
        )
    )

    # Keep only fragments that identify a specific review (see docstring).
    fragment = parts.fragment if parts.fragment.startswith(("review-", "review_")) else ""

    return urlunsplit((scheme, netloc, path, query, fragment))


def registrable_domain(url_or_host: str) -> str:
    """A best-effort grouping key for budgets and per-domain stats.

    Uses `tld` when it is installed (it arrives with Scrapling) for true
    public-suffix handling, and otherwise falls back to the last two labels. The
    fallback is wrong for `example.co.uk` (returns `co.uk`), which costs
    precision in budgeting but never correctness of data.

    >>> registrable_domain("https://www.reddit.com/r/coffee/x")
    'reddit.com'
    >>> registrable_domain("news.ycombinator.com")
    'ycombinator.com'
    """
    raw = (url_or_host or "").strip().lower()
    if not raw:
        return ""
    host = raw
    if "://" in raw:
        try:
            host = (urlsplit(raw).hostname or "").lower()
        except ValueError:
            host = ""
    else:
        host = raw.split("/")[0]
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host  # an IP is its own grouping key
    except ValueError:
        pass
    try:
        from tld import get_fld  # type: ignore

        fld = get_fld(f"https://{host}", fail_silently=True)
        if fld:
            return fld
    except ImportError:
        pass
    labels = [p for p in host.split(".") if p]
    return ".".join(labels[-2:]) if len(labels) >= 2 else host
