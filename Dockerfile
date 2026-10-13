# Cloud image for the daily live scan (Cloud Run Job). Built by Cloud Build
# (cloudbuild.yaml), started by scripts/deploy_gcp.ps1.
# Kept small on purpose: the Artifact Registry free tier is 0.5 GB.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app
RUN pip install --no-cache-dir uv==0.11.32

# Dependencies first, so code changes do not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra gcp --no-install-project && rm -rf /root/.cache

COPY src ./src
COPY scripts ./scripts
COPY data ./data
RUN uv sync --frozen --no-dev --extra gcp && rm -rf /root/.cache \
 && python scripts/seed_erp.py \
 && useradd --create-home app \
 && mkdir -p logs data/chroma data/live \
 && chown -R app:app logs data

USER app
# Gemini key comes from Secret Manager as GEMINI_API_KEY; the BigQuery table
# and the call cap come from the job's environment (see infra/main.tf).
ENTRYPOINT ["python", "-m", "agent.run"]
CMD ["--live", "--max-live", "15"]
