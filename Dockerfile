FROM python:3.11-slim

# The package is installed editable so `agents/` and `prompts/` stay resolvable
# via pyproject.toml as the repo-root marker (see src/driftpin/paths.py) —
# this container's /app is a permanent, baked-in "repo root", not a dev checkout.
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY agents ./agents
COPY prompts ./prompts

RUN pip install --no-cache-dir -e .

# The user's actual project (PRDs, .driftpin/, generated artifacts) lives here,
# mounted as a volume at runtime — this is deliberately separate from /app.
WORKDIR /workspace

ENTRYPOINT ["driftpin"]
CMD ["--help"]
