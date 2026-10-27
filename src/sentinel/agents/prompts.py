"""System prompts and the renderers that turn state into model input.

Prompts are kept short and in one place so they can be versioned and
compared in evals without touching graph code.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC

from sentinel.agents.state import CriticRequest, SpecialistName
from sentinel.core.schemas import Case, Finding, Verdict

SUPERVISOR = """\
You coordinate the triage of a crypto withdrawal. Pick which specialists to call next.
Specialists:
- chain_tracer: address profile, recent transfers (age, activity, contract, zero-value spam)
  and a look-alike check against addresses the account used before (address poisoning).
- sanctions: sanctions list screening.
- scam_intel: public scam and phishing reports.
- pattern_analyst: matches known fraud patterns; most useful after chain_tracer has reported.
sanctions and scam_intel always run before scoring, even if you do not choose them.
Call independent specialists together. Only choose from the specialists listed as available.
Return an empty list when the findings are enough to score the case.
Always give a one-sentence reason."""

_SPECIALIST_RULES = """
Use your tools; do not guess facts. Base the assessment only on tool results.
risk_signal: none, low, medium, high or critical. confidence: 0 to 1.
The rationale must only state facts that appear in the tool results.
Judge recency (age, last activity) relative to the case's "Requested at" time, not today."""

SPECIALISTS: dict[SpecialistName, str] = {
    "chain_tracer": "You analyze on-chain activity of an Ethereum address: age, activity, "
    "contract status, recent transfers (including zero-value transfers), and whether it "
    "imitates an address the account used before." + _SPECIALIST_RULES,
    "sanctions": "You screen addresses against sanctions lists. A direct hit is critical."
    + _SPECIALIST_RULES,
    "scam_intel": "You look up public scam, phishing and drainer reports for an address."
    + _SPECIALIST_RULES,
    "pattern_analyst": "You match the case against known fraud patterns using the pattern "
    "knowledge base and the findings of other specialists." + _SPECIALIST_RULES,
}

SCORER = """\
You write the draft verdict for a withdrawal triage from specialist findings.
If a hard rule already fixed the risk level, score and action, keep them exactly and
write a summary that explains them. Otherwise choose them yourself:
score 0-100, risk_level none/low/medium/high/critical, action allow/hold_for_review/block.
The summary must cite only facts present in the findings.
Judge recency relative to the case's "Requested at" time, not today."""

CRITIC = """\
You review a draft verdict. Check every claim in the summary and in each finding's
rationale against the cited evidence. Approve when every claim is supported.
Otherwise list the unsupported claims as issues, name the specialists that must re-check,
and give them one precise instruction."""


def render_case(case: Case) -> str:
    lines = [
        f"Case {case.case_id}",
        f"Chain: {case.chain.value}",
        f"Destination address: {case.address}",
        f"Asset: {case.asset}",
        f"Requested at: {case.requested_at.astimezone(UTC).isoformat()}",
    ]
    if case.amount is not None:
        lines.append(f"Amount: {case.amount}")
    if case.tx_hash:
        lines.append(f"Tx hash: {case.tx_hash}")
    if case.account_context:
        lines.append(f"Account context: {case.account_context}")
    return "\n".join(lines)


def render_findings(findings: Mapping[str, Finding], *, with_evidence: bool = False) -> str:
    if not findings:
        return "No findings yet."
    blocks = []
    for name, finding in findings.items():
        block = (
            f"- {name}: risk={finding.risk_signal.value} confidence={finding.confidence:.2f}\n"
            f"  rationale: {finding.rationale}"
        )
        if with_evidence:
            cited = "\n".join(f"    * [{e.kind}] {e.summary}" for e in finding.evidence)
            block += "\n  evidence:\n" + (cited or "    (none)")
        blocks.append(block)
    return "\n".join(blocks)


def render_supervisor_input(
    case: Case,
    findings: Mapping[str, Finding],
    available: Iterable[SpecialistName],
    critic_request: CriticRequest | None,
) -> str:
    text = (
        f"{render_case(case)}\n\nFindings so far:\n{render_findings(findings)}\n\n"
        f"Available specialists: {', '.join(available)}"
    )
    if critic_request is not None:
        text += (
            f"\n\nThe critic asks to re-check with {', '.join(critic_request.specialists)}: "
            f"{critic_request.instruction}"
        )
    return text


def render_specialist_input(
    case: Case,
    findings: Mapping[str, Finding],
    instruction: str | None,
    prechecks: Sequence[tuple[str, str]] = (),
) -> str:
    text = f"{render_case(case)}\n\nFindings from other specialists:\n{render_findings(findings)}"
    if prechecks:
        results = "\n".join(f"- {name}: {content}" for name, content in prechecks)
        text += (
            "\n\nDeterministic checks already run on the destination (no need to repeat "
            f"them; include them in your assessment):\n{results}"
        )
    if instruction:
        text += f"\n\nReviewer instruction for this run: {instruction}"
    return text


def render_scorer_input(
    case: Case, findings: Mapping[str, Finding], rule_outcome: str | None
) -> str:
    rule = rule_outcome or "No hard rule applies; decide level, score and action yourself."
    return (
        f"{render_case(case)}\n\nFindings:\n{render_findings(findings, with_evidence=True)}"
        f"\n\nHard rule outcome: {rule}"
    )


def render_critic_input(draft: Verdict) -> str:
    findings = {f.agent: f for f in draft.findings}
    return (
        f"Draft verdict: risk={draft.risk_level.value} score={draft.score} "
        f"action={draft.action.value}\nSummary: {draft.summary}\n\n"
        f"Findings:\n{render_findings(findings, with_evidence=True)}"
    )
