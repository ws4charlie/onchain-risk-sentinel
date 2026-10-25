# Sanctioned entity one hop away

## Description
The destination is not listed itself but transacts directly with a sanctioned address: it received funds from one or sent funds to one. Indirect exposure is a common way to evade screening, for example by placing a fresh intermediary between a sanctioned wallet and a regulated service.

## On-chain indicators
- A recent counterparty of the destination is on a sanctions list
- The destination was funded by a sanctioned address shortly before the withdrawal
- The destination forwards funds to a sanctioned address or service soon after receiving them

## Example signals
- List counterparties from the destination's recent transfers and check each against sanctions
- Weigh the share of the destination's volume that touches sanctioned addresses
- Prefer recent and large exposure over old dust

## References
- https://ofac.treasury.gov/faqs/topic/1626
- https://home.treasury.gov/news/press-releases/jy0768
