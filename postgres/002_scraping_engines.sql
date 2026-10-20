-- ReviewBay migration 002 — indexes and run-metrics columns.
--
-- Run once against DATABASE_URL, after postgres/ddl.sql:
--     psql "$DATABASE_URL" -f postgres/002_scraping_engines.sql
--
-- Everything here is ADDITIVE and idempotent. No column is dropped, no type is
-- changed, and the pgvector dimension contract (vector(768)) is untouched, so
-- an older build of the app keeps working against a migrated database and this
-- file can be re-run safely.

-- ---------------------------------------------------------------------------
-- 1. Enrichment was O(n²/batch).
--
-- enrich() finds pending rows with
--     raw.reviews_raw LEFT JOIN marts.reviews ... LIMIT :batch
-- in a loop until the query returns nothing. With no index on raw.reviews_raw
-- every iteration re-scanned raw and re-joined marts to produce one batch, so
-- enriching 3,000 rows ran ~188 full joins.
--
-- The join is on id (already the primary key on both sides); what was missing
-- was any way to narrow or order the raw side cheaply.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS reviews_raw_brand_idx       ON raw.reviews_raw (brand);
CREATE INDEX IF NOT EXISTS reviews_raw_captured_idx    ON raw.reviews_raw (captured_at);
CREATE INDEX IF NOT EXISTS reviews_raw_source_idx      ON raw.reviews_raw (source);

-- ---------------------------------------------------------------------------
-- 2. Dashboard and QC filter predicates.
--
-- Every stats query is `WHERE brand ILIKE :brand AND relevant IS NOT FALSE`,
-- and the QC pass scans `WHERE qc_checked IS NOT TRUE`. Both were unindexed.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS reviews_brand_relevant_idx
    ON marts.reviews (brand, relevant);
CREATE INDEX IF NOT EXISTS reviews_qc_pending_idx
    ON marts.reviews (brand, captured_at DESC)
    WHERE qc_checked IS NOT TRUE;
CREATE INDEX IF NOT EXISTS reviews_brand_sentiment_idx
    ON marts.reviews (brand, sentiment);

-- ---------------------------------------------------------------------------
-- 3. Per-run collection metrics.
--
-- marts.scrape_runs already recorded brand / sources / count / status /
-- duration. These columns add the engine-level detail the collection-health
-- panel and the benchmarks need, all nullable so existing rows stay valid.
--
--   engines      which extraction engines ran, comma separated
--   by_engine    {engine: {attempted, valid, duplicates, errors, ms, ...}}
--   target       the collection target this run aimed at
--   floor        the floor it tried to clear
--   ai_calls     AI-assisted extraction calls made
--   ai_cost_usd  estimated spend on those calls
-- ---------------------------------------------------------------------------
ALTER TABLE marts.scrape_runs ADD COLUMN IF NOT EXISTS engines     text;
ALTER TABLE marts.scrape_runs ADD COLUMN IF NOT EXISTS by_engine   jsonb;
ALTER TABLE marts.scrape_runs ADD COLUMN IF NOT EXISTS target      integer;
ALTER TABLE marts.scrape_runs ADD COLUMN IF NOT EXISTS floor       integer;
ALTER TABLE marts.scrape_runs ADD COLUMN IF NOT EXISTS ai_calls    integer;
ALTER TABLE marts.scrape_runs ADD COLUMN IF NOT EXISTS ai_cost_usd double precision;

CREATE INDEX IF NOT EXISTS scrape_runs_brand_started_idx
    ON marts.scrape_runs (brand, started_at DESC);

-- ---------------------------------------------------------------------------
-- 4. Extraction cache for AI-assisted extraction.
--
-- Keyed by a hash of (content, objective, model) so the same page is never paid
-- for twice, including across process restarts and across brands that happen to
-- surface the same URL. `expires_at` lets a deployment age entries out; nothing
-- reads a row past it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marts.extraction_cache (
    cache_key    text PRIMARY KEY,       -- sha256(content_hash|objective|model)
    source_url   text,
    engine       text NOT NULL,          -- which engine produced this
    model        text,
    payload      jsonb NOT NULL,         -- the validated extraction result
    items        integer NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz
);
CREATE INDEX IF NOT EXISTS extraction_cache_expires_idx
    ON marts.extraction_cache (expires_at);

-- ---------------------------------------------------------------------------
-- 5. Per-domain extraction outcomes, for routing.
--
-- The router prefers engines that have actually worked on a domain before. This
-- table is that memory: one row per (domain, engine), updated in place.
-- It is advisory only — an empty table just means the router falls back to its
-- static rules, so losing it costs nothing but a little efficiency.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS marts.engine_stats (
    domain        text NOT NULL,
    engine        text NOT NULL,
    attempts      integer NOT NULL DEFAULT 0,
    successes     integer NOT NULL DEFAULT 0,
    items         integer NOT NULL DEFAULT 0,
    total_ms      bigint  NOT NULL DEFAULT 0,
    last_status   text,
    last_attempt  timestamptz,
    PRIMARY KEY (domain, engine)
);
