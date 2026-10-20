FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt requirements-scraping.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Optional heavy scraping extras, OFF by default so this image stays the same
# size as before unless you ask for them.
#
#   docker build --build-arg WITH_BROWSER=1 -f docker/worker.Dockerfile .
#
# Build with WITH_BROWSER=1 to add Scrapling's fetcher tiers and a Chromium
# binary (~400MB). Only the worker gets this option: the API serves a dashboard
# on a small instance and has no business launching browsers. Everything still
# runs without it, one tier down (requests + lxml parsing), which is the default
# path and the one the benchmarks measure.
ARG WITH_BROWSER=0
RUN if [ "$WITH_BROWSER" = "1" ]; then \
      pip install --no-cache-dir -r requirements-scraping.txt && \
      python -m playwright install --with-deps chromium; \
    else \
      echo "Skipping browser extras (build with --build-arg WITH_BROWSER=1 to include them)"; \
    fi

COPY src/ ./src/
COPY config/ ./config/
ENV PYTHONPATH=/app/src

CMD ["python", "-m", "reviewbot.worker"]
