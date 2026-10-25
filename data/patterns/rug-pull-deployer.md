# Rug pull token deployer

## Description
A deployer launches a token, adds liquidity, promotes it, and then removes the liquidity or mints and dumps supply, leaving holders with worthless tokens. Many deployers repeat the cycle with new tokens from the same or linked addresses.

## On-chain indicators
- Address deployed many short-lived token contracts
- Liquidity added and removed by the same address within days
- Large mint or transfer of supply to the deployer just before a sell-off
- Proceeds moved to fresh addresses or mixers after each launch

## Example signals
- Check whether the destination is a contract deployer with many contract creations
- Look for liquidity-removal or large token sell transactions
- Look up the address in scam intel as a malicious contract deployer

## References
- https://en.wikipedia.org/wiki/Rug_pull
- https://github.com/forta-network/labelled-datasets
