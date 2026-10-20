FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# No local embedding model: embeddings are computed via the OpenAI API (see
# embeddings.py), so there is nothing heavy to bake and no ~1GB model to load at
# runtime. This keeps the image small and the container's memory footprint low.
#
# No browser either, and that is a decision rather than an omission. The
# extraction engine's default tier parses with Scrapling's light core (lxml) and
# fetches with `requests`, both already installed above. The browser tier is
# opt-in via requirements-scraping.txt and belongs in a service with the memory
# headroom for a Chromium process (150-300MB resident), which a small API
# instance serving the dashboard does not have. See
# docs/SCRAPING_ARCHITECTURE.md § Deployment.
#
# ScrapeGraphAI is likewise not installed: it resolves to 74 extra packages and
# downgrades a package uvicorn depends on (see requirements-ai-extraction.txt).
# The AI-extraction capability still works here through its `native` backend,
# which uses the openai client this image already has.

COPY src/ ./src/
COPY config/ ./config/
ENV PYTHONPATH=/app/src

# Railway (and similar PaaS) inject $PORT and route the public URL to it. Fall
# back to 8000 so local `docker compose` keeps working unchanged.
EXPOSE 8000
CMD ["sh", "-c", "uvicorn reviewbot.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
