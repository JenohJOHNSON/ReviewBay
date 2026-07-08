"""Retrieval-augmented generation over the reviews in Postgres.

Two steps:
  1. RETRIEVE: embed the user's question with the OpenAI embeddings API and rank
     the stored reviews by pgvector cosine distance (one query against marts.reviews).
  2. GENERATE: hand the top reviews to OpenAI (Responses API) with a system prompt
     that forces an answer grounded only in those reviews, with inline [n] source
     links. The citations are the whole point of the product, so they are a hard
     requirement, and we also return the raw sources to the UI. If OpenAI is
     unavailable (no key or quota), we fall back to an extractive answer built
     from the top reviews so the chat degrades gracefully instead of crashing.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

from .. import db, embeddings

log = logging.getLogger(__name__)

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "low")


def _is_reasoning_model(m: str) -> bool:
    """reasoning={effort} is only valid for reasoning models (o-series, gpt-5).
    Sending it to a standard model (gpt-4o-mini, gpt-4.1, ...) 400s."""
    m = (m or "").lower()
    return m.startswith(("o1", "o3", "o4")) or "gpt-5" in m
TOP_K = int(os.environ.get("RAG_TOP_K", "8"))

# Rank marts.reviews by similarity to the question. We embed the question with
# the same OpenAI embedding settings used to build the stored vectors, then
# Postgres does the vector math with pgvector's <=> cosine distance operator
# (score = 1 - distance). Optional brand filter narrows to one brand.
_SEARCH_SQL = """
SELECT
    text, source, source_url, author, rating, brand, sentiment,
    1 - (embedding <=> %(qvec)s::vector) AS score
FROM marts.reviews
WHERE (%(brand)s::text IS NULL OR brand ILIKE %(brand)s::text)
  AND relevant IS NOT FALSE
ORDER BY embedding <=> %(qvec)s::vector
LIMIT %(k)s
"""

# Words to ignore when matching a brand name inside a question, so "Blue Bottle
# Coffee" is recognised from "blue bottle" and a generic word like "coffee" alone
# never triggers a match.
_GENERIC_BRAND_WORDS = {
    "coffee", "inc", "co", "company", "ltd", "llc", "corp", "corporation",
    "group", "the", "brand", "app",
}


@dataclass
class Source:
    text: str
    source: str
    source_url: str
    author: str | None
    rating: float | None
    brand: str
    sentiment: str | None
    score: float


@dataclass
class Answer:
    answer: str
    sources: list[Source]
    confidence: str = "Low"          # High / Medium / Low
    evidence: dict | None = None     # {reviews, sources, source_types}


def _confidence_and_evidence(sources: list[Source]) -> tuple[str, dict]:
    """Grade how well-grounded the answer is, from retrieval strength.

    Retrieval always returns up to TOP_K rows even for an off-topic question, so
    the top cosine score (not the count) is what separates a solid answer from a
    weakly-matched one. We surface both so the UI can say "High, based on N
    reviews from M sources" and the reader can trust or discount accordingly.
    """
    n = len(sources)
    types = sorted({s.source for s in sources if s.source})
    top = max((s.score for s in sources), default=0.0)
    if top >= 0.5 and n >= 3:
        conf = "High"
    elif top >= 0.35 and n >= 2:
        conf = "Medium"
    else:
        conf = "Low"
    return conf, {"reviews": n, "sources": len(types), "source_types": types}


def _connect():
    return db.connect()


def retrieve(question: str, brand: str | None = None, k: int = TOP_K) -> list[Source]:
    conn = _connect()
    try:
        qvec = json.dumps(embeddings.embed_one(question))
        with conn.cursor() as cur:
            cur.execute(
                _SEARCH_SQL,
                {
                    "qvec": qvec,
                    "brand": brand,
                    "k": k,
                },
            )
            cols = [c[0].lower() for c in cur.description]
            return [Source(**dict(zip(cols, row))) for row in cur.fetchall()]
    finally:
        conn.close()


def _brand_key(name: str) -> str:
    """Distinctive part of a brand name (generic words dropped), e.g.
    'Blue Bottle Coffee' -> 'blue bottle', "Peet's Coffee" -> 'peet s' -> 'peet'."""
    toks = [
        t for t in re.findall(r"[a-z0-9]+", (name or "").lower())
        if len(t) > 1 and t not in _GENERIC_BRAND_WORDS
    ]
    return " ".join(toks)


def _mentioned(question: str, available: list[str]) -> list[str]:
    """Which of the available brands are named in the question."""
    q = " " + re.sub(r"[^a-z0-9]+", " ", (question or "").lower()).strip() + " "
    hits = []
    for b in available:
        key = _brand_key(b)
        full = re.sub(r"[^a-z0-9]+", " ", (b or "").lower()).strip()
        if (key and key in q) or (full and full in q):
            hits.append(b)
    return hits


def _resolve_targets(question: str, brand: str | None, brands: list[str] | None) -> list[str]:
    """Decide which brands to answer over.

    - If the question names specific brands (from the ones available), use those
      (this is what lets "compare A and B" pull both, even from a single-brand view).
    - Else if one brand is selected, use it.
    - Else use every available brand ("All brands"); empty means a global search.
    """
    available = [b for b in (brands or []) if b] or ([brand] if brand else [])
    mentioned = _mentioned(question, available)
    if mentioned:
        targets = mentioned
    elif brand:
        targets = [brand]
    else:
        targets = available
    return list(dict.fromkeys(targets))  # dedup, keep order


_SYSTEM_PROMPT = """You are ReviewBay's review assistant. ReviewBay gathers real \
customer reviews for a brand, and your job is to tell the user what those reviews say, \
like a sharp, warm colleague who just read every one of them and cannot wait to share \
what they found.

