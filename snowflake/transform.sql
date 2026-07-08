-- DEPRECATED under the free-trial / local-embedding setup.
--
-- This step used SNOWFLAKE.CORTEX.SENTIMENT + EMBED_TEXT_768, but those AI
-- functions are NOT available on Snowflake trial accounts. Enrichment (sentiment
-- + embedding) now runs in Python instead — see src/reviewbot/enrich/run.py —
-- which computes real vectors locally and writes them into MARTS.REVIEWS.
--
--   Run it with:  python -m reviewbot.enrich.run
--   (the ingestion container does this automatically after each scrape pass,
--    and the Airflow DAG runs it as the `enrich` task.)
--
-- If you UPGRADE to a paid Snowflake account and prefer in-warehouse Cortex
-- embeddings, you can restore the original MERGE from git history and skip the
-- Python enrich step. Both write the same MARTS.REVIEWS shape.

SELECT 'enrichment runs in Python now — see src/reviewbot/enrich/run.py' AS note;
