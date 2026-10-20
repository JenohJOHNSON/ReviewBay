"""Text embeddings via the OpenAI API + local sentiment. No local ML model.

Embeddings are computed by calling OpenAI's embeddings endpoint (the same
OPENAI_API_KEY the chat uses), NOT a local model. This is deliberate: loading a
~1GB local embedding model into the web container crashed small cloud instances
the moment enrichment started. An API call has a tiny, constant memory footprint
and works anywhere the chat already works.

`text-embedding-3-small` supports a `dimensions` parameter, so we ask for 768-dim
vectors to drop straight into the existing pgvector ``vector(768)`` column, no DDL
change. pgvector still does the vector math (cosine distance via ``<=>``).

Sentiment stays local (vaderSentiment): it's a tiny pure-Python lexicon, no model
to load, so it carries no memory/crash risk.
"""

from __future__ import annotations

import logging
import os
import random
import time
from functools import lru_cache

log = logging.getLogger(__name__)

# text-embedding-3-small at 768 dims matches the VECTOR(768) column in the DDL.
_DEFAULT_MODEL = "text-embedding-3-small"
_env_model = os.environ.get("EMBED_MODEL", _DEFAULT_MODEL).strip()
# Guard: a leftover non-OpenAI local-model value passed straight to the OpenAI API
# 400s as an invalid model id. Ignore anything that isn't an OpenAI embedding model.
if not _env_model.startswith("text-embedding"):
    log.warning("EMBED_MODEL=%r is not an OpenAI embedding model; using %s", _env_model, _DEFAULT_MODEL)
    _env_model = _DEFAULT_MODEL
EMBED_MODEL = _env_model
EMBED_DIM = int(os.environ.get("EMBED_DIM", "768"))

# OpenAI accepts many inputs per call; keep chunks modest to stay well under the
# per-request token ceiling. Each input is also truncated so one long article
# can't blow the per-input token limit.
_MAX_INPUTS_PER_CALL = 96
_MAX_CHARS_PER_INPUT = 8000

# A rate limit or a blip used to fail the whole enrichment batch. Retry with
# exponential backoff plus jitter (jitter matters: without it, every worker that
# hit the same limit retries in lockstep and hits it again).
_MAX_ATTEMPTS = int(os.environ.get("EMBED_MAX_ATTEMPTS", "4"))
_BACKOFF_BASE = float(os.environ.get("EMBED_BACKOFF_SECONDS", "1.0"))


@lru_cache(maxsize=1)
def _client():
    # Lazy import + cache so importing this module stays cheap and no client is
    # built until we actually embed something.
    from openai import OpenAI  # type: ignore

    return OpenAI()  # reads OPENAI_API_KEY


def _prep(text: str) -> str:
    # OpenAI rejects empty input; also cap length to stay under the token limit.
    t = (text or "").strip()
    if not t:
        t = " "
    return t[:_MAX_CHARS_PER_INPUT]


def _retryable(exc: Exception) -> bool:
    """Whether an embeddings failure is worth retrying.

    Rate limits, timeouts, connection errors and 5xx are transient. An invalid
    request (a bad model id, an input over the token limit) is not: retrying it
    just burns time and returns the same error.
    """
    name = type(exc).__name__
    if name in ("RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError"):
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    return isinstance(status, int) and (status == 429 or status >= 500)


def _embed_chunk(client, chunk: list[str]) -> list[list[float]]:
    """One embeddings request, retried on transient failures."""
    last: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = client.embeddings.create(
                model=EMBED_MODEL, input=chunk, dimensions=EMBED_DIM
            )
            # resp.data comes back in input order, but sort by index to be safe.
            return [
                [float(x) for x in d.embedding]
                for d in sorted(resp.data, key=lambda d: d.index)
            ]
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt >= _MAX_ATTEMPTS or not _retryable(exc):
                raise
            delay = _BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, _BACKOFF_BASE)
            log.warning(
                "embeddings: %s on attempt %d/%d for %d input(s); retrying in %.1fs",
                type(exc).__name__, attempt, _MAX_ATTEMPTS, len(chunk), delay,
            )
            time.sleep(delay)
    raise last if last else RuntimeError("embeddings failed with no error recorded")


def embed(texts: list[str]) -> list[list[float]]:
    """Turn a batch of texts into 768-dim vectors (list of floats each).

    Order is preserved. Chunks large batches across calls to respect request
    limits, and retries a chunk that hits a rate limit or a transient error.
    """
    if not texts:
        return []
    client = _client()
    out: list[list[float]] = []
    prepped = [_prep(t) for t in texts]
    for i in range(0, len(prepped), _MAX_INPUTS_PER_CALL):
        out.extend(_embed_chunk(client, prepped[i : i + _MAX_INPUTS_PER_CALL]))
    return out


def embed_one(text: str) -> list[float]:
    return embed([text])[0]


@lru_cache(maxsize=1)
def _sentiment_analyzer():
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer  # type: ignore

    return SentimentIntensityAnalyzer()


def sentiment_bucket(text: str, rating: float | None = None) -> str:
    """Coarse positive/neutral/negative bucket.

    When the review carries a star rating, the STARS win: the label is derived
    from the rating so it can never contradict it (a 5-star review never reads as
    negative just because its wording tripped the text model). Text-only VADER is
    used only for sources that have no rating (Reddit, Hacker News, Mastodon, web
    mentions, ...). 1-5 scale: >=3.5 positive, <=2.5 negative, else neutral.
    """
    if rating is not None:
        try:
            r = float(rating)
            if r >= 3.5:
                return "positive"
            if r <= 2.5:
                return "negative"
            return "neutral"
        except (TypeError, ValueError):
            pass  # unparseable rating -> fall back to text sentiment
    score = _sentiment_analyzer().polarity_scores(text or "")["compound"]
    if score > 0.3:
        return "positive"
    if score < -0.3:
        return "negative"
    return "neutral"
