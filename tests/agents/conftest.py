from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from sentinel.agents import Models, build_graph
from sentinel.agents.critic import Critique
from sentinel.agents.graph import TriageGraph, checkpoint_serde
from sentinel.agents.risk_scorer import ScoreAssessment
from sentinel.agents.specialists import SpecialistAssessment
from sentinel.agents.supervisor import RoutingDecision
from sentinel.core.config import Settings
from tests.agents.fake_llm import ScriptedChatModel, queued, specialist_policy
from tests.fakes import fake_ports

# A typical supervisor plan: three independent checks in parallel, then patterns, then score.
DEFAULT_ROUTES: list[dict[str, Any]] = [
    {"specialists": ["chain_tracer", "sanctions", "scam_intel"], "reason": "independent checks"},
    {"specialists": ["pattern_analyst"], "reason": "match patterns on the chain facts"},
]
SCORE: dict[str, Any] = {"specialists": [], "reason": "enough evidence"}
LOW_RISK: dict[str, Any] = {
    "risk_level": "low",
    "score": 10,
    "action": "allow",
    "summary": "No adverse evidence was found.",
}
APPROVE: dict[str, Any] = {"approved": True}

GraphFactory = Callable[..., TriageGraph]


def make_models(
    routes: Iterable[dict[str, Any]] = DEFAULT_ROUTES,
    critiques: Iterable[dict[str, Any]] = (),
) -> Models:
    return Models(
        supervisor=ScriptedChatModel(policy=queued(RoutingDecision, routes, SCORE)),
        specialist=ScriptedChatModel(policy=specialist_policy(SpecialistAssessment)),
        scorer=ScriptedChatModel(policy=queued(ScoreAssessment, [], LOW_RISK)),
        critic=ScriptedChatModel(policy=queued(Critique, critiques, APPROVE)),
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def saver() -> InMemorySaver:
    return InMemorySaver(serde=checkpoint_serde())


@pytest.fixture
def graph_factory(settings: Settings, saver: InMemorySaver) -> GraphFactory:
    def factory(models: Models | None = None, **overrides: Any) -> TriageGraph:
        effective = settings.model_copy(update=overrides)
        return build_graph(fake_ports(), models or make_models(), effective, saver)

    return factory
