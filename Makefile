.PHONY: lint format

# Mirror the CI checks in .github/workflows/lint.yml.
lint:
	ruff check .
	ruff format --check .

# Auto-fix lint findings and reformat in place.
format:
	ruff check --fix .
	ruff format .
