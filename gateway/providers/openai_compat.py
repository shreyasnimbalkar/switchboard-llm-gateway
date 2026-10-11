"""Adapter for any provider with an OpenAI-compatible /chat/completions endpoint.

Groq, OpenRouter, Gemini and Ollama all speak this format, so one class covers them.
"""

from __future__ import annotations

from typing import Any

import httpx

from gateway.config import ProviderConfig


class ProviderError(Exception):
    """A provider call failed. `kind` tells the gateway how to react."""

    # kind values:
    #   "transient"    timeout / network blip / 5xx      -> retry same deployment
    #   "unreachable"  connection refused (e.g. Ollama not running) -> fall back, no retry
    #   "rate_limited" 429                               -> cool the provider down, fall back
    #   "auth"         401 / 403                         -> fall back (key missing or wrong)
    #   "bad_request"  400 / 404 / 422 etc.              -> fall back, no retry
    #   "unconfigured" no API key set                    -> skip
    def __init__(self, provider: str, kind: str, message: str, status: int | None = None):
        super().__init__(f"{provider}: {kind} ({status}): {message}")
        self.provider = provider
        self.kind = kind
        self.status = status
        self.message = message

    @property
    def retryable(self) -> bool:
        return self.kind == "transient"


def _classify(status: int) -> str:
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "auth"
    if status >= 500:
        return "transient"
    return "bad_request"


class OpenAICompatProvider:
    def __init__(self, cfg: ProviderConfig, client: httpx.AsyncClient):
        self.cfg = cfg
        self.client = client

    @property
    def name(self) -> str:
        return self.cfg.name

    def _url(self) -> str:
        return self.cfg.base_url.rstrip("/") + "/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.cfg.extra_headers}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        return headers

    def _check_configured(self) -> None:
        if not self.cfg.configured:
            raise ProviderError(
                self.name, "unconfigured", f"set {self.cfg.api_key_env} in .env to enable"
            )

    def _error_from_response(self, status: int, body: bytes) -> ProviderError:
        text = body.decode("utf-8", errors="replace")[:500]
        return ProviderError(self.name, _classify(status), text, status)

    def _transport_error(self, e: httpx.TransportError) -> ProviderError:
        if isinstance(e, httpx.TimeoutException):
            return ProviderError(self.name, "transient", f"timeout: {e!r}")
        if isinstance(e, httpx.ConnectError):
            return ProviderError(self.name, "unreachable", f"cannot connect: {e!r}")
        return ProviderError(self.name, "transient", f"network error: {e!r}")

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._check_configured()
        try:
            resp = await self.client.post(
                self._url(), json=payload, headers=self._headers(), timeout=self.cfg.timeout_s
            )
        except httpx.TransportError as e:
            raise self._transport_error(e) from e
        if resp.status_code != 200:
            raise self._error_from_response(resp.status_code, resp.content)
        return resp.json()

    async def open_stream(self, payload: dict[str, Any]) -> httpx.Response:
        """Start a streaming request and return the open response once we know it succeeded.

        Checking the status *before* handing the stream to the client is what lets the
        gateway fall back to another provider for streaming requests too.
        The caller must `await resp.aclose()` when done.
        """
        self._check_configured()
        payload = dict(payload, stream=True)
        if self.cfg.stream_usage:
            payload["stream_options"] = {"include_usage": True}
        request = self.client.build_request(
            "POST", self._url(), json=payload, headers=self._headers(), timeout=self.cfg.timeout_s
        )
        try:
            resp = await self.client.send(request, stream=True)
        except httpx.TransportError as e:
            raise self._transport_error(e) from e
        if resp.status_code != 200:
            body = await resp.aread()
            await resp.aclose()
            raise self._error_from_response(resp.status_code, body)
        return resp
