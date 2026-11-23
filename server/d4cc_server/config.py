"""Runtime configuration, read from environment variables (D4CC_*)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _minutes(name: str, default: float) -> float:
    return float(_env(name, str(default))) * 60


@dataclass
class Config:
    db_path: str = "./data/d4cc.db"
    admin_token: str | None = None
    auth_disabled: bool = False
    # How long ended sessions stay visible on the board (seconds).
    ended_visible: float = 24 * 3600
    # Health thresholds (seconds) - evaluated from phase 2 on.
    stuck_after: float = 30 * 60
    offline_after: float = 15 * 60

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            db_path=_env("D4CC_DB", "./data/d4cc.db"),
            admin_token=_env("D4CC_ADMIN_TOKEN"),
            auth_disabled=_env("D4CC_AUTH_DISABLED", "0") == "1",
            ended_visible=float(_env("D4CC_ENDED_VISIBLE_HOURS", "24")) * 3600,
            stuck_after=_minutes("D4CC_STUCK_AFTER_MIN", 30),
            offline_after=_minutes("D4CC_OFFLINE_AFTER_MIN", 15),
        )

    def validate(self) -> None:
        if not self.auth_disabled and not self.admin_token:
            raise SystemExit(
                "D4CC_ADMIN_TOKEN is not set. Set it, or set D4CC_AUTH_DISABLED=1 "
                "for local development only."
            )
