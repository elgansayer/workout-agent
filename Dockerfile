# Workout agent in a container. You never run Python directly: build once,
# then `docker compose up -d` and it messages you every morning at 07:00.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_PATH=/data/workout_agent.db

# tzdata so RUN_AT/TZ are interpreted in your local time; ca-certificates for HTTPS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x /app/docker-entrypoint.sh \
    && useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data
USER appuser

ENTRYPOINT ["/app/docker-entrypoint.sh"]
