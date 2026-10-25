# Honeypot token

## Description
A honeypot token can be bought but not sold, because its contract blocks transfers from anyone except an allowlist, applies a near-100% sell tax, or can be paused by the owner. Victims buy in while the owner sells.

## On-chain indicators
- Token contract with owner-only transfer allowlists, adjustable fees or a pause switch
- Many buy transfers into holders and almost no successful sells except from a few addresses
- Unverified or recently changed contract code

## Example signals
- If the destination is a token contract, check verification status and contract name
- Look at the transfer history for one-way flow
- Check scam intel for the contract and its deployer

## References
- https://ethereum.org/en/developers/docs/smart-contracts/security/
- https://en.wikipedia.org/wiki/Rug_pull
