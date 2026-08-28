"""Configuration. Everything that affects a decision is recorded in the audit trail.

`config_hash` is persisted on every run: six months later, "why did this
auto-resolve?" is unanswerable if the thresholds have since moved and nobody
wrote down what they were at the time.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM ---------------------------------------------------------------------
    gemini_api_key: str = Field("", alias="GEMINI_API_KEY")

    # Pinned exact ids, never floating aliases: a silently-upgraded model is an
    # unversioned change to a system whose whole value is auditability (ADR-008).
    model_cheap: str = Field("gemini-3.1-flash-lite", alias="MODEL_CHEAP")
    model_smart: str = Field("gemini-3.5-flash", alias="MODEL_SMART")

    llm_timeout_s: float = Field(30.0, alias="LLM_TIMEOUT_S")
    llm_max_retries: int = Field(2, alias="LLM_MAX_RETRIES")

    # Free tier measured at ~15 RPM (phase0-findings.md). Stay under it.
    llm_rate_limit_rpm: int = Field(12, alias="LLM_RATE_LIMIT_RPM")

    # --- routing thresholds -------------------------------------------------------
    tau_auto: float = Field(0.85, alias="TAU_AUTO")
    tau_draft: float = Field(0.55, alias="TAU_DRAFT")

    # Band in which the adaptive cross-model check is worth its quota.
    crossmodel_band_low: float = 0.45
    crossmodel_band_high: float = 0.92

    # --- storage -------------------------------------------------------------------
    database_url: str = Field(
        "postgresql://concierge:concierge@localhost:5434/concierge",
        alias="DATABASE_URL",
    )

    # --- circuit breaker -----------------------------------------------------------
    breaker_error_rate: float = 0.25
    breaker_window: int = 12
    breaker_cooldown_s: float = 30.0

    graph_version: str = "1.0.0"

    @property
    def config_hash(self) -> str:
        """Hash of every setting that can change a routing outcome."""
        material = json.dumps(
            {
                "graph_version": self.graph_version,
                "model_cheap": self.model_cheap,
                "model_smart": self.model_smart,
                "tau_auto": self.tau_auto,
                "tau_draft": self.tau_draft,
                "crossmodel_band": [self.crossmodel_band_low, self.crossmodel_band_high],
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode()).hexdigest()[:12]

    @property
    def psycopg_url(self) -> str:
        """psycopg3 accepts the postgresql:// form directly."""
        return self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def sample_tickets_path() -> Path:
    return BACKEND_ROOT / "data" / "sample_tickets.json"


def results_dir() -> Path:
    d = REPO_ROOT / "results"
    d.mkdir(parents=True, exist_ok=True)
    return d
