FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY config/ ./config/
ENV PYTHONPATH=/app/src

# Railway (and similar PaaS) inject $PORT and route the public URL to it. Fall
# back to 8000 so local `docker compose` keeps working unchanged.
EXPOSE 8000
CMD ["sh", "-c", "uvicorn reviewbot.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
