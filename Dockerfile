FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# FFmpeg normalizes every Plex track to the same stream format.
RUN apt-get update \
  && apt-get install -y --no-install-recommends tzdata ffmpeg \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir poetry==2.2.1 \
  && poetry config virtualenvs.create false

COPY pyproject.toml poetry.lock /app/

RUN poetry install --only main --no-root --no-interaction --no-ansi

COPY src /app/src
COPY README.md /app/README.md

# Public DB-IP Lite data is bundled outside /data, so the persistent volume
# cannot hide it. Runtime lookups never send listener IP addresses to a service.
RUN python -m src.geoip_update

EXPOSE 8000

CMD ["uvicorn", "src.main:application", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
