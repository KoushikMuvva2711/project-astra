FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# No apt layer: every dependency ships a cp312 wheel (psycopg2-binary is
# prebuilt, asyncpg has wheels, pgvector is pure Python), so build-essential and
# libpq-dev would only add build time and image size. The healthcheck uses
# python rather than curl for the same reason. If a future dependency needs to
# compile, add the toolchain back here rather than reaching for source builds.

# Dependencies first so the layer caches across source edits.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install -e ".[dev]"

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
