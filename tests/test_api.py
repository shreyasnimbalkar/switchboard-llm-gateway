"""End-to-end tests of the HTTP API against the fake upstream."""

from __future__ import annotations

import json

import pytest

from tests.conftest import chat


# ---------- basic proxying & resolution ----------


def test_alias_is_served_by_first_deployment(client, upstream):
    r = chat(client, model="fast")
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hello from alpha.test"
    assert r.headers["x-gateway-provider"] == "alpha"
    assert r.headers["x-gateway-model"] == "a-small"
    assert r.headers["x-gateway-attempts"] == "1"
    # 10 prompt tokens * $1/M + 5 completion tokens * $2/M
    assert float(r.headers["x-gateway-est-cost-usd"]) == pytest.approx(20 / 1_000_000)
    assert upstream.calls[0][1]["model"] == "a-small"


def test_auto_routes_simple_prompt_to_fast(client):
    r = chat(client, "what's the capital of France?", model="auto")
    assert r.headers["x-gateway-alias"] == "fast"
    assert r.headers["x-gateway-route"] == "rule:default"


def test_auto_routes_coding_prompt_to_smart(client):
    r = chat(client, "write code to reverse a list", model="auto")
    assert r.headers["x-gateway-alias"] == "smart"
    assert r.headers["x-gateway-route"] == "rule:keyword:code"


def test_direct_provider_model(client, upstream):
    r = chat(client, model="beta/some-model")
    assert r.status_code == 200
    assert upstream.calls[0] == ("beta.test", upstream.calls[0][1])
    assert upstream.calls[0][1]["model"] == "some-model"


def test_unknown_model_is_404(client, upstream):
    r = chat(client, model="nope")
    assert r.status_code == 404
    assert upstream.calls == []


def test_extra_openai_params_are_passed_through(client, upstream):
    chat(client, temperature=0.2, top_p=0.9, max_tokens=50)
    sent = upstream.calls[0][1]
    assert (sent["temperature"], sent["top_p"], sent["max_tokens"]) == (0.2, 0.9, 50)


def test_empty_messages_rejected(client):
    r = client.post("/v1/chat/completions", json={"model": "fast", "messages": []})
    assert r.status_code == 422


# ---------- reliability ----------


def test_transient_errors_are_retried_on_same_deployment(client, upstream):
    upstream.fail("alpha.test", 503, "timeout")
    r = chat(client)
    assert r.status_code == 200
    assert r.headers["x-gateway-provider"] == "alpha"
    assert r.headers["x-gateway-attempts"] == "3"
    assert upstream.hosts() == ["alpha.test"] * 3


def test_falls_back_when_retries_exhausted(client, upstream):
    upstream.fail("alpha.test", 500, 500, 500)
    r = chat(client)
    assert r.status_code == 200
    assert r.headers["x-gateway-provider"] == "local"
    assert upstream.hosts() == ["alpha.test"] * 3 + ["local.test"]


def test_connection_refused_falls_back_without_retry(client, upstream):
    upstream.fail("alpha.test", "refused")
    r = chat(client)
    assert r.headers["x-gateway-provider"] == "local"
    assert upstream.hosts() == ["alpha.test", "local.test"]


def test_rate_limit_puts_provider_in_cooldown(client, upstream):
    upstream.fail("alpha.test", 429)
    first = chat(client, "one")
    assert first.headers["x-gateway-provider"] == "local"
    assert client.get("/health").json()["providers"]["alpha"]["cooldown_s"] > 0

    second = chat(client, "two")
    assert second.headers["x-gateway-provider"] == "local"
    # alpha was only called once: the second request skipped it entirely
    assert upstream.hosts().count("alpha.test") == 1


def test_provider_without_key_is_skipped(make_client, upstream):
    client = make_client(env={"BETA_KEY": None})
    r = chat(client, model="smart")
    assert r.headers["x-gateway-provider"] == "alpha"
    assert "beta.test" not in upstream.hosts()
    assert client.get("/health").json()["providers"]["beta"]["configured"] is False


def test_all_failed_returns_502_with_attempt_details(client, upstream):
    upstream.fail("alpha.test", 500, 500, 500)
    upstream.fail("local.test", "refused")
    r = chat(client)
    assert r.status_code == 502
    attempts = r.json()["error"]["attempts"]
    assert len(attempts) == 4
    assert attempts[-1].startswith("local/l-tiny: unreachable")


def test_all_bad_request_returns_400(client, upstream):
    upstream.fail("alpha.test", 400)
    upstream.fail("local.test", 400)
    assert chat(client).status_code == 400


