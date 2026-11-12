# One image for all three processes; the command picks which one runs:
#   api       (default) uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
#   admitter  python -m workers.admitter      exactly ONE replica
#   consumer  python -m workers.consumer
#   migrate   alembic upgrade head            one-off, before the others start
# Health: api -> GET /health/ready; workers -> python -m workers.healthcheck <name>

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# Dependencies first, so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY workers ./workers

FROM python:3.12-slim-bookworm
RUN useradd --system --uid 10001 --no-create-home queue
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    LOG_FORMAT=json
USER queue
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
