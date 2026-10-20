"""Text cleaning shared by every connector.

Connectors used to each do their own cleanup (or none): Mastodon stripped HTML,
Hacker News did not, so HN rows landed in Postgres full of `<p>` tags and
`&#x27;` entities that then got embedded, shown on the dashboard, and fed to the
LLM. Putting the cleaners here means a new connector gets correct behaviour by
default instead of re-deciding it.

Nothing here invents content: it only removes markup, normalizes whitespace and
encoding, and truncates. A review that cleans down to nothing is dropped by the
caller rather than stored as an empty row.
"""

from __future__ import annotations

import html
import re
import unicodedata

# Tags whose *content* is never human-readable review text. Removed body and all.
_DROP_TAGS = re.compile(
    r"<(script|style|noscript|template|svg)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
# Block-level tags become a newline so sentences don't run together.
_BLOCK_TAGS = re.compile(
    r"</?(p|div|br|li|tr|h[1-6]|blockquote|pre|section|article|header|footer)\b[^>]*>",
    re.IGNORECASE,
)
_ANY_TAG = re.compile(r"<[^>]+>")
_WS_RUNS = re.compile(r"[ \t  -​﻿]+")
_NEWLINE_RUNS = re.compile(r"\n{3,}")
# C0/C1 control characters except tab and newline. Postgres rejects NUL in text.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def looks_like_html(value: str) -> bool:
    """Cheap test for 'this string carries markup or entities'."""
    if not value:
        return False
    return bool(_ANY_TAG.search(value)) or ("&" in value and ";" in value)


def strip_html(value: str) -> str:
    """Markup and entities out, readable text in.

    Safe to call on text that is already plain: it is then just a whitespace and
    encoding normalization pass.

    Order matters, and so does doing the unescape exactly once. Entities are
    decoded AFTER tag removal, so `&lt;b&gt;` cannot be decoded into a tag that
    the stripper has already finished looking for. The decoded result is then
    stripped a SECOND time, which removes markup that arrived escaped.

    What it deliberately does not do is unescape twice. A second pass would turn
    `&amp;lt;script&amp;gt;` — which legitimately means the literal characters
    `&lt;script&gt;` — into `<script>`, manufacturing live-looking markup out of
    text whose author escaped it on purpose. One decode is the correct reading of
    the input; a second changes its meaning.
    """
    if not value:
        return ""
    out = _strip_tags(value)
    out = html.unescape(out)
    # Escaped markup is now literal markup; strip it, but do not decode again.
    if _ANY_TAG.search(out):
        out = _strip_tags(out)
    return normalize(out)


def _strip_tags(value: str) -> str:
    out = _DROP_TAGS.sub(" ", value)
    out = _BLOCK_TAGS.sub("\n", out)
    return _ANY_TAG.sub(" ", out)


def normalize(value: str) -> str:
    """Normalize encoding and whitespace without changing the words.

    NFKC folds the look-alike Unicode that scraped pages are full of (full-width
    punctuation, non-breaking spaces, zero-width joiners) so that two captures of
    the same review hash to the same `stable_id` instead of two near-identical
    rows.
    """
    if not value:
        return ""
    out = unicodedata.normalize("NFKC", value)
    out = _CONTROL.sub("", out)
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    out = _WS_RUNS.sub(" ", out)
    out = "\n".join(line.strip() for line in out.split("\n"))
    out = _NEWLINE_RUNS.sub("\n\n", out)
    return out.strip()


def clean(value: str | None) -> str:
    """The one-call cleaner a connector should use on any field from the web."""
    if not value:
        return ""
    raw = str(value)
    return strip_html(raw) if looks_like_html(raw) else normalize(raw)


def truncate(value: str, limit: int, suffix: str = "...") -> str:
    """Cut to `limit` characters on a word boundary where there is one."""
    if not value or limit <= 0 or len(value) <= limit:
        return value or ""
    head = value[:limit]
    cut = head.rsplit(" ", 1)[0]
    # Don't let a single very long token collapse the text to almost nothing.
    if len(cut) < limit * 0.6:
        cut = head
    return cut.rstrip() + suffix


def join_parts(*parts: str | None, separator: str = "\n\n") -> str:
    """Join the non-empty pieces of a review (title, body, ...) after cleaning."""
    cleaned = [clean(p) for p in parts]
    return separator.join(p for p in cleaned if p)
