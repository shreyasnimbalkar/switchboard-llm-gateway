# Switchboard

[![tests](https://github.com/shreyasnimbalkar/switchboard-llm-gateway/actions/workflows/tests.yml/badge.svg)](https://github.com/shreyasnimbalkar/switchboard-llm-gateway/actions/workflows/tests.yml)

Switchboard is a small LLM gateway with an OpenAI-compatible API. Apps send chat
requests to it instead of calling providers directly. It picks a model, retries or
falls back when a provider fails, caches repeated requests, and logs the cost and
latency of every call.

I run it on free tiers (Groq, OpenRouter, Gemini) plus a local Ollama model. Free
tiers rate-limit constantly, which is why a lot of the code is about 429s.

## Features

- `/v1/chat/completions` in the OpenAI format, including streaming. The OpenAI SDK
  works by pointing `base_url` at the gateway.
- Model aliases (`fast`, `smart`, `local`) that map to ordered fallback chains in
  `config.yaml`. Asking for `auto` picks an alias using simple keyword and length rules.
- Retries for timeouts and 5xx errors with exponential backoff, then fallback to the
  next model in the chain.
- Per-model rate-limit handling. A model that returns 429 is skipped for as long as its
  `Retry-After` header says. If every model in the chain is rate-limited, the request
  waits for the first one to free up (90 s by default) instead of failing.
- Failover for streaming requests, as long as nothing has been sent to the client yet.
- An in-memory cache for identical requests.
- A SQLite log of tokens, estimated cost, latency, time to first token and queue time
  for every request, summarized at `/stats`.

Responses carry `X-Gateway-*` headers saying which model answered, how many attempts
it took, whether it hit the cache, how long it queued and what it cost.

See [DESIGN.md](DESIGN.md) for how requests flow through the gateway and why it
handles failures the way it does.

## Quick start

```powershell
git clone https://github.com/shreyasnimbalkar/switchboard-llm-gateway.git
cd switchboard-llm-gateway
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
copy .env.example .env               # add your API keys
ollama pull llama3.2:3b              # optional local fallback model
.venv\Scripts\python scripts\check_providers.py
.venv\Scripts\uvicorn gateway.main:create_app --factory
```

On macOS or Linux use `.venv/bin/` instead of `.venv\Scripts\`.

`check_providers.py` sends one tiny prompt to every model in `config.yaml` and tells
you which keys and model IDs work. Free-tier model IDs change often, so run it first.

## Usage

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="unused-locally")
reply = client.chat.completions.create(
    model="auto",  # or "fast", "smart", "local", or "groq/openai/gpt-oss-20b"
    messages=[{"role": "user", "content": "Explain recursion in one sentence."}],
)
print(reply.choices[0].message.content)
```

`scripts/demo.py` sends a few prompts through a running gateway and prints where each
one was routed and what it cost. Other endpoints: `/v1/models`, `/health`, `/stats`,
`/logs/recent`. Set `GATEWAY_API_KEY` in `.env` before exposing the gateway publicly.

## Results so far

From demo runs against the real providers:

- The fast tier (Groq `gpt-oss-20b`) answered with a median latency of about 0.5 s.
- At list prices the smart model is about 25 times more expensive per token than the
  fast one, which is the whole case for routing easy prompts away from it.
- Six parallel requests to one Groq model went over its 8,000 tokens per minute limit.
  Five went straight through and one was rate-limited; the gateway queued it for the
  16 seconds Groq asked for, and it then succeeded.

Costs are estimates. Free tiers bill nothing, so each call is priced at the model's
list price from `config.yaml`.

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```

52 tests, all offline. They use a scriptable fake upstream (return 500 three times,
then succeed; return 429 with `Retry-After: 5`) and a fake clock, so the retry and
queue logic is checked exactly without real waiting.

## Layout

```
gateway/
  main.py        FastAPI app and endpoints
  engine.py      resolve -> cache -> call with retries/fallback/queue -> log
  router.py      rules for "auto"
  cache.py       exact-match LRU cache with TTL
  store.py       SQLite request log and /stats
  usage.py       token estimates and cost
  config.py      config.yaml + .env loading
  providers/     one adapter for every OpenAI-compatible provider
config.yaml      providers, fallback chains, prices, routing rules, limits
scripts/         check_providers.py, demo.py
tests/
```

## Limitations and next steps

- Cache, cooldowns and the queue live in one process. Running several instances would
  need shared state (Redis).
- The `auto` router is keyword based and crude. "Write a haiku about code" goes to the
  expensive tier.
- Next: a semantic cache (match questions by meaning, not exact text), a learned
  router, and an eval set to measure the cost/quality trade-off properly.

## License

MIT
