# Model comparison (pre counterparty screening)

Generated 2026-10-27 03:33 UTC by `sentinel-eval compare`. Metric definitions: `src/sentinel/evals/metrics.py` and `evals/README.md`.

## Results

| Config | Cases | Errors | Accuracy | Block recall | False block | Risk floor | Pause | Evidence cov. | p50 s | p95 s | Tokens/case | Cost/case |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `gemini-flash-lite` | 87 | 0 | 77% | 100% | 0% | 75% | 43% | 98% | 7.6 | 13.0 | 12,721 | $0.0060 |
| `gpt-mini` | 87 | 0 | 75% | 100% | 0% | 74% | 45% | 100% | 13.1 | 21.8 | 15,315 | $0.0171 |
| `gpt-nano` | 87 | 1 | 74% | 97% | 0% | 77% | 43% | 100% | 22.3 | 29.5 | 25,393 | $0.0083 |
| `ollama-qwen` | 71 | 15 | 41% | 67% | 0% | 46% | 30% | 88% | 109.3 | 183.0 | 8,628 | $0.0000 |

## Configurations

| Config | Run id | Models (role: model) |
|---|---|---|
| `gemini-flash-lite` | `gemini-flash-lite-20261027T025952Z-fd4333` | supervisor: `openrouter:google/gemini-3.5-flash-lite`, specialist: `openrouter:google/gemini-3.5-flash-lite`, scorer: `openrouter:google/gemini-3.5-flash-lite`, critic: `openrouter:google/gemini-3.5-flash-lite` |
| `gpt-mini` | `gpt-mini-20261027T031300Z-e1b723` | supervisor: `openai:gpt-5.4-mini`, specialist: `openai:gpt-5.4-mini`, scorer: `openai:gpt-5.4-mini`, critic: `openai:gpt-5.4-mini` |
| `gpt-nano` | `gpt-nano-20261027T030301Z-577626` | supervisor: `openai:gpt-5.4-nano`, specialist: `openai:gpt-5.4-nano`, scorer: `openai:gpt-5.4-nano`, critic: `openai:gpt-5.4-nano` |
| `ollama-qwen` | `ollama-qwen-20261025T203507Z-aa8730` | supervisor: `ollama:qwen3:8b`, specialist: `ollama:qwen3:8b`, scorer: `ollama:qwen3:8b`, critic: `ollama:qwen3:8b` |

## Not run

- `default` (Project default (core Settings): all roles on Gemini 3.5 Flash-Lite.): OPENROUTER_API_KEY is not set (needed for openrouter:google/gemini-3.5-flash-lite).
- `mixed-flash` (Gemini Flash specialists, GPT-5.4 mini elsewhere (former default).): OPENAI_API_KEY is not set (needed for openai:gpt-5.4-mini); OPENROUTER_API_KEY is not set (needed for openrouter:google/gemini-3.8-flash).

## Confusion matrices (rows: expected, columns: predicted)

### `gemini-flash-lite`

| expected \ predicted | allow | hold_for_review | block | error |
|---|---|---|---|---|
| allow | 25 | 0 | 0 | 0 |
| hold_for_review | 18 | 8 | 2 | 0 |
| block | 0 | 0 | 34 | 0 |

### `gpt-mini`

| expected \ predicted | allow | hold_for_review | block | error |
|---|---|---|---|---|
| allow | 25 | 0 | 0 | 0 |
| hold_for_review | 22 | 6 | 0 | 0 |
| block | 0 | 0 | 34 | 0 |

### `gpt-nano`

| expected \ predicted | allow | hold_for_review | block | error |
|---|---|---|---|---|
| allow | 22 | 3 | 0 | 0 |
| hold_for_review | 19 | 9 | 0 | 0 |
| block | 0 | 0 | 33 | 1 |

### `ollama-qwen`

| expected \ predicted | allow | hold_for_review | block | error |
|---|---|---|---|---|
| allow | 12 | 0 | 0 | 7 |
| hold_for_review | 15 | 1 | 4 | 8 |
| block | 8 | 0 | 16 | 0 |

## Accuracy by scenario

| Scenario | `gemini-flash-lite` | `gpt-mini` | `gpt-nano` | `ollama-qwen` |
|---|---|---|---|---|
| address_poisoning | 8/8 | 8/8 | 8/8 | 2/8 |
| benign_contract | 5/5 | 5/5 | 5/5 | 0/5 |
| benign_eoa | 10/10 | 10/10 | 7/10 | 8/10 |
| benign_fresh_small | 4/4 | 4/4 | 4/4 | 4/4 |
| benign_protocol | 6/6 | 6/6 | 6/6 | - |
| drainer_report | 8/8 | 8/8 | 8/8 | 8/8 |
| fresh_large_inflow | 4/8 | 0/8 | 2/8 | 0/8 |
| mixer_exposure | 0/5 | 1/5 | 0/5 | 0/5 |
| nonblocking_scam_report | 3/5 | 5/5 | 4/5 | 1/5 |
| ofac_listed | 10/10 | 10/10 | 9/10 | - |
| peel_chain | 0/4 | 0/4 | 3/4 | 0/4 |
| sanctioned_direct | 8/8 | 8/8 | 8/8 | 6/8 |
| sanctioned_one_hop | 1/6 | 0/6 | 0/6 | 0/6 |

## Recommendation

Rule-based pick: `gemini-flash-lite` has block recall 100% (best is 100%), action accuracy 77%, cost $0.0060 per case and p95 latency 13.0s.

## Notes

- The former default (Gemini 3.8 Flash specialists via OpenRouter, GPT-5.4 mini elsewhere) is
  not in the table: every case hit OpenRouter's new-account limit of 20 requests per minute
  for that model. Not rerun, to save cost.
- Decision: Gemini 3.5 Flash-Lite for every role becomes the project default. It leads on
  action accuracy, block recall, latency and cost per case.
- Almost every miss is `hold_for_review -> allow` on indirect-risk scenarios (one hop from a
  sanctioned address, mixer exposure, peel chain, fresh address with large inflow). No tool
  screens counterparties yet, so no model can see these signals. Fix planned in the agents.
- These numbers come from the development set. There is no held-out test set yet.
