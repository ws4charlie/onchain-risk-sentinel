# Account takeover withdrawal

## Description
When an attacker takes over a customer account (phishing, SIM swap, stolen session), they usually change security settings, add a new withdrawal address and withdraw the full balance quickly. The on-chain side is typically a fresh address that forwards the funds immediately.

## On-chain indicators
- Withdrawal to an address never used by the account before, right after a login from a new device or a security change
- Withdrawal of almost the entire balance
- Destination forwards funds to a mixer, bridge or exchange within minutes

## Example signals
- Read account context (account age, recent security changes, usual destinations)
- Check the destination's age and onward flow
- Check whether the destination appears in scam intel

## References
- https://en.wikipedia.org/wiki/SIM_swap_scam
