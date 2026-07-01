"""Central configuration loading (CLAUDE.md §14: centralize config, fail closed)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """Raised when required configuration is missing (fail closed)."""


def _mask(value: str | None) -> str:
    return "None" if value is None else "'***REDACTED***'"


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Secrets are never shown in ``repr``."""

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    openclaw_gateway_token: str | None = None
    ssh_alias: str = "server232"
    dry_run: bool = True

    def __repr__(self) -> str:
        return (
            "Settings("
            f"ssh_alias={self.ssh_alias!r}, dry_run={self.dry_run}, "
            f"telegram_bot_token={_mask(self.telegram_bot_token)}, "
            f"telegram_chat_id={_mask(self.telegram_chat_id)}, "
            f"openclaw_gateway_token={_mask(self.openclaw_gateway_token)})"
        )

    def require_telegram(self) -> tuple[str, str]:
        """Return (bot_token, chat_id) or fail closed if either is missing."""
        if not self.telegram_bot_token or not self.telegram_chat_id:
            raise ConfigError(
                "Telegram is not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
            )
        return self.telegram_bot_token, self.telegram_chat_id


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from environment variables.

    Reads from ``env`` (defaults to ``os.environ``). Phase 1 monitoring can run
    without channels configured, so secrets are optional here; callers that
    need a channel must check and fail closed (see :meth:`Settings.require_telegram`).
    """
    src: Mapping[str, str] = os.environ if env is None else env
    dry_run = src.get("OS_AGENT_DRY_RUN", "true").strip().lower() not in {"false", "0", "no"}
    return Settings(
        telegram_bot_token=src.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=src.get("TELEGRAM_CHAT_ID") or None,
        openclaw_gateway_token=src.get("OPENCLAW_GATEWAY_TOKEN") or None,
        ssh_alias=src.get("SERVER232_SSH_ALIAS", "server232"),
        dry_run=dry_run,
    )