Your scope (important):
- Answer ONLY from the reviews provided in the message. They are your single source of \
truth. Do not use outside knowledge about the brand, and never invent facts, numbers, \
or quotes.
- If the reviews do not cover what was asked, say so plainly and kindly, then point to \
what they DO cover. Do not guess to fill the gap.
- If asked something unrelated to these reviews (general knowledge, coding, math, \
personal advice), gently decline and steer back: you are here to explain what \
customers are saying about this brand.

How to answer:
- Be conversational and personable. Talk straight to the user with "you", keep it \
natural and human, and let a little personality through. Never sound corporate or \
robotic.
- Open with the real answer in a warm, direct sentence, then back it up with \
specifics from the reviews.
- Make it personal to the brand(s) and THIS question. Name the brand, and quote a short \
telling phrase from a review when it lands.
- The reviews may cover one or more brands, and each review is labeled with its brand. \
If the user asks about several brands, compare them fairly using the reviews, attribute \
every point to the right brand by name, and it is fine to end with a short verdict.
- ALWAYS cite inline with the numbered markers you are given, like "the app keeps \
crashing [2][5]". Every real claim needs at least one citation. Citations are the \
whole point of ReviewBay, so this is not optional.
- Be fair. Separate what lots of reviewers say from a one-off gripe, and mention \
the good alongside the bad when both show up.
- Never use an em dash. Use commas, periods, or parentheses instead.
- Keep it tight and skimmable. A short, lively paragraph or a few bullets beats a \
wall of text."""


def _format_context(sources: list[Source]) -> str:
    blocks = []
    for i, s in enumerate(sources, start=1):
        rating = f" | rating: {s.rating}" if s.rating is not None else ""
        sentiment = f" | sentiment: {s.sentiment}" if s.sentiment else ""
        blocks.append(
            f"[{i}] brand: {s.brand} | source: {s.source}{rating}{sentiment} | url: {s.source_url}\n"
            f"{s.text.strip()}"
        )
    return "\n\n".join(blocks)


def _extractive_fallback(brand: str | None, sources: list[Source]) -> str:
    """Deterministic answer for when the OpenAI call is unavailable (missing key
    or exhausted quota): quote the top matching reviews with the same [n]
    citations, so the chat and its clickable citations degrade gracefully instead
    of erroring."""
    who = f" about {brand}" if brand else ""
    lines = [
        f"OpenAI generation is not available right now, so here are the most relevant reviews{who}:"
    ]
    for i, s in enumerate(sources[:5], start=1):
        snippet = " ".join((s.text or "").split())
        if len(snippet) > 220:
            snippet = snippet[:220].rstrip() + "..."
        lines.append(f"- {snippet} [{i}]")
    return "\n".join(lines)


def _openai_text(resp) -> str:
    text = getattr(resp, "output_text", None)
    if text:
        return str(text)
    parts = []
    for item in getattr(resp, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            value = getattr(content, "text", None)
            if value:
                parts.append(str(value))
    return "".join(parts)


def answer(question: str, brand: str | None = None, brands: list[str] | None = None) -> Answer:
    targets = _resolve_targets(question, brand, brands)
    multi = len(targets) > 1

    try:
        if not targets:
            sources = retrieve(question, brand=None)  # global (all brands in the store)
        elif not multi:
            sources = retrieve(question, brand=targets[0])
        else:
            # Retrieve a slice per brand so every named brand is represented (a single
            # global search would let one brand dominate). Dedupe across brands.
            per = max(3, TOP_K // len(targets) + 2)
            seen, sources = set(), []
            for b in targets:
                for s in retrieve(question, brand=b, k=per):
                    key = (s.source_url, s.text)
                    if key in seen:
                        continue
                    seen.add(key)
                    sources.append(s)
    except db.DatabaseConfigError:
        return Answer(
            answer=(
                "ReviewBay is running, but the review database is not configured yet. "
                "Set DATABASE_URL to your Neon Postgres URL, run postgres/ddl.sql, "
                "then restart the API and worker."
            ),
            sources=[],
            confidence="Low",
            evidence={"reviews": 0, "sources": 0, "source_types": []},
        )

    if not sources:
        scope = " for those brands" if targets else ""
        return Answer(
            answer=(
                f"I could not find anything in the reviews about that yet{scope}. Try "
                "asking about their product, service, app, or prices, or add more "
                "sources and check back."
            ),
            sources=[],
            confidence="Low",
            evidence={"reviews": 0, "sources": 0, "source_types": []},
        )

    confidence, evidence = _confidence_and_evidence(sources)
    context = _format_context(sources)
    if multi:
        header = (
            f"The user is comparing these brands: {', '.join(targets)}. Each review "
            "below is labeled with its brand; compare them and attribute every point "
            "to the right brand. "
        )
    elif targets:
        header = f"The user is asking about {targets[0]}. "
    else:
        header = ""
    user_input = (
        f"{header}Talk to me like a helpful friend who just read these reviews.\n\n"
        f"My question: {question}\n\n"
        f"Here are the most relevant reviews and posts (cite each point by its [number]):\n\n"
        f"{context}"
    )

    if not os.environ.get("OPENAI_API_KEY"):
        text = _extractive_fallback(targets[0] if len(targets) == 1 else None, sources)
    else:
        try:
            from openai import OpenAI  # type: ignore

            client = OpenAI()  # reads OPENAI_API_KEY
            kwargs = dict(
                model=OPENAI_MODEL,
                instructions=_SYSTEM_PROMPT,
                input=user_input,
                max_output_tokens=1500,
            )
            if _is_reasoning_model(OPENAI_MODEL):
                kwargs["reasoning"] = {"effort": OPENAI_REASONING_EFFORT}
            resp = client.responses.create(**kwargs)
            text = _openai_text(resp).strip()
            if not text:
                raise ValueError("empty response from the model")
        except Exception:  # noqa: BLE001
            log.exception("openai chat failed; using extractive fallback")
            text = _extractive_fallback(targets[0] if len(targets) == 1 else None, sources)

    return Answer(answer=text, sources=sources, confidence=confidence, evidence=evidence)
