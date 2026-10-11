"""Unit tests for the small pure pieces: router, cache, usage, percentiles."""

from __future__ import annotations

import pytest

from gateway.cache import ExactCache, cache_key
from gateway.config import Deployment, Price, RoutingConfig
from gateway.router import RulesRouter
from gateway.store import percentile
from gateway.usage import estimate_cost, estimate_tokens, message_text


@pytest.fixture
def router() -> RulesRouter:
    return RulesRouter(
        RoutingConfig(long_prompt_chars=100, long_conversation_turns=2, smart_keywords=["code"])
    )


def user(text: str) -> dict:
    return {"role": "user", "content": text}


def test_router_default_is_fast(router):
    assert router.route([user("hello there")]) == ("fast", "rule:default")


def test_router_keyword_is_whole_word(router):
    assert router.route([user("decode this base64")])[0] == "fast"
    assert router.route([user("review my Code please")])[0] == "smart"


def test_router_code_block(router):
    assert router.route([user("what does ```x = 1``` do")]) == ("smart", "rule:code_block")


def test_router_long_prompt(router):
    assert router.route([user("a" * 101)]) == ("smart", "rule:long_prompt")


def test_router_long_conversation(router):
    msgs = [user("hi"), {"role": "assistant", "content": "hey"}] * 3
    assert router.route(msgs) == ("smart", "rule:long_conversation")


def test_router_only_looks_at_last_user_message_for_keywords(router):
    msgs = [user("write code"), {"role": "assistant", "content": "done"}, user("thanks")]
    assert router.route(msgs)[0] == "fast"


def test_cache_ttl_and_lru():
    now = [0.0]
    cache = ExactCache(ttl_s=10, max_entries=2, clock=lambda: now[0])
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")  # "a" is now most recently used
    cache.set("c", 3)  # evicts "b"
    assert cache.get("b") is None
    assert cache.get("a") == 1
    now[0] = 11
    assert cache.get("a") is None


def test_cache_key_ignores_stream_flag():
    base = {"model": "fast", "messages": [user("x")]}
    assert cache_key({**base, "stream": True}) == cache_key(base)
    assert cache_key({**base, "temperature": 1}) != cache_key(base)


def test_message_text_handles_multipart_content():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image_url"}]}]
    assert message_text(msgs) == "hi\n"


def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_estimate_cost():
    prices = {"p/m": Price(input_per_m=1.0, output_per_m=4.0)}
    assert estimate_cost(prices, Deployment("p", "m"), 1_000_000, 500_000) == pytest.approx(3.0)
    assert estimate_cost(prices, Deployment("p", "other"), 10, 10) is None


def test_percentile():
    assert percentile([], 95) is None
    assert percentile([5.0], 95) == 5.0
    values = [float(i) for i in range(1, 101)]
    assert percentile(values, 50) == 50.0
    assert percentile(values, 95) == 95.0
