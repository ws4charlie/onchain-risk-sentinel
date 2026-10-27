# Development

```bash
uv sync
cp .env.example .env   # add keys only if you run live tests
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest            # unit tests: no network, no LLM calls
uv run pytest -m live    # live tests: real APIs and models
SENTINEL_DATABASE_URL=postgresql://localhost/sentinel uv run pytest -m integration
uv run pre-commit install
```

Test markers: unit tests (default) make no network or LLM calls; `live` tests hit real
APIs and models; `integration` tests need Postgres. Paid model runs (`-m live`, evals)
cost money: run a small subset first.

Evaluation: see [`evals/README.md`](../evals/README.md).
