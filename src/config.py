"""Settings: `.env` + process env + CLI overrides (precedence CLI > process env > .env)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator

HARD_LLM_CAP = 9  # 8 planned + 1 retry; never configurable above this (spec FR-15)
OWNERS = ("product", "tech", "design")
DEFAULT_OWNER_LABELS = "product:@product team,tech:@tech team,design:@design team"


class ConfigError(Exception):
    """Config or input problem. The CLI maps it to exit code 1."""


def parse_owner_labels(raw: str | Mapping[str, str] | None) -> dict[str, str]:
    """`product:@product team,tech:@tech team` -> {"product": "@product team", ...}; missing owners get defaults."""
    if isinstance(raw, Mapping):
        pairs = dict(raw)
    else:
        pairs = {}
        for item in filter(None, (s.strip() for s in (raw or "").split(","))):
            key, sep, label = item.partition(":")
            if not sep or not label.strip():
                raise ConfigError(f"OWNER_LABELS entry {item!r} must look like 'owner:label'.")
            pairs[key.strip().lower()] = label.strip()
    unknown = set(pairs) - set(OWNERS)
    if unknown:
        raise ConfigError(f"OWNER_LABELS has unknown owner(s) {sorted(unknown)}; allowed: {list(OWNERS)}.")
    return {o: pairs.get(o, f"@{o} team") for o in OWNERS}


class Settings(BaseModel):
    # Models (slot validation lives in models.py)
    vision_model: str = ""
    text_model: str = ""
    model_analyst: str = ""
    model_auditor: str = ""
    model_specialists: str = ""
    model_curator: str = ""

    # Secrets: SecretStr so they never show up in repr/logs
    google_api_key: SecretStr = SecretStr("")
    groq_api_key: SecretStr = SecretStr("")
    jira_api_token: SecretStr = SecretStr("")
    jira_base_url: str = ""
    jira_email: str = ""

    # Run input
    jira_issue_key: str = ""
    file_path: str = ""

    # Limits
    max_screens: int = Field(8, ge=1)
    max_image_edge: int = Field(2000, ge=256)
    max_image_mb: float = Field(10, gt=0)
    max_llm_calls: int = HARD_LLM_CAP
    panel_concurrency: int = Field(5, ge=1, le=5)
    max_q_per_screen: int = Field(8, ge=1)
    max_flow_q: int = Field(8, ge=0)
    max_total_q: int = Field(30, ge=1)
    jira_comment_max_chars: int = Field(30000, ge=1000, le=32000)

    # Behaviour
    temperature_vision: float = Field(0.2, ge=0, le=2)
    temperature_text: float = Field(0.3, ge=0, le=2)
    owner_labels: dict[str, str] = Field(default_factory=lambda: parse_owner_labels(DEFAULT_OWNER_LABELS))
    dry_run: bool = False
    force: bool = False
    log_level: str = "INFO"

    @field_validator("max_llm_calls")
    @classmethod
    def _clamp_calls(cls, v: int) -> int:
        return max(1, min(v, HARD_LLM_CAP))

    @field_validator("owner_labels", mode="before")
    @classmethod
    def _owner_labels(cls, v: Any) -> dict[str, str]:
        return parse_owner_labels(v)

    @field_validator("log_level")
    @classmethod
    def _log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("must be DEBUG, INFO, WARNING or ERROR")
        return v

    @field_validator("jira_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    def require_run_inputs(self) -> None:
        """Fail fast (exit 1) when values a real run needs are missing. Jira creds are optional in dry-run."""
        needed = {"VISION_MODEL": self.vision_model, "TEXT_MODEL": self.text_model,
                  "GOOGLE_API_KEY": self.google_api_key.get_secret_value(),
                  "GROQ_API_KEY": self.groq_api_key.get_secret_value(),
                  "FILE_PATH / --file-path": self.file_path, "JIRA_ISSUE_KEY / --jira-id": self.jira_issue_key}
        if not self.dry_run:
            needed |= {"JIRA_BASE_URL": self.jira_base_url, "JIRA_EMAIL": self.jira_email,
                       "JIRA_API_TOKEN": self.jira_api_token.get_secret_value()}
        missing = [k for k, v in needed.items() if not str(v).strip()]
        if missing:
            raise ConfigError(f"Missing required setting(s): {', '.join(missing)}. Set them in .env or pass via CLI.")

    @property
    def has_jira_credentials(self) -> bool:
        return bool(self.jira_base_url and self.jira_email and self.jira_api_token.get_secret_value())


def export_provider_env(settings: Settings) -> None:
    """google-genai (Gemini) and LiteLLM (Groq) read their keys from os.environ; values loaded from .env are
    exported for this process only. Existing process values win."""
    for key, value in (("GOOGLE_API_KEY", settings.google_api_key.get_secret_value()),
                       ("GROQ_API_KEY", settings.groq_api_key.get_secret_value()),
                       ("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")):
        if value and not os.environ.get(key):
            os.environ[key] = value


_ENV_FIELDS = {name.upper(): name for name in Settings.model_fields}
_TRUE = {"1", "true", "yes", "on"}


def load_settings(
    cli: Mapping[str, Any] | None = None,
    env_file: str | Path | None = ".env",
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Build Settings. `cli` keys: file_path, jira_id, dry_run, force, verbose (None/False = not given)."""
    raw: dict[str, Any] = {}
    if env_file and Path(env_file).is_file():
        raw.update({k: v for k, v in dotenv_values(env_file).items() if v is not None})
    raw.update(os.environ if environ is None else environ)

    values: dict[str, Any] = {_ENV_FIELDS[k]: v for k, v in raw.items() if k in _ENV_FIELDS and v != ""}
    if "dry_run" in values:
        values["dry_run"] = str(values["dry_run"]).strip().lower() in _TRUE

    cli = cli or {}
    if cli.get("file_path"):
        values["file_path"] = cli["file_path"]
    if cli.get("jira_id"):
        values["jira_issue_key"] = cli["jira_id"]
    if cli.get("dry_run"):
        values["dry_run"] = True
    if cli.get("force"):
        values["force"] = True
    if cli.get("verbose"):
        values["log_level"] = "DEBUG"

    try:
        return Settings(**values)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc'])).upper()}: {err['msg']}" for err in e.errors())
        raise ConfigError(f"Invalid configuration: {problems}") from None
