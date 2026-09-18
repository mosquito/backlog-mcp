.PHONY: all sync format lint types test coverage serve clean

all: lint types test

sync:
	uv sync --all-groups

format:
	uv run ruff format src tests
	uv run ruff check --fix src tests

lint:
	uv run ruff format --check src tests
	uv run ruff check src tests

types:
	uv run mypy src

test:
	uv run pytest

coverage:
	uv run pytest --cov=backlog_mcp --cov-report=term-missing

serve:
	uv run backlog mcp --protocol=http --http-listen=127.0.0.1 --http-port=3009

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -name __pycache__ -type d -not -path './.venv/*' -prune -exec rm -rf {} +
