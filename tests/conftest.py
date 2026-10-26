import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest


@pytest.fixture(autouse=True)
def _no_live_sorsa(monkeypatch):
    """Never spend real Sorsa quota in tests: the key is removed from the environment for every test."""
    monkeypatch.delenv("SORSA_API_KEY", raising=False)
    monkeypatch.setenv("SORSA_AUTO_FETCH", "false")
