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
| `sentinel.knowledge` | Fraud-pattern documents and vector search |

## Development

```bash
uv sync
cp .env.example .env   # add keys only if you run live tests
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest            # unit tests: no network, no LLM calls
uv run pytest -m live    # live tests: real APIs and models
uv run pre-commit install
```

## License

MIT
