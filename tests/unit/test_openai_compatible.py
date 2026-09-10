"""The one adapter serving ~50 vendors, plus a sweep over the whole spec table.

Credentials arrive per call from keyring, so these tests inject a ResolvedAuth
rather than setting environment variables.
"""

import httpx
import pytest
import respx

from app.core.errors import (
    ProviderUnavailableError,
    RateLimitedError,
    TimeoutProblem,
    UpstreamError,
)
from app.services.keyring.client import NO_AUTH, ResolvedAuth
from app.services.llm.base import ChatMessage, ChatRequest
from app.services.llm.providers.openai_compatible import OpenAICompatibleProvider, _as_int
from app.services.llm.specs import (
    OPENAI_COMPATIBLE_SPECS,
    SPECS_BY_KEY,
    AuthStyle,
    ProviderSpec,
)

SPEC = ProviderSpec(
    key="testvendor",
    label="Test Vendor",
    base_url="https://api.test.dev/v1",
)

AUTH = ResolvedAuth(headers={"Authorization": "Bearer test-key"})

CHAT = ChatRequest(
    model="gpt-4o",
    messages=[ChatMessage(role="user", content="summarise")],
    system="Be terse.",
)


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def provider(client):
    return OpenAICompatibleProvider(SPEC, client)


def completion(text="An executive summary.", **extra):
    payload = {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }
    payload.update(extra)
    return payload


# --- configuration --------------------------------------------------------- #


def test_configuration_no_longer_depends_on_a_credential(client, monkeypatch):
    """Keys belong to callers now, so a provider is configured without one."""
    monkeypatch.delenv("TESTVENDOR_API_KEY", raising=False)
    assert OpenAICompatibleProvider(SPEC, client).is_configured() is True


def test_credentialed_providers_are_flagged(client):
    assert OpenAICompatibleProvider(SPEC, client).requires_credential is True


def test_keyless_providers_are_flagged(client):
    spec = ProviderSpec(
        key="local", label="Local", base_url="http://localhost:1234/v1", auth=AuthStyle.NONE
    )
    assert OpenAICompatibleProvider(spec, client).requires_credential is False


def test_empty_base_url_means_not_configured(client):
    assert OpenAICompatibleProvider(SPEC, client, base_url="").is_configured() is False


def test_explicit_base_url_beats_the_spec(client):
    p = OpenAICompatibleProvider(SPEC, client, base_url="https://custom.dev/v1/")
    assert p._base_url == "https://custom.dev/v1"


# --- attaching the caller's credential ------------------------------------- #


