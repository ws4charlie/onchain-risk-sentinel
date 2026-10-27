"""Domain models shared by every module.

These models are the contract between the agent graph, the data adapters and
any entry point (CLI, API, evals). Change them only through the lead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_address(address: str) -> str:
    """Lowercase an EVM address and validate its shape (0x + 40 hex chars)."""
    value = address.strip().lower()
    if len(value) != 42 or not value.startswith("0x"):
        raise ValueError(f"not an EVM address: {address!r}")
    int(value[2:], 16)
    return value


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Chain(StrEnum):
    ETHEREUM = "ethereum"


class RiskLevel(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Action(StrEnum):
    ALLOW = "allow"
    HOLD_FOR_REVIEW = "hold_for_review"
    BLOCK = "block"


class Case(_Model):
    """A withdrawal (or address) submitted for triage."""

    case_id: str
    chain: Chain = Chain.ETHEREUM
    address: str = Field(description="Destination address of the withdrawal.")
    tx_hash: str | None = None
    asset: str = "ETH"
    amount: float | None = Field(default=None, ge=0, description="Amount in asset units.")
    account_context: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional facts about the withdrawing account, e.g. account age.",
    )
    requested_at: datetime = Field(default_factory=_utcnow)

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        return normalize_address(value)


class Evidence(_Model):
    """A single verifiable fact that supports a finding."""

    source: str = Field(description="Where the fact came from, e.g. 'etherscan', 'ofac'.")
    kind: str = Field(description="Short machine label, e.g. 'sanctions_hit'.")
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    url: str | None = None
    observed_at: datetime = Field(default_factory=_utcnow)


class Finding(_Model):
    """The output of one specialist agent."""

    agent: str
    risk_signal: RiskLevel
    confidence: float = Field(ge=0, le=1)
    rationale: str
    evidence: list[Evidence] = Field(default_factory=list)


class Verdict(_Model):
    """The final (or draft) decision for a case."""

    case_id: str
    risk_level: RiskLevel
    score: int = Field(ge=0, le=100)
    action: Action
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    needs_human_review: bool = False
    reviewed_by: str | None = None
    user_notice: str | None = Field(
        default=None,
        description="Plain-language warning for the withdrawing user (fixed templates only).",
    )


class ReviewDecision(_Model):
    """A human reviewer's answer to a paused case."""

    case_id: str
    reviewer: str
    approve: bool = Field(description="True accepts the proposed action as-is.")
    override_action: Action | None = Field(
        default=None, description="Required when approve is False."
    )
    note: str = ""


class AddressProfile(_Model):
    """Summary facts about an address from chain data."""

    chain: Chain
    address: str
    is_contract: bool
    balance_wei: int = Field(ge=0)
    tx_count: int = Field(ge=0)
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    labels: list[str] = Field(default_factory=list)


class Transfer(_Model):
    """One value transfer touching an address."""

    chain: Chain
    tx_hash: str
    block_number: int
    timestamp: datetime
    from_address: str
    to_address: str
    asset: str
    amount: float = Field(ge=0)


class SanctionHit(_Model):
    """A match against a sanctions list."""

    address: str
    list_name: str = Field(description="e.g. 'OFAC SDN'.")
    entity_name: str
    program: str | None = None
    listed_on: datetime | None = None
    source_url: str | None = None


class ScamReport(_Model):
    """A public report linking an address to a scam or phishing campaign."""

    address: str
    category: str = Field(description="e.g. 'phishing', 'drainer', 'address_poisoning'.")
    source: str
    description: str = ""
    reported_at: datetime | None = None
    source_url: str | None = None


class PatternDoc(_Model):
    """A fraud-pattern document returned by the knowledge base."""

    doc_id: str
    title: str
    content: str
    indicators: list[str] = Field(default_factory=list)
    score: float = Field(description="Similarity score; higher is more relevant.")
