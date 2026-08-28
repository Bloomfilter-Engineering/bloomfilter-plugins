.PHONY: lint format validate check

# Mirror the CI checks in .github/workflows/lint.yml.
lint:
	ruff check .
	ruff format --check .

# Auto-fix lint findings and reformat in place.
format:
	ruff check --fix .
	ruff format .

# Mirror the manifest half of .github/workflows/validate-plugin.yml. Covers all
# four vendors; the `claude plugin validate` half needs the Claude Code CLI and
# runs in CI only.
validate:
	python3 .github/scripts/validate_manifests.py

# The pull-request gates that run locally. Not exhaustive: check-version.yml
# needs origin/main and `claude plugin validate` needs the vendor CLI, so a
# green `make check` is necessary but not sufficient for a green pull request.
check: lint validate
