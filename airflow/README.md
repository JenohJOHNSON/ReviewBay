# Airflow orchestration

Runs ReviewBay on a schedule instead of using the ingestion service's internal
poll loop:

```text
scrape_web ┐
scrape_app ├─(parallel)─▶ enrich ─▶ chatbot sees it
scrape_etc ┘
```

- One task per source, so slow or failing sources retry independently.
- `enrich` embeds and scores everything in `raw.reviews_raw` into
  `marts.reviews`.
- The DAG uses the same `DATABASE_URL` / `NEON_DATABASE_URL` from `.env` as the
  main app.

## Setup

1. Make sure the root `.env` has your Neon, Claude, and source credentials.
2. Start Airflow from the repo root:

   ```bash
   docker compose -f airflow/docker-compose.airflow.yml up --build
   ```

3. Open http://localhost:8080 with `admin` / `admin`.
4. Enable `reviewbot_pipeline`.

The API service from the root `docker-compose.yml` serves the chatbot over the
same Neon database.

## Notes

- This compose is for local/dev Airflow only.
- For production, use real Airflow secrets, a non-default admin password, and a
  more durable executor.
