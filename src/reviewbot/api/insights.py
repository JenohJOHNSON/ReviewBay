"""Local, deterministic dashboard insights (no LLM).

From a sample of a brand's reviews we build the same structured report the UI
already expects (executive summary, pros/cons, top themes, consumer-behavior
note) using classic ML: sentiment aggregation, TF-IDF keyphrases, and KMeans
clustering for themes. This is free, offline, and reproducible, so the dashboard
never depends on an LLM or spends tokens. Results are cached per brand (TTL) so
the page stays snappy. The `/api/insights` JSON shape is unchanged.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time

from .. import db

log = logging.getLogger(__name__)

# Generic filler words that add no topical signal, folded into the stop-word set
# alongside the brand's own name (see _stopwords). Without this, a coffee brand's
# top "theme" is just its own name (e.g. "Blue bottle"), which reads as noise.
_GENERIC_STOP = frozenset({
    # filler
    "just", "like", "really", "ve", "don", "didn", "doesn", "got", "get",
    "also", "would", "could", "one", "even", "much", "way", "thing", "im",
    # pure sentiment / meta words: they signal HOW people feel, not WHAT about,
    # so they make lousy topic/theme labels ("Great" is not a theme).
    "best", "great", "good", "bad", "worst", "nice", "love", "loved", "amazing",
    "awesome", "terrible", "horrible", "excellent", "review", "reviews",
})

INSIGHTS_TTL = int(os.environ.get("INSIGHTS_TTL", "900"))       # cache 15 min
INSIGHTS_SAMPLE = int(os.environ.get("INSIGHTS_SAMPLE", "60"))  # reviews per report

# ::text so Postgres can infer the parameter type in the `IS NULL` branch.
_WHERE = "(%(brand)s::text IS NULL OR brand ILIKE %(brand)s::text)"
_CACHE: dict[str, tuple[float, dict]] = {}
_LOCK = threading.Lock()


def _connect():
    return db.connect()


def _cols(cur) -> list[str]:
    cols = []
    for c in cur.description:
        name = getattr(c, "name", None)
        cols.append((name if name is not None else c[0]).lower())
    return cols


def _sample_reviews(brand: str | None) -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT text, source, rating, sentiment
                FROM marts.reviews
                WHERE {_WHERE} AND text IS NOT NULL
                ORDER BY captured_at DESC
                LIMIT %(n)s
                """,
                {"brand": brand, "n": INSIGHTS_SAMPLE},
            )
            cols = _cols(cur)
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


# --- small text helpers (deterministic) --------------------------------------

def _brand_tokens(brand: str | None) -> set[str]:
    """The brand's own words, so they don't show up as 'themes'. 'Blue Bottle
    Coffee' -> {blue, bottle, coffee}. These are in nearly every review, so they
    carry no topical signal and only crowd out the real topics."""
    return set(re.findall(r"[a-z0-9]+", (brand or "").lower()))


def _stopwords(brand: str | None) -> list[str]:
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS  # type: ignore

    return list(ENGLISH_STOP_WORDS | _GENERIC_STOP | _brand_tokens(brand))


def _titleize(term: str) -> str:
    return (term[:1].upper() + term[1:]) if term else term


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _behavior(pos: int, neg: int, pros: list[str], cons: list[str]) -> str:
    if pos > neg * 2:
        base = "Customers are largely loyal, with praise clearly outweighing complaints."
    elif neg > pos:
        base = "Frustration outweighs praise, so retention is the thing to watch."
    else:
        base = "Opinions are mixed, with praise and complaints fairly balanced."
    if pros and cons:
        return f"{base} Love tends to come from {pros[0].lower()}, while frustration centers on {cons[0].lower()}."
    return base


# --- the ML report ------------------------------------------------------------

