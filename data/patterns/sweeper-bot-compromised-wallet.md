# Sweeper bot on a compromised wallet

## Description
If a wallet's private key leaks, attackers often run a bot that watches the wallet and immediately forwards any incoming ETH. Victims who send gas to rescue tokens lose it within the same block. A withdrawal destination with this behavior means the customer will not keep the funds.

## On-chain indicators
- Every inbound ETH transfer is followed within seconds or one block by an outbound transfer of nearly the same amount
- Outbound transfers pay high gas fees to win the race
- All outflows go to the same collector address

## Example signals
- Pair inflows and outflows by block number in the destination's recent transfers
- Check whether balances stay near zero despite inflows
- Report the collector address as a counterparty to check

## References
- https://ethereum.org/en/security/
