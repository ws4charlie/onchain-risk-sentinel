# Approval drainer

## Description
A phishing site (fake mint, fake airdrop, fake wallet support) asks the victim to sign an ERC-20 approve or an NFT setApprovalForAll for a contract or address the attacker controls. Once the approval is on chain, the attacker calls transferFrom and sweeps the approved assets, often minutes later and often for many victims through the same contract.

## On-chain indicators
- Recipient is a contract that receives assets through transferFrom from many unrelated owners
- Victims granted unlimited allowances to the same spender shortly before losing funds
- Recent contract deployment, unverified source code, or a generic name
- Swept tokens are swapped to ETH quickly and forwarded to a small set of collector addresses

## Example signals
- Check whether the destination is a contract and whether its source is verified
- Look for many inbound token transfers from distinct senders within hours
- Look up the destination and its deployer in scam intel feeds

## References
- https://www.chainalysis.com/blog/crypto-drainers/
- https://revoke.cash/learn/approvals/what-are-token-approvals
- https://eips.ethereum.org/EIPS/eip-20
