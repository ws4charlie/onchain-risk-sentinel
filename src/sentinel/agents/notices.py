"""User notices: plain-language warnings shown to the withdrawing user.

Only for fraud that targets the user (scam reports and address look-alikes), so the
user can protect their funds. Never for sanctions, mixers or other AML signals:
telling a customer about an AML suspicion ("tipping off") is not allowed.

Notices come from fixed templates, never from a model, so they cannot overstate or
invent a risk.
"""

from __future__ import annotations

from collections.abc import Iterable

from sentinel.core.schemas import Evidence

_SCAM_NOTICES: dict[str, str] = {
    "ponzi": (
        "This address is reported as part of a high-yield investment scheme that pays "
        "earlier investors with new deposits. Such schemes usually collapse and funds are "
        "rarely recovered."
    ),
    "honeypot": (
        "This address is reported as a honeypot: tokens or funds sent to it can often not "
        "be sold or withdrawn again."
    ),
    "fake_token": (
        "This address is reported as issuing a counterfeit token that imitates a real one."
    ),
    "suspicious_activity": "This address is reported for suspicious activity.",
    "drainer": (
        "This address is reported as a wallet drainer that steals funds after you sign an approval."
    ),
    "phishing": "This address is reported as part of a phishing scam.",
    "address_poisoning": (
        "This address is reported for address poisoning: it imitates an address you used "
        "before so it gets copied by mistake."
    ),
}
_SCAM_DEFAULT = "This address is reported in public scam databases."
_LOOKALIKE = (
    "This address looks almost the same as one you sent to before, but it is a different "
    "address. Scammers create look-alike addresses so they get copied by mistake."
)
_ADVICE = "Please check the address with the recipient through a trusted channel before sending."


def user_notice(evidence: Iterable[Evidence]) -> str | None:
    """The notice for the strongest user-facing risk in the evidence, or None."""
    items = list(evidence)
    if any(e.kind == "lookalike_match" for e in items):
        return f"{_LOOKALIKE} {_ADVICE}"
    categories = [str(e.data.get("category", "")) for e in items if e.kind == "scam_report"]
    if not categories:
        return None
    known = [c for c in categories if c in _SCAM_NOTICES]
    text = _SCAM_NOTICES[known[0]] if known else _SCAM_DEFAULT
    return f"{text} {_ADVICE}"
