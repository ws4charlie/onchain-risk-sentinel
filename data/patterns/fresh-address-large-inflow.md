# Fresh address receiving a large amount

## Description
Legitimate users usually test a new address with a small amount first. A destination that has never been used, or was created hours ago, and is about to receive a large withdrawal is typical of scam deposit addresses, account takeovers and coached victims. On its own it is a weak signal, but it strengthens other signals.

## On-chain indicators
- Destination has no history, or its first activity is within the last few days
- Withdrawal amount is large compared to the account's usual withdrawals
- No small test transfer preceded the large one
- The destination was funded with just enough gas by an unrelated address

## Example signals
- Read first_seen and tx_count from the destination profile
- Compare the amount with account context (typical withdrawal size, account age)
- Check where the destination's first gas funding came from

## References
- https://www.ic3.gov/PSA/2023/PSA230314
