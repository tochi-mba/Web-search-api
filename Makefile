.DEFAULT_GOAL := help
UV := uv

.PHONY: help install fmt lint types test cov check run clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install all dependencies into .venv
	$(UV) sync --all-extras

fmt: ## Auto-format the codebase
	$(UV) run ruff format app tests scripts
	$(UV) run ruff check --fix app tests scripts

lint: ## Lint without modifying files
	$(UV) run ruff format --check app tests scripts
	$(UV) run ruff check app tests scripts

types: ## Strict type check
	$(UV) run mypy app tests

test: ## Run the test suite
	$(UV) run pytest

cov: ## Run tests with the 100% coverage gate
	$(UV) run pytest --cov=app --cov-report=term-missing --cov-report=xml

check: lint types cov ## Everything CI runs

run: ## Run the API locally with reload
	$(UV) run uvicorn app.main:app --reload --port 8000

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage coverage.xml htmlcov dist build
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
