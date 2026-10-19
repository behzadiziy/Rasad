"""Create the configured `LLMClient` from `config.yaml` and the environment settings."""

from __future__ import annotations

from loguru import logger

from src.llm.base import LLMClient
from src.llm.gemini import GeminiClient
from src.models import AppConfig, Settings


def create_llm_client(config: AppConfig, settings: Settings) -> LLMClient:
    """Return the client for `config.llm.provider`; open it with `async with`.

    Raises:
        ValueError: If the `llm` section is missing from `config.yaml`, the
            provider's API key is not set in `config/.env`, or the provider is
            not implemented.
    """
    llm = config.llm
    if llm is None:
        raise ValueError(
            "LLM features are disabled: config/config.yaml has no 'llm' section. "
            "Add one (provider, model, ...) to enable resume tailoring"
        )
    if llm.provider == "gemini":
        if not settings.GEMINI_API_KEY:
            raise ValueError(
                "GEMINI_API_KEY is not set in config/.env; it is required for llm.provider 'gemini'"
            )
        logger.debug("Creating Gemini client for model {}", llm.model)
        return GeminiClient(llm, api_key=settings.GEMINI_API_KEY, proxy_url=settings.LLM_PROXY_URL)
    raise ValueError(f"LLM provider {llm.provider!r} is not implemented")
