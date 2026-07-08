FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Chromium + its OS deps for the self-hosted Trustpilot scraper (opt-in source).
# This is what makes the ingestion image heavier than the api image.
RUN playwright install --with-deps chromium
# Embeddings are computed via the OpenAI API (see embeddings.py), so there is no
# local model to bake or load.

COPY src/ ./src/
COPY config/ ./config/
ENV PYTHONPATH=/app/src

CMD ["python", "-m", "reviewbot.ingestion.run"]
