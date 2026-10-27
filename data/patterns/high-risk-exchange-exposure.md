# High-risk or sanctioned exchange exposure

## Description
Some virtual asset services have been sanctioned for processing ransomware, darknet and scam proceeds, or operate without customer checks. Sending a withdrawal straight to a deposit address of such a service, or receiving funds from one, raises the risk of the destination.

## On-chain indicators
- Destination is a deposit address of a sanctioned or no-KYC service
- Destination's counterparties include addresses of services listed in OFAC actions
- Funds routed through instant swap services that hide the final recipient

## Example signals
- Check the destination and counterparties against the sanctions list
- Note listed entities whose type is a virtual asset service
- Combine with the share of volume going to those services

## References
- https://home.treasury.gov/news/press-releases/jy0701
- https://ofac.treasury.gov/faqs/topic/1626
