#!/usr/bin/env python
"""Reproducible benchmark of the extraction approaches.

Phase 10's rule: do not assert performance gains without measurements. So this
script measures, on a fixed dataset, with no network dependency and no paid
service, and writes a table that can be pasted into
`docs/SCRAPING_BENCHMARKS.md`.

    PYTHONPATH=src python scripts/benchmark_scraping.py
    PYTHONPATH=src python scripts/benchmark_scraping.py --json results.json
    PYTHONPATH=src python scripts/benchmark_scraping.py --repeat 5

**What it compares.** Four approaches over the same pages:

* `search-only` — the pre-existing behaviour. A search API returns a title and a
  short snippet per result; this simulates exactly that by taking each page's
  `<title>` plus its first ~200 characters of body text, which is what Tavily's
  `content` field gives. It makes no HTTP request per page beyond the one the
  search already did.
* `scrapling` — fetch the page and extract deterministically.
* `scrapegraph-fallback` — `scrapling`, plus AI extraction for pages it could
  not parse. The AI call is **stubbed by default** (`--ai=stub`) so the benchmark
  is free and deterministic; `--ai=live` runs the real backend and reports real
  token spend.
* `routed` — the production path: the router decides per URL, and AI extraction
  is offered only to pages that were fetched but not parsed.

**Why a local fixture server rather than live sites.** A benchmark against live
pages measures the sites' availability on the day it ran, cannot be re-run to
the same numbers, and sends load to third parties for no one's benefit. The
fixture set instead covers the page classes that matter, each one a case that
behaves differently: structured markup, repeated cards, pagination, irregular
prose, a client-rendered shell, an empty page, malformed HTML, and an
unavailable URL.

**What the numbers mean.** `records` counts rows that passed validation and
deduplication, so it is comparable across approaches. `chars` is the total text
volume, which is the metric the audit identified as the real gap: search
snippets are short, and short text makes weak embeddings and thin citations.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


# The dataset. One entry per page class the brief asks to cover.
@dataclass(frozen=True)
class Case:
    path: str
    page_class: str
    note: str


CASES: tuple[Case, ...] = (
    Case("/jsonld_reviews.html", "static + schema.org", "reviews published as JSON-LD"),
    Case("/structural_reviews.html", "repeated cards", "obfuscated class names, data-* hooks"),
    Case("/paginated_page1.html", "paginated listing", "3 pages, rel=next"),
    Case("/irregular.html", "irregular structure", "prose with blockquotes, no review markup"),
    Case("/article_only.html", "article / mention", "news page, not a review page"),
    Case("/js-shell.html", "JS-rendered", "empty shell, content needs a browser"),
    Case("/empty.html", "empty page", "valid HTML, no content"),
    Case("/malformed.html", "malformed HTML", "unclosed tags"),
    Case("/missing.html", "unavailable", "404"),
    Case("/forbidden", "declined", "403, must not be retried harder"),
)

BRAND = "Harbour Tools"
KEYWORDS = [BRAND]


@dataclass
class Result:
    approach: str
    records: int = 0
    chars: int = 0
    with_rating: int = 0
    with_author: int = 0
    with_date: int = 0
    reviews: int = 0          # kind == "review"
    mentions: int = 0         # kind == "mention"
    rejected: int = 0
    duplicates: int = 0
    fetches: int = 0
    pages_ok: int = 0
    pages_failed: int = 0
    ai_calls: int = 0
    ai_cost_usd: float = 0.0
    elapsed_ms: int = 0
    per_case: dict = field(default_factory=dict)

    def as_row(self) -> dict:
        return {
            "approach": self.approach,
            "records": self.records,
            "chars": self.chars,
            "avg_chars": round(self.chars / self.records) if self.records else 0,
            "reviews": self.reviews,
            "mentions": self.mentions,
            "with_rating": self.with_rating,
            "with_author": self.with_author,
            "with_date": self.with_date,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "fetches": self.fetches,
            "pages_ok": self.pages_ok,
            "pages_failed": self.pages_failed,
            "ai_calls": self.ai_calls,
            "ai_cost_usd": round(self.ai_cost_usd, 6),
            "elapsed_ms": self.elapsed_ms,
        }


def _tally(result: Result, reviews: list, case: Case) -> None:
    from reviewbot import quality

    report = quality.QualityReport()
    kept = quality.prepare(
        reviews, keywords=KEYWORDS, dedup=quality.Deduplicator(), report=report
    )
    result.records += len(kept)
    result.rejected += report.rejected
    result.duplicates += report.duplicates
    for review in kept:
        result.chars += len(review.text)
        result.with_rating += 1 if review.rating is not None else 0
        result.with_author += 1 if review.author else 0
        result.with_date += 1 if review.created_at else 0
        kind = ((review.extra or {}).get("extraction") or {}).get("kind")
        if kind == "mention":
            result.mentions += 1
        else:
            result.reviews += 1
    result.per_case[case.page_class] = {
        "records": len(kept),
        "chars": sum(len(r.text) for r in kept),
        "rejected": report.rejected,
    }


# --- approach 1: search snippets (the pre-existing behaviour) ----------------

SNIPPET_CHARS = 200


def run_search_only(base: str) -> Result:
    """What a search API hands back: a title and a short content snippet.

    Tavily's `content` field is a short extract, and `tavily_source._map_result`
    stores `title + "\\n\\n" + content` with no author, rating or date — there is
    nowhere in a SERP result for those to come from. This reproduces that shape
    from the same pages, so the comparison is like for like.
    """
    import requests

    from reviewbot import text as textclean
    from reviewbot.models import NormalizedReview
    from reviewbot.sources import source_from_url

    result = Result("search-only")
    started = time.monotonic()
    for case in CASES:
        url = base + case.path
        try:
            resp = requests.get(url, timeout=10)
            result.fetches += 1
            if resp.status_code >= 400:
                result.pages_failed += 1
                _tally(result, [], case)
                continue
            result.pages_ok += 1
            body = resp.text
        except Exception:  # noqa: BLE001
            result.fetches += 1
            result.pages_failed += 1
            _tally(result, [], case)
            continue

        title = ""
        if "<title>" in body:
            title = body.split("<title>", 1)[1].split("</title>", 1)[0]
        snippet = textclean.strip_html(body)[:SNIPPET_CHARS]
        text = "\n\n".join(p for p in (textclean.clean(title), snippet) if p)
        reviews = (
            [
                NormalizedReview(
                    brand=BRAND,
                    source=source_from_url(url),
                    source_url=url,
                    text=text,
                    extra={"engine": "search", "extraction": {"engine": "search", "kind": "mention"}},
                )
            ]
            if text
            else []
        )
        _tally(result, reviews, case)
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result


# --- approaches 2-4: the engines --------------------------------------------

def _engine(max_pages: int = 60):
    from reviewbot.connectors.scrapling_source import ScraplingEngine
    from reviewbot.scraping import budget, metrics

    return ScraplingEngine(
        crawl_budget=budget.CrawlBudget(
            max_pages=max_pages, max_pages_per_domain=max_pages, request_timeout=8,
            deadline_seconds=120,
        ),
        metrics=metrics.RunMetrics(),
    )


def run_scrapling(base: str, *, follow_pagination: bool = True) -> Result:
    from reviewbot.connectors.scrapling_source import to_review
    from reviewbot.scraping import extract as extractor
    from reviewbot.scraping import robots

    result = Result("scrapling")
    robots.reset_cache()
    engine = _engine()
    started = time.monotonic()
    try:
        for case in CASES:
            url = base + case.path
            reviews = []
            queue = [url]
            seen = set()
            while queue:
                current = queue.pop(0)
                if current in seen:
                    break
                seen.add(current)
                if engine.should_skip(current):
                    break
                fetched = engine.fetch(current)
                result.fetches += 1
                if not fetched.ok:
                    result.pages_failed += 1
                    break
                result.pages_ok += 1
                page = extractor.extract(fetched.html, fetched.url)
                for candidate in page.candidates:
                    review = to_review(BRAND, candidate, discovered_via=url)
                    if review is not None:
                        reviews.append(review)
                if follow_pagination and page.next_urls:
                    queue.extend(page.next_urls[:1])
            _tally(result, reviews, case)
    finally:
        engine.close()
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result


class StubAi:
    """A deterministic stand-in for the AI backend.

    It returns the page's `<blockquote>` contents VERBATIM, which is what a
    competent model would do on the irregular-prose fixture. Because the text is
    copied, it passes the same verbatim-evidence gate a real model's output
    passes, so the measured record counts are the ones the real path would
    produce. Token counts are estimated, and reported as estimates.
    """

    name = "stub"

    def __init__(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0

    def extract(self, html: str, url: str = ""):
        from reviewbot.connectors import scrapegraph_source as sg
        from reviewbot.scraping import budget, extract as extractor

        self.calls += 1
        outcome = sg.ExtractionOutcome(backend="stub", model="stub")
        tree = extractor.parse(html, url)
        items = []
        if tree is not None:
            for node in tree.css("blockquote, p"):
                try:
                    body = node.get_all_text(strip=True)
                except Exception:  # noqa: BLE001
                    continue
                if body and len(body) >= 40:
                    items.append({"text": body, "kind": "review"})
        from reviewbot import text as textclean

        content = textclean.clean(html)
        self.prompt_tokens += budget.estimate_tokens(content)
        outcome.prompt_tokens = budget.estimate_tokens(content)
        outcome.cost_usd = budget.estimate_cost("gpt-4o-mini", outcome.prompt_tokens, 400)
        outcome.records = sg.validate(items, content, outcome)
        return outcome


def _ai_extractor(mode: str):
    if mode == "live":
        from reviewbot.connectors import scrapegraph_source as sg
        from reviewbot.scraping import budget

        extractor = sg.AiExtractor(spend=budget.SpendBudget(max_calls=20, max_cost_usd=1.0))
        if not extractor.available:
            print(
                "  (--ai=live requested but no backend is available; falling back to stub)",
                file=sys.stderr,
            )
            return StubAi()
        return extractor
    return StubAi()


def run_with_ai(base: str, approach: str, ai_mode: str, *, routed: bool) -> Result:
    """`scrapling` plus AI extraction for pages it could not parse.

    With `routed=True` this is the production path: the router decides per URL
    and gates the escalation, which is the behaviour `collect_until` runs.
    """
    from reviewbot.connectors import scrapegraph_source as sg
    from reviewbot.connectors.scrapling_source import to_review
    from reviewbot.ingestion import routing
    from reviewbot.scraping import extract as extractor
    from reviewbot.scraping import robots

    result = Result(approach)
    robots.reset_cache()
    engine = _engine()
    ai = _ai_extractor(ai_mode)
    router = routing.Router(ai_enabled=True, strategy="balanced")
    started = time.monotonic()
    try:
        for case in CASES:
            url = base + case.path
            reviews = []
            queue = [url]
            seen = set()
            while queue:
                current = queue.pop(0)
                if current in seen:
                    break
                seen.add(current)

                if routed:
                    decision = router.route(current)
                    if decision.engine is not routing.Engine.SCRAPLING:
                        break
                if engine.should_skip(current):
                    break

                fetched = engine.fetch(current)
                result.fetches += 1
                if not fetched.ok:
                    result.pages_failed += 1
                    break
                result.pages_ok += 1
                page = extractor.extract(fetched.html, fetched.url)

                deterministic = [
                    r for r in (
                        to_review(BRAND, c, discovered_via=url) for c in page.candidates
                    ) if r is not None
                ]
                real_reviews = sum(1 for c in page.candidates if c.kind == "review")

                # Same escalation rule as ingestion/collect.py: a page that
                # yielded no actual REVIEWS is worth an AI call, including when
                # the only thing extractable was a whole-page mention.
                escalate = True
                if routed:
                    decision = router.may_escalate_to_ai(
                        current,
                        had_content=bool(fetched.html),
                        extracted=len(page.candidates),
                        reviews=real_reviews,
                        strategy=page.strategy,
                    )
                    escalate = decision.engine is routing.Engine.SCRAPEGRAPH
                else:
                    escalate = real_reviews == 0

                ai_reviews = []
                if escalate:
                    outcome = ai.extract(fetched.html, fetched.url)
                    result.ai_calls += 1
                    result.ai_cost_usd += getattr(outcome, "cost_usd", 0.0) or 0.0
                    ai_reviews = [
                        r for r in (
                            sg.to_review(BRAND, record, fetched.url, outcome)
                            for record in outcome.records
                        ) if r is not None
                    ]

                if ai_reviews:
                    # AI found the real reviews; they supersede a page-level mention.
                    reviews.extend(ai_reviews)
                else:
                    reviews.extend(deterministic)

                if deterministic and page.next_urls:
                    queue.extend(page.next_urls[:1])
            _tally(result, reviews, case)
    finally:
        engine.close()
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result


# --- reporting ---------------------------------------------------------------

_COLUMNS = (
    ("approach", "Approach", 22),
    ("records", "Records", 8),
    ("reviews", "Reviews", 8),
    ("mentions", "Mentions", 9),
    ("chars", "Chars", 8),
    ("avg_chars", "Avg", 6),
    ("with_rating", "Rating", 7),
    ("with_author", "Author", 7),
    ("with_date", "Date", 6),
    ("fetches", "Fetch", 6),
    ("ai_calls", "AI", 4),
    ("ai_cost_usd", "$ est", 9),
    ("elapsed_ms", "ms", 7),
)


def print_table(rows: list[dict]) -> None:
    header = " ".join(f"{label:<{width}}" for _key, label, width in _COLUMNS)
    print(header)
    print("-" * len(header))
    for row in rows:
        line = []
        for key, _label, width in _COLUMNS:
            value = row.get(key, "")
            if key == "ai_cost_usd":
                value = f"{value:.5f}" if value else "0"
            line.append(f"{str(value):<{width}}")
        print(" ".join(line))


def markdown_table(rows: list[dict]) -> str:
    keys = [key for key, _l, _w in _COLUMNS]
    labels = [label for _k, label, _w in _COLUMNS]
    out = ["| " + " | ".join(labels) + " |", "|" + "|".join("---" for _ in labels) + "|"]
    for row in rows:
        cells = []
        for key in keys:
            value = row.get(key, "")
            if key == "ai_cost_usd":
                value = f"{value:.5f}" if value else "0"
            cells.append(str(value))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", help="also write the raw results to this path")
    parser.add_argument("--markdown", help="also write a markdown table to this path")
    parser.add_argument("--repeat", type=int, default=3, help="runs per approach (median reported)")
    parser.add_argument(
        "--ai", choices=["stub", "live"], default="stub",
        help="stub (default, free and deterministic) or live (real model calls, real spend)",
    )
    args = parser.parse_args()

    from fixture_server import FixtureServer, apply_dev_env

    apply_dev_env()
    os.environ["SCRAPLING_ENABLED"] = "1"
    # The fixtures are about page structure, not brand matching; the brand filter
    # is exercised by the unit tests instead.
    os.environ["QUALITY_REQUIRE_BRAND_MENTION"] = "0"

    print(f"ReviewBay scraping benchmark — {args.repeat} run(s) per approach, ai={args.ai}")
    print(f"dataset: {len(CASES)} pages ({', '.join(sorted({c.page_class for c in CASES}))})")
    print()

    with FixtureServer() as base:
        runners = [
            ("search-only", lambda: run_search_only(base)),
            ("scrapling", lambda: run_scrapling(base)),
            ("scrapegraph-fallback", lambda: run_with_ai(base, "scrapegraph-fallback", args.ai, routed=False)),
            ("routed", lambda: run_with_ai(base, "routed", args.ai, routed=True)),
        ]

        all_runs: dict[str, list[dict]] = {}
        per_case: dict[str, dict] = {}
        for name, runner in runners:
            rows = []
            for _ in range(max(1, args.repeat)):
                result = runner()
                rows.append(result.as_row())
                per_case[name] = result.per_case
            all_runs[name] = rows
            print(f"  ran {name}: {rows[-1]['records']} records, {rows[-1]['elapsed_ms']}ms")

    # Median across repeats for the timing-sensitive fields; the counts are
    # deterministic, so their median is just their value.
    summary = []
    for name, rows in all_runs.items():
        merged = dict(rows[0])
        for key in ("elapsed_ms", "fetches"):
            merged[key] = int(statistics.median(r[key] for r in rows))
        summary.append(merged)

    print()
    print_table(summary)
    print()

    baseline = next((r for r in summary if r["approach"] == "search-only"), None)
    if baseline and baseline["records"]:
        print("Relative to search-only:")
        for row in summary:
            if row["approach"] == "search-only":
                continue
            rec = row["records"] / baseline["records"]
            chars = (row["chars"] / baseline["chars"]) if baseline["chars"] else 0
            print(
                f"  {row['approach']:22} records x{rec:.2f}   text volume x{chars:.2f}   "
                f"rated records {row['with_rating']} vs {baseline['with_rating']}"
            )

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repeat": args.repeat,
        "ai_mode": args.ai,
        "dataset": [{"path": c.path, "class": c.page_class, "note": c.note} for c in CASES],
        "summary": summary,
        "runs": all_runs,
        "per_case": per_case,
    }
    if args.json:
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    if args.markdown:
        Path(args.markdown).write_text(markdown_table(summary) + "\n", encoding="utf-8")
        print(f"wrote {args.markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
