"""Settings from environment variables (or a local .env file)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Domain-fixed admission deadlines. They are settings only so tests can shrink
# them to seconds; production must never override them.
SELECTION_WINDOW_SECONDS = 5 * 60
ADMISSION_WINDOW_SECONDS = 11 * 60


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    mysql_url: str
    redis_url: str
    rabbitmq_url: str

    jwt_private_key_path: Path
    jwt_kid: str = Field(min_length=1)
    jwt_public_keys_dir: Path | None = None

    admitter_tick_seconds: float = Field(default=1.0, gt=0)
    # True (local, tests): the consumer declares the whole topology, exchanges included.
    # False (shared broker): it declares nothing and only checks its own queues exist.
    rabbitmq_declare_topology: bool = True
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"  # text is easier to read locally

    selection_window_seconds: int = Field(default=SELECTION_WINDOW_SECONDS, gt=0)
    admission_window_seconds: int = Field(default=ADMISSION_WINDOW_SECONDS, gt=0)

    @field_validator("jwt_public_keys_dir", mode="before")
    @classmethod
    def _empty_dir_is_none(cls, value: Any) -> Any:
        return None if value == "" else value

    @model_validator(mode="after")
    def _selection_before_admission_end(self) -> Settings:
        if self.selection_window_seconds >= self.admission_window_seconds:
            raise ValueError("selection_window_seconds must be < admission_window_seconds")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
