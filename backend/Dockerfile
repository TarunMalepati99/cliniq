# Build context: repository root (needs backend/, dbt/, data/corpus/, eval/)
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends git postgresql-client && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/cliniq backend/cliniq
COPY backend/migrations backend/migrations
RUN pip install "./backend[dbt]"
COPY dbt dbt
COPY data/corpus/reference data/corpus/reference
COPY data/demo data/demo
COPY eval eval
RUN useradd --create-home --uid 10001 cliniq && mkdir -p data/synthetic data/uploads data/corpus/trials eval/reports dbt/target \
    && chown -R cliniq:cliniq /app
USER cliniq
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"
# bootstrap is idempotent: generates data only if missing, skips already-ingested files.
CMD ["sh", "-c", "cliniq bootstrap && cliniq serve --host 0.0.0.0 --port 8000"]
