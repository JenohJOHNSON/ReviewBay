"""ReviewBay pipeline DAG.

    scrape_reddit ┐
    scrape_gmaps  ├─(parallel)─▶ normalize_airbyte ─▶ transform ─▶ (chatbot sees it)
    scrape_yelp   │
    scrape_tripadvisor ┘

- One scrape task per source, so cadences/retries/failures are independent.
- normalize_airbyte folds Airbyte's app-store rows into RAW.REVIEWS_RAW.
- transform embeds + scores everything in RAW into MARTS (what the bot queries).

Runtime needs: the `reviewbot` package on PYTHONPATH + source creds (Reddit,
Apify, Snowflake) in the environment, and an Airflow Snowflake connection named
`snowflake_default`. The Dockerfile / compose in this folder set all of that up.
"""

from __future__ import annotations

import pendulum
from airflow.models.dag import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator

from reviewbot.enrich.run import enrich
from reviewbot.ingestion.run import run_source

SOURCES = ["reddit", "web", "app_store", "google_play", "facebook", "google_maps", "yelp", "tripadvisor"]

default_args = {
    "retries": 2,
    "retry_delay": pendulum.duration(minutes=5),
}

with DAG(
    dag_id="reviewbot_pipeline",
    description="Scrape sources, normalize Airbyte app reviews, embed into MARTS.",
    schedule="*/30 * * * *",  # every 30 min; tighten toward near-real-time as needed
    start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,  # avoid overlapping loads into RAW
    default_args=default_args,
    template_searchpath=["/opt/airflow/sql"],  # where the .sql files are mounted
    tags=["reviewbot", "etl"],
) as dag:

    # One task per source — a failing/slow source doesn't block the others.
    scrape_tasks = [
        PythonOperator(
            task_id=f"scrape_{source}",
            python_callable=run_source,
            op_args=[source],
        )
        for source in SOURCES
    ]

    # Fold Airbyte's landed app-store/google-play reviews into RAW.REVIEWS_RAW.
    # No-op (0 rows) until Airbyte is configured — safe to keep in the DAG.
    normalize_airbyte = SQLExecuteQueryOperator(
        task_id="normalize_airbyte",
        conn_id="snowflake_default",
        sql="normalize_airbyte.sql",
        split_statements=True,
    )

    # Embed + score everything in RAW into MARTS (local model, no Cortex).
    transform = PythonOperator(
        task_id="enrich",
        python_callable=enrich,
    )

    scrape_tasks >> normalize_airbyte >> transform

    # --- Optional: let Airflow TRIGGER the Airbyte sync before normalizing -----
    # Airbyte Cloud already runs on its own cron (set in airbyte/main.tf), so this
    # is only needed if you want Airflow to control timing. Requires an Airflow
    # 'airbyte_default' connection + the connection UUIDs from Terraform output.
    #
    # from airflow.providers.airbyte.operators.airbyte import AirbyteTriggerSyncOperator
    # appstore_sync = AirbyteTriggerSyncOperator(
    #     task_id="appstore_sync", airbyte_conn_id="airbyte_default",
    #     connection_id="<app_store connection uuid>", asynchronous=False,
    # )
    # appstore_sync >> normalize_airbyte
