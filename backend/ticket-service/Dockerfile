FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install .


# Used by `docker compose run --rm tests`; the source is mounted at /src.
FROM base AS test

RUN pip install '.[dev]'
WORKDIR /src
CMD ["pytest", "-p", "no:cacheprovider"]


FROM base AS runtime

# Create the folder first, or ADD --chmod=644 makes it unreadable for the non-root user.
RUN useradd --system --no-create-home --uid 10001 ticketsvc \
    && mkdir -p /etc/ssl/rds

# RDS CA bundle, used when DB_SSL_CA points at it.
ADD --chmod=644 https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem /etc/ssl/rds/global-bundle.pem

COPY alembic.ini ./
COPY alembic ./alembic

USER ticketsvc
ENV PORT=3006
EXPOSE 3006

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"3006\")}/health', timeout=2)"

# Migrations run separately (Compose locally, a one-off ECS task in AWS).
# The worker (RabbitMQ consumers + TicketIssued publishing) uses this image: `python -m app.worker`.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT} --workers 2 'app.main:create_app()'"]
