# Evals

Measures the triage graph end to end: decision quality, evidence discipline, latency,
tokens and cost, per model configuration. Code: `src/sentinel/evals/`. Token and cost
tracking: `src/sentinel/observability/`.

## Layout

| Path | What |
|---|---|
| `dataset/cases.jsonl` | One labeled case per line (`EvalCase`): input + label + tags |
| `dataset/snapshots/<case_id>.json` | Recorded port responses for the case (`Snapshot`) |
| `results/` | Raw run results (`RunResults` JSON), one file per run |
| `reports/` | Committed Markdown comparison reports |
| `baseline.json` | Reference metrics and tolerances for the regression gate |

## Reproducibility: recorded snapshots

Evals measure agents and models, not drifting chain data. Every case has a snapshot of
every port answer the agents may need, keyed by address: address profiles, recent
transfers, sanctions results (a hit, or `null` for "checked and clean") and scam reports.
`RecordedPorts` replays it.

- Lookups the snapshot does not cover raise `UnrecordedLookupError`. The agent tools
  turn that into an error message (no evidence), and the result records it under
  `unrecorded_lookups`, so a model that wanders off the record is visible.
- Pattern search uses the real knowledge base (`data/patterns/`) with the offline
  `HashingEmbeddings`: deterministic, no API key, identical on every machine. It is a
  bag-of-words embedder, so pattern relevance is weaker than with Gemini embeddings;
  this is the same for every configuration, so comparisons stay fair.
- Cases have a fixed `requested_at` (2026-10-20 12:00 UTC); synthetic timestamps are
  relative to it. Case ids are opaque (`ev-0001`) because the graph shows them to models.

## Dataset and label policy

87 cases: 71 synthetic (runnable now) and 16 real-address cases that need recording
(`uv run sentinel-eval dataset` prints the current counts).

| Expected action | Scenarios | Cases |
|---|---|---|
| `block` | sanctioned destination (synthetic 8, OFAC 10), drainer or phishing report (8), address-poisoning look-alike (8) | 34 |
| `hold_for_review` | one hop from a sanctioned address (6), fresh address with large inflow (8), non-blocking scam category (5), mixer exposure (5), peel chain (4) | 28 |
| `allow` | long-lived active EOA (10), labeled protocol contract (5), fresh address receiving a small amount (4), real protocol contracts (6) | 25 |

Label rules:

1. **Block** when the destination is sanctioned, publicly reported as a drainer,
   phishing or address-poisoning operation, or is an address-poisoning look-alike of an
   address the account used before (same first and last characters, days old,
   zero-value transfers). Minimum risk: `critical` for sanctions, otherwise `high`.
2. **Hold for review** when there is material indirect risk but no listing: funds one
   hop from a sanctioned address, mixer exposure, layering patterns (peel chain), a
   days-old address with large inflows receiving a large withdrawal from a young account,
   or a scam report in a non-blocking category (ponzi, honeypot, fake token, suspicious
   activity). Minimum risk: `medium`.
3. **Allow** when there are no adverse signals: long-lived active addresses with
   ordinary counterparties, well-known protocol contracts, and fresh addresses receiving
   a small amount from an established account (freshness alone is not a reason for
   friction). Minimum risk: `none`.

Every label has a `rationale`, a `scenario` and a `source` (`synthetic`, `ofac`,
`scam_feed`, `benign_public`). Authored text never names an exchange or any company.

Metrics compare the predicted action with `expected_action`; `min_risk_level` feeds the
"risk floor" metric. `block` cases are the "must-block" set for block recall; `allow`
cases are the "benign" set for false-block rate.

### Data sources and licenses

| Source | Used for | License | What is committed |
|---|---|---|---|
| Synthetic scenarios (`sentinel.evals.synthetic`, seed `20261020`) | 71 cases with constructed snapshots | MIT (this repository) | Everything; regenerate with `uv run python -m sentinel.evals.synthetic` |
| OFAC SDN snapshot (`data/sanctions/`) | 10 sanctioned real addresses | U.S. Government work, public domain | Addresses and program names (labels); snapshots once recorded |
| Well-known protocol contracts | 6 benign real addresses | Public on-chain facts | Addresses and a generic description |
| Etherscan API (via `EtherscanChainData`) | Recorded chain data | Etherscan API terms | Derived facts in snapshots (profile, transfers), as returned by the adapter |
| Scam feeds (ScamSniffer GPL-3.0, Forta MIT/derived labels) | Recorded scam-intel answers | See `data/SOURCES.md` | Derived facts only: category and feed name, with our own one-line description; no raw rows or feed text |

