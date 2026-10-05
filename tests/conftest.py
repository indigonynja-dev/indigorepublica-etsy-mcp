import pytest


@pytest.fixture(autouse=True)
def _isolated_research_db(tmp_path, monkeypatch):
    """Never touch the real ~/.indigorepublica-etsy-mcp/research.db from tests."""
    monkeypatch.setenv("ETSY_RESEARCH_DB", str(tmp_path / "research-data" / "research.db"))
