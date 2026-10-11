"""Send a few prompts through a running gateway and show where each one went.

Usage:
  1. start the gateway:  .venv\\Scripts\\uvicorn gateway.main:create_app --factory
  2. in another terminal: .venv\\Scripts\\python scripts\\demo.py
"""

from __future__ import annotations

import os
import sys

import httpx

GATEWAY = os.getenv("GATEWAY_URL", "http://127.0.0.1:8000")

PROMPTS = [
    "What is the capital of Japan?",
    "Write code for a Python function that checks if a string is a palindrome.",
    "Give me a synonym for 'happy'.",
    "Explain why the sky is blue, step by step.",
    "What is the capital of Japan?",  # repeated on purpose: should be a cache hit
]


def main() -> int:
    headers = {}
    if os.getenv("GATEWAY_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['GATEWAY_API_KEY']}"

    with httpx.Client(base_url=GATEWAY, headers=headers, timeout=180) as client:
        for prompt in PROMPTS:
            r = client.post(
                "/v1/chat/completions",
                json={"model": "auto", "messages": [{"role": "user", "content": prompt}], "max_tokens": 600},
            )
            h = r.headers
            print(f"\n> {prompt}")
            if r.status_code != 200:
                print(f"  ERROR {r.status_code}: {r.json().get('error', {}).get('message')}")
                for attempt in r.json().get("error", {}).get("attempts", []):
                    print(f"    - {attempt}")
                continue
            answer = r.json()["choices"][0]["message"]["content"].strip().replace("\n", " ")
            print(
                f"  route={h.get('x-gateway-route')}  alias={h.get('x-gateway-alias')}  "
                f"served_by={h.get('x-gateway-provider')}/{h.get('x-gateway-model')}  "
                f"cache={h.get('x-gateway-cache')}  est_cost=${float(h.get('x-gateway-est-cost-usd') or 0):.6f}"
            )
            print(f"  {answer[:160]}{'...' if len(answer) > 160 else ''}")

        stats = client.get("/stats").json()["total"]
        print(
            f"\nTotals: {stats['requests']} requests, {stats['cache_hits']} cache hits, "
            f"{stats['errors']} errors, p95 latency {stats['p95_latency_ms'] or 0:.0f} ms, "
            f"est. cost ${stats['est_cost_usd']:.6f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
