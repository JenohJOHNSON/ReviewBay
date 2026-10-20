# ReviewBay

**ReviewBay turns public customer reviews into brand-reputation intelligence:
give it any brand name and it scrapes reviews and mentions from across the web and
social, then serves a dashboard, a cited AI chat, and a per-brand intelligence
report.** ([case study](CASESTUDY.md))

Live: https://reviewbay-production.up.railway.app

Engineering documentation: [architecture audit](docs/ARCHITECTURE_AUDIT.md) ·
[scraping architecture](docs/SCRAPING_ARCHITECTURE.md) ·
[benchmarks](docs/SCRAPING_BENCHMARKS.md) · [handoff](HANDOFF.md)

It is a review-intelligence ETL pipeline plus a Retrieval-Augmented-Generation
(RAG) chatbot. It collects what people say about a brand, stores it in **Neon
Postgres** with **pgvector**, embeds it with the **OpenAI embeddings API**, and
answers questions **with links back to the original reviews**. A dashboard adds
sentiment, source breakdown, trends, and a deterministic **scikit-learn** review
summary (no LLM, so it is fast and reproducible).

---

## What it does

- **Onboard any brand.** Type a name (optionally a website). Collection starts
  immediately and the dashboard fills in as data arrives.
- **Scrape from anywhere, aiming for ~200 samples.** Free and open sources first,
  paid scrapers only as a last resort.
- **Cited chat.** Ask a question in plain English; every claim links to the exact
  review it came from, grounded only in retrieved reviews (no hallucinated facts).
- **Intelligence report.** A brand health score plus AI-synthesized recurring
  issues, purchase drivers, churn risks, and marketing angles, each cited.
- **Compare.** Two brands side by side (sentiment, rating, volume, health).

---

## Architecture

```
  collect_until orchestrator  (cloud-safe first, paid-last, target ~200 samples)
  ────────────────────────────────────────────────────────────────────────
   web        Tavily search  (fallback: Apify google-search)      ── breadth
   hackernews Algolia HN API             mastodon   public hashtag timelines
   app_store  Apple RSS      google_play  google-play-scraper
   reddit / trustpilot / firecrawl       opt-in direct connectors
                              │
   scrapling  READ the pages web search found, not just their snippets ── depth
              robots-aware, SSRF-guarded, lxml parsing; browser only when needed
                              │
   scrapegraphai  AI extraction, opt-in, ONLY for pages selectors cannot read
                  verbatim-verified against the page, hard spend ceiling
                              │
   instagram / facebook / google_maps / yelp / tripadvisor   ── Apify (last resort)
                              │
        every source yields the SAME NormalizedReview
        {brand, source, source_url, text, author, rating, created_at, extra}
        validated + deduplicated by quality.py before it is stored
                              ▼
        Neon  raw.reviews_raw     INSERT ... ON CONFLICT (id)   ← idempotent upsert,
                              │                                    id keyed by brand
                              │   enrich:  OpenAI embeddings (768-d) + VADER sentiment
                              ▼
        Neon  marts.reviews      embedding vector(768), HNSW cosine index
                              │   retrieval:  embedding <=> query  (pgvector)
                              ▼
        FastAPI  +  OpenAI Responses API   →   answer with inline [n] citations
                              ▼
        Web UI:  landing / onboarding / dashboard / report / compare / chat
```

**The core design bet:** every source implements one interface
(`connectors/base.py`) and yields the same `NormalizedReview`. Adding source #N is
a new plugin, not a rewrite, and that normalized record (which always carries
`source_url`) is exactly what powers the citations.

---

## Engineering decisions (the interesting parts)

These are the choices worth talking through:

**1. Scrape anywhere, cloud-safe first, paid-last.** Most platforms either block
scraping or charge for it. Instead of paying for everything, `collect_until`
(`ingestion/collect.py`) runs cloud-safe free/open sources first (web search,
Hacker News, Mastodon, app stores), and only falls back to paid Apify actors for
the walled gardens (Instagram/Facebook) if it still hasn't hit its floor. It
targets ~200 samples and degrades gracefully when the internet simply doesn't
have that many for a niche brand.

