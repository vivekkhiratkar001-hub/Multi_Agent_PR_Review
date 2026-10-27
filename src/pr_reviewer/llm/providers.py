from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class LLMResponse:
    """Normalized response returned by an LLM provider."""

    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMProvider(ABC):
    """Contract implemented by each supported LLM provider."""

    @abstractmethod
    def generate(self, prompt: str) -> LLMResponse:
        """Generate a response from a text prompt."""
        raise NotImplementedError
