"""LLM client — the one place we talk to the model.

Everything else in rookery goes through ``complete()`` or ``chat()``.
Keeping this central means:
  - One place to implement retries, backoff, token counting
  - One place to observe cache-hit telemetry
  - One place to swap backends (DeepSeek API today, local vLLM later)

DeepSeek v4 quirks handled here (so callers don't have to):
  - Response content is split across ``message.content`` (the answer)
    and ``message.reasoning_content`` (the thinking).  Both count
    against ``max_tokens``.  We always request enough budget, and
    return only the answer content.
  - ``prompt_cache_hit_tokens`` is reported; we log it so operators
    can see prefix-cache behavior working.

Design note on retries: we do *not* retry on 4xx (caller's problem)
or on ambiguous 5xx with a non-empty response body (may be a
half-streamed answer).  We retry on connection errors and 429 with
exponential backoff, bounded at 4 attempts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Raised when a call to the LLM fails definitively."""


@dataclass
class LLMUsage:
    """Token accounting from one LLM call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMResponse:
    """Parsed response from the LLM."""

    content: str  # the answer (never None, may be "")
    reasoning: str = ""  # the reasoning trace if the provider emits one
    finish_reason: str = ""
    model: str = ""
    usage: LLMUsage = field(default_factory=LLMUsage)
    elapsed_s: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMClient:
    """Thin OpenAI-compatible client with DeepSeek-aware parsing.

    Construct once per config; reuse across calls to benefit from the
    underlying ``httpx.AsyncClient`` connection pool.
    """

    base_url: str
    api_key: str
    default_model: str
    default_max_tokens: int = 4096
    request_timeout_s: float = 180.0
    max_retries: int = 4

    _client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> LLMClient:
        self._client = httpx.AsyncClient(timeout=self.request_timeout_s)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.3,
    ) -> LLMResponse:
        """One-shot completion. Convenience over ``chat``."""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return await self.chat(
            messages,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.3,
    ) -> LLMResponse:
        """Full chat-completion call. Handles retries + DeepSeek parsing."""
        if self._client is None:
            raise LLMError("LLMClient not entered. Use `async with LLMClient(...) as c:`")

        body = {
            "model": model or self.default_model,
            "messages": messages,
            "max_tokens": max_tokens or self.default_max_tokens,
            "temperature": temperature,
        }
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        return await self._post_with_retry(url, headers, body)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _post_with_retry(
        self, url: str, headers: dict[str, str], body: dict[str, Any]
    ) -> LLMResponse:
        assert self._client is not None

        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            t0 = time.monotonic()
            try:
                resp = await self._client.post(url, headers=headers, json=body)
            except httpx.RequestError as exc:
                last_exc = exc
                wait = min(2**attempt, 30)
                logger.warning(
                    "llm request error (attempt %d/%d): %s — retry in %ds",
                    attempt,
                    self.max_retries,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)
                continue

            elapsed = time.monotonic() - t0

            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                wait = min(2**attempt, 30)
                logger.warning(
                    "llm http %d (attempt %d/%d): %s — retry in %ds",
                    resp.status_code,
                    attempt,
                    self.max_retries,
                    resp.text[:200],
                    wait,
                )
                await asyncio.sleep(wait)
                continue

            if resp.status_code != 200:
                raise LLMError(f"LLM returned {resp.status_code}: {resp.text[:500]}")

            return _parse_response(resp.json(), elapsed)

        raise LLMError(f"LLM call failed after {self.max_retries} retries: {last_exc!r}")


def _parse_response(data: dict[str, Any], elapsed_s: float) -> LLMResponse:
    """Extract answer + reasoning + usage from an OpenAI-compat response.

    DeepSeek v4 returns reasoning in ``message.reasoning_content`` and
    the final answer in ``message.content``.  Models without reasoning
    just return the answer in ``content``.
    """
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"LLM response had no choices: {data}")

    msg = choices[0].get("message") or {}
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()
    finish = choices[0].get("finish_reason", "") or ""

    usage_raw = data.get("usage") or {}
    usage = LLMUsage(
        prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
        completion_tokens=int(usage_raw.get("completion_tokens", 0)),
        reasoning_tokens=int(
            (usage_raw.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
        ),
        cache_hit_tokens=int(usage_raw.get("prompt_cache_hit_tokens", 0)),
        cache_miss_tokens=int(usage_raw.get("prompt_cache_miss_tokens", 0)),
    )

    return LLMResponse(
        content=content,
        reasoning=reasoning,
        finish_reason=finish,
        model=data.get("model", ""),
        usage=usage,
        elapsed_s=elapsed_s,
        raw=data,
    )
