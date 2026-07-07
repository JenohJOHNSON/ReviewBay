"""LLM-generated dashboard insights.

Claude reads a sample of a brand's reviews and returns a compact, structured
report: an executive summary, the pros/cons that drive sentiment, the top themes,
and a consumer-behavior note. Results are cached per brand (TTL) so the dashboard
stays snappy and we don't pay for a generation on every page load.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

log = logging.getLogger(__name__)

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
INSIGHTS_TTL = int(os.environ.get("INSIGHTS_TTL", "900"))       # cache 15 min
INSIGHTS_SAMPLE = int(os.environ.get("INSIGHTS_SAMPLE", "60"))  # reviews per call

_WHERE = "(%(brand)s IS NULL OR brand ILIKE %(brand)s)"
_CACHE: dict[str, tuple[float, dict]] = {}
_LOCK = threading.Lock()

_SYSTEM = """You are a brand-reputation analyst. From the customer reviews given, \
produce a compact JSON report. Ground EVERYTHING only in the reviews provided, \
never invent facts or use outside knowledge. In any text you write, never use an \
em dash; use commas, periods, or parentheses instead.

Return ONLY valid JSON (no markdown, no prose) with EXACTLY this shape:
{
  "summary": "2-3 sentence executive overview of what customers think overall",
  "pros": ["short phrase", "..."],            // up to 5, most common praises
  "cons": ["short phrase", "..."],            // up to 5, most common complaints
  "themes": [                                  // up to 6 recurring topics
    {"name": "short topic label", "sentiment": "positive|neutral|negative", "mentions": 0,
     "description": "1-2 sentences on what customers say about this theme"}
  ],
  "behavior": "1-2 sentences on consumer-behavior patterns, what drives love vs frustration"
}
Set "mentions" to how many of the provided reviews touch that theme (your best estimate)."""


# Tool schema: forcing the model to call this guarantees valid, shape-checked JSON,
# so a messy brand can never produce an unparseable summary.
_REPORT_TOOL = {
    "name": "report",
    "description": "Return the brand-reputation report built only from the reviews.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "2-3 sentence executive overview"},
            "pros": {"type": "array", "items": {"type": "string"}, "description": "up to 5 common praises"},
            "cons": {"type": "array", "items": {"type": "string"}, "description": "up to 5 common complaints"},
            "themes": {
                "type": "array",
                "description": "up to 6 recurring topics",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "short topic label"},
                        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
                        "mentions": {"type": "integer"},
                        "description": {
                            "type": "string",
                            "description": "one or two sentences on what customers say about this theme, grounded only in the reviews",
                        },
                    },
                    "required": ["name", "sentiment", "description"],
                },
            },
            "behavior": {"type": "string", "description": "1-2 sentences on consumer-behavior patterns"},
        },
        "required": ["summary", "pros", "cons", "themes", "behavior"],
    },
}


def _connect():
    import snowflake.connector  # type: ignore

    return snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ.get("SNOWFLAKE_PASSWORD"),
        role=os.environ.get("SNOWFLAKE_ROLE"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
        database=os.environ.get("SNOWFLAKE_DATABASE", "REVIEWBOT"),
        schema="MARTS",
    )


def _sample_reviews(brand: str | None) -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT text, source, rating, sentiment
                FROM MARTS.REVIEWS
                WHERE {_WHERE} AND text IS NOT NULL
                ORDER BY captured_at DESC
                LIMIT %(n)s
                """,
                {"brand": brand, "n": INSIGHTS_SAMPLE},
            )
            cols = [c[0].lower() for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):  # strip an accidental code fence
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    a, b = text.find("{"), text.rfind("}")
    if a != -1 and b != -1:
        text = text[a : b + 1]
    return json.loads(text)


def _generate(brand: str | None, rows: list[dict]) -> dict:
    import anthropic  # type: ignore

    context = "\n".join(
        f"- ({r.get('source')}, {r.get('sentiment')}, rating {r.get('rating')}) "
        f"{(r.get('text') or '').strip()[:300]}"
        for r in rows
    )
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1300,
        system=_SYSTEM,
        tools=[_REPORT_TOOL],
        tool_choice={"type": "tool", "name": "report"},
        messages=[
            {
                "role": "user",
                "content": (
                    f"Brand: {brand or 'all tracked brands'}\n"
                    f"Here are {len(rows)} recent customer reviews:\n\n{context}\n\n"
                    "Analyze them and call the report tool."
                ),
            }
        ],
    )
    # tool_choice forces the tool call, so its input is already valid, schema-checked
    # JSON. No text parsing, so a syntax error is impossible.
    data = next((b.input for b in resp.content if b.type == "tool_use"), None)
    if not isinstance(data, dict):
        raise ValueError("insights: model did not return the report tool")
    data = dict(data)
    # normalise shape defensively
    data.setdefault("summary", "")
    for key in ("pros", "cons", "themes"):
        if not isinstance(data.get(key), list):
            data[key] = []
    data.setdefault("behavior", "")
    return data


def get_insights(brand: str | None = None, refresh: bool = False) -> dict:
    key = (brand or "").strip().lower()
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and not refresh and now - hit[0] < INSIGHTS_TTL:
            return hit[1]

    rows = _sample_reviews(brand)
    if not rows:
        data = {
            "summary": "No reviews yet for this selection. Check back once collection runs.",
            "pros": [], "cons": [], "themes": [], "behavior": "", "reviews_analyzed": 0,
        }
    else:
        try:
            data = _generate(brand, rows)
            data["reviews_analyzed"] = len(rows)
        except Exception:  # noqa: BLE001
            log.exception("insights generation failed for brand=%s", brand)
            data = {
                "summary": "Could not generate the AI summary right now.",
                "pros": [], "cons": [], "themes": [], "behavior": "",
                "reviews_analyzed": len(rows), "error": True,
            }

    with _LOCK:
        _CACHE[key] = (time.time(), data)
    return data
