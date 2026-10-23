.PHONY: up down test e2e demo lint migrate
up:
	docker compose up --build -d --wait

down:
	docker compose down

test:
	docker compose --profile test run --build --rm tests

e2e:
	docker compose up --build -d --wait
	docker compose --profile e2e run --build --rm e2e

demo:
	docker compose exec api python -m scripts.demo

lint:
	docker compose run --rm --no-deps api ruff check .

migrate:
	docker compose run --rm migrate
