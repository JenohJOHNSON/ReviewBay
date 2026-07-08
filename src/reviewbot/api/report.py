"""Review Intelligence Report: the brand's hero output.

Combines DETERMINISTIC facts (a brand health score, sentiment mix, average
rating, and the scikit-learn themes/pros/cons from insights.py) with ONE OpenAI
synthesis call that writes the interpretive, business-facing sections (recurring
issues, purchase drivers, churn risks, marketing angles, product fixes) grounded
ONLY in a real sample of the brand's reviews, cited with [n] markers.

If OpenAI is unavailable the report still renders with all the deterministic
facts and empty interpretive sections (marked facts-only), so it never crashes.
Cached per brand (TTL) like insights.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time

from . import insights, stats
from .rag import OPENAI_MODEL, OPENAI_REASONING_EFFORT, _is_reasoning_model

log = logging.getLogger(__name__)

REPORT_TTL = int(os.environ.get("REPORT_TTL", "900"))          # cache 15 min
REPORT_SAMPLE = int(os.environ.get("REPORT_SAMPLE", "24"))     # reviews cited in the report

_WHERE = "(%(brand)s::text IS NULL OR brand ILIKE %(brand)s::text)"
_CACHE: dict[str, tuple[float, dict]] = {}
_LOCK = threading.Lock()

# The interpretive sections the LLM fills in, and their empty default.
_SECTIONS = (
    "recurring_issues",
    "purchase_drivers",
    "churn_risks",
    "marketing_angles",
    "product_fixes",
    "ad_ideas",
)


def _connect():
    from ..db import connect

    return connect()


def _health(st: dict) -> dict:
    """A 0-100 brand health score from sentiment mix, blended with rating."""
    s = st.get("sentiment") or {}
    pos, neu, neg = s.get("positive", 0), s.get("neutral", 0), s.get("negative", 0)
    n = pos + neu + neg
    if not n:
        return {"score": None, "label": "No data"}
    sent_score = 100 * (pos + 0.5 * neu) / n
    ar = st.get("avg_rating")
    score = round(0.6 * sent_score + 0.4 * (100 * ar / 5)) if ar is not None else round(sent_score)
    label = (
        "Excellent" if score >= 80
        else "Good" if score >= 65
        else "Mixed" if score >= 45
        else "At risk"
    )
    return {"score": score, "label": label}


def _sample(brand: str | None) -> list[dict]:
    """A balanced sample (negatives first, then positives) WITH source_url, so the
    LLM has real evidence to cite and the UI can link every source."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT text, source, source_url, rating, sentiment
                FROM marts.reviews
                WHERE {_WHERE} AND text IS NOT NULL
                ORDER BY CASE sentiment WHEN 'negative' THEN 0 WHEN 'positive' THEN 1 ELSE 2 END,
                         captured_at DESC
                LIMIT %(n)s
                """,
                {"brand": brand, "n": REPORT_SAMPLE},
            )
            cols = [c[0].lower() for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


_SYNTH_INSTRUCTIONS = """You are ReviewBay's brand-reputation analyst. From the \
reviews provided, write the interpretive part of a brand intelligence report.

Rules:
- Use ONLY the provided reviews as evidence. Do not invent facts, numbers, or quotes.
- Cite every point inline with the [n] markers from the reviews, like "checkout is \
confusing [3][7]".
- Be specific and business-useful, not generic. Each item is one tight sentence.
- Never use an em dash. Use commas, periods, or parentheses.