def _generate(brand: str | None | list[dict], rows: list[dict] | None = None) -> dict:
    """Build the report with TF-IDF keyphrases + KMeans themes + sentiment stats.

    Fully deterministic (fixed random_state, no sampling), so the same reviews
    always yield the same report. Degrades gracefully on tiny or low-signal
    corpora (fewer or no themes) rather than erroring.
    """
    if rows is None:
        rows = brand if isinstance(brand, list) else []
        brand = None

    import numpy as np  # type: ignore
    from sklearn.cluster import KMeans  # type: ignore
    from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore

    pairs = [((r.get("text") or "").strip(), r.get("sentiment"), r.get("rating")) for r in rows]
    pairs = [(t, s, rt) for (t, s, rt) in pairs if t]
    texts = [t for (t, _, _) in pairs]
    sent = [s for (_, s, _) in pairs]
    ratings = [rt for (_, _, rt) in pairs if rt is not None]
    n = len(texts)
    pos, neg, neu = sent.count("positive"), sent.count("negative"), sent.count("neutral")
    avg = round(sum(ratings) / len(ratings), 2) if ratings else None
    label = brand or "these brands"

    # TF-IDF over the corpus (unigrams + bigrams). min_df tames noise but must not
    # exceed the corpus size.
    vec = TfidfVectorizer(
        stop_words=_stopwords(brand), ngram_range=(1, 2),
        min_df=2 if n >= 6 else 1, max_features=500, sublinear_tf=True,
    )
    try:
        X = vec.fit_transform(texts)
        terms = vec.get_feature_names_out()
    except ValueError:  # empty vocabulary (all stop-words / too little text)
        X, terms = None, []

    def top_terms(indices: list[int], m: int) -> list[str]:
        if X is None or not indices or len(terms) == 0:
            return []
        centroid = np.asarray(X[indices].mean(axis=0)).ravel()
        out: list[str] = []
        for j in centroid.argsort()[::-1]:
            if centroid[j] <= 0:
                break
            out.append(terms[j])
            if len(out) >= m:
                break
        return out

    # Themes: cluster the TF-IDF vectors, label each cluster by its top terms.
    themes: list[dict] = []
    if X is not None and X.shape[1] >= 2 and n >= 4:
        k = max(2, min(6, n // 12 or 2, X.shape[1]))
        labels = KMeans(n_clusters=k, n_init=10, random_state=0).fit_predict(X)
        for c in range(k):
            idx = [i for i in range(n) if labels[i] == c]
            top = top_terms(idx, 3)
            if not idx or not top:
                continue
            cs = [sent[i] for i in idx]
            csent = max(("positive", "neutral", "negative"), key=cs.count)
            themes.append({
                "name": _titleize(top[0]),
                "sentiment": csent,
                "mentions": len(idx),
                "description": (
                    f"Around {len(idx)} reviews cluster here, most often mentioning "
                    f"{_join(top)}. The tone is mostly {csent}."
                ),
            })
        themes.sort(key=lambda t: -t["mentions"])
        themes = themes[:6]

    # Pros / cons: the most distinctive terms among positive vs negative reviews.
    pros = [_titleize(t) for t in top_terms([i for i in range(n) if sent[i] == "positive"], 5)]
    cons = [_titleize(t) for t in top_terms([i for i in range(n) if sent[i] == "negative"], 5)]

    def pct(x: int) -> int:
        return round(100 * x / n) if n else 0

    lead = themes[0]["name"].lower() if themes else (pros[0].lower() if pros else "their overall experience")
    rating_txt = f", averaging {avg} out of 5 stars" if avg is not None else ""
    summary = (
        f"Across {n} recent reviews of {label}, sentiment runs {pct(pos)}% positive, "
        f"{pct(neu)}% neutral, and {pct(neg)}% negative{rating_txt}. "
        f"The most common topic is {lead}. "
        + (f"The strongest praise is around {_join(pros[:3]).lower()}. " if pros else "")
        + (f"The main frustrations center on {_join(cons[:3]).lower()}." if cons else "")
    ).strip()

    return {
        "summary": summary,
        "pros": pros,
        "cons": cons,
        "themes": themes,
        "behavior": _behavior(pos, neg, pros, cons),
        "reviews_analyzed": len(rows),
    }


def get_insights(brand: str | None = None, refresh: bool = False) -> dict:
    key = (brand or "").strip().lower()
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and not refresh and now - hit[0] < INSIGHTS_TTL:
            return hit[1]

    data = None
    try:
        rows = _sample_reviews(brand)
    except db.DatabaseConfigError:
        data = {
            "summary": "Database is not configured yet. Set DATABASE_URL to your Neon Postgres URL.",
            "pros": [], "cons": [], "themes": [], "behavior": "",
            "reviews_analyzed": 0, "setup_required": "database",
        }

    if data is None and not rows:
        data = {
            "summary": "No reviews yet for this selection. Check back once collection runs.",
            "pros": [], "cons": [], "themes": [], "behavior": "", "reviews_analyzed": 0,
        }
    elif data is None:
        try:
            data = _generate(brand, rows)
            data["reviews_analyzed"] = len(rows)
        except Exception:  # noqa: BLE001
            log.exception("insights generation failed for brand=%s", brand)
            data = {
                "summary": "Could not build the review summary right now.",
                "pros": [], "cons": [], "themes": [], "behavior": "",
                "reviews_analyzed": len(rows), "error": True,
            }

    with _LOCK:
        _CACHE[key] = (time.time(), data)
    return data
