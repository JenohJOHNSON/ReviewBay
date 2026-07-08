# ReviewBay: a case study

**ReviewBay turns public customer reviews into brand reputation intelligence,
with source-backed AI answers and dashboard-ready insights.**

A portfolio write-up focused on the business problem, not just the stack.

---

## Problem

Brands receive customer feedback across review sites, app stores, social
platforms, and public web mentions. That feedback is fragmented, hard to compare,
and hard to turn into action. A team that wants to know "what do customers
actually complain about, and what should we fix first?" has to read hundreds of
scattered reviews by hand, and still cannot cite the evidence.

## Solution

ReviewBay collects public customer feedback, enriches it with sentiment and topic
analysis, stores it with vector embeddings, and lets users ask source-backed
questions through an AI chat interface. On top of that sits a **Review
Intelligence Report**: a brand health score, the top praises and complaints,
recurring issues, purchase drivers, churn risks, marketing angles, and suggested
product fixes, each point cited back to a real review.

## How it works

```
Collectors (Tavily web, app stores, Apify, self-hosted Playwright)
      -> Neon Postgres (raw)
      -> local embeddings (fastembed) + sentiment (VADER)
      -> Neon + pgvector (marts)
      -> retrieval (cosine) + OpenAI  ->  answers WITH [n] citations
      -> dashboard: sentiment, source categories, ML themes, run history
      -> Review Intelligence Report (facts + AI synthesis), exportable
```

Every source implements one connector interface and yields the same normalized
record, so adding a source is a plugin, not a rewrite. That normalized record is
exactly what carries the citation from the raw scrape all the way to the answer.

## Impact

The product helps teams identify customer pain points, recurring praise themes,
brand risks, and marketing opportunities from real customer language, with the
evidence attached. What took hours of manual reading becomes a scored report and
a chat you can trust because every claim links to its source.

## Technical proof

- ETL pipeline with a single normalized contract across all sources.
- Neon Postgres with pgvector for storage and cosine retrieval.
- Local embeddings and sentiment (no per-token embedding cost, portable).
- RAG chat with OpenAI, grounded only in retrieved reviews, with inline citations
  and a confidence + evidence readout so it never overclaims.
- Local ML insights (TF-IDF + KMeans) for deterministic, free dashboard themes.
- Review Intelligence Report: deterministic facts plus one grounded AI synthesis.
- Observability: connector status and a scrape run history.
- Exports: CSV of reviews, print-to-PDF report, and saved report snapshots.
- Containerized; API hosted on Railway, ingestion runs where the scraper keys live.

## Honest limitations

- Some connectors (review-site actors, Trustpilot selectors) are wired but not
  yet verified against live output, so the reliable demo path is web + app stores.
- Scraping third-party sites carries ToS and reliability risk; official APIs are
  preferred where available.
