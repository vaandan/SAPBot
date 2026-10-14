import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SAPBOT_HOME", str(tmp_path / "SAPBot"))
    monkeypatch.delenv("SAPBOT_WORKSPACE", raising=False)
    from sapbot import config
    from sapbot.memory import embeddings
    config.get_settings.cache_clear()
    embeddings._cached = embeddings.HashingEmbedder()  # offline, deterministic
    yield tmp_path / "SAPBot"
    config.get_settings.cache_clear()


@pytest.fixture()
def fake_factory():
    from sapbot.sap.fake import FakeSapApp
    return FakeSapApp.factory("SUQ", "400")


@pytest.fixture()
def approvals():
    """Records approval requests; decision controlled by .answer."""
    from sapbot.security.approvals import Decision

    class A:
        answer = True
        seen: list = []

        def __call__(self, aid, summary, details):
            self.seen.append((aid, summary, details))
            return Decision(self.answer, "test-approver", aid)
    a = A()
    a.seen = []
    return a


@pytest.fixture()
def ex(home, fake_factory, approvals):
    from sapbot import workspaces
    from sapbot.runtime import build
    workspaces.create("acme", "ACME Ltd", [{"sid": "SUQ", "client": "400", "env": "QA", "write": True},
                                           {"sid": "SUP", "client": "300", "env": "PROD"}],
                      allowed_tcodes=["VA01", "COR2"])
    e = build("cowork", app_factory=fake_factory, approval_callback=approvals,
              script_run_fn=lambda args, timeout: (0, "ATTACHED|SUQ|400|VANDAN\nRESULT|STATUS|REL\n", ""))
    e.set_workspace("acme")
    return e
