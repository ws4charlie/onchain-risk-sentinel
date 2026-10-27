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

## How a case flows

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

Rules decide the hard cases, the LLM handles the rest, and a person reviews risky
ones. More: [architecture](docs/architecture.md).

## Quick start

```bash
uv sync
cp .env.example .env    # keys: see docs/running.md
uv run sentinel triage 0xADDRESS --amount 2.5 --asset ETH
```

## Results

Latest eval: 87 labeled cases replayed from recorded data, default model.

| Action accuracy | Block recall | False-block rate | p50 latency | Cost per case |
|---|---|---|---|---|
| 96.6% | 100% | 0% | 7.5 s | $0.007 |

Measured on the development set, so optimistic. Details: [`evals/README.md`](evals/README.md).

## Docs

| | |
|---|---|
| [Architecture](docs/architecture.md) | Agent flow, layers and ports, design choices |
| [Running](docs/running.md) | Configuration, Postgres, CLI, HTTP API, tracing |
| [Development](docs/development.md) | Checks, tests, pre-commit |
| [Evaluation](evals/README.md) | Dataset, metrics, model comparison, regression gate |
| [Agents](src/sentinel/agents/README.md) | Rules, guards, prompts, user notices |

## License

MIT
