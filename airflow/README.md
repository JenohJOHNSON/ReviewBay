# Airflow orchestration

Runs the whole pipeline on a schedule instead of you invoking SQL by hand:

```
scrape_reddit ┐
scrape_gmaps  ├─(parallel)─▶ normalize_airbyte ─▶ transform ─▶ chatbot sees it
scrape_yelp   │
scrape_tripadvisor ┘
```

- **One task per source** — a slow/failing source retries independently and
  doesn't block the others.
- **normalize_airbyte** folds Airbyte's app-store/Google-Play rows into
  `RAW.REVIEWS_RAW` (no-op until Airbyte is set up — safe to leave in).
- **transform** embeds + scores everything in `RAW` into `MARTS`.

DAG: [`dags/reviewbot_pipeline.py`](dags/reviewbot_pipeline.py). Default schedule
is every 30 min (`schedule="*/30 * * * *"`); tighten toward near-real-time there.

## Setup

1. **Snowflake connection for Airflow.** Add to your `.env` (repo root) — the SQL
   operators use it as `snowflake_default`:
   ```
   AIRFLOW_CONN_SNOWFLAKE_DEFAULT=snowflake://USER:PASSWORD@ACCOUNT/REVIEWBOT?warehouse=REVIEWBOT_WH&role=REVIEWBOT_ROLE
   ```
   (ACCOUNT is your identifier, e.g. `xy12345.us-east-1`. The scraper tasks reuse
   the `SNOWFLAKE_*` vars already in `.env`.)
2. **Start Airflow** (from the repo root):
   ```bash
   docker compose -f airflow/docker-compose.airflow.yml up --build
   ```
3. Open http://localhost:8080 (admin / admin), enable **`reviewbot_pipeline`**.

That's it — the DAG scrapes, normalizes, and embeds on schedule. The `api`
service (main `docker-compose.yml`) serves the chatbot over the results.

## Notes

- **Package + creds are baked in.** `airflow/Dockerfile` installs the ingestion
  deps and copies `src/` so `PythonOperator` can `import reviewbot`; `.env`
  supplies Reddit/Apify/Snowflake creds to the scrape tasks.
- **SQL files are mounted read-only** from `../snowflake` at `/opt/airflow/sql`
  (the DAG's `template_searchpath`). Edit the SQL, no rebuild needed.
- **Airbyte timing (optional).** Airbyte Cloud runs on its own cron, so the DAG
  just normalizes whatever has landed. To have Airflow *trigger* the sync first,
  uncomment the `AirbyteTriggerSyncOperator` block in the DAG and add an
  `airbyte_default` Airflow connection + the connection UUIDs.
- **Prod:** swap `LocalExecutor` for Celery/Kubernetes executor, set a real
  `AIRFLOW__CORE__FERNET_KEY` and admin password, and put secrets in a backend
  (this compose is for local/dev).
