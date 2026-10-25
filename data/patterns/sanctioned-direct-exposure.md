# Direct sanctioned address

## Description
The destination address itself appears on a sanctions list such as the OFAC SDN list, which publishes digital currency addresses of designated persons and entities. Sending funds to it may be prohibited for regulated businesses, regardless of the customer's intent.

## On-chain indicators
- Exact, case-insensitive match of the destination against a sanctions list
- The listed entity's program, for example cyber-related, DPRK or narcotics programs
- Recent list updates: designations are added and sometimes removed

## Example signals
- Check the destination against the sanctions source
- Record the list name, entity and program as evidence
- Note the list's publish date, since lists change

## References
- https://ofac.treasury.gov/faqs/topic/1626
- https://sanctionssearch.ofac.treas.gov/
