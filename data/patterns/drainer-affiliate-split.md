# Drainer-as-a-service profit split

## Description
Wallet drainer kits are sold as a service. When a victim is drained, the kit's contract splits the stolen funds between the affiliate who ran the phishing site and the kit operator, often with a fixed ratio such as 80/20 or 85/15. The same operator address therefore receives a constant share from thousands of unrelated thefts.

## On-chain indicators
- Incoming transfers to one address that are always a fixed percentage of a parallel transfer to another address in the same transaction
- Thousands of small inflows from different victim contracts, with the same split ratio
- Collector addresses that later move funds to mixers or cross-chain bridges

## Example signals
- Inspect transactions that send funds to two recipients at the same time and compute the ratio
- Count distinct senders of inbound transfers over the last days
- Check scam intel for drainer or phishing reports on either recipient

## References
- https://www.chainalysis.com/blog/crypto-drainers/
- https://github.com/scamsniffer/scam-database
