# 跨平台（Windows Git Bash / macOS）：venv 目录布局与 python 二进制名随 OS 切换。
# Mac 迁移（deploy/mac-setup/）起两平台共用本文件；venv 亦可由 `uv sync --locked --extra dev`
# 生成（setup-mac.sh 采用），布局同样兼容下方 PY 路径。
ifeq ($(OS),Windows_NT)
PY := .venv/Scripts/python
PY_BIN := python
else
PY := .venv/bin/python
PY_BIN := python3
endif

.PHONY: install lint fmt test test-unit test-api ci up down

install:
	$(PY_BIN) -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

lint:
	$(PY) -m ruff check app tests
	$(PY) -m ruff format --check app tests

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
