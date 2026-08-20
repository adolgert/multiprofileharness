import pytest

from multiagent import probe


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """A test that patches the wrong thing must fail, not probe the real ollama."""
    def refuse(url, *args, **kwargs):
        raise AssertionError(f"test reached the network: {url}")

    monkeypatch.setattr(probe, "_http_json", refuse)
