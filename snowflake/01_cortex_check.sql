-- Cortex smoke-test — run as REVIEWBOT_ROLE after 00_bootstrap.sql + ddl.sql.
-- Proves the ONE hard dependency (Cortex embeddings + sentiment + vector search)
-- works in your account/region BEFORE you wire up the pipeline. If any query
-- here errors, the region likely lacks Cortex or the CORTEX_USER grant is missing.

USE ROLE REVIEWBOT_ROLE;
USE WAREHOUSE REVIEWBOT_WH;
USE DATABASE REVIEWBOT;

-- 1. Sentiment (used by transform.sql). Expect a positive score (> 0.3).
SELECT SNOWFLAKE.CORTEX.SENTIMENT(
  'The coffee was amazing and the staff were incredibly friendly.'
) AS sentiment_should_be_positive;

-- 2. Embedding + vector similarity (used by rag.py retrieval).
--    Related sentences should score HIGH; unrelated should score LOW.
SELECT
  VECTOR_COSINE_SIMILARITY(
    SNOWFLAKE.CORTEX.EMBED_TEXT_768('snowflake-arctic-embed-m-v1.5', 'the coffee is excellent'),
    SNOWFLAKE.CORTEX.EMBED_TEXT_768('snowflake-arctic-embed-m-v1.5', 'great tasting espresso')
  ) AS related_should_be_high,
  VECTOR_COSINE_SIMILARITY(
    SNOWFLAKE.CORTEX.EMBED_TEXT_768('snowflake-arctic-embed-m-v1.5', 'the coffee is excellent'),
    SNOWFLAKE.CORTEX.EMBED_TEXT_768('snowflake-arctic-embed-m-v1.5', 'my flight was delayed three hours')
  ) AS unrelated_should_be_low;

-- 3. Confirm the app can write to and read the target tables (round-trip).
INSERT INTO RAW.REVIEWS_RAW (id, brand, source, source_url, text, captured_at)
SELECT '__cortex_check__', 'TestBrand', 'web', 'https://example.com/x',
       'placeholder review for the cortex check', TO_VARCHAR(CURRENT_TIMESTAMP());

SELECT COUNT(*) AS raw_rows_visible FROM RAW.REVIEWS_RAW WHERE id = '__cortex_check__';

DELETE FROM RAW.REVIEWS_RAW WHERE id = '__cortex_check__';  -- clean up