@respx.mock
async def test_resolved_headers_are_attached(provider):
    route = respx.get("https://api.test.dev/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    await provider.list_models(ResolvedAuth(headers={"x-api-key": "sk-ant-1"}))
    assert route.calls[0].request.headers["x-api-key"] == "sk-ant-1"


@respx.mock
async def test_resolved_query_params_are_attached(provider):
    route = respx.get("https://api.test.dev/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    await provider.list_models(ResolvedAuth(query_params={"key": "AIza-123"}))
    assert "key=AIza-123" in str(route.calls[0].request.url)


@respx.mock
async def test_whatever_keyring_returns_is_forwarded_verbatim(provider):
    """Keyring decides which header a key belongs on; this adapter does not."""
    route = respx.get("https://api.test.dev/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    await provider.list_models(
        ResolvedAuth(headers={"X-Weird-Vendor-Auth": "token abc", "X-Extra": "1"})
    )
    sent = route.calls[0].request.headers
    assert sent["x-weird-vendor-auth"] == "token abc"
    assert sent["x-extra"] == "1"


@respx.mock
async def test_no_credential_sends_no_auth_header(provider):
    route = respx.get("https://api.test.dev/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    await provider.list_models(NO_AUTH)
    assert "authorization" not in route.calls[0].request.headers


# --- listing models -------------------------------------------------------- #


@respx.mock
async def test_models_are_namespaced(provider):
    respx.get("https://api.test.dev/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "model-a"}, {"id": "model-b"}]})
    )
    models = await provider.list_models(AUTH)
    assert [m.id for m in models] == ["testvendor:model-a", "testvendor:model-b"]
    assert models[0].provider == "testvendor"
    assert models[0].model == "model-a"


@respx.mock
async def test_bare_list_payloads_are_accepted(provider):
    respx.get("https://api.test.dev/v1/models").mock(
        return_value=httpx.Response(200, json=[{"id": "model-a"}])
    )
    assert len(await provider.list_models(AUTH)) == 1


@respx.mock
async def test_context_metadata_is_captured(provider):
    respx.get("https://api.test.dev/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "m", "context_length": 128000, "max_output_tokens": 4096}]},
        )
    )
    model = (await provider.list_models(AUTH))[0]
    assert model.context_window == 128_000
    assert model.max_output_tokens == 4_096


@respx.mock
async def test_entries_without_an_id_are_skipped(provider):
    respx.get("https://api.test.dev/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"name": "no id"}, {"id": "ok"}]})
    )
    assert [m.model for m in await provider.list_models(AUTH)] == ["ok"]


@respx.mock
async def test_malformed_model_payload_raises(provider):
    respx.get("https://api.test.dev/v1/models").mock(
        return_value=httpx.Response(200, json={"data": "not a list"})
    )
    with pytest.raises(UpstreamError, match="Malformed model list"):
        await provider.list_models(AUTH)


async def test_static_models_are_used_when_declared(client):
    spec = ProviderSpec(
        key="pplx",
        label="Perplexity",
        base_url="https://api.perplexity.ai",
        static_models=("sonar", "sonar-pro"),
    )
    models = await OpenAICompatibleProvider(spec, client).list_models(AUTH)
    assert [m.id for m in models] == ["pplx:sonar", "pplx:sonar-pro"]


# --- chat ------------------------------------------------------------------ #


@respx.mock
async def test_chat_returns_text_and_usage(provider):
    respx.post("https://api.test.dev/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=completion())
    )
    response = await provider.chat(CHAT, AUTH)
    assert response.text == "An executive summary."
    assert response.input_tokens == 100
    assert response.output_tokens == 20
    assert response.provider == "testvendor"
    assert response.finish_reason == "stop"


@respx.mock
async def test_chat_body_is_shaped_for_the_model(provider):
    route = respx.post("https://api.test.dev/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=completion())
    )
    await provider.chat(CHAT, AUTH)
    import json

    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "gpt-4o"
    assert body["max_tokens"] == 2000
    assert body["messages"][0] == {"role": "system", "content": "Be terse."}


@respx.mock
async def test_reasoning_model_body_differs_from_a_chat_model(provider):
    import json

    route = respx.post("https://api.test.dev/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=completion())
    )
    await provider.chat(
        ChatRequest(model="o3", messages=[ChatMessage(role="user", content="x")], system="s"),
        AUTH,
    )
    body = json.loads(route.calls[0].request.content)
    assert "max_completion_tokens" in body
    assert "temperature" not in body
    assert body["messages"][0]["role"] == "developer"


@respx.mock
async def test_empty_choices_raises(provider):
    respx.post("https://api.test.dev/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )
    with pytest.raises(UpstreamError, match="Empty completion"):
        await provider.chat(CHAT, AUTH)


@respx.mock
async def test_missing_choices_key_raises(provider):
    respx.post("https://api.test.dev/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={})
    )
    with pytest.raises(UpstreamError, match="Empty completion"):
        await provider.chat(CHAT, AUTH)


@respx.mock
async def test_invalid_json_raises(provider):
    respx.post("https://api.test.dev/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=b"not json")
    )
    with pytest.raises(UpstreamError, match="invalid JSON"):
        await provider.chat(CHAT, AUTH)


@respx.mock
async def test_missing_usage_is_tolerated(provider):
    respx.post("https://api.test.dev/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
    )
    response = await provider.chat(CHAT, AUTH)
    assert response.input_tokens is None
    assert response.finish_reason is None


@respx.mock
async def test_null_content_becomes_empty_text(provider):
    respx.post("https://api.test.dev/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": None}}]})
    )
    assert (await provider.chat(CHAT, AUTH)).text == ""


