# ReviewBay

A brand-reputation ETL pipeline + RAG chatbot. It scrapes what people say about
a brand across the public web (Reddit, Google Maps, Yelp, TripAdvisor — with
Instagram/Facebook as best-effort add-ons), lands it in **Snowflake**, embeds it
with **Snowflake Cortex**, and serves an interactable **Claude**-powered chatbot
that answers questions **with links back to the original reviews**.

## Architecture

```
 SCRAPERS (Docker) ────────────┐
 Reddit       ┐                 │  NormalizedReview
 Web mentions │                 │  {brand, source, source_url, text,
 Google Maps  ├─ Apify actors   │   author, rating, captured_at}
 Yelp         │  (one token)    │
 TripAdvisor ─┘                 ▼
                       Snowflake  RAW.REVIEWS_RAW    (MERGE, idempotent)
 AIRBYTE (Cloud) ──────────────▲       │
 App Store reviews    │        │       │  dbt/SQL: dedupe + Cortex sentiment + embed
 Google Play reviews  ├─ AIRBYTE.* ────┘       ▼
                      │  normalize_airbyte.sql   Snowflake  MARTS.REVIEWS   (VECTOR(768))
                                                     │  VECTOR_COSINE_SIMILARITY retrieval
                                                     ▼
                              FastAPI + Claude  → answer WITH inline [n] citations
                                                     ▼
                                             Chat UI  (/, static)
```

Two ingestion paths, one contract. **Custom scrapers** handle scrape-only sources
(Reddit, Google Maps, Yelp, TripAdvisor). **Airbyte Cloud** handles managed
API/connector sources (App Store + Google Play reviews) — it does the Extract +
Load, and `normalize_airbyte.sql` reshapes its output into the *same*
`RAW.REVIEWS_RAW` the scrapers feed. Everything downstream is source-agnostic.

The design bet: **every source implements one interface** (`connectors/base.py`)
and yields the same `NormalizedReview`. Adding source #N is a new plugin, not a
rewrite — and that normalized record is exactly what powers the citations.

## Layout

```
config/brands.yml            brands + keywords + which sources (the control surface)
snowflake/ddl.sql            RAW + MARTS + AIRBYTE schema (run once)
snowflake/normalize_airbyte.sql  AIRBYTE.* -> RAW.REVIEWS_RAW (app-store reviews)
snowflake/transform.sql      RAW -> MARTS: dedupe, sentiment, embed
airbyte/                     Airbyte Cloud config-as-code (Terraform) + setup guide
airflow/                     orchestration: DAG + Airflow compose (scrape→normalize→transform)
src/reviewbot/
  models.py                  NormalizedReview + stable id
  connectors/                base.py, reddit.py, apify_source.py
  ingestion/                 snowflake_loader.py (MERGE), run.py (poll loop)
  api/                       rag.py (retrieve+generate), main.py (FastAPI), static/
docker/                      api.Dockerfile, ingestion.Dockerfile
docker-compose.yml
```

## Prerequisites

- A Snowflake account **in a region with Cortex** (`SNOWFLAKE.CORTEX.*`).
- Credentials: Reddit API app, Apify token, and a Claude API key (or `ant auth login`).
- Docker Desktop.

## Setup

1. **Snowflake schema** — run the DDL once:
   ```bash
   snowsql -f snowflake/ddl.sql        # or paste into a worksheet
   ```
2. **Configure** — copy the env template and fill it in:
   ```bash
   cp .env.example .env
   # edit .env: Snowflake, ANTHROPIC_API_KEY, Reddit, APIFY_TOKEN
   ```
   Edit `config/brands.yml` for the brands you want to track.
3. **Run**:
   ```bash
   docker compose up --build
   ```
   - `ingestion` starts scraping on a loop and MERGEs into `RAW.REVIEWS_RAW`.
   - `api` serves the chatbot at http://localhost:8000
4. **Enrich** — turn raw reviews into embedded, searchable rows:
   ```bash
   snowsql -f snowflake/normalize_airbyte.sql   # only if Airbyte is set up (step 5)
   snowsql -f snowflake/transform.sql
   ```
   For near-real-time, schedule these as Snowflake Tasks (snippet at the bottom
   of `transform.sql`) or an Airflow DAG.
5. **(Optional) Airbyte — App Store + Google Play reviews.** Managed, no scraper
   code. Configure via Terraform and see the run order in
   [`airbyte/README.md`](airbyte/README.md). Airbyte lands reviews in the
   `AIRBYTE` schema; `normalize_airbyte.sql` folds them into `RAW.REVIEWS_RAW`.

## Try it

```bash
curl -s localhost:8000/chat -H 'content-type: application/json' \
  -d '{"question":"What do people complain about most?","brand":"Acme Coffee"}' | jq
```
Or open http://localhost:8000 and ask in the UI. Every answer cites `[n]`
markers that link to the original review.

## Local dev (without Docker)

```bash
pip install -r requirements.txt
export PYTHONPATH=src
set -a && source .env && set +a

RUN_ONCE=1 python -m reviewbot.ingestion.run      # one scrape pass
snowsql -f snowflake/transform.sql                # embed
uvicorn reviewbot.api.main:app --reload           # chatbot on :8000
```

## Claude auth

`api/rag.py` constructs `anthropic.Anthropic()` with no key, so it resolves
credentials from the environment: `ANTHROPIC_API_KEY`, or `ANTHROPIC_AUTH_TOKEN`,
or an `ant auth login` profile. Set `ANTHROPIC_API_KEY` in `.env` for the
container, or mount your profile.

Default model is `claude-sonnet-5` (override with `CLAUDE_MODEL`) — a good
speed/cost fit for a high-volume chatbot. The RAG prompt uses adaptive thinking
and **requires** inline citations grounded only in the retrieved reviews.

## Roadmap (build order)

1. ✅ **Skeleton** — Reddit + Apify → Snowflake → Cortex → FastAPI, dockerized.
2. ✅ **Airbyte Cloud** — App Store + Google Play reviews → Snowflake (config-as-code).
3. ✅ **Airflow** — DAG orchestrates scrapers + normalize + transform on a schedule
   ([`airflow/`](airflow/README.md)).
4. Add an **event bus** (Kafka/Redpanda) only if 15-min freshness isn't enough.
5. Bolt on **Instagram/Facebook** as best-effort connectors (Meta Graph API where
   permissioned; expect ToS limits and flakiness — treated as lower-cadence).

## Caveats

- **Scraping ToS/legality** is the biggest real-world risk, Instagram/Facebook
  especially. Prefer official APIs (Reddit, Google Business Profile, Meta Graph)
  where you have access; treat third-party actors as best-effort.
- Cortex functions must be enabled in your Snowflake region.