Synthetic sanctions hits use the list name "OFAC SDN" with clearly fake entity names
("SYNTHETIC SANCTIONED ENTITY 07"); synthetic scam reports use the source
`synthetic-feed`. Synthetic addresses are random and not meant to match real ones.

Scam-feed addresses are not used as real cases yet: the feeds are GPL-3.0 or derived
from third-party labels, so committing their addresses as a case list needs a decision.

## Commands

```bash
uv run sentinel-eval configs                       # model configs and whether they can run here
uv run sentinel-eval dataset                       # validate the dataset, print counts

# Record snapshots for real-address cases (needs SENTINEL_ETHERSCAN_API_KEY; never touches labels)
uv run python -m sentinel.evals.record             # all unrecorded real cases
uv run python -m sentinel.evals.record --ids ev-0072 ev-0073

# Run (each run writes evals/results/<config>-<time>-<id>.json)
uv run sentinel-eval run --config default                    # all runnable cases
uv run sentinel-eval run --config gpt-nano --cases 20        # first 20 (scenarios are interleaved)
uv run sentinel-eval run --config default --tag smoke        # the 6-case CI subset

# Compare runs into a committed report
uv run sentinel-eval compare evals/results/a.json evals/results/b.json \
  --out evals/reports/<date>-comparison.md

# Gate: non-zero exit on regression
uv run sentinel-eval gate evals/results/<run>.json --baseline evals/baseline.json [--subset smoke]
uv run sentinel-eval baseline evals/results/<run>.json --subsets smoke   # promote a run
```

Paused cases are auto-resumed with an "approve" decision by reviewer `eval-harness`;
`pause_rate` reports how often that happened.

## Model configurations

| Name | Supervisor | Specialists | Scorer | Critic |
|---|---|---|---|---|
| `default` | `openai:gpt-5.4-mini` | `google_genai:gemini-flash-latest` | `openai:gpt-5.4-mini` | `openai:gpt-5.4-mini` |
| `gpt-mini` | `openai:gpt-5.4-mini` | same | same | same |
| `gpt-nano` | `openai:gpt-5.4-nano` | same | same | same |
| `gemini-flash-lite` | `google_genai:gemini-3.5-flash-lite` | same | same | same |
| `ollama-qwen` | `ollama:qwen3:8b` | same | same | same |

Model ids and prices were checked on 2026-10-25 (sources in
`src/sentinel/observability/pricing.py`). `gemini-flash-latest` is an alias; it is
priced as `gemini-3.8-flash`, the newest stable Flash. Override prices with
`--prices <file.json>` (same shape as `PriceTable`).

## Metrics

| Metric | Definition |
|---|---|
| Accuracy | predicted action == expected action, over all cases (errors count as wrong) |
| Block recall | share of `block` cases predicted `block` |
| False block | share of `allow` cases predicted `block` |
| Risk floor | share of cases whose risk level is at least the label's minimum |
| Pause | share of cases that paused for human review |
| Evidence coverage | share of medium-or-higher findings that cite at least one evidence item |
| p50 / p95 latency | per case, seconds, including the auto-resume |
| Tokens / cost per case | from `usage_metadata` via `UsageCallbackHandler`, priced per model |

Plus a confusion matrix over actions and accuracy per scenario in every report.

## Regression gate

`evals/baseline.json` stores reference metrics for `all` cases and for each tagged
subset (`smoke`), plus tolerances. The gate fails when block recall drops, the
false-block rate rises, accuracy drops by more than 0.10, the error rate rises by more
than 0.02, or cost per case rises by more than 25%. Cost is compared only when the run
uses the same model configuration as the baseline.

CI (`.github/workflows/evals.yml`, manual or on pull requests to `main`) runs the
`smoke` subset with `default` and gates it, only when `OPENAI_API_KEY` and
`GOOGLE_API_KEY` secrets exist; otherwise it skips with a notice.

## Latest results

Run 2026-10-25, 71 runnable cases (full report: `reports/2026-10-25-comparison.md`).
Only the local configuration could run; cloud configurations need provider keys.

| Config | Errors | Accuracy | Block recall | False block | Pause | Evidence cov. | p95 s | Tokens/case | Cost/case |
|---|---|---|---|---|---|---|---|---|---|
| `ollama-qwen` | 15 | 41% | 67% | 0% | 30% | 88% | 183 | 8,628 | $0 |

The 15 errors come from one bug, the same for every model: checkpointing crashes when
a profile balance is 2^64 wei (about 18.45 ETH) or more. See the report.
