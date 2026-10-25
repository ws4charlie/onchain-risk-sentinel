# Permit signature phishing

## Description
Instead of an on-chain approval, the victim signs an off-chain typed message (EIP-2612 permit or a Permit2 signature). The message looks harmless in many wallets, costs no gas, and leaves no trace until the attacker submits it together with a transfer. Losses can be large because a single signature can authorize the full balance of a token.

## On-chain indicators
- Victim funds leave through a permit plus transferFrom executed by a third party in one transaction
- The spender in the permit is a fresh EOA or a contract tied to known drainer kits
- Several victims' permits are executed by the same relayer address in a short window

## Example signals
- Search recent transfers into the destination for tokens that support permit (for example stablecoins) from many senders
- Check whether the destination forwards received tokens within the same block or the next few blocks
- Look up the destination in scam intel feeds as a drainer

## References
- https://eips.ethereum.org/EIPS/eip-2612
- https://docs.uniswap.org/contracts/permit2/overview
- https://www.chainalysis.com/blog/crypto-drainers/
