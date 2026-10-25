# Malicious airdrop

## Description
Attackers airdrop tokens or NFTs whose name or metadata points to a website, for example a claim page. The site is a drainer. The airdrop itself is harmless; the risk is the follow-up interaction. Addresses that sent spam airdrops to many wallets are useful leads.

## On-chain indicators
- One address sends the same token or NFT to thousands of unrelated wallets
- Token names containing URLs or claim instructions
- Recipients later sign approvals to the same spender

## Example signals
- Check whether the destination is a mass-airdrop sender
- Look up the destination and linked contracts in phishing feeds
- Look for many outbound transfers of the same asset with equal amounts

## References
- https://github.com/scamsniffer/scam-database
- https://eips.ethereum.org/EIPS/eip-721
