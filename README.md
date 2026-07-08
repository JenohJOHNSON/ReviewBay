# ReviewBay

**ReviewBay turns public customer reviews into brand-reputation intelligence:
give it any brand name and it scrapes reviews and mentions from across the web and
social, then serves a dashboard, a cited AI chat, and a per-brand intelligence
report.** ([case study](CASESTUDY.md))

Live: https://reviewbay-production.up.railway.app

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
  collect_until orchestrator  (free-first, paid-last, target ~200 samples)
  ────────────────────────────────────────────────────────────────────────
   web        Tavily search  (fallback: Apify google-search)
   reddit     public search JSON        hackernews  Algolia HN API
   mastodon   public hashtag timelines  trustpilot  Playwright + Selectolax
   app_store  Apple RSS      google_play  google-play-scraper
   firecrawl  opt-in deep scrape
   instagram / facebook / google_maps / yelp / tripadvisor   ── Apify (last resort)
                              │
        every source yields the SAME NormalizedReview
        {brand, source, source_url, text, author, rating, created_at, extra}
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

**1. Scrape anywhere, free-first, paid-last.** Most platforms either block
scraping or charge for it. Instead of paying for everything, `collect_until`
(`ingestion/collect.py`) runs free/open sources first (web search, Reddit JSON,
Hacker News, Mastodon, app stores, Trustpilot), and only falls back to paid Apify
actors for the walled gardens (Instagram/Facebook) if it still hasn't hit its
floor. It targets ~200 samples and degrades gracefully when the internet simply
doesn't have that many for a niche brand.

**2. "Social SEO" discovery instead of fragile social scraping.** X, LinkedIn, and
Instagram block scrapers, but their public posts are indexed by search engines. So
their content is discovered through the web search and host-tagged into the Social
bucket (`sources.py`), no direct scraping, no ToS-evasion. A B2B brand with no
consumer app still surfaces LinkedIn, Reddit, and YouTube discussion this way.

**3. Resilience: it never returns nothing.** A dead source, a rate-limited IP, or a
broken brand website can't sink a run. The brand website is a *soft anchor* (it
scopes the search, then is dropped for a broad pass), and every source is wrapped
so one failure is skipped, not fatal.

**4. Idempotent, brand-keyed identity.** Each review's primary key is a hash of
`(brand, source, url, text)`. Re-scraping upserts instead of duplicating, and
keying on brand means the same "Brand A vs Brand B" article becomes two correctly
attributed rows instead of one whose brand flips. Brands never mix.

**5. Embeddings via API, not a local model (a production debugging story).** The
first version embedded locally with `fastembed`. It worked on a laptop but the
~1GB model **crashed the cloud container the instant enrichment started**, and
adding RAM didn't help. The fix was to move embeddings to the **OpenAI embeddings
API** (`text-embedding-3-small` at 768 dims via the `dimensions` parameter, a
drop-in for the existing `vector(768)` column). That removed the crashing
component entirely, shrank the Docker image, and made deploys faster. Sentiment
stayed local (VADER is a tiny lexicon with no memory cost).

**6. Deterministic insights, no LLM.** The dashboard "Review summary" is built with
**scikit-learn** (TF-IDF + KMeans) plus VADER, not an LLM, so it's fast, free, and
reproducible. The LLM is reserved for the two places it earns its keep: the chat
and the report synthesis, both grounded in retrieved reviews with citations.

**7. Operable by design.** `/healthz` is instant liveness only; the database probe
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
| Scraping | Tavily, Firecrawl, Apify, Reddit JSON, Algolia HN, Mastodon API, Apple RSS, google-play-scraper, Playwright + Selectolax |
| Packaging | Docker, docker-compose |
| Hosting | Railway (API), Neon (DB) |

---

## Sources and categories

Sources are grouped into categories so the dashboard stays readable
(`src/reviewbot/sources.py`):

| Category | Sources | Cost |
|----------|---------|------|
| App Reviews | app_store, google_play | Free (Apple RSS + google-play-scraper) |
| Review Sites | trustpilot, google_maps, yelp, tripadvisor | Trustpilot self-hosted (Playwright); rest via Apify |
| Social | reddit, mastodon, hackernews, twitter, linkedin, instagram, facebook, youtube | Free (direct APIs + web tagging); IG/FB via Apify last-resort |
| Web & News | web | Tavily (cheap) or Apify search |

