"""Risk scorer: combines findings into a draft `Verdict`.

Hard cases are decided by deterministic rules over the cited evidence, so the
most consequential outcomes never depend on a model. The LLM always writes the
summary, and decides level, score and action only when no rule applies.

| Rule                  | Condition (first match wins)                 | Level    | Score | Action |
|-----------------------|----------------------------------------------|----------|-------|--------|
| sanctions_hit         | evidence `sanctions_hit`                     | critical | 100   | block  |
| scam_report           | `scam_report`, category drainer, phishing    | high     | 90    | block  |
|                       | or address_poisoning                         |          |       |        |
| lookalike             | `lookalike_match` (destination mimics an     | high     | 85    | block  |
|                       | address the account used before)             |          |       |        |
| counterparty_sanction | `counterparty_sanctions_hit` with exposure   | high if  | 80/70 | hold+  |
|                       | share >= 10% (high when >= 50%)              | >= 50%,  |       |        |
|                       |                                              | else med |       |        |
| counterparty_mixer    | `counterparty_mixer` with exposure >= 20%    | medium   | 65    | hold+  |
| peel_chain            | `flow_features.peel_chain`                   | medium   | 65    | hold+  |
| fresh_inflow          | fresh destination, >= 50% of inflow from     | medium   | 60    | hold+  |
|                       | >= 2 fresh funders, unusual withdrawal       |          |       |        |
| no_findings           | no specialist produced a finding             | medium   | 50    | hold   |
| (none)                | anything else                                | LLM      | LLM   | LLM    |

"hold" is `hold_for_review`. "hold+" rules are floors: the verdict is at least that
level, score and action, and the LLM may only escalate (e.g. to block). The other rules
fix the outcome. Exposure share is the counterparty's share of the destination's
inflow or outflow (whichever is larger) in its primary asset; see `screening.py`.

Why these thresholds (they are about real flows, not a test fixture):
- 10% for sanctioned counterparties: any direct exposure matters, but attackers
  "dust" arbitrary addresses with tiny transfers from sanctioned wallets, so a
  floor on share keeps dust from forcing reviews; 10% is still well below "funded by".
- 20% for mixers: mixers are also used for legitimate privacy and dusting is common,
  so the bar is higher than for sanctions; a fifth of the flow is material exposure.
- Peel chain: see `screening.PEEL_*` (dominant inflow, then >= 4 hops to fresh
  addresses, each at most 30% of what is left).
- fresh_inflow: a destination younger than `screening.FRESH_DAYS` (7 days), funded
  mostly (>= 50%) by at least two other days-old addresses, is a mule / layering shape;
  it only holds when the withdrawal is also unusual for the account
  (`withdrawal_is_unusual`): account younger than 30 days, flagged first withdrawal,
  or amount >= 3x the account's `typical_withdrawal_amount`. Freshness alone is not
  a reason for friction.

`needs_human_review` is set when `action != allow` and `score >= review_threshold`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from sentinel.agents import prompts
from sentinel.agents.state import Node, TriageState
from sentinel.agents.structured import ask_structured
from sentinel.core.config import Settings
from sentinel.core.schemas import Action, Case, Evidence, Finding, RiskLevel, Verdict

BLOCKING_SCAM_CATEGORIES = frozenset({"drainer", "phishing", "address_poisoning"})

SANCTIONED_EXPOSURE_MIN = 0.10
SANCTIONED_EXPOSURE_HIGH = 0.50
MIXER_EXPOSURE_MIN = 0.20
FRESH_INFLOW_SHARE_MIN = 0.50
FRESH_FUNDERS_MIN = 2
YOUNG_ACCOUNT_DAYS = 30
UNUSUAL_AMOUNT_MULTIPLE = 3.0

_LEVELS = list(RiskLevel)
_ACTIONS = [Action.ALLOW, Action.HOLD_FOR_REVIEW, Action.BLOCK]


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def withdrawal_is_unusual(case: Case) -> bool:
    """True when the account context says this withdrawal is out of pattern."""
    context = case.account_context
    age = _number(context.get("account_age_days"))
    if age is not None and age < YOUNG_ACCOUNT_DAYS:
        return True
    if context.get("first_withdrawal") is True:
        return True
    typical = _number(context.get("typical_withdrawal_amount"))
    return bool(
        typical and case.amount is not None and case.amount >= UNUSUAL_AMOUNT_MULTIPLE * typical
    )


class RuleOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule: str
    risk_level: RiskLevel
    score: int
    action: Action
    floor: bool = Field(default=False, description="True: the LLM may escalate, not lower.")

    def describe(self) -> str:
        return (
            f"rule '{self.rule}' fixed risk_level={self.risk_level.value}, "
            f"score={self.score}, action={self.action.value}"
            + (" as a minimum (you may escalate if the evidence supports it)" if self.floor else "")
        )


class ScoreAssessment(BaseModel):
    """The scorer LLM's structured answer."""

    risk_level: RiskLevel
    score: int = Field(ge=0, le=100)
    action: Action
    summary: str = Field(description="Two to four sentences citing the findings.")


