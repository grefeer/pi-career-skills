"""Tests for pi_career_skills.model_factory (DeepSeek model factory + key resolver)."""

from __future__ import annotations

import json

import httpx
import pytest

from pi_ai import Context, UserMessage, stream_simple
from pi_ai.types import SimpleStreamOptions
from pi_career_skills.errors import MODEL_API_KEY_MISSING, CareerToolError
from pi_career_skills.model_factory import (
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_CONTEXT_WINDOW,
    DEFAULT_DEEPSEEK_MAX_TOKENS,
    _thinking_extra_body,
    create_deepseek_model,
    get_deepseek_api_key,
    resolve_api_key,
)

# ---------------------------------------------------------------------------
# _thinking_extra_body
# ---------------------------------------------------------------------------


def test_thinking_extra_body_v4_prefix_disables_thinking():
    assert _thinking_extra_body("deepseek-v4-flash") == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }


def test_thinking_extra_body_case_insensitive():
    # Source parity: lowered = model_id.lower() then .startswith(...).
    assert _thinking_extra_body("DEEPSEEK-V4-FLASH") == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }
    assert _thinking_extra_body("  DeepSeek-V4-x  ".strip()) == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }


def test_thinking_extra_body_non_v4_returns_empty():
    assert _thinking_extra_body("deepseek-chat") == {}
    assert _thinking_extra_body("deepseek-reasoner") == {}
    assert _thinking_extra_body("") == {}


# ---------------------------------------------------------------------------
# create_deepseek_model — field values match plan section 8
# ---------------------------------------------------------------------------


def test_create_deepseek_model_v4_fields():
    model = create_deepseek_model("deepseek-v4-flash")
    assert model.id == "deepseek-v4-flash"
    assert model.name == "deepseek-v4-flash"
    assert model.api == "openai-completions"
    assert model.provider == "deepseek"
    assert model.base_url == DEEPSEEK_BASE_URL
    assert model.reasoning is False
    assert model.input == ["text"]
    assert model.context_window == DEFAULT_DEEPSEEK_CONTEXT_WINDOW
    assert model.max_tokens == DEFAULT_DEEPSEEK_MAX_TOKENS


def test_create_deepseek_model_temperature_zero_for_any_id():
    assert create_deepseek_model("deepseek-v4-flash").sampling_params["temperature"] == 0
    assert create_deepseek_model("deepseek-chat").sampling_params["temperature"] == 0


def test_create_deepseek_model_v4_carries_thinking_disable():
    sp = create_deepseek_model("deepseek-v4-flash").sampling_params
    assert sp["extra_body"] == {"thinking": {"type": "disabled"}}


def test_create_deepseek_model_non_v4_has_no_extra_body():
    sp = create_deepseek_model("deepseek-chat").sampling_params
    assert "extra_body" not in sp
    assert sp["temperature"] == 0


def test_thinking_flag_keyed_on_model_id_not_base_url():
    # The v4 disable decision uses model_id.lower(), never the base URL.
    # Same id → same flag regardless of base_url override (model factory
    # always sets DEEPSEEK_BASE_URL, but the underlying helper is pure id).
    assert _thinking_extra_body("DEEPSEEK-v4-x") == {
        "extra_body": {"thinking": {"type": "disabled"}}
    }
    assert _thinking_extra_body("deepseek-chat") == {}


# ---------------------------------------------------------------------------
# get_deepseek_api_key / resolve_api_key
# ---------------------------------------------------------------------------


def test_get_deepseek_api_key_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv(DEEPSEEK_API_KEY_ENV, raising=False)
    assert get_deepseek_api_key() is None


def test_get_deepseek_api_key_returns_value(monkeypatch):
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "sk-test-123")
    assert get_deepseek_api_key() == "sk-test-123"


def test_resolve_api_key_faux_returns_none():
    assert resolve_api_key("faux") is None


def test_resolve_api_key_unknown_returns_none():
    assert resolve_api_key("unknown-provider") is None


def test_resolve_api_key_deepseek_with_key(monkeypatch):
    monkeypatch.setenv(DEEPSEEK_API_KEY_ENV, "sk-live")
    assert resolve_api_key("deepseek") == "sk-live"


def test_resolve_api_key_deepseek_missing_raises(monkeypatch):
    monkeypatch.delenv(DEEPSEEK_API_KEY_ENV, raising=False)
    with pytest.raises(CareerToolError) as exc_info:
        resolve_api_key("deepseek")
    assert exc_info.value.code == MODEL_API_KEY_MISSING
    assert "missing DEEPSEEK_API_KEY" in exc_info.value.message


# ---------------------------------------------------------------------------
# Captured-request test — drive the real openai-completions provider and
# assert temperature + thinking actually reach the provider payload.
# ---------------------------------------------------------------------------


def _sse_chunk(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


@pytest.mark.asyncio
async def test_deepseek_v4_payload_carries_temperature_and_thinking():
    captured_body: bytes | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_body
        captured_body = request.content
        chunk_payload = {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        }
        body = _sse_chunk(chunk_payload) + b"data: [DONE]\n\n"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    model = create_deepseek_model("deepseek-v4-flash")
    ctx = Context(messages=[UserMessage(content="hi")])
    options = SimpleStreamOptions(api_key="test-key", http_client=client)

    event_stream = stream_simple(model, ctx, options)
    async for _ in event_stream:
        pass
    final = await event_stream.result()

    # Stream completed normally (not error).
    assert final.stop_reason == "stop"
    assert captured_body is not None

    body = json.loads(captured_body)
    # Captured body keys: temperature + thinking reach the wire.
    assert body["temperature"] == 0
    assert body["thinking"] == {"type": "disabled"}
    assert body["model"] == "deepseek-v4-flash"
    assert body["stream"] is True


@pytest.mark.asyncio
async def test_deepseek_chat_payload_has_no_thinking_field():
    """Non-v4 model — thinking field must NOT appear in the request body."""

    captured_body: bytes | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_body
        captured_body = request.content
        chunk_payload = {
            "id": "chatcmpl-2",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "deepseek-chat",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "hi"},
                    "finish_reason": "stop",
                }
            ],
        }
        body = _sse_chunk(chunk_payload) + b"data: [DONE]\n\n"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)

    model = create_deepseek_model("deepseek-chat")
    ctx = Context(messages=[UserMessage(content="hi")])
    options = SimpleStreamOptions(api_key="test-key", http_client=client)

    event_stream = stream_simple(model, ctx, options)
    async for _ in event_stream:
        pass
    final = await event_stream.result()

    assert final.stop_reason == "stop"
    assert captured_body is not None

    body = json.loads(captured_body)
    assert body["temperature"] == 0
    assert "thinking" not in body
