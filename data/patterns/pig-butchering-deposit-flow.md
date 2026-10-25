# Pig butchering deposit flow

## Description
In a pig butchering (romance or investment) scam, a scammer builds trust over weeks and then guides the victim to a fake trading platform. The victim buys crypto at a regulated service and withdraws it to a deposit address the scammer controls, often several times with growing amounts. Funds are then consolidated and moved through layers of addresses, usually as USDT.

## On-chain indicators
- A customer with no prior crypto activity starts withdrawing to one new external address, in increasing amounts
- The destination receives deposits from many unrelated retail-looking sources and quickly forwards stablecoins onward
- Destination addresses are rotated per victim but consolidate to the same next-hop wallets
- Account context such as recent account opening, an older customer, or a stated investment purpose

## Example signals
- Check the destination's age and whether it forwards most inflows within hours
- Look at the number of distinct senders feeding the destination
- Compare the withdrawal size with the account's history and stated profile

## References
- https://www.fincen.gov/sites/default/files/shared/FinCEN_Alert_Pig_Butchering_FINAL_508c.pdf
- https://www.ic3.gov/PSA/2023/PSA230314
- https://en.wikipedia.org/wiki/Pig_butchering_scam
