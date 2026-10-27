# Ransomware payment flow

## Description
Ransomware victims pay a ransom to an address supplied by the attackers. Proceeds are quickly split between affiliates and operators and then laundered through mixers, swaps and high-risk services. Paying or facilitating payments to sanctioned ransomware actors carries legal risk.

## On-chain indicators
- Single large inbound payment followed by a fast split into a few outputs
- Destination or its next hop on a sanctions list in a cyber-related program
- Funds quickly reach mixers or high-risk exchanges

## Example signals
- Check the destination and its next hops against sanctions
- Look at the split pattern of the first outflows after a large inflow
- Check scam intel for related reports

## References
- https://www.cisa.gov/stopransomware
- https://en.wikipedia.org/wiki/Ransomware
- https://ofac.treasury.gov/faqs/topic/1626
