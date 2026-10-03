"""Settings, read from environment variables (or a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

MIN_PASSWORD_LENGTH = 16


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    model_base_url: str
    model_name: str
    model_api_key: str
    password_hash: str
    cookie_secure: bool
    session_days: int
    daily_call_limit: int
    max_tool_steps: int
    agent_name: str
    timezone: str
    data_dir: Path
    allowed_hosts: tuple[str, ...] = ()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent.db"

    @property
    def workspace(self) -> Path:
        return self.data_dir / "workspace"

    def check(self) -> None:
        """Refuses to run with settings that would leave the page open."""
        if not self.password_hash.startswith("$argon2"):
            raise SystemExit(
                "AGENT_PASSWORD_HASH is missing. Run `python -m agent hash-password` "
                "(or `docker compose run --rm agent python -m agent hash-password`) and put the "
                "line it prints into .env."
            )

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        return cls(
            model_base_url=os.getenv("MODEL_BASE_URL", "http://localhost:11434/v1"),
            model_name=os.getenv("MODEL_NAME", "qwen2.5:7b"),
            model_api_key=os.getenv("MODEL_API_KEY", "ollama"),
            password_hash=os.getenv("AGENT_PASSWORD_HASH", "").strip().strip("'\""),
            cookie_secure=_bool(os.getenv("COOKIE_SECURE"), True),
            session_days=int(os.getenv("SESSION_DAYS", "14")),
            daily_call_limit=int(os.getenv("DAILY_CALL_LIMIT", "300")),
            max_tool_steps=int(os.getenv("MAX_TOOL_STEPS", "8")),
            agent_name=os.getenv("AGENT_NAME", "Agent"),
            timezone=os.getenv("TIMEZONE", "Europe/Helsinki"),
            data_dir=Path(os.getenv("DATA_DIR", "./data")).resolve(),
            allowed_hosts=tuple(h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",")
                                if h.strip()),
        )
