from __future__ import annotations

from urllib.parse import urlparse

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from cortex.config.models import LLMConfig, LLMProfileName
from cortex.infrastructure.llm.deepseek import DeepSeekChatOpenAI


class LLMError(RuntimeError):
    """Base error for the shared knowledge-capability LLM layer."""


class LLMConfigurationError(LLMError, ValueError):
    """Raised when required LLM connection settings are missing."""


class LLMInvocationError(LLMError):
    """Raised when a model invocation fails."""


class LLMStructuredOutputError(LLMError, ValueError):
    """Raised when structured output remains invalid after one repair attempt."""


def create_chat_model(
    config: LLMConfig,
    *,
    profile: LLMProfileName | None = None,
) -> BaseChatModel:
    """Create the shared OpenAI-compatible chat model for one capability profile."""
    resolved = config.resolve(profile)
    model = resolved.model.strip() if resolved.model else None
    base_url = resolved.base_url.strip() if resolved.base_url else None
    api_key = resolved.api_key.get_secret_value().strip() if resolved.api_key else None
    missing = [
        name
        for name, value in {
            "llm.model": model,
            "llm.base_url": base_url,
            "llm.api_key": api_key,
        }.items()
        if not value
    ]
    if missing:
        raise LLMConfigurationError(f"Missing required LLM configuration: {', '.join(missing)}")

    # A proxy changes the host, not the model protocol. DeepSeek defaults to
    # thinking and some gateways honor reasoning_effort but ignore thinking.
    deepseek = urlparse(base_url).hostname == "api.deepseek.com" or model.lower().startswith("deepseek-")
    chat_class = DeepSeekChatOpenAI if deepseek and resolved.enable_thinking else ChatOpenAI
    reasoning_effort = "none" if deepseek and resolved.enable_thinking is False else resolved.reasoning_effort
    return chat_class(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=resolved.temperature,
        timeout=resolved.timeout,
        max_retries=resolved.max_retries,
        reasoning_effort=reasoning_effort,
        extra_body=(
            ({"thinking": {"type": "enabled" if resolved.enable_thinking else "disabled"}}
             if deepseek
             else {"enable_thinking": resolved.enable_thinking})
            if resolved.enable_thinking is not None
            else None
        ),
    )


def create_embedding_model(config: LLMConfig, *, model: str | None) -> Embeddings | None:
    """Create an optional OpenAI-compatible embedding model using shared connection settings."""

    embedding_model = model.strip() if model else None
    if not embedding_model:
        return None
    base_url = config.base_url.strip() if config.base_url else None
    api_key = config.api_key.get_secret_value().strip() if config.api_key else None
    missing = [
        name
        for name, value in {
            "llm.base_url": base_url,
            "llm.api_key": api_key,
        }.items()
        if not value
    ]
    if missing:
        raise LLMConfigurationError(
            f"Missing required embedding configuration: {', '.join(missing)}"
        )
    return OpenAIEmbeddings(
        model=embedding_model,
        api_key=api_key,
        base_url=base_url,
        timeout=config.timeout,
        max_retries=config.max_retries,
        chunk_size=32,
        # OpenAI-compatible providers do not necessarily use OpenAI's tokenizer.
        # Send text, not locally generated token IDs (e.g. for Qwen embeddings).
        check_embedding_ctx_length=False,
    )


__all__ = [
    "LLMConfigurationError",
    "LLMError",
    "LLMInvocationError",
    "LLMStructuredOutputError",
    "create_chat_model",
    "create_embedding_model",
]
