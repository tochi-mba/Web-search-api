import httpx
import pytest
import respx

from app.core.errors import ProviderUnavailableError, TimeoutProblem, UpstreamError
from app.services.llm.base import ChatMessage, ChatRequest
from app.services.llm.providers.ollama import OllamaProvider

TAGS_URL = "http://localhost:11434/api/tags"
CHAT_URL = "http://localhost:11434/v1/chat/completions"


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def provider(client, monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    return OllamaProvider(client)


def test_defaults_to_localhost(provider):
    assert provider._base_url == "http://localhost:11434"
    assert provider.is_configured() is True


def test_base_url_env_override(client, monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://gpu-box:11434/")
    assert OllamaProvider(client)._base_url == "http://gpu-box:11434"


def test_explicit_base_url_wins(client):
    assert OllamaProvider(client, base_url="http://other:1234")._base_url == "http://other:1234"


def test_empty_base_url_means_not_configured(client):
    assert OllamaProvider(client, base_url="").is_configured() is False


@respx.mock
async def test_models_are_listed_from_tags(provider):
    respx.get(TAGS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [
                    {"name": "llama3.1:8b", "details": {"family": "llama"}},
                    {"name": "qwen3:14b", "details": {"family": "qwen"}},
                ]
            },
        )
    )
    models = await provider.list_models()
    assert [m.id for m in models] == ["ollama:llama3.1:8b", "ollama:qwen3:14b"]
    assert models[0].display_name == "llama"


@respx.mock
async def test_model_ids_keep_their_colons(provider):
    """Ollama tags contain colons; only the first may be treated as a namespace."""
    respx.get(TAGS_URL).mock(
        return_value=httpx.Response(200, json={"models": [{"name": "llama3.1:8b-instruct-q4"}]})
    )
    model = (await provider.list_models())[0]
    assert model.model == "llama3.1:8b-instruct-q4"
    assert model.id == "ollama:llama3.1:8b-instruct-q4"
    assert model.id.split(":", 1)[1] == "llama3.1:8b-instruct-q4"


@respx.mock
async def test_model_key_fallback(provider):
    respx.get(TAGS_URL).mock(
        return_value=httpx.Response(200, json={"models": [{"model": "phi4:latest"}]})
    )
    assert (await provider.list_models())[0].model == "phi4:latest"


@respx.mock
async def test_entries_without_a_name_are_skipped(provider):
    respx.get(TAGS_URL).mock(
        return_value=httpx.Response(200, json={"models": [{"size": 123}, {"name": "ok:1"}]})
    )
    assert [m.model for m in await provider.list_models()] == ["ok:1"]


@respx.mock
async def test_empty_model_list_is_fine(provider):
    respx.get(TAGS_URL).mock(return_value=httpx.Response(200, json={"models": []}))
    assert await provider.list_models() == []


@respx.mock
async def test_daemon_down_maps_to_provider_unavailable(provider):
    respx.get(TAGS_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ProviderUnavailableError, match="unreachable"):
        await provider.list_models()


@respx.mock
async def test_timeout_maps_to_timeout_problem(provider):
    respx.get(TAGS_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(TimeoutProblem):
        await provider.list_models()


@respx.mock
async def test_error_status_maps_to_provider_unavailable(provider):
    respx.get(TAGS_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(ProviderUnavailableError, match="returned an error"):
        await provider.list_models()


@respx.mock
async def test_invalid_json_maps_to_upstream(provider):
    respx.get(TAGS_URL).mock(return_value=httpx.Response(200, content=b"not json"))
    with pytest.raises(UpstreamError, match="invalid JSON"):
        await provider.list_models()


def test_ollama_needs_no_credential(provider):
    assert provider.requires_credential is False


@respx.mock
async def test_chat_goes_through_the_openai_shim(provider):
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "A local summary."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )
    response = await provider.chat(
        ChatRequest(model="llama3.1:8b", messages=[ChatMessage("user", "hi")])
    )
    assert response.text == "A local summary."
    assert response.provider == "ollama"
    assert route.call_count == 1


@respx.mock
async def test_chat_respects_a_custom_base_url(client):
    respx.post("http://gpu-box:11434/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    )
    provider = OllamaProvider(client, base_url="http://gpu-box:11434")
    response = await provider.chat(
        ChatRequest(model="llama3.1:8b", messages=[ChatMessage("user", "hi")])
    )
    assert response.text == "ok"
