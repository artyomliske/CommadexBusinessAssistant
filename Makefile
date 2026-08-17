.PHONY: help install up down logs migrate revision test lint fmt typecheck subscribe

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Установить зависимости в .venv
	python3.12 -m venv .venv
	./.venv/bin/pip install --upgrade pip
	./.venv/bin/pip install -e ".[dev]" psycopg[binary]

up: ## Поднять всё окружение
	docker compose up -d --build

down: ## Остановить окружение
	docker compose down

logs: ## Логи api и worker
	docker compose logs -f api worker

migrate: ## Применить миграции
	docker compose run --rm migrate

revision: ## Создать миграцию: make revision m="описание"
	docker compose run --rm migrate alembic revision --autogenerate -m "$(m)"

test: ## Тесты
	./.venv/bin/pytest -q

lint: ## Линтер
	./.venv/bin/ruff check src tests

fmt: ## Автоформатирование
	./.venv/bin/ruff format src tests
	./.venv/bin/ruff check --fix src tests

typecheck: ## Проверка типов
	./.venv/bin/mypy src

subscribe: ## Зарегистрировать вебхук в MAX
	docker compose run --rm api python -m repairbot.cli subscribe
