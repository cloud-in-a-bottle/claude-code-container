default: test

# Install dependencies, pre-commit hooks, the playwright chromium browser, and build the UI.
setup:
    uv sync
    uv run pre-commit install
    uv run playwright install chromium
    cd ui && npm ci
    just build-ui

# Build the Solid frontend into src/server/static/ui (the container does this in its own stage).
build-ui:
    cd ui && npm run build

# Rebuild the frontend on every change; the server serves the files as they land.
watch-ui:
    cd ui && npm run watch

# Run the app locally on http://localhost:8080.
run: build-ui
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
