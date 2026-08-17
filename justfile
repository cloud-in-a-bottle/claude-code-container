default: test

# Install dependencies, pre-commit hooks, and the playwright chromium browser.
setup:
    uv sync
    uv run pre-commit install
    uv run playwright install chromium

# Run the app locally on http://localhost:8080.
run:
    PORT=8080 uv run python -m server.app

# Run the fast unit tests.
test:
    uv run pytest -x

# Build the image and exercise it under podman via the OpenHost test harness.
test-integration:
    uv run pytest -m integration

# Lint, format, and typecheck (same checks as the pre-commit hooks).
check:
    uv run ruff check --fix .
    uv run ruff format .
    uv run mypy
    uv run mypy tests

# Build the container image.
build:
    podman build -t claude-workbench .
