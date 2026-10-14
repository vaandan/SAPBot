"""Global settings loader (pydantic). Values come from <SAPBOT_HOME>/config/sapbot.yaml over package defaults."""
from __future__ import annotations

import logging
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from . import paths

log = logging.getLogger(__name__)
DEFAULTS_FILE = paths.package_dir() / "defaults" / "sapbot.yaml"


class LocalModelCfg(BaseModel):
    base_url: str = ""
    model: str = "qwen2.5-14b-instruct"
    timeout_seconds: int = 120


class ModelsCfg(BaseModel):
    claude_model: str = "claude-sonnet-5-5"
    claude_escalation_model: str = "claude-opus-5-5"
    local_model: LocalModelCfg = Field(default_factory=LocalModelCfg)


class AgentCfg(BaseModel):
    max_turns: int = 25
    loop_repeat_limit: int = 3
    max_tokens_per_run: int = 400_000


class RouterCfg(BaseModel):
    min_gold_examples: int = 10
    escalate_after_failed_steps: int = 1


class RagCfg(BaseModel):
    top_k: int = 3
    min_similarity: float = 0.35


class SapCfg(BaseModel):
    open_own_session: bool = True
    standard_language: str = "EN"
    standard_date_format: str = "DD.MM.YYYY"
    grid_clipboard_threshold: int = 100
    run_timeout_seconds: int = 600
    warning_allowlist: list[str] = Field(default_factory=list)
    write_vkeys: list[int] = Field(default_factory=lambda: [11])
    write_button_suffixes: list[str] = Field(default_factory=lambda: ["tbar[0]/btn[11]"])
    write_okcodes: list[str] = Field(default_factory=lambda: ["SAVE", "BU", "SICH"])
    safe_okcodes: list[str] = Field(default_factory=lambda: ["BACK", "%EX", "RW", "ENTR", "ONLI", "CANC"])
    readonly_vkeys: list[int] = Field(default_factory=lambda: [0, 2, 3, 8, 12])
    risky_click_regex: str = r"save|post|change|edit|delete|create|release|confirm|\byes\b"
    safe_popup_regex: str = r"cancel|\bno\b|close|continue|information"
    hard_stop_tcodes: list[str] = Field(default_factory=lambda: ["SE14"])
    hard_stop_okcodes: list[str] = Field(default_factory=lambda: ["&SAP_EDIT"])
    display_tcodes: list[str] = Field(default_factory=list)


class ApprovalsCfg(BaseModel):
    mode: str = "dialog"
    timeout_seconds: int = 300


class HubCfg(BaseModel):
    url: str = ""
    public_key_file: str = ""
    ca_bundle: str = ""


class Settings(BaseModel):
    models: ModelsCfg = Field(default_factory=ModelsCfg)
    agent: AgentCfg = Field(default_factory=AgentCfg)
    router: RouterCfg = Field(default_factory=RouterCfg)
    rag: RagCfg = Field(default_factory=RagCfg)
    sap: SapCfg = Field(default_factory=SapCfg)
    approvals: ApprovalsCfg = Field(default_factory=ApprovalsCfg)
    hub: HubCfg = Field(default_factory=HubCfg)
    features_default: dict[str, bool] = Field(default_factory=dict)
    hub_bind_os_user: bool = True   # Hub usernames = Windows user names (A12); policy only valid for that user


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def settings_file() -> Path:
    return paths.config_dir() / "sapbot.yaml"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data = yaml.safe_load(DEFAULTS_FILE.read_text(encoding="utf-8")) or {}
    f = settings_file()
    if f.exists():
        try:
            data = _deep_merge(data, yaml.safe_load(f.read_text(encoding="utf-8")) or {})
        except yaml.YAMLError as e:
            log.error("Invalid %s (%s) - using defaults", f, e)
    return Settings.model_validate(data)


def reload_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


def write_default_settings(overwrite: bool = False) -> Path:
    f = settings_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    if overwrite or not f.exists():
        shutil.copyfile(DEFAULTS_FILE, f)
    return f


def save_settings_patch(patch: dict[str, Any]) -> Settings:
    """Merge a partial dict into the user settings file (used by the Settings/Features pages)."""
    f = settings_file()
    cur = yaml.safe_load(f.read_text(encoding="utf-8")) if f.exists() else {}
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump(_deep_merge(cur or {}, patch), sort_keys=False), encoding="utf-8")
    return reload_settings()
