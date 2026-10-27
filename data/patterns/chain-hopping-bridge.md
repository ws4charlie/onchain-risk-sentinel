# Chain hopping through bridges

## Description
Launderers move funds across chains with bridges and swaps to break tracing within one explorer and to reach chains with weaker monitoring. A withdrawal destination that immediately bridges everything out is harder to monitor and is common in theft and scam cash-out flows.

## On-chain indicators
- Inflows are forwarded to bridge contracts within minutes
- Destination interacts almost only with bridges and swap routers
- Value is converted into a different asset before bridging

## Example signals
- Check whether outflows of the destination go to bridge or router contracts
- Measure the delay between inflow and outflow
- Combine with the destination's age and funding source

## References
- https://www.ic3.gov/PSA/2025/PSA250226
