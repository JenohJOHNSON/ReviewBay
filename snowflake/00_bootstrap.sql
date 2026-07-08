-- ReviewBay Snowflake bootstrap — run ONCE, as ACCOUNTADMIN, before ddl.sql.
-- Creates the role, warehouse, database, Cortex access, and the app user.
-- Names match .env.example (REVIEWBOT_WH / REVIEWBOT_ROLE / REVIEWBOT / REVIEWBOT_USER).
--
-- ▶ Run in a Snowsight worksheet, or: snowsql -f snowflake/00_bootstrap.sql
-- ▶ Then run ddl.sql (as ACCOUNTADMIN too — the FUTURE grants below hand the
--   tables it creates to REVIEWBOT_ROLE automatically).

USE ROLE ACCOUNTADMIN;

-- 1. Application role (kept under SYSADMIN in the hierarchy)
CREATE ROLE IF NOT EXISTS REVIEWBOT_ROLE;
GRANT ROLE REVIEWBOT_ROLE TO ROLE SYSADMIN;

-- 2. Warehouse — XSMALL, auto-suspend fast so it costs ~nothing when idle
CREATE WAREHOUSE IF NOT EXISTS REVIEWBOT_WH
  WAREHOUSE_SIZE       = 'XSMALL'
  AUTO_SUSPEND         = 60
  AUTO_RESUME          = TRUE
  INITIALLY_SUSPENDED  = TRUE;
GRANT USAGE, OPERATE ON WAREHOUSE REVIEWBOT_WH TO ROLE REVIEWBOT_ROLE;

-- 3. Database + privileges for the role.
--    FUTURE grants are set BEFORE ddl.sql runs, so every schema/table it
--    creates is automatically usable by REVIEWBOT_ROLE.
CREATE DATABASE IF NOT EXISTS REVIEWBOT;
GRANT USAGE ON DATABASE REVIEWBOT                       TO ROLE REVIEWBOT_ROLE;
GRANT ALL   ON FUTURE SCHEMAS IN DATABASE REVIEWBOT     TO ROLE REVIEWBOT_ROLE;
GRANT ALL   ON FUTURE TABLES  IN DATABASE REVIEWBOT     TO ROLE REVIEWBOT_ROLE;
-- Also cover anything that already exists (safe re-run):
GRANT ALL   ON ALL SCHEMAS IN DATABASE REVIEWBOT        TO ROLE REVIEWBOT_ROLE;
GRANT ALL   ON ALL TABLES  IN DATABASE REVIEWBOT        TO ROLE REVIEWBOT_ROLE;

-- 4. Cortex access — REQUIRED. Without CORTEX_USER, EMBED_TEXT_768 / SENTIMENT
--    (used by transform.sql and the RAG retrieval query) fail with a grant error.
GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE REVIEWBOT_ROLE;

-- 5. App user — this is what the connectors, transform, and API connect as.
--    ⚠️ CHANGE THE PASSWORD before running, and put the same value in .env.
CREATE USER IF NOT EXISTS REVIEWBOT_USER
  PASSWORD             = 'CHANGE_ME_strong_password'
  DEFAULT_ROLE         = REVIEWBOT_ROLE
  DEFAULT_WAREHOUSE    = REVIEWBOT_WH
  DEFAULT_NAMESPACE    = REVIEWBOT.RAW
  MUST_CHANGE_PASSWORD = FALSE
  COMMENT              = 'ReviewBay service account';
GRANT ROLE REVIEWBOT_ROLE TO USER REVIEWBOT_USER;

-- 6. Show the account identifier you need for SNOWFLAKE_ACCOUNT in .env
--    (use the ORG-ACCOUNT form, e.g. MYORG-MYACCT).
SELECT CURRENT_ORGANIZATION_NAME() AS org, CURRENT_ACCOUNT_NAME() AS account,
       CURRENT_ORGANIZATION_NAME() || '-' || CURRENT_ACCOUNT_NAME() AS snowflake_account_env;
