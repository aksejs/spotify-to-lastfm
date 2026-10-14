FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY spotify_import ./spotify_import
# tzdata: zoneinfo needs IANA data for the per-day cap (UTC + account timezone).
RUN pip install --no-cache-dir . tzdata

# Same uid as the host user owning the bind-mounted state, so files stay 600 and readable.
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin scrobbler
USER 1000:1000

# All state lives in the /data volume: database, config, backups, optional secrets file.
WORKDIR /data
ENTRYPOINT ["spotify-import", "--db", "/data/import.sqlite", "--config", "/data/config.yaml", "--secrets", "/data/secrets.json"]
CMD ["status"]
