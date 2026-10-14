"""Filesystem layout of a SAPBot installation (ADR A7).

<SAPBOT_HOME>                       default C:\\SAPBot on Windows, ~/.sapbot elsewhere (tests)
  config/sapbot.yaml                global settings
  data/sapbot.db                    local store: users, grants, approvals, audit, flags (SQLite)
  scripts/library|custom|drafts     VBS script library (A2/A3)
  workspaces/<client_id>/           per-client isolation (A13): workspace.yaml, chroma/, trajectories/, schema_cache/, output/
  logs/                             application logs
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def home() -> Path:
    env = os.environ.get("SAPBOT_HOME")
    if env:
        return Path(env)
    if sys.platform == "win32":
        return Path(r"C:\SAPBot")
    return Path.home() / ".sapbot"


def config_dir() -> Path:
    return home() / "config"


def data_dir() -> Path:
    return home() / "data"


def db_file() -> Path:
    return data_dir() / "sapbot.db"


def scripts_dir() -> Path:
    return home() / "scripts"


def workspaces_dir() -> Path:
    return home() / "workspaces"


def logs_dir() -> Path:
    return home() / "logs"


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def ensure_layout() -> Path:
    """Create the folder layout if missing. Returns SAPBOT_HOME."""
    for p in (config_dir(), data_dir(), scripts_dir() / "library", scripts_dir() / "custom",
              scripts_dir() / "drafts", workspaces_dir(), logs_dir()):
        p.mkdir(parents=True, exist_ok=True)
    return home()
