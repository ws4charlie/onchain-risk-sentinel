"""Typed wrapper around `with_structured_output`."""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from pydantic import BaseModel


async def ask_structured[T: BaseModel](
    model: BaseChatModel, schema: type[T], messages: Sequence[BaseMessage]
) -> T:
    """Call `model` and return its answer parsed and validated as `schema`."""
    result = await model.with_structured_output(schema).ainvoke(list(messages))
    return result if isinstance(result, schema) else schema.model_validate(result)
