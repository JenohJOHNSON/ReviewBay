"""Local, free text embeddings + sentiment — no Snowflake Cortex needed.

Snowflake's free trial blocks all SNOWFLAKE.CORTEX.* AI functions (SENTIMENT,
EMBED_TEXT_768, ...). So we compute the "text -> numbers" step here in the app
with a small local model, store the resulting vectors in Snowflake, and let
Snowflake do only the vector *math* (VECTOR_COSINE_SIMILARITY), which trials
allow. This is also a perfectly normal production design — embedding outside the
warehouse is common.

Swap EMBED_MODEL for any fastembed model, but keep the Snowflake VECTOR(...)
dimension in sync (arctic-embed-m = 768, matching snowflake/ddl.sql).
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
    """Coarse positive/neutral/negative bucket, the same shape Cortex gave us."""
    score = _sentiment_analyzer().polarity_scores(text or "")["compound"]
    if score > 0.3:
        return "positive"
    if score < -0.3:
        return "negative"
    return "neutral"