**2. Depth, not just breadth.** A search API returns a SERP snippet: a title and
two sentences, with no author, rating or date, because a search result has
nowhere to put them. So three search passes over the same forum thread returned
three ~200-character snippets *of that thread*. The `scrapling` engine takes the
URLs search already found and reads those pages, which turns one snippet into
every review on the page, with its author, rating and date. Measured on a fixed
10-page dataset: **2.3× the records, and 0 → 11 records carrying a star rating**
([benchmarks](docs/SCRAPING_BENCHMARKS.md)). Search still does the finding; this
only does the reading, which is why it replaced no existing connector.

**3. AI extraction as a narrow fallback, not a default.** `scrapegraphai` is
offered a page only when deterministic extraction found no reviews on it, so it
never pays for a fetch and never re-reads a page we could already parse. On the
benchmark it helps on exactly one page class out of ten (prose reviews with no
markup: 1 whole-page mention → 3 attributed reviews) and adds nothing elsewhere.
Everything it returns must appear **verbatim** in the fetched page or it is
discarded as paraphrased or invented, because a paraphrase stored as a quotation
is this product being wrong rather than a cosmetic issue.

**4. "Social SEO" discovery instead of fragile social scraping.** X, LinkedIn, and
Instagram block scrapers, but their public posts are indexed by search engines. So
their content is discovered through the web search and host-tagged into the Social
bucket (`sources.py`), no direct scraping, no ToS-evasion. A B2B brand with no
consumer app still surfaces LinkedIn, Reddit, and YouTube discussion this way.

**5. Resilience: it never returns nothing.** A dead source, a rate-limited IP, or a
broken brand website can't sink a run. The brand website is a *soft anchor* (it
scopes the search, then is dropped for a broad pass), and every source is wrapped
so one failure is skipped, not fatal.

**6. Idempotent, brand-keyed identity.** Each review's primary key is a hash of
`(brand, source, url, text)`. Re-scraping upserts instead of duplicating, and
keying on brand means the same "Brand A vs Brand B" article becomes two correctly
attributed rows instead of one whose brand flips. Brands never mix.

**7. Embeddings via API, not a local model (a production debugging story).** The
first version embedded locally with `fastembed`. It worked on a laptop but the
~1GB model **crashed the cloud container the instant enrichment started**, and
adding RAM didn't help. The fix was to move embeddings to the **OpenAI embeddings
API** (`text-embedding-3-small` at 768 dims via the `dimensions` parameter, a
drop-in for the existing `vector(768)` column). That removed the crashing
component entirely, shrank the Docker image, and made deploys faster. Sentiment
stayed local (VADER is a tiny lexicon with no memory cost).

**8. Deterministic insights, no LLM.** The dashboard "Review summary" is built with
**scikit-learn** (TF-IDF + KMeans) plus VADER, not an LLM, so it's fast, free, and
reproducible. The LLM is reserved for the two places it earns its keep: the chat
and the report synthesis, both grounded in retrieved reviews with citations.

**9. Operable by design.** `/healthz` is instant liveness only; the database probe
lives at `/readyz` (a slow health check could get the container restarted
mid-enrichment). Collection is serialized with a semaphore so two onboards can't
spike memory at once. A scrape-run history table records every collection.

---

## Tech stack

