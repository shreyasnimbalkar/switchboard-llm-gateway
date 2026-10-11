"""HTTP API. OpenAI-compatible, so any OpenAI SDK/client can point at this gateway.

Run:  uvicorn gateway.main:create_app --factory --reload
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from gateway.config import Settings, load_settings
from gateway.engine import Gateway, GatewayError
from gateway.store import RequestLog


class ChatRequest(BaseModel):
    """OpenAI chat request. Unknown fields (top_p, tools, ...) are passed through."""

    model_config = ConfigDict(extra="allow")

    model: str = "auto"
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False


def create_app(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = httpx.AsyncClient(transport=transport)
        log = RequestLog(settings.db_path)
        app.state.gateway = Gateway(settings, client, log, sleep=sleep or asyncio.sleep)
        try:
            yield
        finally:
            await client.aclose()
            log.close()

    app = FastAPI(title="Switchboard", version="0.1.0", lifespan=lifespan)

    async def require_key(authorization: str | None = Header(default=None)) -> None:
        if settings.gateway_api_key and authorization != f"Bearer {settings.gateway_api_key}":
            raise HTTPException(status_code=401, detail="invalid or missing gateway API key")

    def gw(request: Request) -> Gateway:
        return request.app.state.gateway

    @app.post("/v1/chat/completions", dependencies=[Depends(require_key)])
    async def chat_completions(body: ChatRequest, request: Request):
        gateway = gw(request)
        payload = body.model_dump(exclude_none=True)
        use_cache = request.headers.get("x-gateway-cache", "").lower() != "bypass"
        try:
            if body.stream:
                session = await gateway.open_stream(payload)
                return StreamingResponse(
                    gateway.relay(session), media_type="text/event-stream", headers=session.headers
                )
            data, headers = await gateway.chat(payload, use_cache=use_cache)
            return JSONResponse(data, headers=headers)
        except GatewayError as e:
            return JSONResponse(
                {"error": {"message": e.message, "type": "gateway_error", "attempts": e.errors}},
                status_code=e.status,
            )

    @app.get("/v1/models", dependencies=[Depends(require_key)])
    async def list_models(request: Request):
        gateway = gw(request)
        ids = ["auto", *gateway.settings.aliases]
        for chain in gateway.settings.aliases.values():
            ids.extend(d.key for d in chain if d.key not in ids)
        return {"object": "list", "data": [{"id": i, "object": "model", "owned_by": "gateway"} for i in ids]}

    @app.get("/health")
    async def health(request: Request):
        gateway = gw(request)
        return {
            "status": "ok",
            "providers": {
                name: {
                    "configured": p.cfg.configured,
                    "cooldown_s": round(gateway.cooling_down(name), 1),
                }
                for name, p in gateway.providers.items()
            },
            "cache_entries": len(gateway.cache),
        }

    @app.get("/stats", dependencies=[Depends(require_key)])
    async def stats(request: Request):
        return gw(request).log.stats()

    @app.get("/logs/recent", dependencies=[Depends(require_key)])
    async def recent_logs(request: Request, limit: int = 50):
        return gw(request).log.recent(min(max(limit, 1), 500))

    return app
