# Fake investment or Ponzi platform

## Description
Fraudulent yield platforms promise high fixed returns. Early users are paid with later users' deposits until withdrawals stop. On chain, a platform contract or hot wallet takes many deposits, pays some small returns, and the operators drain the rest.

## On-chain indicators
- Contract or address receives many deposits and pays out regular small amounts
- Outflows to a few operator addresses grow over time
- Sudden stop of payouts followed by a large outflow

## Example signals
- Check whether the destination is a contract with many depositors
- Compare inflow and outflow patterns over recent transfers
- Look up the destination in scam intel

## References
- https://en.wikipedia.org/wiki/Ponzi_scheme
- https://www.ic3.gov/PSA/2023/PSA230314
