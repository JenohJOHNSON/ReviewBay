-- ReviewBay Postgres schema (Neon + pgvector). Mirrors the old Snowflake schema
-- (snowflake/ddl.sql) one-to-one so the app logic stays the same:
--   STRING  -> text,  VARIANT -> jsonb,  VECTOR(FLOAT, 768) -> vector(768).
-- Embeddings + sentiment are still computed locally in Python (fastembed +
-- vaderSentiment); Postgres only stores the vectors and does the cosine search
-- via pgvector's `<=>` operator (see src/reviewbot/api/rag.py).
--
-- The bootstrap script runs this against DATABASE_URL; you do not run it by hand.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS raw;      -- landing zone, one row per scraped review
CREATE SCHEMA IF NOT EXISTS marts;    -- cleaned + enriched + embedded, serves the bot
CREATE SCHEMA IF NOT EXISTS airbyte;  -- Airbyte Cloud's own landing tables

-- Maps an app (Apple app_id / Google Play package_name) to a brand, so the
-- Airbyte normalize step can derive `brand` for per-app review streams.
CREATE TABLE IF NOT EXISTS airbyte.app_brand_map (
    app_id        text,   -- Apple numeric app id (for app_store rows)
    package_name  text,   -- Android package name (for google_play rows)
    brand         text NOT NULL
);

-- ---------------------------------------------------------------------------
-- raw: exactly what the connectors produced. The loader upserts here.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw.reviews_raw (
    id           text PRIMARY KEY,          -- stable hash (source, url, text)
    brand        text NOT NULL,
    source       text NOT NULL,             -- web | app_store | google_play | ...
    source_url   text,                      -- the citation link
    author       text,
    rating       double precision,          -- 1-5 where the source has one
    text         text NOT NULL,
    created_at   text,                       -- when the review was posted (source's own)
    captured_at  text,                       -- when we scraped it
    extra        jsonb                       -- source-specific fields
);

-- ---------------------------------------------------------------------------
-- marts: what the app queries. embedding is a 768-dim pgvector so retrieval is
-- a single cosine-distance query (embedding <=> query_vector).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marts.reviews (
    id           text PRIMARY KEY,
    brand        text NOT NULL,
    source       text NOT NULL,
    source_url   text,
    author       text,
    rating       double precision,
    text         text NOT NULL,
    sentiment    text,                       -- positive | neutral | negative
    created_at   text,
    captured_at  text,
    embedding    vector(768),
    relevant     boolean,                     -- LLM QC: is this actually about the brand?
    qc_checked   boolean DEFAULT false        -- has the QC pass looked at this row yet?
);

-- Approximate-nearest-neighbour index for fast cosine retrieval.
CREATE INDEX IF NOT EXISTS reviews_embedding_idx
    ON marts.reviews USING hnsw (embedding vector_cosine_ops);

-- Common filters used by the dashboard / sort feature.
CREATE INDEX IF NOT EXISTS reviews_brand_idx    ON marts.reviews (brand);
CREATE INDEX IF NOT EXISTS reviews_captured_idx ON marts.reviews (captured_at);

-- ---------------------------------------------------------------------------
-- scrape_runs: one row per collection run (per brand), for the run-history UI.
-- Written by run_brand; read by GET /api/runs. Pure observability, no FK.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marts.scrape_runs (
    id             bigserial PRIMARY KEY,
    brand          text NOT NULL,
    sources        text,                       -- comma-separated sources attempted
    reviews_found  integer NOT NULL DEFAULT 0,
    status         text NOT NULL,              -- ok | empty | error
    duration_ms    integer,
    started_at     timestamptz NOT NULL,
    finished_at    timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS scrape_runs_started_idx ON marts.scrape_runs (started_at DESC);

-- ---------------------------------------------------------------------------
-- saved_reports: point-in-time snapshots of a brand's intelligence report, so a
-- user can save one and revisit it later. payload is the full report JSON.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marts.saved_reports (
    id          bigserial PRIMARY KEY,
    brand       text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    payload     jsonb NOT NULL,
    token       text UNIQUE          -- unguessable id for a public /r/<token> share link
);
CREATE INDEX IF NOT EXISTS saved_reports_brand_idx ON marts.saved_reports (brand, created_at DESC);
