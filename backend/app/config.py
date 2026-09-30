"""Settings, all overridable by an environment variable of the same name.

Every limit in the service is here rather than scattered through the code, so
"how big a file will it take" is answered by reading one file, and tuning for a
different deployment is an environment change rather than a patch.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration for one API process."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # -- limits -----------------------------------------------------------

    MAX_UPLOAD_MB: int = Field(
        default=100, gt=0, description="Largest upload accepted, in megabytes."
    )
    MAX_LINE_KB: int = Field(
        default=64,
        gt=0,
        description="Longest single line accepted; longer lines are counted as "
        "unparseable rather than failing the whole request.",
    )
    UPLOAD_IDLE_TIMEOUT_S: float = Field(
        default=30.0,
        gt=0,
        description="Give up on an upload that sends no bytes for this long.",
    )
    MAX_CONCURRENT_ANALYSES: int = Field(
        default=8, gt=0, description="Analyses running at once, per worker process."
    )
    SLOT_WAIT_S: float = Field(
        default=5.0,
        ge=0,
        description="How long a request waits for a free slot before it is told "
        "to come back later.",
    )
    RATE_LIMIT_PER_MIN: int = Field(
        default=30, ge=0, description="Requests per minute per client IP; 0 disables."
    )
    RESULT_TTL_S: int = Field(
        default=3600, gt=0, description="How long a stored result stays fetchable."
    )

    # -- sampling ---------------------------------------------------------

    DEFAULT_SAMPLES: int = Field(
        default=20, ge=0, description="Unparseable examples returned when unasked."
    )
    MAX_SAMPLES: int = Field(
        default=100, ge=0, description="Ceiling on the `samples` query parameter."
    )

    # -- dependencies -----------------------------------------------------

    REDIS_URL: str | None = Field(
        default=None,
        description="Result store. Unset means an in-process store, which is "
        "fine for one worker in development and wrong for several.",
    )
    # `NoDecode` stops pydantic-settings from insisting these arrive as JSON.
    # Without it the environment source fails before the validator below ever
    # runs, and `CORS_ORIGINS=http://localhost:3000` -- the obvious thing to
    # write in a compose file -- is a startup crash.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"],
        description="Browser origins allowed to call the API.",
    )
    API_KEYS: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="If set, requests must carry one of these in X-API-Key.",
    )

    # -- operational ------------------------------------------------------

    LOG_LEVEL: str = Field(default="INFO")
    ENV: str = Field(default="development")

    @field_validator("CORS_ORIGINS", "API_KEYS", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        """Accept ``a,b`` as well as a JSON list.

        Compose files and shell exports write comma-separated strings, which is
        the form a person reaches for; the JSON form still works for anything
        generating the environment programmatically.
        """
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return json.loads(stripped)
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return value

    # -- derived ----------------------------------------------------------

    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_MB * 1024 * 1024

    @property
    def max_line_bytes(self) -> int:
        return self.MAX_LINE_KB * 1024

    @property
    def auth_required(self) -> bool:
        return bool(self.API_KEYS)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings, read from the environment once.

    Cached so that a request handler can call it without re-reading the
    environment; tests clear the cache instead of monkeypatching attributes.
    """
    return Settings()


__all__ = ["Settings", "get_settings"]
