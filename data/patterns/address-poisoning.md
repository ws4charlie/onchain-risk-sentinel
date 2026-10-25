# Address poisoning

## Description
An attacker generates a vanity address whose first and last characters match an address the victim often pays. The attacker then places that look-alike in the victim's transaction history, usually with zero-value token transfers (abusing transferFrom with amount 0) or tiny dust payments. Later the victim copies the wrong address from history and sends real funds to the attacker.

## On-chain indicators
- Destination is very new and has few transactions, almost all of them zero-value or dust transfers
- Destination shares a 4-6 character prefix and suffix with an address the sender or its counterparties used before
- Many zero-value ERC-20 transfers emitted from the look-alike to many unrelated victims in a short window
- Fake tokens with names or symbols that copy popular stablecoins in the same transactions

## Example signals
- Compare the destination's prefix and suffix with recent counterparties of the withdrawing account
- Count zero-value transfers in the destination's recent history
- Check the destination's age (first_seen) against the amount being withdrawn

## References
- https://www.chainalysis.com/blog/address-poisoning-scam/
- https://revoke.cash/learn/security/what-is-address-poisoning
