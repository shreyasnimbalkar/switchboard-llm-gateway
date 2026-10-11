# Switchboard

[![tests](https://github.com/shreyasnimbalkar/switchboard-llm-gateway/actions/workflows/tests.yml/badge.svg)](https://github.com/shreyasnimbalkar/switchboard-llm-gateway/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

**An OpenAI-compatible LLM gateway.** Switchboard sits between apps and LLM providers.
It decides **which model** handles each request, **survives provider failures**,
**caches** repeated questions, and **records cost and latency** for every call.

Built entirely on free tiers: Groq, OpenRouter, Gemini, and a local Ollama model.

```
 your app ──► /v1/chat/completions ──► Switchboard ──┬──► Groq        (fast tier)
  (any OpenAI client)                   │            ├──► OpenRouter  (smart tier)
                                        │            ├──► Gemini      (fallback)
                                        │            └──► Ollama      (local, last resort)
                                        ▼
                         route · retry · fall back · cache · log (SQLite)
```

## Features (so far)

| Feature | What it does |
|---|---|
| OpenAI-compatible API | Any OpenAI SDK works by changing `base_url`. Streaming supported. |
| Aliases + fallback chains | Ask for `fast` or `smart`; the gateway tries deployments in order until one works. |
| `auto` routing | Rules-based router sends simple prompts to `fast` and coding/long/reasoning prompts to `smart`. |
| Retries with backoff | Timeouts and 5xx errors are retried (0.5s, 1s, …) before falling back. |
| Per-model rate-limit cooldown | A model that returns 429 is skipped for as long as the provider's `Retry-After` header says (30s default), without blocking that provider's other models. |
| Rate-limit wait queue | If every option is rate-limited, the request waits for the first one to free up (up to 90s) instead of failing. |
| Streaming fallback | Fallback happens before the first byte is sent, so streams fail over too. |
| Exact cache | Identical requests are answered instantly and for free (`X-Gateway-Cache: bypass` to skip). |
| Cost + latency logging | Every request is logged with tokens, estimated cost at list price, latency, and time-to-first-token. |
| `/stats` | p50/p95 latency, error rate, cache hits, and cost per deployment, alias, and routing rule. |

Every response carries headers showing what happened:
`X-Gateway-Route`, `X-Gateway-Alias`, `X-Gateway-Provider`, `X-Gateway-Model`,
`X-Gateway-Attempts`, `X-Gateway-Queued-Ms`, `X-Gateway-Cache`, `X-Gateway-Est-Cost-USD`.

## Documentation

| Document | What's inside |
|---|---|
| [Part 1: The project in simple words](docs/01-beginners-guide.md) | What it does, the problem it solves, how a request travels step by step |
| [Part 2: How it is built](docs/02-architecture.md) | Components, request lifecycle, error handling, design decisions, testing |
| [Part 3: Setup and usage](docs/03-setup-and-usage.md) | Installing, free API keys, running, API reference, troubleshooting |
| [Part 4: Build log and roadmap](docs/04-build-log-and-roadmap.md) | What was built and why, verification results, next steps, limitations |
| [Full documentation (PDF)](docs/Switchboard-Documentation.pdf) | All four parts in one PDF |

The PDF is generated from the Markdown files: `.venv\Scripts\python scripts\build_docs.py`.

## Setup (Windows)

```powershell
git clone https://github.com/shreyasnimbalkar/switchboard-llm-gateway.git
cd switchboard-llm-gateway
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
copy .env.example .env        # then paste your free API keys into .env
ollama pull llama3.2:3b       # local model (install Ollama first: https://ollama.com/download)
.venv\Scripts\python scripts\check_providers.py   # verify every key/model works
```

## Run

```powershell
.venv\Scripts\uvicorn gateway.main:create_app --factory --reload
```

Then in another terminal:

```powershell
.venv\Scripts\python scripts\demo.py      # sends sample prompts, shows routing + cost
```

Interactive API docs: http://127.0.0.1:8000/docs · Stats: http://127.0.0.1:8000/stats

### Use it from any OpenAI client

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="unused-locally")
reply = client.chat.completions.create(
    model="auto",  # or "fast", "smart", or "groq/openai/gpt-oss-20b"
    messages=[{"role": "user", "content": "Explain recursion in one sentence."}],
)
print(reply.choices[0].message.content)
```

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```

The tests use a fake upstream provider, so they run offline with no API keys.
They cover routing, retries, fallback, per-model cooldowns, Retry-After, the wait queue,
caching, streaming, stats, log migration, and auth.

## Project layout

```
gateway/
  main.py          HTTP API (FastAPI)
  engine.py        resolve -> retry/fallback -> log  (the core)
  router.py        rules-based "auto" router
  cache.py         exact-match LRU + TTL cache
  store.py         SQLite request log + /stats aggregation
  usage.py         token estimation + cost at list price
  config.py        config.yaml + .env loading
  providers/       one adapter for every OpenAI-compatible provider
config.yaml        providers, aliases (fallback chains), prices, routing rules
docs/              documentation (Markdown + PDF)
scripts/           check_providers.py, demo.py, build_docs.py
tests/             offline test suite
```

## Roadmap

- [x] 1. Core proxy with streaming
- [x] 2. Request logging: tokens, cost, latency, TTFT
- [x] 3. Reliability: retries, backoff, fallback, per-model rate-limit cooldowns, wait queue
- [ ] 4. Semantic cache (embeddings + similarity threshold), Redis backend
- [ ] 5. Learned router (classifier) vs. rules baseline
- [ ] 6. Eval set (~200 prompts) + dashboard: quality vs. cost vs. p95 latency
- [ ] 7. Deploy + results write-up

## Notes

- **Costs are estimates.** Free-tier calls cost $0, so the gateway prices each call at
  the model's published list price (`prices` in `config.yaml`) to measure what routing
  would save in production. Verify those prices against each provider's pricing page.
- **Free tiers may use your data for training.** Only send test or synthetic prompts.
- **Model IDs change.** If `check_providers.py` reports `bad_request` for a model,
  look up the current ID in that provider's dashboard and update `config.yaml`.

## License

MIT. See [LICENSE](LICENSE).
