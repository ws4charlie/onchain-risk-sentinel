# State-sponsored hacker laundering

## Description
Groups attributed to North Korea (for example the Lazarus Group) steal large amounts from exchanges and bridges and launder them through many fresh addresses, decentralized swaps, cross-chain bridges, mixers and OTC brokers. Their addresses are often published by law enforcement and added to sanctions lists.

## On-chain indicators
- Destination or a close counterparty is listed in an FBI or OFAC publication on DPRK cyber activity
- Fast fan-out of very large balances into many addresses holding similar amounts
- Heavy use of cross-chain bridges and swaps from tokens into ETH or BTC

## Example signals
- Check the destination and its counterparties against sanctions
- Check scam intel for heist categories
- Look for fan-out patterns with many similar outflows

## References
- https://www.ic3.gov/PSA/2025/PSA250226
- https://home.treasury.gov/news/press-releases/jy0768
- https://home.treasury.gov/news/press-releases/jy1435
- https://en.wikipedia.org/wiki/Lazarus_Group
