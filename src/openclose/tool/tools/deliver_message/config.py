"""Configuration loader for the deliver_message tool.

Reads bot credentials and channel aliases from ``.env`` in the openclose
config directory (``ConfigPaths.config_dir()``, platform-specific) and
real environment variables. Real env vars take precedence — a file
value is only used when the real env is unset (``override=False``).

Env var format
--------------
::

    OPENCLOSE_TELEGRAM_BOT_TOKEN=<token>
    OPENCLOSE_DISCORD_BOT_TOKEN=<token>
    OPENCLOSE_CHANNEL_<ALIAS>=<platform>:<destination_id>

where ``<platform>`` is ``telegram`` or ``discord``. Aliases are stored
lowercased.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv

from openclose.config.paths import ConfigPaths
from openclose.log import get_logger

log = get_logger(__name__)


Platform = Literal["telegram", "discord"]
_VALID_PLATFORMS: frozenset[str] = frozenset({"telegram", "discord"})

_CHANNEL_PREFIX = "OPENCLOSE_CHANNEL_"
_TELEGRAM_TOKEN_VAR = "OPENCLOSE_TELEGRAM_BOT_TOKEN"
_DISCORD_TOKEN_VAR = "OPENCLOSE_DISCORD_BOT_TOKEN"
_TELEGRAM_ALLOWED_USERS_VAR = "OPENCLOSE_TELEGRAM_ALLOWED_USERS"


@dataclass(frozen=True)
class ChannelSpec:
    """A resolved channel destination."""

    alias: str
    platform: Platform
    target_id: str


@dataclass(frozen=True)
class MessagingConfig:
    """Parsed deliver_message configuration."""

    telegram_token: str | None
    discord_token: str | None
    channels: dict[str, ChannelSpec] = field(default_factory=dict)
    telegram_allowed_users: frozenset[str] | None = None
    """Outbound allowlist for Telegram ``chat_id`` values.  ``None``
    means no restriction; when set, sends are refused to any target not
    in this set."""

    def token_for(self, platform: str) -> str | None:
        if platform == "telegram":
            return self.telegram_token
        if platform == "discord":
            return self.discord_token
        return None

    def is_target_allowed(self, spec: ChannelSpec) -> bool:
        """Return ``False`` iff the target is gated by an allowlist."""
        if spec.platform != "telegram":
            return True
        if self.telegram_allowed_users is None:
            return True
        return spec.target_id in self.telegram_allowed_users


@lru_cache(maxsize=1)
def _load_env_file_once() -> None:
    """Load ``ConfigPaths.config_dir() / ".env"`` into ``os.environ``.

    Real env vars are preserved (``override=False``). Safe to call many
    times; the ``lru_cache`` ensures we only read the file once per
    process.
    """
    env_path = ConfigPaths.config_dir() / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)
        log.debug("loaded deliver_message env from %s", env_path)


def load_messaging_config() -> MessagingConfig:
    """Load bot tokens and channel aliases from env (file + process)."""
    _load_env_file_once()

    telegram_token = os.environ.get(_TELEGRAM_TOKEN_VAR) or None
    discord_token = os.environ.get(_DISCORD_TOKEN_VAR) or None

    channels: dict[str, ChannelSpec] = {}
    for key, value in os.environ.items():
        if not key.startswith(_CHANNEL_PREFIX):
            continue
        alias = key[len(_CHANNEL_PREFIX):].lower()
        if not alias:
            continue
        spec = _parse_channel_value(alias, value)
        if spec is not None:
            channels[alias] = spec

    return MessagingConfig(
        telegram_token=telegram_token,
        discord_token=discord_token,
        channels=channels,
        telegram_allowed_users=_parse_allowed_users(
            os.environ.get(_TELEGRAM_ALLOWED_USERS_VAR)
        ),
    )


def _parse_allowed_users(raw: str | None) -> frozenset[str] | None:
    """Parse a comma-separated allowlist; return ``None`` if unset/empty."""
    if raw is None:
        return None
    parts = {p.strip() for p in raw.split(",") if p.strip()}
    return frozenset(parts) if parts else None


def _parse_channel_value(alias: str, raw: str) -> ChannelSpec | None:
    """Parse ``<platform>:<target_id>``. Log and skip on malformed input."""
    if ":" not in raw:
        log.warning(
            "channel %r has malformed value (missing ':'): skipping", alias
        )
        return None
    platform, _, target_id = raw.partition(":")
    platform = platform.strip().lower()
    target_id = target_id.strip()
    if platform not in _VALID_PLATFORMS:
        log.warning(
            "channel %r has unknown platform %r: skipping", alias, platform
        )
        return None
    if not target_id:
        log.warning("channel %r has empty target_id: skipping", alias)
        return None
    # _VALID_PLATFORMS membership guarantees the Literal type.
    return ChannelSpec(
        alias=alias,
        platform=platform,  # type: ignore[arg-type]
        target_id=target_id,
    )


def resolve_channels(
    cfg: MessagingConfig, aliases: list[str]
) -> tuple[list[ChannelSpec], list[str]]:
    """Map alias names to ``ChannelSpec`` objects.

    Returns ``(resolved, unknown)``. Aliases are lowercased for lookup.
    Duplicates are preserved in order of first appearance.
    """
    resolved: list[ChannelSpec] = []
    unknown: list[str] = []
    seen: set[str] = set()

    for raw in aliases:
        key = raw.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        spec = cfg.channels.get(key)
        if spec is None:
            unknown.append(raw)
        else:
            resolved.append(spec)

    return resolved, unknown


def reset_env_cache() -> None:
    """Clear the ``.env``-file cache. Used by tests to force a re-read."""
    _load_env_file_once.cache_clear()
