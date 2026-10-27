# Data sources

Every external source used by `sentinel.tools` and `sentinel.knowledge`, with its license
and what (if anything) is committed to this repository. Retrieval dates are when the
source was last checked or downloaded for this repository.

| Source | Used by | URL | License / terms | Committed? | Retrieved |
|---|---|---|---|---|---|
| Etherscan API V2 | `EtherscanChainData` | https://docs.etherscan.io/ (endpoint `https://api.etherscan.io/v2/api`) | Etherscan API Terms of Service (https://etherscan.io/apiterms); free API key required | No. Queried at runtime; unit tests use hand-written fixtures in the documented response shape | 2026-10-25 |
| Ethereum JSON-RPC (`eth_getBalance`, `eth_getCode`, `eth_getTransactionCount`) | `RpcChainData`, `JsonRpcClient` | https://ethereum.org/en/developers/docs/apis/json-rpc/ (spec: https://ethereum.github.io/execution-apis/; error object: https://www.jsonrpc.org/specification) | Open specification; each node provider's own terms apply to its endpoint (`SENTINEL_ETH_RPC_URLS`) | No. Queried at runtime; unit tests use hand-written responses | 2026-10-25 |
| Alchemy Transfers API (`alchemy_getAssetTransfers`) | `RpcChainData` (address history) | https://www.alchemy.com/docs/reference/alchemy-getassettransfers (endpoint `https://eth-mainnet.g.alchemy.com/v2/<key>`) | Alchemy Terms of Service (https://legal.alchemy.com/); free API key required | No. Queried at runtime; unit tests use hand-written fixtures in the documented response shape (`tests/tools/fixtures/alchemy_transfers.json`) | 2026-10-25 |
| OFAC SDN list (XML) | `OfacSanctions` | https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML | U.S. Government work, public domain (17 U.S.C. § 105) | Yes: `data/sanctions/ofac_sdn_evm_addresses.json`, the EVM-format digital currency addresses extracted from the list (SDN publish date 2026-10-21) | 2026-10-25 |
| ScamSniffer scam-database (`blacklist/address.json`) | `FeedScamIntel` | https://github.com/scamsniffer/scam-database | GPL-3.0 | No. Downloaded at runtime into `.cache/sentinel/scam_intel/` (git-ignored). The address list stopped updating in February 2024; the repository is published with a 7-day delay | 2026-10-25 |
| Forta labelled datasets (`labels/1/phishing_scams.csv`, `malicious_smart_contracts.csv`, `etherscan_malicious_labels.csv`) | `FeedScamIntel` | https://github.com/forta-network/labelled-datasets | MIT (repository); the rows are derived from third-party block-explorer labels, so they are not redistributed here | No. Downloaded at runtime into `.cache/sentinel/scam_intel/` (git-ignored). Last updated January 2023 | 2026-10-25 |
| Gemini embeddings (`gemini-embedding-001`) | `default_embeddings()` | https://ai.google.dev/gemini-api/docs/embeddings | Gemini API Terms of Service; API key required | No. Vectors are cached in `.cache/sentinel/patterns/` (git-ignored) | 2026-10-25 |

## Pattern knowledge base (`data/patterns/`)

The 26 documents in `data/patterns/` were written for this repository (MIT, like the
rest of the code). They summarize publicly documented fraud and risk patterns in our own
words; no text is copied from the sources. Each document lists its public references at
the end. References were checked to resolve on 2026-10-25.

## Refreshing

- Sanctions snapshot: `uv run python -m sentinel.tools.refresh_sanctions`
- Scam feeds: `await FeedScamIntel.load()` downloads feeds older than one day.