Worth knowing:
- **Reddit's API is closed to self-service**, so `reddit` uses the public
  `search.json` (keyless). It works from residential IPs but is blocked on cloud
  IPs, where the web search covers Reddit instead. It degrades quietly.
- **`web` prefers Tavily** when `TAVILY_API_KEY` is set, else Apify google-search.
- **Apify is the last resort**, reached only when free sources fall short and a
  token is configured. No token means those sources are simply skipped.

---

## Repository layout

```
config/brands.yml              brands + keywords + sources (control surface)
postgres/ddl.sql               raw + marts schemas, pgvector, HNSW index (run once)
src/reviewbot/
  models.py                    NormalizedReview + brand-keyed stable id
  db.py                        Neon connection (reads DATABASE_URL)
  sources.py                   source -> category taxonomy + host tagging
  embeddings.py                OpenAI embeddings + local VADER sentiment
  runs.py                      scrape-run history
  connectors/                  base.py + one file per source (web, reddit_json,
                               hackernews, mastodon, tavily, firecrawl, playwright
                               (Trustpilot), app_store, google_play, apify_source)
  ingestion/
    collect.py                 collect_until orchestrator (free-first, target ~200)
    run.py                     build_connectors + poll loop
    loader.py                  Neon upsert into raw.reviews_raw
  enrich/run.py                raw -> marts: OpenAI embed + sentiment
  api/
    main.py                    FastAPI app + routes + auth gate + /healthz /readyz
    rag.py                     retrieve (pgvector) + generate (cited answers)
    report.py                  intelligence report (facts + AI synthesis)
    insights.py                scikit-learn review summary (no LLM)
    stats.py  export.py        dashboard aggregations, CSV export
    static/                    the web UI (terminal-editorial design system)
docker/                        api.Dockerfile, ingestion.Dockerfile
docker-compose.yml             api + ingestion services
railway.json                   Railway build (api) config
```

`snowflake/`, `airflow/`, and `airbyte/` are legacy/optional and not part of the
running system (see [Airbyte](#airbyte-optional) below).

---

## Run it locally

Prerequisites: a **Neon** Postgres project (free tier is fine), an **OpenAI** API
key, and Docker. Optional scraper keys: Tavily, Firecrawl, Apify.

```bash
# 1. Create the schema once
psql "$DATABASE_URL" -f postgres/ddl.sql

# 2. Configure
cp .env.example .env      # set DATABASE_URL, OPENAI_API_KEY, and any scraper keys
#   edit config/brands.yml for the brands/sources you want

# 3. Run
docker compose up --build
#   ingestion  -> scrapes on a loop, upserts raw, enriches into marts
#   api        -> http://localhost:8000
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
RUN_ONCE=1 python -m reviewbot.ingestion.run   # one scrape + enrich pass
uvicorn reviewbot.api.main:app --reload        # app on :8000
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
| `APIFY_TOKEN` | Walled-garden sources (last resort) |
| `AUTH_USER` / `AUTH_PASS` | Optional HTTP basic-auth gate on the whole app |
| `COLLECT_TARGET` / `COLLECT_FLOOR` | Sample target (default 200 / 150) |

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

`airbyte/` and `ingestion/airbyte_normalize.py` are scaffolding for a managed-ELT
route: Airbyte Cloud can land any Apify Dataset source into a Neon `airbyte`
staging schema on a schedule, and a small worker reshapes it into
`raw.reviews_raw` using the same mappers the connectors use. It is **not wired into
the running app**, the direct connectors do all ingestion. It exists as an
optional at-scale option.

---

## Caveats

- **Scraping ToS and legality** is the biggest real-world risk. The design prefers
  official APIs and search-indexed content, and treats third-party actors as
  best-effort. It does not attempt anti-bot evasion.
- **The chat only answers from retrieved reviews.** If nothing relevant is stored,
  it says so rather than guessing.
- **Sample counts are best-effort.** The ~200 target is a soft floor; a niche brand
  may have fewer public mentions in existence.
