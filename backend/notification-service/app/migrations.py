"""Alembic, driven from code: the tests use this, not alembic.ini."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from .config import Settings

ALEMBIC_DIR = Path(__file__).resolve().parents[1] / "alembic"


def alembic_config(settings: Settings) -> Config:
    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.attributes["settings"] = settings
    config.attributes["skip_logging"] = True
    return config


def head_revision(config: Config) -> str | None:
    return ScriptDirectory.from_config(config).get_current_head()
