-- ReviewBay Snowflake schema. Run once against your account (SnowSQL / worksheet).
--
-- NOTE: Cortex AI functions (SENTIMENT, EMBED_TEXT_768) are NOT available on
-- Snowflake trial accounts. This project therefore computes embeddings +
-- sentiment locally in Python (src/reviewbot/enrich/run.py) and stores the
-- vectors here; Snowflake only does vector math (VECTOR_COSINE_SIMILARITY),
-- which works on trials. Nothing below requires Cortex. Keep VECTOR(FLOAT, 768)
-- in sync with EMBED_DIM / the model in embeddings.py.

CREATE DATABASE IF NOT EXISTS REVIEWBOT;
USE DATABASE REVIEWBOT;

CREATE SCHEMA IF NOT EXISTS RAW;      -- landing zone, one row per scraped review
CREATE SCHEMA IF NOT EXISTS MARTS;    -- cleaned + enriched + embedded, serves the bot
CREATE SCHEMA IF NOT EXISTS AIRBYTE;  -- Airbyte's own landing tables (App Store / Google Play)

-- Airbyte (Cloud) writes its synced streams here with its own typed tables
-- (e.g. AIRBYTE.APP_STORE_REVIEWS, AIRBYTE.GOOGLE_PLAY_REVIEWS) plus
-- _AIRBYTE_* metadata columns. snowflake/normalize_airbyte.sql then reshapes
-- those into RAW.REVIEWS_RAW using the same NormalizedReview contract the
-- scrapers use, so everything downstream is source-agnostic.
-- Grant Airbyte's role write access to this schema (see airbyte/README.md).

-- Maps an app (Apple app_id / Google Play package_name) to a brand, so the
-- normalize step can derive `brand` for per-app review streams automatically
-- (no per-run parameter — this is what lets Airflow run it unattended).
CREATE TABLE IF NOT EXISTS AIRBYTE.APP_BRAND_MAP (
    app_id        STRING,   -- Apple numeric app id (for app_store rows)
    package_name  STRING,   -- Android package name (for google_play rows)
    brand         STRING NOT NULL
);
-- Seed it, e.g.:
-- INSERT INTO AIRBYTE.APP_BRAND_MAP (app_id, package_name, brand) VALUES
--   ('1234567890', NULL,                 'Acme Coffee'),
--   (NULL,         'com.globex.airline', 'Globex Airlines');

-- ---------------------------------------------------------------------------
-- RAW: exactly what the connectors produced. The ingestion loader MERGEs here.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS RAW.REVIEWS_RAW (
    id           STRING       NOT NULL PRIMARY KEY,  -- stable hash (source,url,text)
    brand        STRING       NOT NULL,
    source       STRING       NOT NULL,              -- reddit | google_maps | yelp | ...
    source_url   STRING,                             -- the citation link
    author       STRING,
    rating       FLOAT,                              -- 1-5 where the source has one
    text         STRING       NOT NULL,
    created_at   STRING,                             -- when the review was posted (source's own)
    captured_at  STRING,                             -- when we scraped it
    extra        VARIANT                             -- source-specific fields
);

-- ---------------------------------------------------------------------------
-- MARTS: what the chatbot queries. embedding is a 768-dim Cortex vector so
-- retrieval is a single VECTOR_COSINE_SIMILARITY query (see api/rag.py).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS MARTS.REVIEWS (
    id           STRING       NOT NULL PRIMARY KEY,
    brand        STRING       NOT NULL,
    source       STRING       NOT NULL,
    source_url   STRING,
    author       STRING,
    rating       FLOAT,
    text         STRING       NOT NULL,
    sentiment    STRING,                             -- Cortex sentiment bucket
    created_at   STRING,
    captured_at  STRING,
    embedding    VECTOR(FLOAT, 768)
);
