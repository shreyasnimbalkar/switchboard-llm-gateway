"""Test fixtures: a scriptable fake upstream so tests need no API keys or network."""

from __future__ import annotations

import copy
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import settings_from_dict
from gateway.main import create_app

BASE_CONFIG: dict[str, Any] = {
    "providers": {
        "alpha": {"base_url": "https://alpha.test/v1", "api_key_env": "ALPHA_KEY"},
        "beta": {"base_url": "https://beta.test/v1", "api_key_env": "BETA_KEY"},
        "local": {"base_url": "http://local.test/v1"},
    },
    "aliases": {
        "fast": [
            {"provider": "alpha", "model": "a-small"},
            {"provider": "local", "model": "l-tiny"},
        ],
        "smart": [
            {"provider": "beta", "model": "b-big"},
            {"provider": "alpha", "model": "a-big"},
            {"provider": "local", "model": "l-tiny"},
        ],
    },
    "prices": {
        "alpha/a-small": {"input": 1.0, "output": 2.0},
        "beta/b-big": {"input": 10.0, "output": 20.0},
    },
    "routing": {
        "long_prompt_chars": 200,
        "long_conversation_turns": 3,
        "smart_keywords": ["code", "step by step"],
    },
    "reliability": {"max_retries": 2, "backoff_base_s": 0, "cooldown_s": 30},
    "db_path": ":memory:",
}


class FakeUpstream:
    """Pretends to be every provider. Script failures per host with `fail()`;
    anything unscripted succeeds."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._script: dict[str, list[Any]] = {}

    def fail(self, host: str, *outcomes: Any) -> None:
        """Queue outcomes for the next calls to `host`: an HTTP status, "timeout", "refused",
        or (status, headers) to send response headers such as Retry-After."""
        self._script.setdefault(host, []).extend(outcomes)

    def hosts(self) -> list[str]:
        return [h for h, _ in self.calls]

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        payload = json.loads(request.content)
        self.calls.append((host, payload))

        queue = self._script.get(host)
        outcome = queue.pop(0) if queue else 200
        if outcome == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if outcome == "refused":
            raise httpx.ConnectError("connection refused", request=request)
        headers: dict[str, str] = {}
        if isinstance(outcome, tuple):
            outcome, headers = outcome
        if outcome != 200:
            return httpx.Response(
                outcome, json={"error": {"message": f"upstream said {outcome}"}}, headers=headers
            )

        if payload.get("stream"):
            chunks: list[dict[str, Any]] = [
                {"choices": [{"index": 0, "delta": {"content": word}}]}
                for word in ["hello ", "from ", host]
            ]
            if (payload.get("stream_options") or {}).get("include_usage"):
                chunks.append({"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
            body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(
                200, content=body.encode(), headers={"content-type": "text/event-stream"}
            )

        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": f"hello from {host}"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


class FakeClock:
    """A clock that only moves when the gateway sleeps, so waits are instant but exact."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_client(upstream, clock, monkeypatch):
    """Build a TestClient. `config` overrides top-level config keys; `env` sets env vars."""
    clients: list[TestClient] = []

    def _make(config: dict[str, Any] | None = None, env: dict[str, str | None] | None = None):
        defaults: dict[str, str | None] = {"ALPHA_KEY": "a", "BETA_KEY": "b", "GATEWAY_API_KEY": None}
        for name, value in {**defaults, **(env or {})}.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        raw = copy.deepcopy(BASE_CONFIG)
        raw.update(config or {})
        app = create_app(
            settings_from_dict(raw),
            transport=httpx.MockTransport(upstream.handler),
            clock=clock,
            sleep=clock.sleep,
        )
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


def chat(client: TestClient, content: str = "hi", model: str = "fast", **extra: Any):
    return client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": content}], **extra},
    )
