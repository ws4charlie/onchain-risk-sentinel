import pytest
from pydantic import ValidationError

from sentinel.core.config import Settings
from sentinel.core.ports import (
    ChainDataSource,
    PatternKnowledgeBase,
    SanctionsSource,
    ScamIntelSource,
)
from sentinel.core.schemas import Case, normalize_address
from tests.fakes import CLEAN, SANCTIONED, fake_ports


def test_normalize_address_lowercases() -> None:
    assert normalize_address("0x" + "AB" * 20) == "0x" + "ab" * 20


@pytest.mark.parametrize("bad", ["", "0x123", "1" * 42, "0x" + "zz" * 20])
def test_normalize_address_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ValueError):
        normalize_address(bad)


def test_case_normalizes_address() -> None:
    case = Case(case_id="c1", address=CLEAN.upper().replace("0X", "0x"))
    assert case.address == CLEAN


def test_case_rejects_bad_address() -> None:
    with pytest.raises(ValidationError):
        Case(case_id="c1", address="not-an-address")


def test_fakes_satisfy_ports() -> None:
    ports = fake_ports()
    assert isinstance(ports.chain_data, ChainDataSource)
    assert isinstance(ports.sanctions, SanctionsSource)
    assert isinstance(ports.scam_intel, ScamIntelSource)
    assert isinstance(ports.patterns, PatternKnowledgeBase)


async def test_fake_sanctions_hit() -> None:
    ports = fake_ports()
    assert await ports.sanctions.check(SANCTIONED) is not None
    assert await ports.sanctions.check(CLEAN) is None


def test_settings_model_for_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_CRITIC_MODEL", "ollama:qwen3:8b")
    assert Settings(_env_file=None).model_for("critic") == "ollama:qwen3:8b"  # type: ignore[call-arg]


def test_openrouter_models_use_openrouter_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from langchain_openai import ChatOpenAI

    from sentinel.core.llm import OPENROUTER_BASE_URL, chat_model

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    model = chat_model("openrouter:google/gemini-3.5-flash-lite", temperature=0)
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "google/gemini-3.5-flash-lite"
    assert model.openai_api_base == OPENROUTER_BASE_URL


def test_openrouter_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel.core.llm import chat_model

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        chat_model("openrouter:google/gemini-3.5-flash-lite")
