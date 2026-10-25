# Peel chain

## Description
A peel chain moves a large balance through a long series of addresses. At each hop a small amount is peeled off to a service (often an exchange deposit address) and the larger remainder is sent to a new address. It is used to cash out stolen or illicit funds gradually while making tracing tedious.

## On-chain indicators
- Address receives one large inflow and sends it out within hours as one small and one large output
- The large output goes to a brand new address that repeats the same behavior
- Peeled amounts go to deposit addresses of services, often just under reporting thresholds
- Each address in the chain is used only once or twice

## Example signals
- Check whether the destination has exactly one large inflow and two outflows
- Follow the larger output for one or two hops and look for the same shape
- Check how long funds stay at each address

## References
- https://www.chainalysis.com/blog/crypto-mixers/
- https://en.wikipedia.org/wiki/Structuring
