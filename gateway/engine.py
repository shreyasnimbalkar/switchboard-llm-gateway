"""The gateway's core: resolve a model -> try deployments with retries/fallback -> log.

Request flow:
    client asks for "auto" | an alias ("fast", "smart") | "provider/model"
      -> resolve() turns that into a fallback chain of deployments
      -> exact cache check (non-streaming only)
      -> _call_with_fallback(): for each deployment, retry transient errors with
         exponential backoff; on rate limits, cool the provider down and move on
      -> record latency, tokens and estimated cost in SQLite
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

import httpx

from gateway.cache import ExactCache, cache_key
from gateway.config import Deployment, Settings
from gateway.providers import OpenAICompatProvider, ProviderError
from gateway.router import RulesRouter
from gateway.store import RequestLog, RequestRecord
from gateway.usage import estimate_cost, estimate_tokens, message_text

T = TypeVar("T")


class GatewayError(Exception):
    def __init__(self, status: int, message: str, errors: list[str] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.errors = errors or []


@dataclass
class Resolution:
    requested_model: str
    alias: str | None
    routed_by: str
    chain: list[Deployment]


@dataclass
class CallOutcome:
    deployment: Deployment
    attempts: int
    errors: list[str] = field(default_factory=list)
    queued_s: float = 0.0  # time spent waiting for rate limits to clear


@dataclass
class StreamSession:
    resolution: Resolution
    outcome: CallOutcome
    response: httpx.Response
    payload: dict[str, Any]
    started_at: float

    @property
    def headers(self) -> dict[str, str]:
        return _meta_headers(self.resolution, self.outcome, cache="miss")


def _meta_headers(
    res: Resolution, outcome: CallOutcome | None, cache: str, cost: float | None = None
) -> dict[str, str]:
    headers = {"X-Gateway-Route": res.routed_by, "X-Gateway-Cache": cache}
    if res.alias:
        headers["X-Gateway-Alias"] = res.alias
    if outcome:
        headers["X-Gateway-Provider"] = outcome.deployment.provider
        headers["X-Gateway-Model"] = outcome.deployment.model
        headers["X-Gateway-Attempts"] = str(outcome.attempts)
        if outcome.queued_s:
            headers["X-Gateway-Queued-Ms"] = f"{outcome.queued_s * 1000:.0f}"
    if cost is not None:
        headers["X-Gateway-Est-Cost-USD"] = f"{cost:.8f}"
    return headers


class Gateway:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        log: RequestLog,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.settings = settings
        self.log = log
        self.clock = clock
        self.sleep = sleep
        self.providers = {
            name: OpenAICompatProvider(cfg, client) for name, cfg in settings.providers.items()
        }
        self.router = RulesRouter(settings.routing)
        self.cache = ExactCache(settings.cache_ttl_s, settings.cache_max_entries, clock=clock)
        self._cooldown_until: dict[str, float] = {}

    # ---------- resolution ----------

    def resolve(self, model: str, messages: list[dict[str, Any]]) -> Resolution:
        aliases = self.settings.aliases
        if model == "auto":
            alias, reason = self.router.route(messages)
            return Resolution(model, alias, reason, aliases[alias])
        if model in aliases:
            return Resolution(model, model, "explicit", aliases[model])
        provider, sep, name = model.partition("/")
        if sep and provider in self.providers and name:
            return Resolution(model, None, "direct", [Deployment(provider, name)])
        raise GatewayError(
            404,
            f"unknown model '{model}'. Use 'auto', an alias {sorted(aliases)}, "
            f"or 'provider/model' with provider in {sorted(self.providers)}",
        )

    # ---------- reliability ----------

    def cooling_down(self, name: str) -> float:
        """Seconds left in a cooldown (0 if available).

        `name` is a deployment key ("groq/openai/gpt-oss-20b") or a provider name
        ("groq"); for a provider, the longest cooldown among its models is returned.
        Limits are tracked per model because providers like Groq meter each model
        separately: one model hitting its limit must not block its siblings.
        """
        now = self.clock()
        remaining = [
            until - now
            for key, until in self._cooldown_until.items()
            if key == name or key.startswith(name + "/")
        ]
        return max([0.0, *remaining])

    def _start_cooldown(self, dep: Deployment, error: ProviderError) -> None:
        # Prefer the provider's own "retry after N seconds" hint; for per-minute
        # token limits it is usually far shorter than the default cooldown.
        wait = error.retry_after if error.retry_after is not None else self.settings.cooldown_s
        self._cooldown_until[dep.key] = self.clock() + max(wait, 0.0)

    async def _call_with_fallback(
        self,
        res: Resolution,
        payload: dict[str, Any],
        call: Callable[[OpenAICompatProvider, dict[str, Any]], Awaitable[T]],
    ) -> tuple[T, CallOutcome]:
        """Try the chain; if every deployment is rate-limited, queue (wait for the
        first cooldown to end) for up to `max_queue_wait_s` before giving up."""
        deadline = self.clock() + self.settings.max_queue_wait_s
        queued_s = 0.0
        while True:
            try:
                result, outcome = await self._try_chain(res, payload, call)
                outcome.queued_s = queued_s
                return result, outcome
            except GatewayError as e:
                if e.status != 429:
                    raise
                waits = [self.cooling_down(d.key) for d in res.chain]
                wait = min((w for w in waits if w > 0), default=1.0)
                if self.clock() + wait > deadline:
                    raise
                await self.sleep(wait)
                queued_s += wait

    async def _try_chain(
        self,
        res: Resolution,
        payload: dict[str, Any],
        call: Callable[[OpenAICompatProvider, dict[str, Any]], Awaitable[T]],
    ) -> tuple[T, CallOutcome]:
        errors: list[str] = []
        kinds: list[str] = []
        attempts = 0
        max_retries = self.settings.max_retries

        for dep in res.chain:
            provider = self.providers[dep.provider]
            if not provider.cfg.configured:
                errors.append(f"{dep.key}: skipped (no {provider.cfg.api_key_env} in .env)")
                kinds.append("unconfigured")
                continue
            if self.cooling_down(dep.key):
                errors.append(f"{dep.key}: skipped (rate-limited, cooling down)")
                kinds.append("rate_limited")
                continue

            body = dict(payload, model=dep.model)
            for retry in range(max_retries + 1):
                attempts += 1
                try:
                    result = await call(provider, body)
                    return result, CallOutcome(dep, attempts, errors)
                except ProviderError as e:
                    errors.append(f"{dep.key}: {e.kind} ({e.status}) {e.message[:200]}")
                    kinds.append(e.kind)
                    if e.kind == "rate_limited":
                        self._start_cooldown(dep, e)
                    if not e.retryable or retry == max_retries:
                        break
                    await self.sleep(self.settings.backoff_base_s * (2**retry))

        raise GatewayError(_final_status(kinds), "all deployments failed", errors)

    # ---------- non-streaming ----------

    async def chat(
        self, payload: dict[str, Any], use_cache: bool = True
    ) -> tuple[dict[str, Any], dict[str, str]]:
        started = self.clock()
        res = self.resolve(payload["model"], payload["messages"])
        key = cache_key(payload)

        if use_cache:
            cached = self.cache.get(key)
            if cached is not None:
                data, dep = cached
                self.log.record(
                    RequestRecord(
                        requested_model=res.requested_model, alias=res.alias,
                        routed_by=res.routed_by, provider=dep.provider, model=dep.model,
                        stream=False, cache_hit=True, status="ok", attempts=0,
                        latency_ms=(self.clock() - started) * 1000, est_cost_usd=0.0,
                    )
                )
                outcome = CallOutcome(dep, attempts=0)
                return data, _meta_headers(res, outcome, cache="hit", cost=0.0)

        try:
            data, outcome = await self._call_with_fallback(
                res, payload, lambda p, body: p.complete(body)
            )
        except GatewayError as e:
            self._record_error(res, stream=False, started=started, error=e)
            raise

        prompt_tokens, completion_tokens, estimated = _usage_from_response(payload, data)
        cost = estimate_cost(self.settings.prices, outcome.deployment, prompt_tokens, completion_tokens)
        self.log.record(
            RequestRecord(
                requested_model=res.requested_model, alias=res.alias, routed_by=res.routed_by,
                provider=outcome.deployment.provider, model=outcome.deployment.model,
                stream=False, cache_hit=False, status="ok", attempts=outcome.attempts,
                queued_ms=outcome.queued_s * 1000 or None,
                latency_ms=(self.clock() - started) * 1000,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                usage_estimated=estimated, est_cost_usd=cost,
                error="; ".join(outcome.errors) or None,
            )
        )
        if use_cache and data.get("choices"):
            self.cache.set(key, (data, outcome.deployment))
        return data, _meta_headers(res, outcome, cache="miss", cost=cost)

    # ---------- streaming ----------

    async def open_stream(self, payload: dict[str, Any]) -> StreamSession:
        """Find a deployment that accepts the stream. Fallback is only possible here,
        before any bytes have been sent to the client."""
        started = self.clock()
        res = self.resolve(payload["model"], payload["messages"])
        try:
            response, outcome = await self._call_with_fallback(
                res, payload, lambda p, body: p.open_stream(body)
            )
        except GatewayError as e:
            self._record_error(res, stream=True, started=started, error=e)
            raise
        return StreamSession(res, outcome, response, payload, started)

    async def relay(self, session: StreamSession) -> AsyncIterator[bytes]:
        """Pass the provider's SSE stream through to the client, measuring as we go."""
        res, outcome = session.resolution, session.outcome
        ttft_ms: float | None = None
        usage: dict[str, Any] | None = None
        output_parts: list[str] = []
        status, error = "error", "client disconnected"

        try:
            async for line in session.response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content") or ""
                    if piece:
                        if ttft_ms is None:
                            ttft_ms = (self.clock() - session.started_at) * 1000
                        output_parts.append(piece)
                yield f"data: {data}\n\n".encode()
            yield b"data: [DONE]\n\n"
            status, error = "ok", None
        except httpx.HTTPError as e:
            # The provider died mid-stream. Too late to fall back; tell the client.
            error = f"stream interrupted: {e!r}"
            payload = {"error": {"message": error, "type": "gateway_error"}}
            yield f"data: {json.dumps(payload)}\n\n".encode()
        finally:
            await session.response.aclose()
            if usage and usage.get("prompt_tokens") is not None:
                prompt_tokens = int(usage["prompt_tokens"])
                completion_tokens = int(usage.get("completion_tokens") or 0)
                estimated = False
            else:
                prompt_tokens = estimate_tokens(message_text(session.payload["messages"]))
                completion_tokens = estimate_tokens("".join(output_parts))
                estimated = True
            self.log.record(
                RequestRecord(
                    requested_model=res.requested_model, alias=res.alias,
                    routed_by=res.routed_by, provider=outcome.deployment.provider,
                    model=outcome.deployment.model, stream=True, cache_hit=False,
                    status=status, attempts=outcome.attempts,
                    queued_ms=outcome.queued_s * 1000 or None,
                    latency_ms=(self.clock() - session.started_at) * 1000, ttft_ms=ttft_ms,
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                    usage_estimated=estimated,
                    est_cost_usd=estimate_cost(
                        self.settings.prices, outcome.deployment, prompt_tokens, completion_tokens
                    ),
                    error=error or ("; ".join(outcome.errors) or None),
                )
            )

    # ---------- helpers ----------

    def _record_error(
        self, res: Resolution, stream: bool, started: float, error: GatewayError
    ) -> None:
        self.log.record(
            RequestRecord(
                requested_model=res.requested_model, alias=res.alias, routed_by=res.routed_by,
                provider=None, model=None, stream=stream, cache_hit=False, status="error",
                attempts=sum(1 for e in error.errors if "skipped" not in e),
                latency_ms=(self.clock() - started) * 1000,
                error="; ".join(error.errors)[:2000],
            )
        )


def _final_status(kinds: list[str]) -> int:
    """Pick the most useful HTTP status when every deployment failed."""
    if not kinds or all(k == "unconfigured" for k in kinds):
        return 503  # nothing to call: no keys set and/or no local model
    real = [k for k in kinds if k != "unconfigured"]
    if all(k == "unreachable" for k in real):
        return 503  # e.g. no keys set and Ollama isn't running
    if all(k == "bad_request" for k in real):
        return 400
    if all(k == "rate_limited" for k in real):
        return 429
    return 502


def _usage_from_response(
    payload: dict[str, Any], data: dict[str, Any]
) -> tuple[int, int, bool]:
    usage = data.get("usage") or {}
    if usage.get("prompt_tokens") is not None and usage.get("completion_tokens") is not None:
        return int(usage["prompt_tokens"]), int(usage["completion_tokens"]), False
    output = "".join(
        ((c.get("message") or {}).get("content") or "") for c in data.get("choices") or []
    )
    return (
        estimate_tokens(message_text(payload["messages"])),
        estimate_tokens(output),
        True,
    )
