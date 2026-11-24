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


class LLMProfile(BaseModel):
    """One model endpoint plus the environment names its key may come from.

    Two profiles exist because the course gateway and the official platform are
    not interchangeable: the gateway enforces a ~240s proxy limit and serves a
    course-specific model id, while the official endpoint is the reference used
    to tell "the architecture is weak" apart from "this endpoint is slow".
    """

    name: str
    label: str
    base_url: str
    model: str
    api_key_env: tuple[str, ...]
    timeout_s: float = 180.0
    #: Context window and max output the endpoint actually serves. The policy's
    #: prompt ceiling is derived from these, so switching profiles cannot leave a
    #: ceiling larger than the endpoint can accept.
    context_window: int = 1_000_000
    max_output_tokens: int = 384_000


#: The formal runner records the selected endpoint and model with each run.
LLM_PROFILES: dict[str, LLMProfile] = {
    "hkgai": LLMProfile(
        name="hkgai",
        label="HKGAI course gateway",
        base_url="https://api1.hkgai.net/v1",
        model="t2_hkgai-v3_fp8_1m_e7",
        api_key_env=("DEEPSEEK_API_KEY",),
        timeout_s=180.0,
    ),
    "deepseek": LLMProfile(
        name="deepseek",
        label="DeepSeek official platform",
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
        # Deliberately does NOT fall back to DEEPSEEK_API_KEY: that variable
        # holds the gateway key, and silently sending it to the official API
        # would look like an auth failure in the wrong place.
        api_key_env=("DEEPSEEK_API_KEY_OFFICIAL", "MOUMOU_DEEPSEEK_API_KEY"),
        timeout_s=300.0,
        context_window=1_000_000,
        max_output_tokens=384_000,
    ),
}
DEFAULT_PROFILE = "deepseek"


def resolve_profile(name: str | None = None, *, dotenv_profile: str = "") -> LLMProfile:
    """Pick a profile by explicit name, then MOUMOU_LLM_PROFILE, then default."""
    chosen = (name or os.environ.get("MOUMOU_LLM_PROFILE") or dotenv_profile
              or DEFAULT_PROFILE).strip()
    if chosen not in LLM_PROFILES:
        raise ValueError(
            f"unknown LLM profile {chosen!r}; choose one of {sorted(LLM_PROFILES)}"
        )
    return LLM_PROFILES[chosen]


def _dotenv_items(path, *, names=None):
    p = Path(path)
    if not p.is_file():
        return
    with p.open(encoding="utf-8") as stream:
        for raw in stream:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if names is None or key in names:
                yield key, value.strip().strip('"').strip("'")


def load_dotenv(path: Path | str = ".env") -> None:
    """读取当前本地配置，保留进程中已有的环境变量。"""
    for key, value in _dotenv_items(path):
        os.environ.setdefault(key, value)


class LLMSettings(BaseModel):
    api_key: str = ""
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    timeout_s: float = 180.0
    #: Which profile these values came from, and which env var supplied the key.
    profile: str = DEFAULT_PROFILE
    api_key_env: str = ""

    @classmethod
    def from_env(
        cls, dotenv: Path | str = ".env", profile: str | None = None
    ) -> LLMSettings:
        load_dotenv(dotenv)
        settings = cls.public_from_env(profile=profile, dotenv=None)
        names = LLM_PROFILES[settings.profile].api_key_env
        key_name = next((name for name in names if os.environ.get(name)), "")
        return settings.model_copy(update={
            "api_key": os.environ.get(key_name, ""), "api_key_env": key_name,
        })

    @classmethod
    def public_from_env(cls, *, profile=None, dotenv=".env") -> LLMSettings:
        """查看选定端点时只读取公开字段，进程环境保持原样。"""
        allowed = {"MOUMOU_LLM_PROFILE", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
                   "DEEPSEEK_TIMEOUT_S"} | {
            f"MOUMOU_{name.upper()}_{setting}"
            for name in LLM_PROFILES for setting in ("BASE_URL", "MODEL", "TIMEOUT_S")
        }
        values = {}
        if dotenv is not None:
            for key, value in _dotenv_items(dotenv, names=allowed):
                values.setdefault(key, value)
        spec = resolve_profile(profile, dotenv_profile=values.get("MOUMOU_LLM_PROFILE", ""))
        upper = spec.name.upper()

        def _env(*names: str) -> str:
            for name in names:
                value = os.environ.get(name, values.get(name, ""))
                if value:
                    return value
            return ""

        # Explicit per-profile overrides always win; the legacy DEEPSEEK_* names
        # keep working for the gateway profile that they were written for.
        base_url = _env(f"MOUMOU_{upper}_BASE_URL") or (
            _env("DEEPSEEK_BASE_URL") if spec.name == "hkgai" else "") or spec.base_url
        model = _env(f"MOUMOU_{upper}_MODEL") or (
            _env("DEEPSEEK_MODEL") if spec.name == "hkgai" else "") or spec.model
        timeout = _env(f"MOUMOU_{upper}_TIMEOUT_S") or (
            _env("DEEPSEEK_TIMEOUT_S") if spec.name == "hkgai" else "")
        return cls(
            model=model,
            base_url=base_url,
            timeout_s=float(timeout or spec.timeout_s),
            profile=spec.name,
        )

    def require_key(self) -> str:
        if not self.api_key:
            names = LLM_PROFILES[self.profile].api_key_env if self.profile in LLM_PROFILES else ()
            wanted = " or ".join(names) or "DEEPSEEK_API_KEY"
            raise RuntimeError(
                f"no API key for profile {self.profile!r}.\n"
                f"  set {wanted} in .env (the file is gitignored -- never commit a key)\n"
                f"  profile {self.profile} talks to {self.base_url}"
            )
        return self.api_key


class Settings(BaseModel):
    """Project-level settings that are safe to commit."""

    project_id: str = "default"
    conversation_id: str = "default"
    runs_dir: Path = Field(default=Path("runs"))
    max_slots: int = 4