| Layer | Choice |
|-------|--------|
| Language / API | Python, FastAPI, Uvicorn |
| Storage | Neon Postgres + pgvector (`vector(768)`, HNSW cosine) |
| Embeddings | OpenAI `text-embedding-3-small` (768-d) |
| Chat | OpenAI Responses API (RAG), extractive fallback |
| Sentiment | vaderSentiment (local) |
| Insights | scikit-learn (TF-IDF + KMeans) |
| Scraping (discovery) | Tavily, Apify, Algolia HN, Mastodon API, Apple RSS, google-play-scraper; optional Reddit JSON/API |
| Scraping (depth) | **Scrapling** 0.5 — lxml parsing in the base image, optional fetcher/browser tiers |
| Scraping (AI fallback) | **ScrapeGraphAI** (opt-in) or a built-in OpenAI backend; verbatim-verified, budget-capped |
| Scraping (paid, last resort) | Firecrawl, Apify actors |
| Packaging | Docker, docker-compose |
| Hosting | Railway (API), Neon (DB) |

---

## Sources and categories

Sources are grouped into categories so the dashboard stays readable
(`src/reviewbot/sources.py`):

| Category | Sources | Cost |
|----------|---------|------|
| App Reviews | app_store, google_play | Free (Apple RSS + google-play-scraper) |
| Review Sites | trustpilot, google_maps, yelp, tripadvisor | via Apify (the self-hosted Trustpilot path now declines on robots.txt) |
| Social | reddit, mastodon, hackernews, twitter, linkedin, instagram, facebook, youtube | Free via web tagging/direct open APIs; IG/FB via Apify last-resort |
| Web & News | web | Tavily (cheap) or Apify search |

Worth knowing:
- **Reddit's API is closed to self-service**, so the direct `reddit` connector is
  opt-in. On Railway, Reddit is usually covered by web search and host-tagged as
  Social when reddit.com results appear.
- **The self-hosted Trustpilot connector declines.** It consults robots.txt like
  every other fetch path, and Trustpilot's robots.txt disallows unrecognised
  crawlers, so it logs why and yields nothing. The connector is kept rather than
  removed, because the check is what should decide, not its absence. For
  Trustpilot data use the Apify actor source, which is a path licensed to serve
  it.
- **`web` prefers Tavily** when `TAVILY_API_KEY` is set, else Apify google-search.
- **Apify is the last resort**, reached only when free sources fall short and a
  token is configured. No token means those sources are simply skipped.

---

## Repository layout

```
config/brands.yml              brands + keywords + sources (control surface)
config/extraction_strategies.yml  per-site CSS selectors for the depth engine
config/airbyte_sources.yml     Airbyte staging tables -> ReviewBay mappers
postgres/ddl.sql               raw + marts schemas, pgvector, HNSW index (run once)
scripts/migrate.py             applies postgres/*.sql in order; refuses destructive SQL
postgres/002_scraping_engines.sql  additive: indexes, run metrics, caches
src/reviewbot/
  models.py                    NormalizedReview + brand-keyed stable id
  db.py                        Neon connection, idle reuse, LIKE escaping
  sources.py                   source -> category taxonomy + host tagging
  text.py                      HTML/entity/whitespace cleaning (shared)
  quality.py                   validation, deduplication, provenance
  prompting.py                 fencing scraped text out of the instruction channel
  embeddings.py                OpenAI embeddings + local VADER sentiment
  runs.py                      scrape-run history + per-engine metrics
  connectors/                  base.py + one file per source (web, reddit_json,
                               hackernews, mastodon, tavily, firecrawl, playwright
                               (Trustpilot), app_store, google_play, apify_source,
                               scrapling_source, scrapegraph_source)
  scraping/                    shared machinery for the self-hosted engines
    urlpolicy.py               SSRF guard + URL canonicalization
    robots.py                  robots.txt + Crawl-delay
    budget.py                  page/time/concurrency bounds + AI spend ceiling
    extract.py                 jsonld -> microdata -> site -> structural -> article
    evidence.py                 verbatim verification of AI output
    metrics.py                 per-engine counters for the run record
  ingestion/
    collect.py                 collect_until orchestrator (cloud-safe first, target ~200)
    routing.py                 which engine reads which page, and when to stop
    run.py                     build_connectors + poll loop
    loader.py                  Neon upsert into raw.reviews_raw
  airbyte/normalize.py         Airbyte Cloud normalizer used by the worker
  worker.py                    Airbyte import + local sources + enrichment loop
  enrich/run.py                raw -> marts: OpenAI embed + sentiment
  api/
    main.py                    FastAPI app + routes + auth gate + /healthz /readyz
    rag.py                     retrieve (pgvector) + generate (cited answers)
    report.py                  intelligence report (facts + AI synthesis)
    insights.py                scikit-learn review summary (no LLM)
    stats.py  export.py        dashboard aggregations, CSV export
    health.py                  collection-health panel data
    static/                    the web UI (terminal-editorial design system)
docs/                          architecture audit, scraping architecture, benchmarks
scripts/                       benchmark_scraping.py, check_frontend_js.py, check_neon.py
docker/                        api.Dockerfile, worker.Dockerfile
docker-compose.yml             api + worker services
railway.json                   Railway build (api) config
```

