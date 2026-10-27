# Architecture

## A case passes mandatory screening, rules set the floor, and a person decides risky cases

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

## Agents depend only on interfaces, so data sources and models can be swapped

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

Agent internals (rule table, guards, look-alike check, user notices):
[`src/sentinel/agents/README.md`](../src/sentinel/agents/README.md).
