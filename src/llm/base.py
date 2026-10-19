"""The provider-agnostic LLM interface: structured generation into a Pydantic model.

Every provider implements `LLMClient.generate_structured`, which sends a system
prompt and one user message, asks for JSON matching a Pydantic model, and
returns the validated model with its token usage. Callers never see
provider-specific request or response formats.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, Self, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """An LLM call failed: network or API error, a blocked or truncated answer, or invalid JSON.

    Messages never contain prompts or responses, which hold resume content.
    """


class LLMUsage(BaseModel):
    """Token counts of one or more LLM calls.

    `output_tokens` includes the model's thinking tokens, which are billed as output.
    """

    prompt_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    def plus(self, other: LLMUsage) -> LLMUsage:
        """Return the sum of this usage and `other` (e.g. a first attempt and its retry)."""
        return LLMUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class LLMResult(BaseModel, Generic[T]):
    """A validated structured answer, the model version that produced it, its usage, and its latency."""

    value: T
    model: str
    usage: LLMUsage
    latency_seconds: float = Field(ge=0)


class LLMClient(ABC):
    """An LLM provider client, used as an async context manager.

    Example:
        async with create_llm_client(config, settings) as llm:
            result = await llm.generate_structured(system=..., user=..., schema=TailoringPlan)
    """

    async def __aenter__(self) -> Self:
        """Open the client's resources (e.g. its HTTP client)."""
        await self._open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Release the client's resources."""
        await self._close()

    async def _open(self) -> None:
        """Open provider resources; subclasses override this when they hold any."""

    async def _close(self) -> None:
        """Close provider resources; subclasses override this when they hold any."""

    @abstractmethod
    async def generate_structured(self, *, system: str, user: str, schema: type[T]) -> LLMResult[T]:
        """Generate an answer that matches `schema` and return it validated.

        Args:
            system: The system instruction.
            user: The single user message.
            schema: The Pydantic model the answer must match.

        Raises:
            LLMError: If the call fails, the answer is blocked or truncated, or
                it does not validate against `schema`.
        """