The default Docker runtime uses the API plus the worker. Old warehouse and
Airflow orchestration files have been removed to keep the repo focused on the
current Neon/Postgres path.

---

## Run it locally

Prerequisites: a **Neon** Postgres project (free tier is fine), an **OpenAI** API
key, and Docker. Optional scraper keys: Tavily, Firecrawl, Apify.

```bash
# 1. Create the schema and apply any pending migrations. Idempotent, and the
#    runner refuses to execute anything that drops, truncates or retypes.
python scripts/migrate.py --dry-run   # see the plan without touching anything
python scripts/migrate.py             # apply it
#    No psql needed; it uses the psycopg connection the app already has. If you
#    would rather use psql directly:
#      psql "$DATABASE_URL" -f postgres/ddl.sql
#      psql "$DATABASE_URL" -f postgres/002_scraping_engines.sql

# 2. Configure
cp .env.example .env      # set DATABASE_URL, OPENAI_API_KEY, and any scraper keys
#   edit config/brands.yml for the brands/sources you want

# 3. Run
docker compose up --build
#   worker  -> imports Airbyte rows, runs local sources, enriches into marts
#   api     -> http://localhost:8000
```

Ask it something:

```bash
curl -s localhost:8000/chat -H 'content-type: application/json' \
  -d '{"question":"What do people complain about most?","brand":"Blue Bottle Coffee"}' | jq
```

Every answer includes `[n]` markers that link to the original review.

Without Docker:

```bash
pip install -r requirements.txt
export PYTHONPATH=src
set -a && source .env && set +a
RUN_ONCE=1 python -m reviewbot.worker          # one import/source/enrich pass
uvicorn reviewbot.api.main:app --reload        # app on :8000
```

Tests and benchmarks need no database, network or API keys:

```bash
pytest tests/ -q                               # 399 tests
python scripts/check_frontend_js.py            # the inline UI JS has no build step
python scripts/benchmark_scraping.py --repeat 5 # reproducible extraction benchmark
```

Optional scraping extras, deliberately kept out of the default images:

```bash
pip install -r requirements-scraping.txt       # Scrapling fetcher + browser tiers
python -m playwright install chromium          # ~400MB; put this on the worker only
pip install -r requirements-ai-extraction.txt  # the ScrapeGraphAI library (74 packages)
```

---

## Configuration

