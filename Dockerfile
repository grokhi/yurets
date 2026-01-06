FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# tzdata is needed for zoneinfo timezones inside slim images.
RUN apt-get update \
  && apt-get install -y --no-install-recommends tzdata \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir poetry==1.8.5 \
  && poetry config virtualenvs.create false

COPY pyproject.toml /app/

RUN poetry install --no-interaction --no-ansi

COPY src /app/src
COPY README.md /app/README.md

EXPOSE 8000

CMD ["poetry", "run", "uvicorn", "src.main:application", "--host", "0.0.0.0", "--port", "8000"]
