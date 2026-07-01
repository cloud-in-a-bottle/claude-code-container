setup:
    uv sync --all-groups
    uv run pre-commit install

run:
    PORT=8080 .venv/bin/python server.py

test:
    uv run pytest

serve:
    podman build -t claude-workbench .
    podman run --rm -p 8080:8080 -e PORT=8080 -e IS_SANDBOX=1 claude-workbench
