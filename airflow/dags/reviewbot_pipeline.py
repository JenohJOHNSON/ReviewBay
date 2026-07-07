"""ReviewBay pipeline DAG.

    scrape_reddit ┐
    scrape_web    ├─(parallel)─▶ enrich ─▶ (chatbot sees it)
    scrape_apps   │
    scrape_reviews┘

- One scrape task per source, so cadences/retries/failures are independent.
- enrich embeds + scores everything in raw into marts (what the bot queries).

Runtime needs: the `reviewbot` package on PYTHONPATH + source creds (Reddit,
Apify, Neon Postgres) in the environment. The Dockerfile / compose in this
folder set all of that up.
"""

from __future__ import annotations

import pendulum
from airflow.models.dag import DAG
from airflow.operators.python import PythonOperator

from reviewbot.enrich.run import enrich
from reviewbot.ingestion.run import run_source

SOURCES = ["reddit", "web", "app_store", "google_play", "facebook", "google_maps", "yelp", "tripadvisor"]

default_args = {
    "retries": 2,
    "retry_delay": pendulum.duration(minutes=5),
}

with DAG(
    dag_id="reviewbot_pipeline",
    description="Scrape sources and embed into Neon Postgres marts.",
    schedule="*/30 * * * *",  # every 30 min; tighten toward near-real-time as needed
    start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,  # avoid overlapping loads into raw
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

    # Embed + score everything in raw into marts.
    transform = PythonOperator(
        task_id="enrich",
        python_callable=enrich,
    )

    scrape_tasks >> transform
