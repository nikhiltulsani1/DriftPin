from __future__ import annotations

import httpx
import pytest

from driftpin.cli.init_wizard import (
    InitWizardError,
    build_config,
    list_ollama_models,
    validate_groq_key,
)
from driftpin.config.settings import ProviderKind
from driftpin.providers.base import ProviderValidationError


def test_build_config_anthropic() -> None:
    config = build_config(ProviderKind.ANTHROPIC, model="claude-sonnet-5")
    assert config.provider.kind == ProviderKind.ANTHROPIC
    assert config.provider.model == "claude-sonnet-5"
    assert config.provider.base_url is None


def test_build_config_groq() -> None:
    config = build_config(ProviderKind.GROQ, model="llama-3.3-70b-versatile")
    assert config.provider.kind == ProviderKind.GROQ
    assert config.provider.model == "llama-3.3-70b-versatile"


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


@pytest.mark.asyncio
async def test_validate_groq_key_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    monkeypatch.setattr(
        "driftpin.providers.groq_provider.httpx.AsyncClient", _client_with_transport(handler)
    )

    await validate_groq_key("test-key", "llama-3.3-70b-versatile")  # should not raise


@pytest.mark.asyncio
async def test_validate_groq_key_raises_on_bad_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid api key"})

    monkeypatch.setattr(
        "driftpin.providers.groq_provider.httpx.AsyncClient", _client_with_transport(handler)
    )

    with pytest.raises(ProviderValidationError):
        await validate_groq_key("bad-key", "llama-3.3-70b-versatile")
