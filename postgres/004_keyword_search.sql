-- ReviewBay migration 004 — full-text index for keyword retrieval.
--
-- Apply with:   python scripts/migrate.py
--
-- Chat retrieval embeds the question and ranks reviews by vector distance. That
-- needs an embeddings provider, and when every provider is out of quota the
-- chat used to stop answering entirely: generation had a fallback, retrieval
-- did not. rag.py now falls back to Postgres full-text search, and this index is
-- what keeps that fallback from being a sequential scan.
--
-- The expression must stay EXACTLY `to_tsvector('english', text)`: Postgres only
-- uses an expression index for a query that repeats the same expression.
--
-- Built without CONCURRENTLY because the migration runner applies each file in
-- one transaction, and CONCURRENTLY cannot run inside one. On a table of a few
-- thousand reviews the build takes moments; on a very large table, create it
-- by hand with CONCURRENTLY first and this statement becomes a no-op.
CREATE INDEX IF NOT EXISTS reviews_text_fts_idx
    ON marts.reviews USING gin (to_tsvector('english', text));
