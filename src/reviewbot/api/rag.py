"""Retrieval-augmented generation over the reviews in Snowflake.

Two steps:
  1. RETRIEVE — embed the user's question with Snowflake Cortex and rank the
     stored reviews by vector cosine similarity (all in-database, one query).
  2. GENERATE — hand the top reviews to Claude with a system prompt that forces
     an answer grounded in those reviews *with inline source links*. The
     citations are the whole point of the product, so they are a hard
     requirement in the prompt, and we also return the raw sources to the UI.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from .. import embeddings

log = logging.getLogger(__name__)

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
TOP_K = int(os.environ.get("RAG_TOP_K", "8"))

# Rank MARTS.REVIEWS by similarity to the question. We embed the question in
# Python with the SAME local model used to build the stored vectors (so they're
# comparable), then Snowflake does only the vector math — no Cortex, so this
# works on trial accounts. Optional brand filter narrows to one brand.
_SEARCH_SQL = """
SELECT
    text, source, source_url, author, rating, brand, sentiment,
    VECTOR_COSINE_SIMILARITY(
        embedding,
        PARSE_JSON(%(qvec)s)::VECTOR(FLOAT, 768)
    ) AS score
FROM MARTS.REVIEWS
WHERE (%(brand)s IS NULL OR brand ILIKE %(brand)s)
ORDER BY score DESC
LIMIT %(k)s
"""


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


def retrieve(question: str, brand: str | None = None, k: int = TOP_K) -> list[Source]:
    qvec = json.dumps(embeddings.embed_one(question))
    conn = _connect()
    try:
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
- Make it personal to THIS brand and THIS question. Name the brand, and quote a short \
telling phrase from a review when it lands.
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
            f"[{i}] source: {s.source}{rating}{sentiment} | url: {s.source_url}\n"
            f"{s.text.strip()}"
        )
    return "\n\n".join(blocks)


def answer(question: str, brand: str | None = None) -> Answer:
    import anthropic  # type: ignore

    sources = retrieve(question, brand=brand)
    if not sources:
        return Answer(
            answer=(
                "I could not find anything in the reviews about that yet. Try asking "
                "about their product, service, app, or prices, or add more sources for "
                "this brand and check back."
            ),
            sources=[],
        )

    context = _format_context(sources)
    brand_line = f"The user is asking about {brand}. " if brand else ""
    client = anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY / ant profile
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        thinking={"type": "adaptive"},
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"{brand_line}Talk to me like a helpful friend who just read these reviews.\n\n"
                    f"My question: {question}\n\n"
                    f"Here are the most relevant reviews and posts (cite each point by its [number]):\n\n"
                    f"{context}"
                ),
            }
        ],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return Answer(answer=text, sources=sources)
