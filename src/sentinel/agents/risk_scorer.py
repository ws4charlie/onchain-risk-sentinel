"""Risk scorer: combines findings into a draft `Verdict`.

Hard cases are decided by deterministic rules over the cited evidence, so the
most consequential outcomes never depend on a model. The LLM always writes the
summary, and decides level, score and action only when no rule applies.

| Rule          | Condition (first match wins)                   | Level    | Score | Action  |
|---------------|------------------------------------------------|----------|-------|---------|
| sanctions_hit | any evidence of kind `sanctions_hit`           | critical | 100   | block   |
| scam_report   | any `scam_report` evidence whose category is   | high     | 90    | block   |
|               | drainer, phishing or address_poisoning         |          |       |         |
| lookalike     | any `lookalike_match` evidence (destination    | high     | 85    | block   |
|               | mimics an address the account used before)    |          |       |         |
| no_findings   | no specialist produced a finding               | medium   | 50    | hold    |
| (none)        | anything else                                  | LLM      | LLM   | LLM     |

("hold" is `hold_for_review`.)

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
from sentinel.core.schemas import Action, Finding, RiskLevel, Verdict

BLOCKING_SCAM_CATEGORIES = frozenset({"drainer", "phishing", "address_poisoning"})


class RuleOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule: str
    risk_level: RiskLevel
    score: int
    action: Action

    def describe(self) -> str:
        return (
            f"rule '{self.rule}' fixed risk_level={self.risk_level.value}, "
            f"score={self.score}, action={self.action.value}"
        )


class ScoreAssessment(BaseModel):
    """The scorer LLM's structured answer."""

    risk_level: RiskLevel
    score: int = Field(ge=0, le=100)
    action: Action
    summary: str = Field(description="Two to four sentences citing the findings.")


def apply_rules(findings: Mapping[str, Finding]) -> RuleOutcome | None:
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
        rule = apply_rules(findings)
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
        level, score, action = (
            (rule.risk_level, rule.score, rule.action)
            if rule
            else (assessment.risk_level, assessment.score, assessment.action)
        )
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
