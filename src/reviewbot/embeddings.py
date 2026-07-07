"""Local, free text embeddings + sentiment.

We compute the "text -> numbers" step in the app with a small local model, store
the resulting vectors in Neon Postgres, and let pgvector do the vector math.
Embedding outside the database keeps the setup free and portable.

Swap EMBED_MODEL for any fastembed model, but keep the Postgres vector(...)
dimension in sync (arctic-embed-m = 768, matching postgres/schema.sql).
"""

from __future__ import annotations

import os
from functools import lru_cache

# arctic-embed-m -> 768 dims, matching the VECTOR(FLOAT, 768) column in the DDL.
EMBED_MODEL = os.environ.get("EMBED_MODEL", "snowflake/snowflake-arctic-embed-m")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "768"))


@lru_cache(maxsize=1)
def _model():
    # Imported and loaded lazily so importing this module stays cheap and the
    # model only downloads/loads the first time we actually embed something.
    from fastembed import TextEmbedding  # type: ignore

    return TextEmbedding(model_name=EMBED_MODEL)


def embed(texts: list[str]) -> list[list[float]]:
    """Turn a batch of texts into vectors (list of floats each)."""
    if not texts:
        return []
    return [list(map(float, vec)) for vec in _model().embed(texts)]


def embed_one(text: str) -> list[float]:
    return embed([text])[0]


@lru_cache(maxsize=1)
def _sentiment_analyzer():
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer  # type: ignore

    return SentimentIntensityAnalyzer()


def sentiment_bucket(text: str) -> str:
    """Coarse positive/neutral/negative bucket."""
    score = _sentiment_analyzer().polarity_scores(text or "")["compound"]
    if score > 0.3:
        return "positive"
    if score < -0.3:
        return "negative"
    return "neutral"