# --- self-healing retry ---------------------------------------------------- #


@respx.mock
async def test_unsupported_parameter_triggers_one_retry_without_it(provider):
    import json

    route = respx.post("https://api.test.dev/v1/chat/completions")
    route.side_effect = [
        httpx.Response(
            400,
            json={"error": {"message": "Unsupported parameter: 'temperature' is not supported"}},
        ),
        httpx.Response(200, json=completion()),
    ]
    response = await provider.chat(CHAT, AUTH)
    assert response.text == "An executive summary."
    assert any("retried without 'temperature'" in a for a in response.param_adjustments)

    retry_body = json.loads(route.calls[1].request.content)
    assert "temperature" not in retry_body


@respx.mock
async def test_retry_happens_at_most_once(provider):
    route = respx.post("https://api.test.dev/v1/chat/completions")
    route.side_effect = [
        httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'temperature'"}}),
        httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_tokens'"}}),
    ]
    with pytest.raises(UpstreamError):
        await provider.chat(CHAT, AUTH)
    assert route.call_count == 2


@respx.mock
async def test_unrelated_400s_are_not_retried(provider):
    route = respx.post("https://api.test.dev/v1/chat/completions").mock(
        return_value=httpx.Response(400, json={"error": {"message": "context length exceeded"}})
    )
    with pytest.raises(UpstreamError, match="context length"):
        await provider.chat(CHAT, AUTH)
    assert route.call_count == 1


# --- error mapping --------------------------------------------------------- #


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
async def test_auth_failures_map_to_provider_unavailable(provider, status):
    respx.get("https://api.test.dev/v1/models").mock(
        return_value=httpx.Response(status, json={"error": {"message": "bad key"}})
    )
    with pytest.raises(ProviderUnavailableError, match="rejected the credential"):
        await provider.list_models(AUTH)


@respx.mock
async def test_rate_limits_map_to_rate_limited(provider):
    respx.get("https://api.test.dev/v1/models").mock(return_value=httpx.Response(429))
    with pytest.raises(RateLimitedError):
        await provider.list_models(AUTH)


@respx.mock
async def test_server_errors_map_to_upstream(provider):
    respx.get("https://api.test.dev/v1/models").mock(return_value=httpx.Response(500))
    with pytest.raises(UpstreamError, match="500"):
        await provider.list_models(AUTH)


@respx.mock
async def test_timeouts_map_to_timeout_problem(provider):
    respx.get("https://api.test.dev/v1/models").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(TimeoutProblem):
        await provider.list_models(AUTH)


@respx.mock
async def test_connection_errors_map_to_provider_unavailable(provider):
    respx.get("https://api.test.dev/v1/models").mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(ProviderUnavailableError, match="unreachable"):
        await provider.list_models(AUTH)


@respx.mock
async def test_chat_timeouts_map_to_timeout_problem(provider):
    respx.post("https://api.test.dev/v1/chat/completions").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    with pytest.raises(TimeoutProblem):
        await provider.chat(CHAT, AUTH)


@respx.mock
async def test_chat_connection_errors_map_to_provider_unavailable(provider):
    respx.post("https://api.test.dev/v1/chat/completions").mock(
        side_effect=httpx.ConnectError("no route")
    )
    with pytest.raises(ProviderUnavailableError):
        await provider.chat(CHAT, AUTH)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"error": {"message": "structured"}}, "structured"),
        ({"error": "flat string"}, "flat string"),
        ({"message": "top level"}, "top level"),
    ],
)
@respx.mock
async def test_error_details_are_extracted_from_common_shapes(provider, payload, expected):
    respx.get("https://api.test.dev/v1/models").mock(return_value=httpx.Response(500, json=payload))
    with pytest.raises(UpstreamError, match=expected):
        await provider.list_models(AUTH)


@respx.mock
async def test_non_json_error_bodies_fall_back_to_text(provider):
    respx.get("https://api.test.dev/v1/models").mock(
        return_value=httpx.Response(500, content=b"gateway exploded")
    )
    with pytest.raises(UpstreamError, match="gateway exploded"):
        await provider.list_models(AUTH)


