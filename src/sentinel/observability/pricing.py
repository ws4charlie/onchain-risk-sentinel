"""Model price table: USD per 1M tokens, keyed by "<provider>:<model>" strings.

Prices change; every entry records where it came from and when it was checked.
Override the defaults with a JSON file (same shape as `PriceTable`) via
`PriceTable.from_file`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

_OPENAI_SOURCE = "https://developers.openai.com/api/docs/models"
_GEMINI_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
_CHECKED = date(2026, 10, 25)


class ModelPrice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_per_mtok: float = Field(ge=0, description="USD per 1M uncached input tokens.")
    output_per_mtok: float = Field(ge=0, description="USD per 1M output tokens (incl. thinking).")
    cached_input_per_mtok: float | None = Field(
        default=None, ge=0, description="USD per 1M cached input tokens; None = input price."
    )
    source: str
    checked_on: date
    note: str = ""

    def cost(self, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
        cached = min(cached_input_tokens, input_tokens)
        cached_price = (
            self.cached_input_per_mtok
            if self.cached_input_per_mtok is not None
            else self.input_per_mtok
        )
        return (
            (input_tokens - cached) * self.input_per_mtok
            + cached * cached_price
            + output_tokens * self.output_per_mtok
        ) / 1_000_000


class PriceTable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prices: dict[str, ModelPrice]

    @classmethod
    def from_file(cls, path: Path) -> Self:
        return cls.model_validate_json(path.read_text())

    def lookup(self, model: str) -> ModelPrice | None:
        """Find a price by "<provider>:<model>", falling back to the bare model name."""
        if model in self.prices:
            return self.prices[model]
        bare = model.split(":", 1)[1] if ":" in model else model
        for key, price in self.prices.items():
            if key.split(":", 1)[-1] == bare:
                return price
        if model.startswith("ollama:"):
            return self.prices.get("ollama:*")
        return None


def _local(note: str) -> ModelPrice:
    return ModelPrice(
        input_per_mtok=0, output_per_mtok=0, source="local", checked_on=_CHECKED, note=note
    )


DEFAULT_PRICES = PriceTable(
    prices={
        "openai:gpt-5.4-mini": ModelPrice(
            input_per_mtok=0.75,
            cached_input_per_mtok=0.075,
            output_per_mtok=4.50,
            source=f"{_OPENAI_SOURCE}/gpt-5.4-mini",
            checked_on=_CHECKED,
        ),
        "openai:gpt-5.4-nano": ModelPrice(
            input_per_mtok=0.20,
            cached_input_per_mtok=0.02,
            output_per_mtok=1.25,
            source=f"{_OPENAI_SOURCE}/gpt-5.4-nano",
            checked_on=_CHECKED,
        ),
        "google_genai:gemini-flash-latest": ModelPrice(
            input_per_mtok=0.75,
            output_per_mtok=3.75,
            source=_GEMINI_SOURCE,
            checked_on=_CHECKED,
            note="Alias; priced as gemini-3.8-flash, the newest stable Flash on the models page. "
            "Paid tier; the price doubles from 2027-01-01. A free tier exists.",
        ),
        "google_genai:gemini-3.8-flash": ModelPrice(
            input_per_mtok=0.75,
            output_per_mtok=3.75,
            source=_GEMINI_SOURCE,
            checked_on=_CHECKED,
            note="Paid tier through 2026-12-31; a free tier exists.",
        ),
        "google_genai:gemini-3.5-flash-lite": ModelPrice(
            input_per_mtok=0.30,
            output_per_mtok=2.50,
            source=_GEMINI_SOURCE,
            checked_on=_CHECKED,
            note="Paid tier; a free tier exists.",
        ),
        "google_genai:gemini-3.1-flash-lite": ModelPrice(
            input_per_mtok=0.25,
            output_per_mtok=1.50,
            source=_GEMINI_SOURCE,
            checked_on=_CHECKED,
            note="Paid tier; a free tier exists.",
        ),
        "openrouter:google/gemini-3.8-flash": ModelPrice(
            input_per_mtok=0.825,
            output_per_mtok=4.125,
            source="https://openrouter.ai/google/gemini-3.8-flash",
            checked_on="2026-10-26",
            note="OpenRouter list price.",
        ),
        "openrouter:google/gemini-3.5-flash-lite": ModelPrice(
            input_per_mtok=0.30,
            output_per_mtok=2.50,
            source="https://openrouter.ai/google/gemini-3.5-flash-lite",
            checked_on="2026-10-26",
            note="OpenRouter list price.",
        ),
        "ollama:*": _local("Local open-source model: no per-token price (hardware cost only)."),
    }
)
