# Running

## Install and configure

```bash
uv sync                      # add --extra tracing for Langfuse
cp .env.example .env         # then fill in your keys
```

`.env.example` lists every variable with a short comment. The minimum to run a
case: `OPENROUTER_API_KEY`, `SENTINEL_ETH_RPC_URLS`, `SENTINEL_ALCHEMY_API_KEY`, and
`GOOGLE_API_KEY` for pattern embeddings (or `SENTINEL_EMBEDDING_PROVIDER=hashing`).

## Postgres + pgvector (optional, macOS with Homebrew)

Without a database, checkpoints live in memory (a paused case cannot be resumed by
another process) and pattern search uses a local numpy index.

```bash
brew install postgresql@17 pgvector
brew services start postgresql@17
"$(brew --prefix postgresql@17)/bin/createdb" sentinel
"$(brew --prefix postgresql@17)/bin/psql" sentinel -c "CREATE EXTENSION IF NOT EXISTS vector;"
echo "SENTINEL_DATABASE_URL=postgresql://localhost/sentinel" >> .env
```

Tables (LangGraph checkpoints, `pattern_chunks`) are created on first use. Pattern
chunks are embedded once and re-embedded only when a document changes.

## CLI

```bash
uv run sentinel triage 0xADDRESS --amount 2.5 --asset ETH
uv run sentinel triage 0xADDRESS --json              # machine-readable verdict
uv run sentinel triage 0xADDRESS --no-review          # exit 3 + thread id if it pauses
uv run sentinel resume <thread-id> --approve --note "confirmed"
uv run sentinel resume <thread-id> --override hold_for_review --reviewer alice
```

Progress (one line per agent step) goes to stderr, the verdict to stdout. When a
case pauses for human review, `triage` prompts for a decision unless `--no-review`
is set. `resume` from a new process needs `SENTINEL_DATABASE_URL`.

## HTTP API

```bash
uv run uvicorn sentinel.app.api:app --port 8000
```

```bash
# Start a case (runs in the background)
curl -s localhost:8000/cases -H 'content-type: application/json' \
  -d '{"address": "0xADDRESS", "amount": 2.5, "case_id": "demo-1"}'
# -> {"thread_id": "demo-1-1a2b3c4d", "case_id": "demo-1", "status": "running"}

# Watch progress as Server-Sent Events (node updates, then paused/completed/failed)
curl -N localhost:8000/cases/demo-1-1a2b3c4d/events

# Status, verdict or pending review payload
curl -s localhost:8000/cases/demo-1-1a2b3c4d

# Answer a pending review (409 if the case is not awaiting review)
curl -s localhost:8000/cases/demo-1-1a2b3c4d/review -H 'content-type: application/json' \
  -d '{"case_id": "demo-1", "reviewer": "alice", "approve": false,
       "override_action": "hold_for_review", "note": "second look"}'

curl -s localhost:8000/healthz
```

Interactive docs: http://localhost:8000/docs. The API has no auth; run it locally only.

## Tracing with Langfuse

Install the extra (`uv sync --extra tracing`) and set `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY` and `LANGFUSE_HOST`. Every graph run is then traced, with the
case id as the Langfuse session id. Without the keys nothing is imported or sent.
