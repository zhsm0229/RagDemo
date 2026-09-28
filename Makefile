PY := .venv/Scripts/python

.PHONY: install lint fmt test test-unit test-api ci up down

install:
	python -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

lint:
	$(PY) -m ruff check app tests

fmt:
	$(PY) -m ruff check --fix app tests
	$(PY) -m ruff format app tests

test:
	$(PY) -m pytest

test-unit:
	$(PY) -m pytest tests/unit

test-api:
	$(PY) -m pytest tests/api

ci: lint
	$(PY) -m pytest --cov=app/rag --cov=app/services --cov-fail-under=70

up:
	docker compose up -d

down:
	docker compose down
