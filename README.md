# onchain-risk-sentinel

Multi-agent triage of suspicious crypto withdrawals, built with LangGraph.

Given a destination address (Ethereum first), a supervisor agent routes the case to
specialist agents (chain tracing, sanctions screening, scam intelligence, fraud-pattern
retrieval). A risk scorer drafts a verdict, a critic checks every claim against cited
evidence, and high-risk verdicts pause for human review before they become final.

> Status: early development. This is a learning and portfolio project that uses only
> public data. It is not a production compliance tool.

## Architecture

```
Case -> supervisor -> {chain_tracer, sanctions, scam_intel, pattern_analyst}
     -> risk_scorer -> critic -> (high risk) human_review -> verdict
```

| Package | Role |
|---|---|
| `sentinel.core` | Shared schemas, ports (interfaces), settings, model factory |
| `sentinel.agents` | LangGraph graph: supervisor, specialists, scorer, critic, human review |
| `sentinel.tools` | Data adapters: chain data, sanctions list, scam intelligence |
| `sentinel.knowledge` | Fraud-pattern documents and vector search (local numpy or pgvector) |
| `sentinel.app` | Runtime: wiring, `sentinel` CLI, FastAPI service, Postgres persistence, tracing |

## Run it

### 1. Install and configure

```bash
uv sync                      # add --extra tracing for Langfuse
cp .env.example .env         # then fill in the keys below
```

| Variable | Needed for |
|---|---|
| `OPENAI_API_KEY`, `GOOGLE_API_KEY` | the default models (change per role with `SENTINEL_*_MODEL`) |
| `SENTINEL_ETHERSCAN_API_KEY` | chain data |
| `SENTINEL_DATABASE_URL` | optional: Postgres checkpoints and the pgvector pattern index |
| `SENTINEL_EMBEDDING_PROVIDER` | `google` (default, uses `GOOGLE_API_KEY`) or `hashing` (offline) |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | optional tracing |

### 2. Postgres + pgvector (optional, macOS with Homebrew)

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

### 3. CLI

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

### 4. HTTP API

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

### 5. Tracing with Langfuse

Install the extra (`uv sync --extra tracing`) and set `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY` and `LANGFUSE_HOST`. Every graph run is then traced, with the
case id as the Langfuse session id. Without the keys nothing is imported or sent.

## Development

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

## License

MIT