@respx.mock
async def test_json_error_without_a_known_shape_is_stringified(provider):
    respx.get("https://api.test.dev/v1/models").mock(
        return_value=httpx.Response(500, json={"unexpected": "shape"})
    )
    with pytest.raises(UpstreamError):
        await provider.list_models(AUTH)


# --- helpers --------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5, 5),
        (5.9, 5),
        ("42", 42),
        ("not a number", None),
        (None, None),
        (True, None),
        ([], None),
    ],
)
def test_as_int_coercion(value, expected):
    assert _as_int(value) == expected


# --- the whole fleet ------------------------------------------------------- #


def test_every_spec_key_is_unique():
    assert len(SPECS_BY_KEY) == len(OPENAI_COMPATIBLE_SPECS)


@pytest.mark.parametrize("spec", OPENAI_COMPATIBLE_SPECS, ids=lambda s: s.key)
def test_every_spec_is_well_formed(spec):
    assert spec.key
    assert spec.key.replace("_", "").isalnum()
    assert spec.label
    assert spec.base_url.startswith(("http://", "https://"))
    assert not spec.base_url.endswith("/")
    assert spec.models_path.startswith("/")
    assert spec.chat_path.startswith("/")
    if spec.auth is AuthStyle.HEADER:
        assert spec.auth_header, f"{spec.key} uses header auth but names no header"
    # The provisioning fields must be usable, since they decide how a key is
    # stored in keyring and therefore whether it works at all.
    assert spec.default_header
    assert "{value}" in spec.default_template


@pytest.mark.parametrize("spec", OPENAI_COMPATIBLE_SPECS, ids=lambda s: s.key)
@respx.mock
async def test_every_spec_can_list_models_and_chat(spec, client):
    """Every row in the table must work against a compliant server.

    This is what makes adding a provider a one-line change: a new row is
    covered automatically, and a malformed row fails here.
    """
    base = spec.base_url
    respx.get(f"{base}{spec.models_path}").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "some-model"}]})
    )
    respx.post(f"{base}{spec.chat_path}").mock(return_value=httpx.Response(200, json=completion()))

    provider = OpenAICompatibleProvider(spec, client)
    assert provider.is_configured() is True

    models = await provider.list_models(AUTH)
    assert models
    assert all(m.id.startswith(f"{spec.key}:") for m in models)

    response = await provider.chat(CHAT, AUTH)
    assert response.text == "An executive summary."
    assert response.provider == spec.key


@respx.mock
async def test_non_object_choice_is_rejected(provider):
    respx.post("https://api.test.dev/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": ["not an object"]})
    )
    with pytest.raises(UpstreamError, match="Malformed completion"):
        await provider.chat(CHAT, AUTH)


@respx.mock
async def test_non_dict_message_yields_empty_text(provider):
    respx.post("https://api.test.dev/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": "oops"}]})
    )
    assert (await provider.chat(CHAT, AUTH)).text == ""


@respx.mock
async def test_non_dict_json_error_body_is_stringified(provider):
    respx.get("https://api.test.dev/v1/models").mock(
        return_value=httpx.Response(500, json=["a", "list", "of", "things"])
    )
    with pytest.raises(UpstreamError):
        await provider.list_models(AUTH)


@respx.mock
async def test_non_string_finish_reason_becomes_none(provider):
    respx.post("https://api.test.dev/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "x"}, "finish_reason": 7}]}
        )
    )
    assert (await provider.chat(CHAT, AUTH)).finish_reason is None


def test_header_auth_specs_provision_onto_their_own_header():
    """Provisioning must put the key where the vendor expects it."""
    spec = ProviderSpec(
        key="hv",
        label="Header Vendor",
        base_url="https://hv.test/v1",
        auth=AuthStyle.HEADER,
        auth_header="X-Api-Key",
    )
    assert spec.default_header == "X-Api-Key"
    assert spec.default_template == "{value}"


def test_bearer_specs_provision_onto_authorization():
    spec = ProviderSpec(key="bv", label="Bearer Vendor", base_url="https://bv.test/v1")
    assert spec.default_header == "Authorization"
    assert spec.default_template == "Bearer {value}"
