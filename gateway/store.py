"""SQLite request log: one row per request, plus aggregate stats for /stats."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL    NOT NULL,
    requested_model   TEXT    NOT NULL,
    alias             TEXT,
    routed_by         TEXT,
    provider          TEXT,
    model             TEXT,
    stream            INTEGER NOT NULL,
    cache_hit         INTEGER NOT NULL,
    status            TEXT    NOT NULL,   -- "ok" or "error"
    attempts          INTEGER NOT NULL,
    latency_ms        REAL    NOT NULL,
    ttft_ms           REAL,               -- time to first token (streaming only)
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    usage_estimated   INTEGER NOT NULL,   -- 1 if tokens were estimated, not reported
    est_cost_usd      REAL,
    error             TEXT
);
"""


@dataclass
class RequestRecord:
    requested_model: str
    alias: str | None
    routed_by: str | None
    provider: str | None
    model: str | None
    stream: bool
    cache_hit: bool
    status: str
    attempts: int
    latency_ms: float
    ttft_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    usage_estimated: bool = False
    est_cost_usd: float | None = None
    error: str | None = None


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile, e.g. pct=95 -> p95."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, round(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


class RequestLog:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def record(self, rec: RequestRecord) -> None:
        row = asdict(rec)
        row["ts"] = time.time()
        cols = ", ".join(row)
        marks = ", ".join(f":{c}" for c in row)
        with self._lock:
            self._conn.execute(f"INSERT INTO requests ({cols}) VALUES ({marks})", row)
            self._conn.commit()

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            rows = [dict(r) for r in self._conn.execute("SELECT * FROM requests").fetchall()]

        def summarize(group: list[dict[str, Any]]) -> dict[str, Any]:
            ok = [r for r in group if r["status"] == "ok"]
            live = [r["latency_ms"] for r in ok if not r["cache_hit"]]
            ttfts = [r["ttft_ms"] for r in ok if r["ttft_ms"] is not None]
            return {
                "requests": len(group),
                "errors": len(group) - len(ok),
                "cache_hits": sum(1 for r in group if r["cache_hit"]),
                "p50_latency_ms": percentile(live, 50),
                "p95_latency_ms": percentile(live, 95),
                "p50_ttft_ms": percentile(ttfts, 50),
                "prompt_tokens": sum(r["prompt_tokens"] or 0 for r in ok),
                "completion_tokens": sum(r["completion_tokens"] or 0 for r in ok),
                "est_cost_usd": round(sum(r["est_cost_usd"] or 0 for r in ok), 6),
            }

        def grouped(key: str) -> dict[str, Any]:
            groups: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                groups.setdefault(str(r[key]), []).append(r)
            return {k: summarize(v) for k, v in sorted(groups.items())}

        by_deployment: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            if r["provider"]:
                by_deployment.setdefault(f"{r['provider']}/{r['model']}", []).append(r)

        return {
            "total": summarize(rows),
            "by_deployment": {k: summarize(v) for k, v in sorted(by_deployment.items())},
            "by_alias": grouped("alias"),
            "by_route_reason": grouped("routed_by"),
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
