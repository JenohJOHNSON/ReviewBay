-- ReviewBay Neon Postgres schema. Run once in the Neon SQL editor or with psql:
--
--   psql "$DATABASE_URL" -f postgres/schema.sql
--
-- Neon supports pgvector on all plans. This app computes embeddings locally and
-- stores them in marts.reviews.embedding, then queries with pgvector cosine
-- distance.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS marts;

-- Landing zone: exactly what the connectors produced.
CREATE TABLE IF NOT EXISTS raw.reviews_raw (
    id           text PRIMARY KEY,
    brand        text NOT NULL,
    source       text NOT NULL,
    source_url   text,
    author       text,
    rating       double precision,
    text         text NOT NULL,
    created_at   text,
    captured_at  text,
    extra        jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- Enriched reviews: sentiment + vector embedding for retrieval.
CREATE TABLE IF NOT EXISTS marts.reviews (
    id           text PRIMARY KEY,
    brand        text NOT NULL,
    source       text NOT NULL,
    source_url   text,
    author       text,
    rating       double precision,
    text         text NOT NULL,
    sentiment    text,
    created_at   text,
    captured_at  text,
    embedding    vector(768)
);

CREATE INDEX IF NOT EXISTS reviews_raw_brand_idx ON raw.reviews_raw (brand);
CREATE INDEX IF NOT EXISTS reviews_brand_idx ON marts.reviews (brand);
CREATE INDEX IF NOT EXISTS reviews_sentiment_idx ON marts.reviews (sentiment);
CREATE INDEX IF NOT EXISTS reviews_source_idx ON marts.reviews (source);

-- Optional for larger datasets. Exact search is fine for small/free-tier demos.
-- CREATE INDEX IF NOT EXISTS reviews_embedding_hnsw_idx
--     ON marts.reviews USING hnsw (embedding vector_cosine_ops);
