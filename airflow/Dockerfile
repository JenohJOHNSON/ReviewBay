# Airflow image with the reviewbot package + providers baked in, so the DAG's
# PythonOperators can `import reviewbot` and the SQL operators can reach Snowflake.
FROM apache/airflow:2.10.4-python3.12

COPY airflow/requirements-airflow.txt /tmp/requirements-airflow.txt
RUN pip install --no-cache-dir -r /tmp/requirements-airflow.txt

# Ship our source so `from reviewbot.ingestion.run import run_source` works.
COPY src/ /opt/airflow/project_src/
ENV PYTHONPATH=/opt/airflow/project_src
