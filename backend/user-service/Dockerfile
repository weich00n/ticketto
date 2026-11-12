FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt


# Test runner, used by `docker compose run --rm tests`. The source and tests are
# mounted at /src rather than copied, so code changes don't need a rebuild.
FROM base AS test

COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
WORKDIR /src
CMD ["pytest", "-p", "no:cacheprovider"]


# Production image. Kept as the last stage so a plain `docker build` produces it.
FROM base AS runtime

RUN useradd --create-home --uid 10001 appuser

# CA bundle for TLS connections to Amazon RDS (used when DB_SSL_CA points at it).
# Create the directory first: ADD --chmod would otherwise create it as 644 too, and without
# the execute bit appuser can't open the file inside it.
RUN mkdir -m 755 -p /etc/ssl/rds
ADD --chmod=644 https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /etc/ssl/rds/global-bundle.pem

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app

USER appuser
# 8000: the API, through API Gateway and the ALB. 8001: /internal/v1 only, for other services,
# when INTERNAL_PORT=8001 is set (app/serve.py).
EXPOSE 8000 8001

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

# Migrations are NOT run here. Locally, docker-compose runs them first; in AWS, run
# `alembic upgrade head` as a one-off ECS task per deployment.
CMD ["python", "-m", "app.serve"]
