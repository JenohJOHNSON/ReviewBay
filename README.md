# ReviewBay

ReviewBay is a brand-reputation ETL pipeline plus RAG chatbot. It collects what
people say about a brand across the public web and app stores, stores reviews in
**Neon Postgres**, embeds them with a local model, and serves a **Claude**-powered
chatbot that answers with links back to the original reviews.

## Architecture

```text
Collectors ──► raw.reviews_raw ──► local embeddings + sentiment ──► marts.reviews
  web             Neon Postgres                         pgvector vector(768)
  App Store
  Google Play
  Apify actors                                             │
                                                           ▼
                                          FastAPI + Claude answers with citations
```

Every source implements one connector interface and yields the same
`NormalizedReview` record. That one normalized shape is what makes ingestion,
dedupe, enrichment, dashboards, and citations source-agnostic.

## Layout

```text
config/brands.yml            brands + keywords + enabled sources
postgres/schema.sql          Neon/Postgres schema + pgvector extension
src/reviewbot/models.py      NormalizedReview + stable id
src/reviewbot/connectors/    web, app-store, Play-store, Apify-backed sources
src/reviewbot/ingestion/     postgres_loader.py + run.py polling loop
src/reviewbot/enrich/        local embeddings + sentiment into marts.reviews
src/reviewbot/api/           FastAPI, dashboard APIs, RAG chat, static UI
docker/                      api and ingestion Dockerfiles
docker-compose.yml           app runtime
```

## Prerequisites

- A Neon Postgres database.
- `pgvector` enabled by running `postgres/schema.sql`.
- A Claude API key.
- An Apify token if you use `web`, `reddit`, `google_maps`, `yelp`,
  `tripadvisor`, `instagram`, or `facebook`.
- Docker Desktop for the easiest local run.

## Setup

1. Create a Neon project and copy the pooled or direct connection string. It
   should usually include `sslmode=require`.

2. Create `.env` in the repo root:

   ```bash
   cp .env.example .env
   ```

   Fill in `DATABASE_URL`, `ANTHROPIC_API_KEY`, and `APIFY_TOKEN` as needed.

3. Create the database schema:

   ```bash
   psql "$DATABASE_URL" -f postgres/schema.sql
   ```

   Or paste `postgres/schema.sql` into the Neon SQL editor.

4. Edit `config/brands.yml` for the brands and sources you want.

5. Run the app:

   ```bash
   docker compose up --build
   ```

   Open http://localhost:8000.

## Useful Commands

```bash
python3 scripts/check_neon.py
docker compose logs -f api
docker compose logs -f ingestion
```

Run locally without Docker:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export PYTHONPATH=src
set -a && source .env && set +a
export BRANDS_CONFIG=config/brands.yml

RUN_ONCE=1 python3 -m reviewbot.ingestion.run
uvicorn reviewbot.api.main:app --reload --port 8000
```

Run the API and ingestion commands in separate terminals for regular local dev.
The first embedding call can be slow because `fastembed` downloads and loads the
model.

## Main Endpoints

- `GET /healthz`
- `GET /` landing page
- `GET /start` add-a-brand onboarding
- `GET /dashboard`
- `GET /compare`
- `GET /chat` chat UI
- `POST /chat` RAG answer API

## Notes

- App Store reviews use Apple's public RSS feed.
- Google Play reviews use `google-play-scraper`.
- The web and most review-site collectors run through Apify.
- Instagram and Facebook are best-effort because Meta scraping is fragile and
  often blocked.
- The active application path uses Neon Postgres directly. No warehouse-specific
  setup is required.
