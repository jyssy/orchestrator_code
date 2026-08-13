import pytest

from orchestrator import egress_guard


@pytest.fixture(autouse=True)
def successful_secret_scan(monkeypatch):
    """Keep unit tests local; adapter tests exercise real subprocess behavior."""
    monkeypatch.setattr(egress_guard, "scan_text", lambda text: None)