def _share(evidence: Evidence) -> float:
    return _number(evidence.data.get("exposure_share")) or 0.0


def _hold(rule: str, level: RiskLevel, score: int) -> RuleOutcome:
    return RuleOutcome(
        rule=rule, risk_level=level, score=score, action=Action.HOLD_FOR_REVIEW, floor=True
    )


def apply_rules(findings: Mapping[str, Finding], case: Case | None = None) -> RuleOutcome | None:
    """Return the outcome of the first matching hard rule, or None."""
    evidence = [e for f in findings.values() for e in f.evidence]
    if any(e.kind == "sanctions_hit" for e in evidence):
        return RuleOutcome(
            rule="sanctions_hit", risk_level=RiskLevel.CRITICAL, score=100, action=Action.BLOCK
        )
    if any(
        e.kind == "scam_report" and e.data.get("category") in BLOCKING_SCAM_CATEGORIES
        for e in evidence
    ):
        return RuleOutcome(
            rule="scam_report", risk_level=RiskLevel.HIGH, score=90, action=Action.BLOCK
        )
    if any(e.kind == "lookalike_match" for e in evidence):
        return RuleOutcome(
            rule="lookalike", risk_level=RiskLevel.HIGH, score=85, action=Action.BLOCK
        )
    sanctioned = [e for e in evidence if e.kind == "counterparty_sanctions_hit"]
    top = max((_share(e) for e in sanctioned), default=0.0)
    if top >= SANCTIONED_EXPOSURE_MIN:
        if top >= SANCTIONED_EXPOSURE_HIGH:
            return _hold("counterparty_sanction", RiskLevel.HIGH, 80)
        return _hold("counterparty_sanction", RiskLevel.MEDIUM, 70)
    if any(e.kind == "counterparty_mixer" and _share(e) >= MIXER_EXPOSURE_MIN for e in evidence):
        return _hold("counterparty_mixer", RiskLevel.MEDIUM, 65)
    features = [e.data for e in evidence if e.kind == "flow_features"]
    if any(f.get("peel_chain") is True for f in features):
        return _hold("peel_chain", RiskLevel.MEDIUM, 65)
    if (
        case is not None
        and withdrawal_is_unusual(case)
        and any(
            f.get("destination_fresh") is True
            and (_number(f.get("fresh_inflow_share")) or 0.0) >= FRESH_INFLOW_SHARE_MIN
            and (_number(f.get("fresh_funder_count")) or 0) >= FRESH_FUNDERS_MIN
            for f in features
        )
    ):
        return _hold("fresh_inflow", RiskLevel.MEDIUM, 60)
    if not findings:
        return RuleOutcome(
            rule="no_findings",
            risk_level=RiskLevel.MEDIUM,
            score=50,
            action=Action.HOLD_FOR_REVIEW,
        )
    return None


def make_risk_scorer(model: BaseChatModel, settings: Settings) -> Node[dict[str, Any]]:
    async def risk_scorer(state: TriageState) -> dict[str, Any]:
        case = state["case"]
        findings = state.get("findings", {})
        rule = apply_rules(findings, case)
        assessment = await ask_structured(
            model,
            ScoreAssessment,
            [
                SystemMessage(prompts.SCORER),
                HumanMessage(
                    prompts.render_scorer_input(case, findings, rule.describe() if rule else None)
                ),
            ],
        )
        level, score, action = assessment.risk_level, assessment.score, assessment.action
        if rule and rule.floor:
            level = max(level, rule.risk_level, key=_LEVELS.index)
            score = max(score, rule.score)
            action = max(action, rule.action, key=_ACTIONS.index)
        elif rule:
            level, score, action = rule.risk_level, rule.score, rule.action
        draft = Verdict(
            case_id=case.case_id,
            risk_level=level,
            score=score,
            action=action,
            summary=assessment.summary,
            findings=list(findings.values()),
            needs_human_review=action != Action.ALLOW and score >= settings.review_threshold,
        )
        return {"draft_verdict": draft}

    return risk_scorer
