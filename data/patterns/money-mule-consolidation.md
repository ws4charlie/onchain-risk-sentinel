# Money mule consolidation

## Description
Mule accounts receive scam or theft proceeds and pass them on, sometimes knowingly, sometimes recruited by fake job offers. On chain, many mule deposit addresses send to one consolidation address, which then moves funds to cash-out services.

## On-chain indicators
- Destination receives funds from many unrelated addresses that each have short lives
- Inflows are passed on in bulk on a schedule
- Senders are themselves funded by known scam addresses

## Example signals
- Count distinct senders and their ages in the destination's history
- Check a sample of senders in scam intel
- Look at the timing of outflows relative to inflows

## References
- https://en.wikipedia.org/wiki/Money_mule
- https://www.fincen.gov/sites/default/files/shared/FinCEN_Alert_Pig_Butchering_FINAL_508c.pdf
