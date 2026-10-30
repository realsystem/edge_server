"""Shared fixtures for the bootstrap test suite."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_logs(tmp_path, monkeypatch):
    """Keep Bootstrap log files out of the real ~/.edge-server/logs."""
    import bootstrap

    monkeypatch.setattr(bootstrap, "DEFAULT_LOG_DIR", tmp_path / "logs")
    monkeypatch.delenv("SECRETS_PASSWORD", raising=False)