Key environment variables (all in `.env` locally, or the host's variables tab):

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | Neon connection string (required) |
| `OPENAI_API_KEY` | Embeddings + chat (required) |
| `OPENAI_MODEL` | Chat model; set to one your key supports |
| `TAVILY_API_KEY` | Web search (else falls back to Apify) |
| `FIRECRAWL_API_KEY` | Opt-in deep scrape |
| `AIRBYTE_SOURCE_MAP` | Airbyte staging table map (default `config/airbyte_sources.yml`) |
| `LOCAL_SOURCES` | Free sources the worker runs directly (default `app_store,google_play`) |
| `APIFY_TOKEN` | Walled-garden sources via Airbyte/Apify, or direct last resort |
| `AUTH_USER` / `AUTH_PASS` | Optional HTTP basic-auth gate on the whole app |
| `COLLECT_TARGET` / `COLLECT_FLOOR` | Sample target (default 200 / 150) |
| `SCRAPLING_ENABLED` | Depth engine: read the pages search found (default on) |
| `SCRAPLING_FETCH_TIER` | `auto` / `requests` / `scrapling` / `browser` |
| `SCRAPLING_MAX_PAGES` / `_PER_DOMAIN` | Fetch budget per run (40 / 8) |
| `SCRAPING_OBEY_ROBOTS` | Honour robots.txt and Crawl-delay (default on; leave it on) |
| `SCRAPEGRAPH_ENABLED` | AI-assisted extraction fallback (default **off**) |
| `SCRAPEGRAPH_MODEL` / `_MAX_CALLS_PER_RUN` / `_MAX_COST_PER_RUN` | AI spend controls |
| `SCRAPING_ENGINE_STRATEGY` | `cheap` / `balanced` / `thorough` routing posture |

Every scraping setting is documented with its operational implication in
[`.env.example`](.env.example); the full list and defaults are in
[`docs/SCRAPING_ARCHITECTURE.md`](docs/SCRAPING_ARCHITECTURE.md).

---

## Deploy

The app is hosted on **Railway** (built from `docker/api.Dockerfile` via
`railway.json`). A single API service handles both serving and onboarding-time
scraping, so it needs `DATABASE_URL`, `OPENAI_API_KEY`, and (for full coverage)
the scraper keys in its Variables. `/healthz` is the platform liveness check;
`/readyz` reports database connectivity.

A couple of hard-won deployment notes:
- On Railway, variables must be set on the **exact service and environment** that
  serves the domain; edits elsewhere silently don't apply.
- The API embeds via the OpenAI API (not a local model), so it runs comfortably on
  a small instance.

---

## Airbyte (optional)

Airbyte Cloud can land any Apify Dataset source into a Neon `airbyte` staging
schema on a schedule, and the worker reshapes it into `raw.reviews_raw` using the
same mappers the direct connectors use. The Python worker also keeps free local
sources such as App Store and Google Play running directly, so Airbyte is only
needed for the sources you choose to manage there.

---

## Caveats

- **Scraping ToS and legality** is the biggest real-world risk. The design prefers
  official APIs and search-indexed content, and treats third-party actors as
  best-effort. It does not attempt anti-bot evasion.
- **The chat only answers from retrieved reviews.** If nothing relevant is stored,
  it says so rather than guessing.
- **Sample counts are best-effort.** The ~200 target is a soft floor; a niche brand
  may have fewer public mentions in existence. The target is reached by
  collecting more genuine data, never by relaxing what counts as a record.
- **No JavaScript rendering by default.** The browser tier is an opt-in install
  and is kept off the API image, so client-rendered review pages yield nothing
  unless you enable it on a worker. This is measured, not assumed, in
  [the benchmarks](docs/SCRAPING_BENCHMARKS.md).
- **No anti-bot circumvention, ever.** A site that returns 403/429 or challenges
  us is a site we stop asking. Scrapling's Cloudflare-solving and
  fingerprint-spoofing features are not enabled and there is no setting that
  enables them.
- **AI extraction is narrow.** It helps on one page class out of the ten
  benchmarked. Enable it for prose-style review pages, not as a general scraper.
- **Turn on the password gate for real data.** `GET /api/reports/saved/{id}`
  takes a sequential id and is only protected by `AUTH_USER`/`AUTH_PASS`, so
  without them a visitor can enumerate saved reports. See
  [`docs/SCRAPING_ARCHITECTURE.md`](docs/SCRAPING_ARCHITECTURE.md) §6.