def test_no_keys_and_local_down_returns_503(make_client, upstream):
    client = make_client(env={"ALPHA_KEY": None})
    upstream.fail("local.test", "refused")
    assert chat(client).status_code == 503


def test_nothing_configured_returns_503(make_client):
    client = make_client(
        config={"aliases": {"fast": [{"provider": "alpha", "model": "a-small"}]}},
        env={"ALPHA_KEY": None},
    )
    r = chat(client)
    assert r.status_code == 503
    assert "ALPHA_KEY" in r.json()["error"]["attempts"][0]


# ---------- caching ----------


def test_identical_request_is_served_from_cache(client, upstream):
    first = chat(client, "same question")
    second = chat(client, "same question")
    assert first.headers["x-gateway-cache"] == "miss"
    assert second.headers["x-gateway-cache"] == "hit"
    assert second.json() == first.json()
    assert len(upstream.calls) == 1


def test_cache_bypass_header(client, upstream):
    chat(client, "q")
    r = client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "q"}]},
        headers={"X-Gateway-Cache": "bypass"},
    )
    assert r.headers["x-gateway-cache"] == "miss"
    assert len(upstream.calls) == 2


def test_different_params_are_different_cache_entries(client, upstream):
    chat(client, "q", temperature=0)
    chat(client, "q", temperature=1)
    assert len(upstream.calls) == 2


# ---------- streaming ----------


def _sse_events(text: str) -> list[str]:
    return [line[len("data: "):] for line in text.splitlines() if line.startswith("data: ")]


def test_streaming_passes_chunks_through(client, upstream):
    r = chat(client, stream=True)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(r.text)
    assert events[-1] == "[DONE]"
    content = "".join(
        c["delta"].get("content", "")
        for e in events[:-1]
        for c in json.loads(e)["choices"]
    )
    assert content == "hello from alpha.test"
    assert upstream.calls[0][1]["stream_options"] == {"include_usage": True}


def test_streaming_records_reported_usage(client):
    chat(client, stream=True)
    row = client.get("/logs/recent").json()[0]
    assert row["stream"] == 1
    assert row["status"] == "ok"
    assert (row["prompt_tokens"], row["completion_tokens"], row["usage_estimated"]) == (7, 3, 0)
    assert row["ttft_ms"] is not None


def test_streaming_estimates_usage_when_provider_omits_it(make_client):
    client = make_client(
        config={
            "providers": {
                "alpha": {
                    "base_url": "https://alpha.test/v1",
                    "api_key_env": "ALPHA_KEY",
                    "stream_usage": False,
                },
                "local": {"base_url": "http://local.test/v1"},
            },
            "aliases": {"fast": [{"provider": "alpha", "model": "a-small"}]},
        }
    )
    chat(client, stream=True)
    row = client.get("/logs/recent").json()[0]
    assert row["usage_estimated"] == 1
    assert row["completion_tokens"] > 0


def test_streaming_falls_back_before_first_byte(client, upstream):
    upstream.fail("alpha.test", 503, 503, 503)
    r = chat(client, stream=True)
    assert r.status_code == 200
    assert r.headers["x-gateway-provider"] == "local"
    assert "local.test" in r.text


def test_streaming_all_failed_returns_json_error(client, upstream):
    upstream.fail("alpha.test", 401)
    upstream.fail("local.test", "refused")
    r = chat(client, stream=True)
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "gateway_error"


# ---------- observability & auth ----------


def test_stats_aggregates_requests(client, upstream):
    chat(client, "a")
    chat(client, "a")  # cache hit
    upstream.fail("alpha.test", 500, 500, 500)
    upstream.fail("local.test", "refused")
    chat(client, "b")  # error
    stats = client.get("/stats").json()
    assert stats["total"]["requests"] == 3
    assert stats["total"]["errors"] == 1
    assert stats["total"]["cache_hits"] == 1
    assert stats["by_deployment"]["alpha/a-small"]["requests"] == 2
    assert stats["total"]["est_cost_usd"] == pytest.approx(20 / 1_000_000)


def test_models_endpoint_lists_aliases_and_deployments(client):
    ids = [m["id"] for m in client.get("/v1/models").json()["data"]]
    assert ids[:3] == ["auto", "fast", "smart"]
    assert "beta/b-big" in ids


def test_gateway_api_key_required_when_set(make_client):
    client = make_client(env={"GATEWAY_API_KEY": "secret"})
    assert chat(client).status_code == 401
    ok = client.post(
        "/v1/chat/completions",
        json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer secret"},
    )
    assert ok.status_code == 200
    assert client.get("/health").status_code == 200  # health stays public
