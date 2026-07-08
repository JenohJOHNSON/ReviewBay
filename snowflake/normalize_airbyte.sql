-- Normalize Airbyte-landed review streams into the shared RAW.REVIEWS_RAW.
--
-- Airbyte (Cloud) lands each connector's stream as its own typed table in the
-- AIRBYTE schema. This step reshapes those rows into the SAME NormalizedReview
-- contract the scrapers produce, so MARTS/transform.sql stays source-agnostic.
--
-- Run order:  airbyte sync -> THIS -> transform.sql -> chatbot
-- Idempotent: MERGE keyed on the same stable hash (source|url|text) the loader uses.
-- Unattended: `brand` comes from AIRBYTE.APP_BRAND_MAP (no bind parameter), so
--             Airflow can run this file directly. Seed that table per app id.
--
-- ⚠️ Column names below are the TYPICAL shape of app-review connectors. Confirm
-- them against your chosen connector's schema in Snowflake
-- (DESC TABLE AIRBYTE.APP_STORE_REVIEWS;) and adjust the SELECT mappings.

USE DATABASE REVIEWBOT;

MERGE INTO RAW.REVIEWS_RAW AS tgt
USING (
    -- ---- Apple App Store -------------------------------------------------
    SELECT
        SHA2(CONCAT_WS('|', 'app_store', COALESCE(r.review_url, r.id), r.body)) AS id,
        m.brand                                       AS brand,
        'app_store'                                   AS source,
        COALESCE(r.review_url,
                 'https://apps.apple.com/app/id' || r.app_id)  AS source_url,
        r.author_name                                 AS author,
        TRY_TO_NUMBER(r.rating)                        AS rating,
        NULLIF(TRIM(COALESCE(r.title, '')
               || CASE WHEN r.title IS NOT NULL AND r.body IS NOT NULL THEN ': ' ELSE '' END
               || COALESCE(r.body, '')), '')          AS text,
        TO_VARCHAR(r.updated_at)                      AS created_at,
        TO_VARCHAR(CURRENT_TIMESTAMP())               AS captured_at,
        OBJECT_CONSTRUCT('app_id', r.app_id, 'country', r.country) AS extra
    FROM AIRBYTE.APP_STORE_REVIEWS r
    JOIN AIRBYTE.APP_BRAND_MAP m ON m.app_id = r.app_id

    UNION ALL

    -- ---- Google Play -----------------------------------------------------
    SELECT
        SHA2(CONCAT_WS('|', 'google_play', COALESCE(r.review_url, r.review_id), r.content)) AS id,
        m.brand                                       AS brand,
        'google_play'                                 AS source,
        COALESCE(r.review_url,
                 'https://play.google.com/store/apps/details?id=' || r.package_name) AS source_url,
        r.author_name                                 AS author,
        TRY_TO_NUMBER(r.score)                        AS rating,
        NULLIF(TRIM(r.content), '')                   AS text,
        TO_VARCHAR(r.review_created_at)               AS created_at,
        TO_VARCHAR(CURRENT_TIMESTAMP())               AS captured_at,
        OBJECT_CONSTRUCT('package_name', r.package_name) AS extra
    FROM AIRBYTE.GOOGLE_PLAY_REVIEWS r
    JOIN AIRBYTE.APP_BRAND_MAP m ON m.package_name = r.package_name
) AS src
ON tgt.id = src.id
WHEN MATCHED THEN UPDATE SET
    captured_at = src.captured_at,
    rating      = src.rating,
    extra       = src.extra
WHEN NOT MATCHED AND src.text IS NOT NULL THEN INSERT
    (id, brand, source, source_url, author, rating, text, created_at, captured_at, extra)
    VALUES
    (src.id, src.brand, src.source, src.source_url, src.author, src.rating,
     src.text, src.created_at, src.captured_at, src.extra);

-- Reviews for an app with no AIRBYTE.APP_BRAND_MAP row are skipped by the JOIN
-- (unmapped app = we don't know whose brand it is). Add the mapping to include it.
