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
    """Per-user data folder. IT-managed laptops (machine policy present): %LOCALAPPDATA%\\SAPBot."""
    env = os.environ.get("SAPBOT_HOME")
    if env and not (sys.platform == "win32" and machine_policy_file().exists()):
        return Path(env)
    if sys.platform == "win32":
        if machine_policy_file().exists():
            return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "SAPBot"
        return Path(r"C:\SAPBot")
    return Path.home() / ".sapbot"


def machine_dir() -> Path:
    """Machine-wide, IT-controlled folder (read-only for consultants): %ProgramData%\\SAPBot.
    Holds machine.yaml (locked settings) and the Hub public key in IT-managed installs."""
    if sys.platform == "win32":
        real = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "SAPBot"
        if (real / "machine.yaml").exists():      # an IT machine policy cannot be redirected by the user
            return real
    env = os.environ.get("SAPBOT_MACHINE_DIR")
    if env:
        return Path(env)
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "SAPBot"
    return Path("/etc/sapbot")


def machine_policy_file() -> Path:
    return machine_dir() / "machine.yaml"


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
