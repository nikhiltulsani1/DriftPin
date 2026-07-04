from __future__ import annotations

import httpx
import pytest

from driftpin.cli.init_wizard import InitWizardError, build_config, list_ollama_models
from driftpin.config.settings import ProviderKind


def test_build_config_anthropic() -> None:
    config = build_config(ProviderKind.ANTHROPIC, model="claude-sonnet-5")
    assert config.provider.kind == ProviderKind.ANTHROPIC
    assert config.provider.model == "claude-sonnet-5"
    assert config.provider.base_url is None


def test_build_config_ollama_with_base_url() -> None:
    config = build_config(ProviderKind.OLLAMA, model="llama3", base_url="http://localhost:11434")
    assert config.provider.base_url == "http://localhost:11434"


def _client_with_transport(handler):
    class _MockAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    return _MockAsyncClient


@pytest.mark.asyncio
async def test_list_ollama_models_returns_installed_names(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "llama3"}, {"name": "mistral"}]})

    monkeypatch.setattr(
        "driftpin.cli.init_wizard.httpx.AsyncClient", _client_with_transport(handler)
    )

    models = await list_ollama_models("http://localhost:11434")
    assert models == ["llama3", "mistral"]


@pytest.mark.asyncio
async def test_list_ollama_models_raises_when_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        "driftpin.cli.init_wizard.httpx.AsyncClient", _client_with_transport(handler)
    )

    with pytest.raises(InitWizardError, match="not reachable"):
        await list_ollama_models("http://localhost:11434")


@pytest.mark.asyncio
async def test_list_ollama_models_raises_when_no_models_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": []})

    monkeypatch.setattr(
        "driftpin.cli.init_wizard.httpx.AsyncClient", _client_with_transport(handler)
    )

    with pytest.raises(InitWizardError, match="no models installed"):
        await list_ollama_models("http://localhost:11434")
