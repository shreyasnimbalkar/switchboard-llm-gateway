"""Rate-limit handling: per-model cooldowns, Retry-After, and the wait queue."""

from __future__ import annotations

import email.utils
import time

import pytest

from gateway.providers.openai_compat import parse_retry_after
from tests.conftest import chat

QUEUE = {"max_retries": 2, "backoff_base_s": 0, "cooldown_s": 30}


def rate_limited(seconds: int) -> tuple[int, dict[str, str]]:
    return 429, {"retry-after": str(seconds)}


# ---------- per-model cooldowns ----------


def test_rate_limited_model_does_not_block_sibling_model(client, upstream):
    # alpha/a-small (fast tier) hits its limit...
    upstream.fail("alpha.test", 429)
    assert chat(client, "one", model="fast").headers["x-gateway-provider"] == "local"

    # ...but alpha/a-big (smart tier) has its own limit and must still be tried.
    upstream.fail("beta.test", 400)
    r = chat(client, "two", model="smart")
    assert r.headers["x-gateway-provider"] == "alpha"
    assert r.headers["x-gateway-model"] == "a-big"


def test_rate_limited_model_is_skipped_while_cooling_down(client, upstream):
    upstream.fail("alpha.test", 429)
    chat(client, "one", model="fast")
    chat(client, "two", model="fast")
    sent_to_alpha = [p["model"] for host, p in upstream.calls if host == "alpha.test"]
    assert sent_to_alpha == ["a-small"]  # second request skipped it


def test_health_reports_provider_cooldown_from_its_models(client, upstream):
    upstream.fail("alpha.test", rate_limited(5))
    chat(client)
    assert client.get("/health").json()["providers"]["alpha"]["cooldown_s"] == 5.0


# ---------- Retry-After ----------


def test_retry_after_header_sets_cooldown_length(client, upstream, clock):
    upstream.fail("alpha.test", rate_limited(5))
    chat(client, "one")
    clock.now += 5  # cooldown over
    r = chat(client, "two")
    assert r.headers["x-gateway-provider"] == "alpha"


def test_default_cooldown_without_retry_after(client, upstream, clock):
    upstream.fail("alpha.test", 429)
    chat(client, "one")
    clock.now += 29
    assert chat(client, "two").headers["x-gateway-provider"] == "local"
    clock.now += 1
    assert chat(client, "three").headers["x-gateway-provider"] == "alpha"


def test_parse_retry_after_formats():
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after("1.5") == 1.5
    assert parse_retry_after("-3") == 0.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    in_30s = email.utils.formatdate(time.time() + 30, usegmt=True)
    assert parse_retry_after(in_30s) == pytest.approx(30, abs=2)


# ---------- wait queue ----------


def test_queue_waits_for_first_model_to_free_up(make_client, upstream, clock):
    client = make_client(config={"reliability": {**QUEUE, "max_queue_wait_s": 60}})
    upstream.fail("alpha.test", rate_limited(3))
    upstream.fail("local.test", rate_limited(7))

    r = chat(client)
    assert r.status_code == 200
    assert r.headers["x-gateway-provider"] == "alpha"  # freed up first (3s < 7s)
    assert r.headers["x-gateway-queued-ms"] == "3000"
    assert clock.slept == [3.0]


def test_queue_gives_up_when_wait_exceeds_budget(make_client, upstream, clock):
    client = make_client(config={"reliability": {**QUEUE, "max_queue_wait_s": 5}})
    upstream.fail("alpha.test", rate_limited(30))
    upstream.fail("local.test", rate_limited(30))

    r = chat(client)
    assert r.status_code == 429
    assert clock.slept == []  # did not wait for something that cannot arrive in time


def test_queue_disabled_by_default(client, upstream, clock):
    upstream.fail("alpha.test", rate_limited(1))
    upstream.fail("local.test", rate_limited(1))
    assert chat(client).status_code == 429
    assert clock.slept == []


def test_queue_does_not_wait_for_non_rate_limit_errors(make_client, upstream, clock):
    client = make_client(config={"reliability": {**QUEUE, "max_queue_wait_s": 60}})
    upstream.fail("alpha.test", 500, 500, 500)
    upstream.fail("local.test", "refused")
    assert chat(client).status_code == 502
    assert clock.slept == [0, 0]  # only the (zero-length) retry backoffs


def test_streaming_requests_also_queue(make_client, upstream, clock):
    client = make_client(config={"reliability": {**QUEUE, "max_queue_wait_s": 60}})
    upstream.fail("alpha.test", rate_limited(2))
    upstream.fail("local.test", rate_limited(4))

    r = chat(client, stream=True)
    assert r.status_code == 200
    assert r.headers["x-gateway-provider"] == "alpha"
    assert "alpha.test" in r.text


# ---------- logging ----------


def test_queued_time_is_logged_and_counted(make_client, upstream, clock):
    client = make_client(config={"reliability": {**QUEUE, "max_queue_wait_s": 60}})
    upstream.fail("alpha.test", rate_limited(3))
    upstream.fail("local.test", rate_limited(7))
    chat(client)
    assert client.get("/logs/recent").json()[0]["queued_ms"] == 3000
    assert client.get("/stats").json()["total"]["queued_requests"] == 1


def test_old_log_files_are_migrated(tmp_path):
    import sqlite3

    from gateway.store import RequestLog

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, ts REAL)")  # pre-queue schema
    conn.close()
    RequestLog(str(db)).close()
    columns = {row[1] for row in sqlite3.connect(db).execute("PRAGMA table_info(requests)")}
    assert "queued_ms" in columns
