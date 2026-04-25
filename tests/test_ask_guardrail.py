"""The ask guardrail must be prepended to every question.

Regression test for the Q4-style failure mode: clone confidently
answers when the relevant file was outside its scope. The guardrail
text asks the model to check its corpus manifest first.
"""

from __future__ import annotations

import httpx
import pytest

from rookery.clones.model import Clone
from rookery.clones.persist import persist_clone_version
from rookery.config import Config
from rookery.datalake.store import BlobStore, IndexDb
from rookery.operator.ask import ask_clone


@pytest.fixture
def config_for(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOKERY_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("ROOKERY_API_KEY", "test-key")
    monkeypatch.setenv("ROOKERY_DATALAKE_DIR", str(tmp_path))
    return Config.from_env()


async def test_ask_prepends_scope_guardrail(config_for, tmp_path, monkeypatch):
    # Seed a clone in the datalake
    blobs = BlobStore(tmp_path)
    index = IndexDb.open(tmp_path / "index.duckdb")
    clone = Clone(id="test_keeper", repo_id="repo_x", role="test")
    v = clone.new_version(role_prompt="You are the Test Keeper.")
    v.append_segment(kind="corpus", text="# Files read: a.py\n", origin="t")
    v.append_segment(kind="residue", text="a.py has a bug.\n", origin="t")
    persist_clone_version(
        clone=clone,
        version=v,
        run_id="r1",
        blobs=blobs,
        index=index,
        size_tokens=100,
    )
    index.close()

    # Capture the outbound request
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "model": "deepseek-v4-flash",
            },
        )

    transport = httpx.MockTransport(_handler)

    # Swap the LLMClient's transport
    from rookery.llm import LLMClient

    orig_enter = LLMClient.__aenter__

    async def patched_enter(self):
        result = await orig_enter(self)
        await self._client.aclose()
        self._client = httpx.AsyncClient(transport=transport)
        return result

    monkeypatch.setattr(LLMClient, "__aenter__", patched_enter)

    result = await ask_clone(
        config=config_for,
        repo_id="repo_x",
        clone_id="test_keeper",
        question="does a.py have a bug?",
    )

    # The user-facing result is unchanged — the guardrail is internal
    assert result.answer == "ok"
    # But the body sent to the model MUST contain the guardrail text
    body = captured["body"]
    assert "does a.py have a bug?" in body
    assert "Before answering, check the corpus manifest" in body
    assert "Do not extrapolate from absence" in body
