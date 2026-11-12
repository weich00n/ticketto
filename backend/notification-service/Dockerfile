# The service's image (python 3.12). It starts the API by default; the worker and the migration
# run the same image with their own command (deploy/worker-task-definition.json,
# scripts/ci/ecs-migrate.sh). Kept as the last stage, so a plain `docker build` produces it.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install .
COPY alembic.ini ./
COPY alembic ./alembic


# Used by `docker compose run --rm tests`; the source is mounted at /src.
FROM base AS test

RUN pip install '.[dev]'
WORKDIR /src
CMD ["pytest", "-p", "no:cacheprovider"]


FROM base AS runtime

RUN useradd --create-home --uid 10001 appuser

# RDS CA bundle, used when DB_SSL_CA points at it. Create the directory first: ADD --chmod
# would otherwise create it as 644 too, and without the execute bit appuser can't read the file.
RUN mkdir -m 755 -p /etc/ssl/rds
ADD --chmod=644 https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /etc/ssl/rds/global-bundle.pem

USER appuser
EXPOSE 8082

# Migrations are NOT run here: the pipeline runs `alembic upgrade head` as a one-off task.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8082", "--no-access-log"]