Return ONLY a JSON object (no prose, no code fence) with exactly these keys, each an \
array of 2-4 short strings:
- "recurring_issues": the problems that come up again and again.
- "purchase_drivers": what makes customers choose or love the brand.
- "churn_risks": what could make customers leave.
- "marketing_angles": strengths worth leaning into in marketing, in the customers' own words.
- "product_fixes": concrete improvements the reviews point to.
- "ad_ideas": short, punchy ad headlines or taglines (5-8 words each) built from \
real praise in the reviews. These are ready-to-use ad copy, not strategy."""


def _synthesize(brand: str | None, facts: dict, sample: list[dict]) -> dict | None:
    """One OpenAI call -> the interpretive sections. Returns None on any failure."""
    if not sample:
        return None
    numbered = "\n\n".join(
        f"[{i}] source: {r.get('source')} | sentiment: {r.get('sentiment')} | "
        f"rating: {r.get('rating')}\n{(r.get('text') or '').strip()}"
        for i, r in enumerate(sample, start=1)
    )
    facts_line = (
        f"Brand: {brand}. Health score: {facts['health'].get('score')} "
        f"({facts['health'].get('label')}). "
        f"Praise themes: {', '.join(facts.get('top_praises') or []) or 'n/a'}. "
        f"Complaint themes: {', '.join(facts.get('top_complaints') or []) or 'n/a'}."
    )
    user_input = (
        f"{facts_line}\n\nHere are real reviews to ground the report "
        f"(cite each point by its [number]):\n\n{numbered}"
    )
    try:
        from openai import OpenAI  # type: ignore

        client = OpenAI()
        kwargs = dict(
            model=OPENAI_MODEL,
            instructions=_SYNTH_INSTRUCTIONS,
            input=user_input,
            max_output_tokens=1200,
        )
        if _is_reasoning_model(OPENAI_MODEL):
            kwargs["reasoning"] = {"effort": OPENAI_REASONING_EFFORT}
        resp = client.responses.create(**kwargs)
        text = (resp.output_text or "").strip()
        data = _parse_json(text)
        if not data:
            raise ValueError("no JSON object in model output")
        # Keep only known keys, coerce to lists of strings.
        out = {}
        for key in _SECTIONS:
            val = data.get(key) or []
            out[key] = [str(x).strip() for x in val if str(x).strip()][:4]
        return out
    except Exception:  # noqa: BLE001
        log.exception("report synthesis failed for brand=%s; facts-only", brand)
        return None


def _parse_json(text: str) -> dict | None:
    """Best-effort: parse the whole thing, else the first {...} block."""
    for candidate in (text, _first_brace(text)):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            continue
    return None


def _first_brace(text: str) -> str | None:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    return m.group(0) if m else None


def get_report(brand: str | None = None, refresh: bool = False) -> dict:
    key = (brand or "").strip().lower()
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and not refresh and now - hit[0] < REPORT_TTL:
            return hit[1]

    st = stats.get_stats(brand)
    if not st.get("total"):
        data = {"brand": brand, "empty": True,
                "message": "No reviews yet for this brand. Collection runs in the background."}
        with _LOCK:
            _CACHE[key] = (time.time(), data)
        return data

    ins = insights.get_insights(brand)
    facts = {
        "brand": brand,
        "health": _health(st),
        "totals": {
            "reviews": st.get("total"),
            "avg_rating": st.get("avg_rating"),
            "sentiment": st.get("sentiment"),
        },
        "by_category": st.get("by_category") or [],
        "summary": ins.get("summary"),
        "behavior": ins.get("behavior"),
        "top_praises": ins.get("pros") or [],
        "top_complaints": ins.get("cons") or [],
        "themes": ins.get("themes") or [],
    }

    sample = _sample(brand)
    interp = _synthesize(brand, facts, sample)
    data = dict(facts)
    data["generated_with"] = "openai" if interp else "facts-only"
    for sec in _SECTIONS:
        data[sec] = (interp or {}).get(sec, [])
    data["sources"] = [
        {
            "n": i,
            "source": r.get("source"),
            "source_url": r.get("source_url"),
            "sentiment": r.get("sentiment"),
            "rating": r.get("rating"),
            "excerpt": ((r.get("text") or "")[:220] + "...") if len(r.get("text") or "") > 220 else (r.get("text") or ""),
        }
        for i, r in enumerate(sample, start=1)
    ]

    with _LOCK:
        _CACHE[key] = (time.time(), data)
    return data


def save_report(brand: str | None) -> dict:
    """Snapshot the brand's current report into marts.saved_reports."""
    data = get_report(brand)
    if data.get("empty"):
        return {"ok": False, "error": "Nothing to save yet for this brand."}
    token = secrets.token_urlsafe(9)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO marts.saved_reports (brand, payload, token) VALUES (%s, %s::jsonb, %s) "
                "RETURNING id, created_at",
                (brand, json.dumps(data), token),
            )
            rid, created = cur.fetchone()
        conn.commit()
        return {"ok": True, "id": rid, "created_at": created.isoformat(), "token": token}
    finally:
        conn.close()


def list_saved(brand: str | None = None, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    where = "WHERE brand ILIKE %(brand)s" if brand else ""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, brand, created_at, token FROM marts.saved_reports {where} "
                "ORDER BY created_at DESC LIMIT %(limit)s",
                {"brand": brand, "limit": limit},
            )
            out = []
            for rid, b, created, token in cur.fetchall():
                out.append({"id": rid, "brand": b, "token": token,
                            "created_at": created.isoformat() if created else None})
            return out
    finally:
        conn.close()


def get_saved(report_id: int) -> dict:
    """Return a saved report's payload, tagged with when it was saved."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload, created_at, token FROM marts.saved_reports WHERE id = %s",
                (report_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {"empty": True, "message": "That saved report was not found."}
    payload, created, token = row
    data = dict(payload) if isinstance(payload, dict) else payload  # psycopg returns jsonb as dict
    data["saved_at"] = created.isoformat() if created else None
    data["token"] = token
    return data


def get_shared(token: str) -> dict:
    """Public (unauthenticated) lookup of a saved report by its share token."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload, created_at FROM marts.saved_reports WHERE token = %s",
                (token,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {"empty": True, "message": "This shared report link is not valid."}
    payload, created = row
    data = dict(payload) if isinstance(payload, dict) else payload
    data["saved_at"] = created.isoformat() if created else None
    data["shared"] = True
    return data
