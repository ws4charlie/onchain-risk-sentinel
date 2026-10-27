# Flash-Lite before vs after counterparty screening

Generated 2026-10-27 04:31 UTC by `sentinel-eval compare`. Metric definitions: `src/sentinel/evals/metrics.py` and `evals/README.md`.

## Results

| Config | Cases | Errors | Accuracy | Block recall | False block | Risk floor | Pause | Evidence cov. | p50 s | p95 s | Tokens/case | Cost/case |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `gemini-flash-lite` | 87 | 0 | 77% | 100% | 0% | 75% | 43% | 98% | 7.6 | 13.0 | 12,721 | $0.0060 |
| `default` | 87 | 0 | 97% | 100% | 0% | 100% | 71% | 100% | 7.5 | 12.0 | 15,548 | $0.0069 |

## Configurations

| Config | Run id | Models (role: model) |
|---|---|---|
| `gemini-flash-lite` | `gemini-flash-lite-20261027T025952Z-fd4333` | supervisor: `openrouter:google/gemini-3.5-flash-lite`, specialist: `openrouter:google/gemini-3.5-flash-lite`, scorer: `openrouter:google/gemini-3.5-flash-lite`, critic: `openrouter:google/gemini-3.5-flash-lite` |
| `default` | `default-20261027T042836Z-577889` | supervisor: `openrouter:google/gemini-3.5-flash-lite`, specialist: `openrouter:google/gemini-3.5-flash-lite`, scorer: `openrouter:google/gemini-3.5-flash-lite`, critic: `openrouter:google/gemini-3.5-flash-lite` |

## Not run

- `mixed-flash` (Gemini Flash specialists, GPT-5.4 mini elsewhere (former default).): OPENAI_API_KEY is not set (needed for openai:gpt-5.4-mini); OPENROUTER_API_KEY is not set (needed for openrouter:google/gemini-3.8-flash).
- `gpt-mini` (All roles on GPT-5.4 mini.): OPENAI_API_KEY is not set (needed for openai:gpt-5.4-mini).
- `gpt-nano` (All roles on GPT-5.4 nano.): OPENAI_API_KEY is not set (needed for openai:gpt-5.4-nano).
- `ollama-qwen` (All roles on local Qwen3 8B via Ollama, thinking disabled.): not run in this comparison.

## Confusion matrices (rows: expected, columns: predicted)

### `gemini-flash-lite`

| expected \ predicted | allow | hold_for_review | block | error |
|---|---|---|---|---|
| allow | 25 | 0 | 0 | 0 |
| hold_for_review | 18 | 8 | 2 | 0 |
| block | 0 | 0 | 34 | 0 |

### `default`

| expected \ predicted | allow | hold_for_review | block | error |
|---|---|---|---|---|
| allow | 24 | 1 | 0 | 0 |
| hold_for_review | 0 | 26 | 2 | 0 |
| block | 0 | 0 | 34 | 0 |

## Accuracy by scenario

| Scenario | `gemini-flash-lite` | `default` |
|---|---|---|
| address_poisoning | 8/8 | 8/8 |
| benign_contract | 5/5 | 5/5 |
| benign_eoa | 10/10 | 10/10 |
| benign_fresh_small | 4/4 | 3/4 |
| benign_protocol | 6/6 | 6/6 |
| drainer_report | 8/8 | 8/8 |
| fresh_large_inflow | 4/8 | 8/8 |
| mixer_exposure | 0/5 | 5/5 |
| nonblocking_scam_report | 3/5 | 4/5 |
| ofac_listed | 10/10 | 10/10 |
| peel_chain | 0/4 | 4/4 |
| sanctioned_direct | 8/8 | 8/8 |
| sanctioned_one_hop | 1/6 | 5/6 |

## Recommendation

Rule-based pick: `default` has block recall 100% (best is 100%), action accuracy 97%, cost $0.0069 per case and p95 latency 12.0s.

## Notes

- Same model (Gemini 3.5 Flash-Lite) and dataset; the change is in the agents:
  counterparty screening, flow features, new hold floors, mandatory chain tracing.
  The real-address snapshots were re-recorded between the runs.
- Remaining misses (3/87): two `hold_for_review -> block` escalations by the scorer
  (floors allow escalation), one benign fresh address held for review.
- Caution: the new rules were designed after looking at failures on this same
  development set, so this score is optimistic. A held-out test set is planned.
