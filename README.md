# onchain-risk-sentinel

Multi-agent triage of crypto withdrawals, built with LangGraph.

Exchanges and wallets must decide, before funds leave, whether a withdrawal goes to a
sanctioned entity, a scam, or an address that only looks like the one the user meant.
Analysts do this by hand: trace the destination, screen it and its counterparties,
compare it with known fraud patterns, then decide. This project automates that first
pass with a team of agents. A supervisor routes each case to specialist agents (chain
tracing, sanctions screening, scam intelligence, fraud-pattern retrieval), a risk
scorer drafts a verdict, a critic checks every claim against the evidence, and
high-risk cases pause for a human reviewer. When the user is the likely victim, the
verdict carries a plain-language warning for them.

> Status: early development, Ethereum mainnet only. All data comes from public
> sources (node RPC, the OFAC SDN list, public scam feeds).

## Architecture

### A case passes mandatory screening, rules set the floor, and a person decides risky cases

```
 withdrawal case: destination address, amount, account context
        |
        v
 +-------------+   routes (max_steps guard)     +---------------------------+
 | supervisor  |------------------------------->| specialists (ReAct)       |
 |             |<-------------------------------|   chain_tracer     *      |
 +-------------+   findings + cited evidence    |   sanctions        *      |
        |                                       |   scam_intel       *      |
        | ready to score                        |   pattern_analyst         |
        v                                       +---------------------------+
 +-------------+                                  * mandatory: always runs
 | risk_scorer |   1. hard rules: sanctions hit, drainer -> block
 +-------------+   2. floors: risky counterparties -> at least hold
        |          3. LLM decides the rest and writes the summary
        v
 +-------------+   claim without evidence?
 |   critic    |------------------------------> back to supervisor (1 round max)
 +-------------+
        |
        v
  action != allow and score >= threshold ?
        | yes                                 | no
        v                                     v
 +--------------+                       +-----------+
 | human_review |---------------------->|  verdict  |   action, risk, score,
 |  (interrupt) |   approve / override  +-----------+   evidence, user notice
 +--------------+
```

The LLM never has the last word on the hard cases: deterministic rules decide sanctions
hits and known drainers, and set a minimum outcome for risky counterparties. Every
finding must cite evidence that a tool returned, and the critic checks that it does.

### Agents depend only on interfaces, so data sources and models can be swapped

```
+---------------------------------------------------------------+
| sentinel.app (CLI, FastAPI + SSE)  |  sentinel.evals (harness)|
+---------------------------------------------------------------+
| sentinel.agents   LangGraph triage graph (diagram above)      |
+---------------------------------------------------------------+
| sentinel.core     schemas | ports | settings | model factory  |
+-------------------------------+-------------------------------+
| sentinel.tools                | sentinel.knowledge            |
| node RPC (Alchemy, QuickNode, | 26 fraud-pattern documents    |
|   Ankr) -> Etherscan fallback | numpy or pgvector index       |
| OFAC SDN list, scam feeds     |                               |
+-------------------------------+-------------------------------+
```

Lower layers never import upper ones. `sentinel.core.ports` defines what the agents need
(chain data, sanctions, scam intel, pattern search); adapters, test fakes and recorded
eval snapshots all implement the same ports. Every role's model is a config string.

## Run it

### 1. Install and configure

```bash
uv sync                      # add --extra tracing for Langfuse
cp .env.example .env         # then fill in the keys below
```

| Variable | Needed for |
|---|---|
| `OPENROUTER_API_KEY` | the default model, Gemini 3.5 Flash-Lite, for every role (change per role with `SENTINEL_*_MODEL`) |
| `OPENAI_API_KEY`, `GOOGLE_API_KEY` | GPT models; Gemini embeddings for pattern search |
| `SENTINEL_ETH_RPC_URLS` | chain data: comma-separated mainnet RPC URLs, tried in order |
| `SENTINEL_ALCHEMY_API_KEY` | address history (Alchemy Transfers API) |
| `SENTINEL_ETHERSCAN_API_KEY` | optional: last fallback and contract names |
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

## Evaluation

87 labeled cases (71 synthetic scenarios, 16 real addresses) replay recorded data, so a
run measures the agents and models, not changing chain data. Latest run, default
model, development set:

| Action accuracy | Block recall | False-block rate | p50 latency | Cost per case |
|---|---|---|---|---|
| 96.6% | 100% | 0% | 7.5 s | $0.007 |

The rules were designed after studying failures on this same set, so these numbers are
optimistic; a held-out test set is planned. Details, the model comparison and how to
run evals: [`evals/README.md`](evals/README.md), [`evals/reports/`](evals/reports/).

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
