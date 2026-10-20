"""Shared machinery for the self-hosted extraction engines.

The specialized connectors in `reviewbot.connectors` each talk to one API that
someone else operates: Tavily, Apify, Apple, Algolia. They hand us URLs and we
store them. Nothing in that path ever *fetches* a URL chosen by a user or by a
web page.

Scrapling and ScrapeGraphAI do. That changes the threat model and the operational
burden, and this package is the part that has to be right before either engine is
allowed to run:

* `urlpolicy` — can we fetch this URL at all? Scheme, DNS resolution, private and
  link-local and cloud-metadata ranges, redirect re-validation, canonical form.
* `robots` — does the site's robots.txt allow it, and how long does it want us to
  wait between requests?
* `budget` — per-domain request budgets, page caps, bounded concurrency,
  rate limiting, and a spend ceiling for the AI engine.
* `extract` — turning a fetched page into candidate reviews, deterministically,
  with per-site strategies and a generic fallback.
* `metrics` — what actually happened, per engine, so the benchmarks and the
  collection-health panel are measured rather than asserted.

Everything here is pure Python with no hard dependency on either new library, so
it imports and tests fine on a deployment where neither is installed.
"""
