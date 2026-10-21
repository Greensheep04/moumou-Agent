"""Runtime configuration.

Everything secret comes from the environment (or a local ``.env`` which is
gitignored). Nothing here ever gets committed with a real key in it.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_BASE_URL = "https://api.deepseek.com"
#: DeepSeek serves V4.1-Flash-1017 under this stable id. The dated aliases also
#: work, but they get retired -- always prefer the stable one.
DEFAULT_MODEL = "deepseek-flash"


def load_dotenv(path: Path | str = ".env") -> None:
    """Minimal ``.env`` reader. Real environment variables always win."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class LLMSettings(BaseModel):
    api_key: str = ""
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout_s: float = 180.0

    @classmethod
    def from_env(cls, dotenv: Path | str = ".env") -> LLMSettings:
        load_dotenv(dotenv)
        return cls(
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            model=os.environ.get("DEEPSEEK_MODEL") or DEFAULT_MODEL,
            base_url=os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL,
            timeout_s=float(os.environ.get("DEEPSEEK_TIMEOUT_S") or 180.0),
        )

    def require_key(self) -> str:
        if not self.api_key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is not set.\n"
                "  cp .env.example .env   then put your own key in .env\n"
                "  (.env is gitignored -- never commit a key)"
            )
        return self.api_key


class Settings(BaseModel):
    """Project-level settings that are safe to commit."""

    project_id: str = "default"
    conversation_id: str = "default"
    runs_dir: Path = Field(default=Path("runs"))
    max_slots: int = 4
