.PHONY: up down logs test test-js lint fmt migrate revision

test-js:
	node --test "tests/js/*.test.mjs"

up:
	docker compose up -d db redis azurite api

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

test:
	docker compose run --rm api pytest -q

lint:
	docker compose run --rm api ruff check .

fmt:
	docker compose run --rm api sh -c "ruff format . && ruff check --fix ."

migrate:
	docker compose run --rm api alembic upgrade head

revision:
	docker compose run --rm api alembic revision --autogenerate -m "$(m)"
