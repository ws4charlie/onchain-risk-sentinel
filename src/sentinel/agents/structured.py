"""Typed wrapper around `with_structured_output`, with one corrective retry."""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import BaseModel

# One retry fixes most slips (a wrong enum value, a missing field) without letting a
# model that cannot follow the schema loop and burn tokens.
STRUCTURED_RETRIES = 1


class StructuredOutputError(RuntimeError):
    """The model did not produce a valid answer for the schema, even after a retry."""

    def __init__(self, schema: type[BaseModel], attempts: int, error: Exception) -> None:
        super().__init__(
            f"{schema.__name__}: no valid structured answer after {attempts} attempts: {error}"
        )
        self.schema = schema
        self.attempts = attempts


async def ask_structured[T: BaseModel](
    model: BaseChatModel, schema: type[T], messages: Sequence[BaseMessage]
) -> T:
    """Call `model` and return its answer parsed and validated as `schema`.

    On a parsing or validation error the call is repeated once, with the error
    appended to the conversation so the model can correct itself.
    """
    runnable = model.with_structured_output(schema)
    conversation = list(messages)
    error: Exception | None = None
    for _ in range(1 + STRUCTURED_RETRIES):
        try:
            result = await runnable.ainvoke(conversation)
            if result is None:
                raise OutputParserException(f"no {schema.__name__} answer was returned")
            return result if isinstance(result, schema) else schema.model_validate(result)
        except (OutputParserException, ValueError) as exc:  # pydantic ValidationError too
            error = exc
            conversation = [
                *conversation,
                HumanMessage(
                    f"Your previous answer was not a valid {schema.__name__}: {exc}\n"
                    "Answer again, following the schema exactly."
                ),
            ]
    assert error is not None
    raise StructuredOutputError(schema, 1 + STRUCTURED_RETRIES, error)
