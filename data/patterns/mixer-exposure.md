# Mixer exposure

## Description
Mixers pool deposits from many users and pay out to fresh addresses, breaking the visible link between source and destination. Some mixers have been sanctioned or used heavily to launder theft proceeds. Direct deposits into a mixer, or receiving funds straight from one, is a strong risk signal for a withdrawal destination. Sanctions status changes over time (for example, a large Ethereum mixer was designated in 2022 and removed from the SDN list in 2025), so check the current list rather than assuming.

## On-chain indicators
- Transfers to or from known mixer pool contracts, often in fixed denominations such as 0.1, 1, 10 or 100 ETH
- A fresh address funded by a mixer withdrawal and used soon after for a large action
- Repeated deposits of identical round amounts spread over time to avoid patterns

## Example signals
- Check counterparties of the destination against mixer contract labels
- Look for fixed-denomination transfers in recent history
- Check whether the first funding of the destination came from a mixer

## References
- https://home.treasury.gov/news/press-releases/jy0916
- https://home.treasury.gov/news/press-releases/jy0768
- https://www.chainalysis.com/blog/crypto-mixers/
- https://ofac.treasury.gov/recent-actions/20250321
- https://en.wikipedia.org/wiki/Tornado_Cash
