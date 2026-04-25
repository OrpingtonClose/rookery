"""Real-path tests for the LLM client — no mocks.

We stand up a tiny aiohttp-free asgi app served by httpx's
MockTransport. That's not a mock of our code; it's a real HTTP
endpoint served in-process, exercising every byte of the client's
parsing and retry logic.
"""

from __future__ import annotations

import json

import httpx
import pytest

from rookery.llm import LLMClient, LLMError, _parse_response


def test_parse_response_deepseek_v4_shape() -> None:
    """DeepSeek v4 splits content/reasoning; the parser must handle it."""
    raw = {
        "choices": [
            {
                "message": {
                    "content": "final answer",
                    "reasoning_content": "chain of thought",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "completion_tokens_details": {"reasoning_tokens": 40},
            "prompt_cache_hit_tokens": 80,
            "prompt_cache_miss_tokens": 20,
        },
        "model": "deepseek-v4-flash",
    }
    resp = _parse_response(raw, elapsed_s=0.5)
    assert resp.content == "final answer"
    assert resp.reasoning == "chain of thought"
    assert resp.usage.reasoning_tokens == 40
    assert resp.usage.cache_hit_tokens == 80


def test_parse_response_openai_classic_shape() -> None:
    """Classic OpenAI-compat response without reasoning field."""
    raw = {
        "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        "model": "gpt-whatever",
    }
    resp = _parse_response(raw, elapsed_s=0.1)
    assert resp.content == "hello"
    assert resp.reasoning == ""


def test_parse_response_no_choices_raises() -> None:
    with pytest.raises(LLMError):
        _parse_response({"choices": []}, 0.0)


def _handler(fixture_response: dict, status: int = 200, failures_first: int = 0):
    """Return a httpx.MockTransport handler that fails N times then succeeds."""
    call_count = {"n": 0}

    def _respond(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] <= failures_first:
            return httpx.Response(503, text="transient")
        body = request.read().decode()
        # Echo the model so the test can verify it was forwarded
        assert "messages" in json.loads(body)
        return httpx.Response(status, json=fixture_response)

    _respond.call_count = call_count  # type: ignore[attr-defined]
    return _respond


@pytest.mark.asyncio
async def test_client_posts_and_parses() -> None:
    pytest.importorskip("pytest_asyncio")
    fixture = {
        "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        "model": "deepseek-v4-flash",
    }
    handler = _handler(fixture)
    transport = httpx.MockTransport(handler)

    client = LLMClient(
        base_url="https://api.example.invalid/v1",
        api_key="k",
        default_model="deepseek-v4-flash",
    )
    async with client:
        # Inject transport — test-only access.
        assert client._client is not None  # noqa: SLF001
        await client._client.aclose()  # noqa: SLF001
        client._client = httpx.AsyncClient(transport=transport)  # noqa: SLF001
        try:
            resp = await client.complete("ping")
        finally:
            pass

    assert resp.content == "pong"
    assert handler.call_count["n"] == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_client_retries_on_5xx() -> None:
    fixture = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    handler = _handler(fixture, failures_first=2)
    transport = httpx.MockTransport(handler)

    client = LLMClient(
        base_url="https://api.example.invalid/v1",
        api_key="k",
        default_model="m",
        max_retries=5,
    )
    async with client:
        assert client._client is not None  # noqa: SLF001
        await client._client.aclose()  # noqa: SLF001
        client._client = httpx.AsyncClient(transport=transport)  # noqa: SLF001
        # Patch sleep to zero so the test is fast
        import rookery.llm as llm_mod

        original_sleep = llm_mod.asyncio.sleep

        async def _no_sleep(_: float) -> None:
            pass

        llm_mod.asyncio.sleep = _no_sleep  # type: ignore[assignment]
        try:
            resp = await client.complete("x")
        finally:
            llm_mod.asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert resp.content == "ok"
    # 2 failures + 1 success = 3 calls
    assert handler.call_count["n"] == 3  # type: ignore[attr-defined]
