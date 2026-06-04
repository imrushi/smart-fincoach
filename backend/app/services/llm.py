from __future__ import annotations

from typing import Optional

import openai

from app.core.config import get_settings


def get_llm_client() -> Optional[openai.AsyncOpenAI]:
    """Return a configured async LLM client for OpenAI or OpenRouter."""
    settings = get_settings()
    if settings.OPENROUTER_API_KEY:
        return openai.AsyncOpenAI(
            api_key=settings.OPENROUTER_API_KEY,
            base_url=settings.OPENROUTER_BASE_URL,
        )

    if settings.OPENAI_API_KEY:
        kwargs = {"api_key": settings.OPENAI_API_KEY}
        if settings.OPENAI_API_BASE_URL:
            kwargs["base_url"] = settings.OPENAI_API_BASE_URL
        return openai.AsyncOpenAI(**kwargs)

    return None


def get_llm_model() -> str:
    """Return the configured LLM model name for the selected provider."""
    settings = get_settings()
    if settings.OPENROUTER_API_KEY:
        return settings.OPENROUTER_MODEL or settings.AI_MODEL
    return settings.AI_MODEL
