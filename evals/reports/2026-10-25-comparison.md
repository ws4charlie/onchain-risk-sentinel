# Model comparison (2026-10-25)

Generated 2026-10-25 21:36 UTC by `sentinel-eval compare`. Metric definitions: `src/sentinel/evals/metrics.py` and `evals/README.md`.

## Results

| Config | Cases | Errors | Accuracy | Block recall | False block | Risk floor | Pause | Evidence cov. | p50 s | p95 s | Tokens/case | Cost/case |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `ollama-qwen` | 71 | 15 | 41% | 67% | 0% | 46% | 30% | 88% | 109.3 | 183.0 | 8,628 | $0.0000 |

## Configurations

| Config | Run id | Models (role: model) |
|---|---|---|
| `ollama-qwen` | `ollama-qwen-20261025T203507Z-aa8730` | supervisor: `ollama:qwen3:8b`, specialist: `ollama:qwen3:8b`, scorer: `ollama:qwen3:8b`, critic: `ollama:qwen3:8b` |

## Not run

- `default` (Project default: Gemini Flash specialists, GPT-5.4 mini elsewhere.): GOOGLE_API_KEY is not set (needed for google_genai:gemini-flash-latest); OPENAI_API_KEY is not set (needed for openai:gpt-5.4-mini).
- `gpt-mini` (All roles on GPT-5.4 mini.): OPENAI_API_KEY is not set (needed for openai:gpt-5.4-mini).
- `gpt-nano` (All roles on GPT-5.4 nano.): OPENAI_API_KEY is not set (needed for openai:gpt-5.4-nano).
- `gemini-flash-lite` (All roles on Gemini 3.5 Flash-Lite.): GOOGLE_API_KEY is not set (needed for google_genai:gemini-3.5-flash-lite).

## Confusion matrices (rows: expected, columns: predicted)

### `ollama-qwen`

| expected \ predicted | allow | hold_for_review | block | error |
|---|---|---|---|---|
| allow | 12 | 0 | 0 | 7 |
| hold_for_review | 15 | 1 | 4 | 8 |
| block | 8 | 0 | 16 | 0 |

## Accuracy by scenario

| Scenario | `ollama-qwen` |
|---|---|
| address_poisoning | 2/8 |
| benign_contract | 0/5 |
| benign_eoa | 8/10 |
| benign_fresh_small | 4/4 |
| drainer_report | 8/8 |
| fresh_large_inflow | 0/8 |
| mixer_exposure | 0/5 |
| nonblocking_scam_report | 1/5 |
| peel_chain | 0/4 |
| sanctioned_direct | 6/8 |
| sanctioned_one_hop | 0/6 |

## Recommendation

No run qualifies (all runs error on >= 5% of cases).

## Findings and conclusion (written by hand)

Only the local `ollama-qwen` configuration could run: no OpenAI or Google keys were set
on the machine that produced this report. The cloud configurations are listed under
"Not run" with the command to run them below.

What the run shows:

1. **15 of 71 cases crash, for every model.** Checkpointing fails with "Type is not
   msgpack serializable" whenever an address profile has a balance of 2^64 wei
   (about 18.45 ETH) or more: the evidence data keeps `balance_wei` as an integer, and
   msgpack cannot encode integers above 64 bits. All `fresh_large_inflow` and
   `benign_contract` cases and two `benign_eoa` cases hit it. This is a production bug,
   not an eval artifact: any real destination holding more than about 18 ETH would fail.
   Without these crashes, accuracy on the 56 completed cases is 29/56 (52%).
2. **The supervisor can skip the sanctions check.** Two sanctioned destinations were
   allowed: the supervisor routed to two specialists, never to `sanctions`, so the
   deterministic `sanctions_hit` rule never saw a hit. A deterministic guard that always
   runs `sanctions` (and `scam_intel`) before scoring would close this gap for any model.
3. **Address poisoning is caught only with a scam report.** The two caught cases had a
   report; the six without one were allowed. No tool compares the destination with
   addresses in `account_context` (`recent_withdrawal_addresses`), so the look-alike
   is invisible unless a model notices it in the prompt.
4. **Qwen3 8B under-escalates indirect risk.** It never chose `hold_for_review` for
   one-hop sanctions exposure, mixer exposure or peel chains (0/15), and blocked four of
   five non-blocking scam reports. It never blocked a benign case (0% false block).
5. Cost is zero (local), but p95 latency is about 3 minutes per case on an Apple M3 Pro,
   with about 8.6k tokens per case.

Conclusion: local Qwen3 8B is not fit for production triage (block recall 67%,
accuracy 41%). It remains useful as a free, offline configuration for developing the
harness and for smoke tests. The production choice needs the cloud runs; the
rule-based recommendation in this report will pick one once they exist. Run them with:

```bash
uv run sentinel-eval run --config default --concurrency 4
uv run sentinel-eval run --config gpt-mini --concurrency 4
uv run sentinel-eval run --config gpt-nano --concurrency 4
uv run sentinel-eval run --config gemini-flash-lite --concurrency 4
uv run sentinel-eval compare evals/results/*.json --title "Model comparison" --out evals/reports/<date>-comparison.md
```
