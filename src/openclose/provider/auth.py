"""API key loading from environment and config."""

from __future__ import annotations

import os

from openclose.config.config import get_config
from openclose.log import get_logger

log = get_logger(__name__)


def load_api_key(provider_name: str = "default") -> str:
    """Load API key for a provider.

    Priority:
    1. Env var named by the provider's ``api_key_env`` field (preferred for
       multi-provider configs — e.g. ``api_key_env = "OPENROUTER_API_KEY"``)
    2. Inline ``api_key`` in the provider's config
    3. ``OPENCLOSE_API_KEY`` env (legacy, single-provider setups)
    4. ``OPENAI_API_KEY`` env (legacy fallback)
    """
    config = get_config()
    provider_cfg = next(
        (p for p in config.providers if p.name == provider_name), None,
    )

    if provider_cfg is not None:
        if provider_cfg.api_key_env:
            key = os.environ.get(provider_cfg.api_key_env, "")
            if key:
                return key
        if provider_cfg.api_key:
            return provider_cfg.api_key

    env_key = os.environ.get("OPENCLOSE_API_KEY", "")
    if env_key:
        return env_key

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if openai_key:
        return openai_key

    log.warning("No API key found for provider %r", provider_name)
    return ""
