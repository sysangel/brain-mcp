# Cloud Run image for the thin MCP. Supabase holds the documents.
# Embeddings are the OpenAI API from this process, not a local ONNX model
# and not a Supabase Edge Function. fastembed and sqlite-vec are omitted
# on purpose so cold start does not load an 800 MB model.

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    BRAIN_HTTP_HOST=0.0.0.0 \
    BRAIN_BACKEND=postgres \
    BRAIN_EMBED_PROVIDER=openai \
    BRAIN_EMBED_MODEL=text-embedding-3-small \
    BRAIN_EMBED_DIM=256

WORKDIR /app

# pg_dump for the nightly backup. The Supabase server is Postgres 17.
# Bookworm's default client is 15; a 16 client also refuses a 17 server.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
         https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
         > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-17 tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir \
      "fastmcp>=2,<3" \
      "psycopg[binary]>=3.2" \
      "psycopg-pool>=3.2" \
      "pgvector>=0.3" \
      "uvicorn>=0.32" \
    && pip install --no-cache-dir --no-deps .

EXPOSE 8080

CMD ["brain-server-http"]
