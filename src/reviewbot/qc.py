"""LLM quality-control pass over collected reviews.

After collection and enrichment, an LLM double-checks each review for a brand on
two axes:

  - relevance: is the text actually ABOUT this brand, or a keyword collision (a
    common word used in its ordinary sense, a different company/person with the
    same name, a definition/pronunciation, unrelated news, generic advice, spam)?
  - sentiment: does the expressed opinion match the label?

Verdicts land in marts.reviews (`relevant`, `qc_checked`). For star-rated reviews
the star-derived sentiment is authoritative and kept; for star-less sources
(Reddit, Hacker News, Mastodon, web) the LLM's sentiment replaces the weaker
lexicon guess. It is cheap, batched, and best-effort: any failure leaves a review
un-checked (`relevant` stays NULL, so it still shows) rather than breaking a run.
Irrelevant reviews are then excluded from the dashboard by the stats queries.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("reviewbot.qc")

QC_MODEL = os.environ.get("QC_MODEL", "gpt-4o-mini")
QC_BATCH = int(os.environ.get("QC_BATCH", "25"))
_MAX_ROWS = int(os.environ.get("QC_MAX_ROWS", "600"))  # cap work per run

# Sources that come from the brand's OWN page/app, so they are on-topic by
# construction (resolved by app id or brand page, not a keyword match). We mark
# these relevant without spending an LLM call; the relevance check only runs on
# search/social sources where keyword collisions actually happen.
TRUSTED_SOURCES = ("app_store", "google_play", "trustpilot", "google_maps", "yelp", "tripadvisor")

# The tight, custom system prompt for the QC agent.
_SYSTEM = (
    "You are a strict quality-control checker for a brand-reputation tool. You are "
    "given a BRAND and a numbered list of texts that a search turned up as possible "
    "mentions of it. These came from open web and social search, so some only match "
    "the brand NAME by coincidence. For EACH item decide two things:\n\n"
    "1. \"relevant\": default TRUE. Keep it true whenever the brand or its "
    "products, service, company, or app are clearly named and discussed, an "
    "experience, opinion, review, complaint, question, product page, or product "
    "video all count, even if brief. Set it FALSE only when you are confident the "
    "text is NOT about this brand: the name is a common word used in its ordinary "
    "sense, it is a DIFFERENT company/person/product that merely shares the name, "
    "it is pure site boilerplate with no brand content (cookie or login notices, "
    "terms, navigation), a dictionary or pronunciation entry, or spam. If the brand "
    "is clearly named and the text is about it, keep it true.\n\n"
    "2. \"sentiment\": the author's opinion of the brand, exactly one of "
    "\"positive\", \"neutral\", or \"negative\". If not relevant, use \"neutral\".\n\n"
    "Judge ONLY from the text provided. Do not use outside knowledge and do not "
    "invent facts. Return STRICT JSON of the form "
    "{\"items\":[{\"i\":<index>,\"relevant\":<true|false>,\"sentiment\":\"...\"}]}, "
    "with one entry for every item, using the same index you were given."
)


def _client():
    from openai import OpenAI  # type: ignore

    return OpenAI()  # reads OPENAI_API_KEY


def _connect():
    from .db import connect

    return connect()


def _judge_batch(brand: str, items: list[tuple]) -> dict:
    """items: list of (id, text). Returns {id: (relevant_bool, sentiment_str)}."""
    numbered = "\n".join(
        f'{n}. """{(txt or "").strip()[:600]}"""' for n, (_id, txt) in enumerate(items)
    )
    user = f"BRAND: {brand}\n\nTexts:\n{numbered}"
    try:
        resp = _client().chat.completions.create(
            model=QC_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception:  # noqa: BLE001 — a failed batch just stays un-checked
        log.exception("qc: judge batch failed for brand=%s (%d items)", brand, len(items))
        return {}

    out: dict = {}
    for it in data.get("items", []) or []:
        try:
            i = int(it.get("i"))
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(items):
            sent = str(it.get("sentiment", "neutral")).lower()
            if sent not in ("positive", "neutral", "negative"):
                sent = "neutral"
            out[items[i][0]] = (bool(it.get("relevant")), sent)
    return out


_UPDATE = """
UPDATE marts.reviews
SET relevant = %(relevant)s,
    qc_checked = TRUE,
    -- keep star-derived sentiment; only let QC set it for star-less sources
    sentiment = CASE WHEN rating IS NULL THEN %(sentiment)s ELSE sentiment END
WHERE id = %(id)s
"""


def qc(brand: str | None = None, limit: int = _MAX_ROWS) -> dict:
    """QC un-checked reviews (optionally for one brand). Best-effort; returns a
    small summary. Reads and writes with short-lived connections and holds no DB
    connection during the LLM calls (same discipline as enrich)."""
    brand_clause = " AND brand ILIKE %(brand)s" if brand else ""

    # Trusted sources (from the brand's own page/app) are on-topic by construction:
    # mark them relevant with no LLM call.
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE marts.reviews SET relevant = TRUE, qc_checked = TRUE "
                f"WHERE qc_checked IS NOT TRUE AND source = ANY(%(trusted)s){brand_clause}",
                {"trusted": list(TRUSTED_SOURCES), "brand": brand},
            )
        conn.commit()
    finally:
        conn.close()

    # LLM-check only the remaining (search/social) sources, where the brand name
    # can match by coincidence.
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, text, brand FROM marts.reviews "
                f"WHERE qc_checked IS NOT TRUE AND NOT (source = ANY(%(trusted)s)){brand_clause} "
                "ORDER BY captured_at DESC LIMIT %(limit)s",
                {"trusted": list(TRUSTED_SOURCES), "brand": brand,
                 "limit": max(1, min(int(limit), 5000))},
            )
            rows = [(r[0], r[1], r[2]) for r in cur.fetchall()]
    finally:
        conn.close()

    if not rows:
        return {"checked": 0, "irrelevant": 0}

    checked = irrelevant = 0
    for start in range(0, len(rows), QC_BATCH):
        chunk = rows[start : start + QC_BATCH]
        b = chunk[0][2] or brand or ""
        verdicts = _judge_batch(b, [(rid, txt) for (rid, txt, _br) in chunk])
        if not verdicts:
            continue
        params = [
            {"id": rid, "relevant": rel, "sentiment": sent}
            for rid, (rel, sent) in verdicts.items()
        ]
        try:
            conn = _connect()
            try:
                with conn.cursor() as cur:
                    cur.executemany(_UPDATE, params)
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 — a failed write leaves the batch un-checked
            log.exception("qc: write batch failed for brand=%s", b)
            continue
        checked += len(params)
        irrelevant += sum(1 for _rid, (rel, _s) in verdicts.items() if not rel)

    log.info("qc: brand=%s checked=%d flagged_irrelevant=%d", brand, checked, irrelevant)
    return {"checked": checked, "irrelevant": irrelevant}
