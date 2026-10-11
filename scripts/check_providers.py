"""Check that every deployment in config.yaml actually works with your keys.

Usage:  .venv\\Scripts\\python scripts\\check_providers.py

Sends one tiny prompt to each provider/model directly (bypassing routing and
fallback) and prints a status table. Run this after adding keys to .env.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.config import Deployment, load_settings  # noqa: E402
from gateway.providers import OpenAICompatProvider, ProviderError  # noqa: E402

HINTS = {
    "unconfigured": "add the key to .env",
    "unreachable": "is it running? (for Ollama: start the app, then `ollama pull <model>`)",
    "auth": "key is wrong or revoked - create a new one",
    "rate_limited": "free-tier limit hit - wait a minute",
    "bad_request": "model ID may be outdated - check the provider's model list",
    "transient": "provider error or timeout - try again",
}


async def check(provider: OpenAICompatProvider, dep: Deployment) -> tuple[str, str, str]:
    payload = {
        "model": dep.model,
        "messages": [{"role": "user", "content": "Reply with just the word OK."}],
        "max_tokens": 300,  # reasoning models spend tokens thinking first
    }
    started = time.perf_counter()
    try:
        data = await provider.complete(payload)
    except ProviderError as e:
        detail = HINTS.get(e.kind, "")
        if e.kind in ("bad_request", "auth", "transient"):
            detail += f"\n{'':>8}provider said: {e.message[:160]}"
        return "FAIL", e.kind, detail
    ms = (time.perf_counter() - started) * 1000
    reply = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return "OK", f"{ms:.0f} ms", repr(reply.strip()[:30])


async def main() -> int:
    settings = load_settings()
    deployments = sorted({d for chain in settings.aliases.values() for d in chain}, key=lambda d: d.key)
    async with httpx.AsyncClient() as client:
        providers = {n: OpenAICompatProvider(c, client) for n, c in settings.providers.items()}
        results = await asyncio.gather(*(check(providers[d.provider], d) for d in deployments))

    width = max(len(d.key) for d in deployments)
    print(f"\n{'deployment'.ljust(width)}  status  detail")
    print("-" * (width + 50))
    for dep, (status, detail, extra) in zip(deployments, results):
        print(f"{dep.key.ljust(width)}  {status.ljust(6)}  {detail}  {extra}")

    working = sum(1 for s, _, _ in results if s == "OK")
    print(f"\n{working}/{len(results)} deployments working.")
    working_keys = {d.key for d, (s, _, _) in zip(deployments, results) if s == "OK"}
    for alias, chain in settings.aliases.items():
        first = next((d.key for d in chain if d.key in working_keys), None)
        print(f"  alias '{alias}': {'served by ' + first if first else 'NO working deployment'}")
    return 0 if working else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
