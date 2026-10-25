# Counterfeit token impersonation

## Description
Scammers deploy tokens whose name and symbol copy a well-known asset, such as a stablecoin, and send them to victims or use them in fake trades. The token is worthless but shows up in wallets and explorers next to the real one. It is also used to fake proof of funds in investment scams.

## On-chain indicators
- Token symbol matches a major asset but the contract address differs from the canonical one
- Token was deployed recently and has few holders other than recipients of airdropped spam
- Transfers of these tokens appear alongside address poisoning activity

## Example signals
- Compare the token contract in transfers with the canonical contract for that symbol
- Treat balances in non-canonical tokens as zero value
- Check whether the counterfeit token's sender also sent zero-value transfers

## References
- https://eips.ethereum.org/EIPS/eip-20
- https://www.chainalysis.com/blog/address-poisoning-scam/
