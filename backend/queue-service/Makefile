.PHONY: up up-app down keys migrate api admitter consumer fmt check test-unit test-integration test-up test-down test-contract

TEST_COMPOSE = docker compose -f docker-compose.test.yml

# --- Local stack --------------------------------------------------------------------

up:               ## start MySQL, Redis, RabbitMQ (run the app yourself with api/admitter/consumer)
	docker compose up -d --wait

up-app:           ## same, plus migrate, api, admitter and consumer in containers (builds the image)
	docker compose --profile app up -d --build

down:             ## stop everything started by up or up-app (keeps the MySQL volume)
	docker compose --profile app down

keys:             ## generate a local JWT signing key pair into secrets/
	uv run python -m scripts.gen_jwt_key

# --- Running the service on your machine (needs `make up`) --------------------------

migrate:          ## apply database migrations
	uv run alembic upgrade head

api:              ## HTTP API with auto-reload on http://localhost:8000 (docs at /docs)
	uv run uvicorn app.main:create_app --factory --reload

admitter:         ## admitter tick loop (exactly one)
	uv run python -m workers.admitter

consumer:         ## RabbitMQ consumers
	uv run python -m workers.consumer

# --- Code quality and tests ---------------------------------------------------------

fmt:              ## auto-fix lint issues and reformat
	uv run ruff check --fix .
	uv run ruff format .

check:            ## lint, format check, pylint and type check (what CI runs)
	uv run ruff check .
	uv run ruff format --check .
	uv run pylint app workers scripts tests
	uv run mypy

test-unit:        ## unit tests, no Docker needed
	uv run pytest -m unit

test-up:          ## start the throwaway test stack (other ports than `make up`)
	$(TEST_COMPOSE) up -d --wait

test-integration: test-up  ## integration + e2e tests (starts the test stack if needed)
	REQUIRE_TEST_SERVICES=1 uv run pytest -m "integration or e2e"

test-contract:    ## AMQP message contract tests (needs UV_INDEX_TICKETTO_* set; see README)
	uv run --group contract pytest tests/contract -m contract

test-down:        ## stop the test stack and discard its data
	$(TEST_COMPOSE) down -v
