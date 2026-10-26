SHELL := /bin/bash
PY ?= python3.11
VENV ?= .venv
PYTHON := $(VENV)/bin/python

.DEFAULT_GOAL := help

.PHONY: help venv install install-dev test lint fmt run demo dashboard docker-build docker-run clean

help: ## Show the available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv
	$(PY) -m venv $(VENV)

install: venv ## Install runtime dependencies
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt

install-dev: install ## Install runtime + lint tooling
	$(PYTHON) -m pip install -r requirements-dev.txt

test: ## Run the unit test suite
	$(PYTHON) -m unittest discover -s tests -v

lint: ## Lint with ruff
	$(PYTHON) -m ruff check .

fmt: ## Auto-fix lint findings and format
	$(PYTHON) -m ruff check --fix .
	$(PYTHON) -m ruff format .

run: ## Start the bot (DRY-RUN unless LIVE=true)
	$(PYTHON) main.py

demo: ## Serve the console locally with a seeded demo ledger
	$(PYTHON) scripts/demo_server.py

dashboard: ## Serve only the static console on :3000 (no API)
	$(PYTHON) -m http.server 3000 --directory dashboard/public

docker-build: ## Build the container image
	docker build -t polymarket-bot .

docker-run: ## Run the container with a persistent volume
	docker run --rm -p 127.0.0.1:8099:8099 -e API_HOST=0.0.0.0 \
		-e LIVE=false -v polymarket-data:/data polymarket-bot

clean: ## Remove caches and local state
	rm -rf $(VENV) .ruff_cache .pytest_cache *.db *.db-wal *.db-shm
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
