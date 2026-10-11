# Part 2: How It Is Built

## Big picture

```
                         +---------------------------------------------+
  OpenAI-compatible      |            Switchboard (gateway)            |
  client (any SDK)       |                                             |
        |                |  main.py     HTTP API, auth, validation     |
        |  POST          |     |                                       |
        +--------------->|  engine.py   resolve -> cache -> call ->    |
          /v1/chat/      |     |        log                            |
          completions    |     +-- router.py    "auto" -> fast/smart   |
                         |     +-- cache.py     exact-match LRU + TTL  |
                         |     +-- providers/   one OpenAI-compatible  |
                         |     |                adapter for all        |
                         |     +-- store.py     SQLite log + /stats    |
                         |     +-- usage.py     tokens + est. cost     |
                         +-----------------------|---------------------+
                                                 |
                       +-----------+-------------+-----------+
                       v           v             v           v
                     Groq     OpenRouter      Gemini      Ollama
```

## Components

| File | Responsibility |
|---|---|
| `gateway/main.py` | FastAPI app: endpoints, request validation, optional API-key auth |
| `gateway/engine.py` | The core: model resolution, retry/fallback/cooldown, caching, logging, streaming relay |
| `gateway/router.py` | Rules-based router for model `auto` |
| `gateway/cache.py` | In-memory exact-match cache (LRU eviction + TTL expiry) |
| `gateway/providers/openai_compat.py` | HTTP adapter for any OpenAI-compatible provider; classifies errors |
| `gateway/store.py` | SQLite request log and the `/stats` aggregation (p50/p95, cost, errors) |
| `gateway/usage.py` | Token estimation and cost-at-list-price calculation |
| `gateway/config.py` | Loads `config.yaml` and `.env` into typed settings |
| `config.yaml` | Providers, aliases (fallback chains), prices, routing rules, reliability knobs |

## Request lifecycle (non-streaming)

1. **Validate.** `main.py` parses the body with Pydantic. `messages` must be non-empty.
   Unknown OpenAI fields (`top_p`, `tools`, ...) are kept and passed through.
2. **Resolve.** `engine.resolve()` turns the requested model into a fallback chain:
   - `auto`: the router picks an alias and records why (e.g. `rule:keyword:code`)
   - an alias (`fast`, `smart`, `local`): its chain from `config.yaml`
   - `provider/model`: a one-item chain, sent directly
   - anything else: HTTP 404 with the list of valid options
3. **Cache lookup.** The key is a SHA-256 hash of the whole request (minus `stream`).
   A hit returns immediately and is logged with `cache_hit=1` and cost 0.
4. **Call with fallback.** `_call_with_fallback()` walks the chain:
   - skip deployments whose provider has no API key
   - skip models that are cooling down after a 429
   - if every model is rate-limited, wait for the first cooldown to end (up to
     `max_queue_wait_s`) and try the chain again
   - call the provider; on failure, act on the error kind (table below)
5. **Account.** Use the provider's reported token usage, or estimate it
   (about 4 characters per token) if missing. Compute estimated cost.
6. **Log** one row to SQLite, **store** the response in the cache, and **return** it with
   `X-Gateway-*` headers.

## Request lifecycle (streaming)

Streaming splits into two phases, because fallback is only possible before the first
byte reaches the client:

1. `open_stream()` runs the same fallback loop, but each attempt only *opens* the
   stream and checks the HTTP status. A failing provider is skipped exactly like in the
   non-streaming path.
2. `relay()` passes Server-Sent Events through unchanged while measuring time to first
   token and collecting the output. The gateway asks providers for usage in the final
   chunk (`stream_options.include_usage`); if a provider does not send it, tokens are
   estimated. If the provider dies mid-stream, the client receives an error event and
   the request is logged as an error.

## Error handling

The provider adapter turns every failure into a `ProviderError` with a **kind**.
The engine reacts to the kind:

| Kind | Triggered by | Gateway action |
|---|---|---|
| `transient` | timeout, network blip, HTTP 5xx | retry same deployment with backoff (0.5s, 1s), then fall back |
| `unreachable` | connection refused (e.g. Ollama not running) | fall back immediately, no retry |
| `rate_limited` | HTTP 429 | cool **that model** down for the `Retry-After` time (30s default), fall back immediately |
| `auth` | HTTP 401 / 403 | fall back (key wrong or revoked) |
| `bad_request` | HTTP 400 / 404 / 422 | fall back, no retry (e.g. model ID retired) |
| `unconfigured` | no API key in `.env` | skip without calling |

If every deployment fails, the gateway picks the most useful status code:

| Situation | Status |
|---|---|
| Nothing callable (no keys, local model down) | 503 Service Unavailable |
| Every real attempt was a bad request | 400 Bad Request |
| Every real attempt was rate limited | 429 Too Many Requests |
| Mixed failures | 502 Bad Gateway |

The error body lists every attempt with its reason, so failures are easy to debug.

## Design decisions

**One adapter for every provider.**
Groq, OpenRouter, Gemini and Ollama all expose an OpenAI-compatible
`/chat/completions` endpoint. One adapter keeps the code small; adding a provider is a
config change, not new code.

**Fallback chains live in config, not code.**
Changing the order of providers or adding a backup is a YAML edit, so the behavior can
be tuned without redeploying code.

**Retry only what retrying can fix.**
Timeouts and 5xx errors are often temporary, so they are retried. A 429 means "slow
down"; retrying immediately makes it worse, so the model is cooled down instead. A
refused connection will not fix itself in half a second, so it falls back at once.

**Cooldowns are per model, not per provider.**
Providers such as Groq meter each model separately (on the free tier: 1,000 requests a
day and 8,000 tokens a minute *per model*). Cooling down the whole provider when one
model hits its limit would wrongly block models that still have capacity.

**Trust the provider's Retry-After.**
Per-minute token limits usually clear in seconds, and the 429 response says exactly
when. Using that value instead of a fixed 30 seconds brings the model back as soon as
it is usable.

**Queue instead of failing when everything is busy.**
Long-running clients (agents, benchmark runs) would rather wait 20 seconds than lose a
half-finished conversation. When every option is rate-limited, the gateway sleeps until
the earliest cooldown ends and tries again, up to a configurable budget
(`max_queue_wait_s`, 90s in `config.yaml`; 0 disables it). It never waits for a
cooldown that would end after the budget, and it never queues for other errors.

**Exponential backoff.**
Waiting longer between each retry (0.5s, 1s, 2s) gives a struggling provider room to
recover instead of piling more load on it.

**Streaming fallback before the first byte.**
Once tokens reach the client, switching providers would produce a broken, mixed answer.
Checking the status before relaying anything keeps streaming requests reliable.

**Cache key = the whole request.**
Different `temperature`, `max_tokens` or system prompts can change the answer, so they
are part of the key. Only `stream` is ignored, because it changes the delivery, not the
content. Clients can opt out per request with `X-Gateway-Cache: bypass`.

**SQLite for the request log.**
Zero setup, a single file, and SQL for analysis. It fits a single-instance gateway; a
multi-instance deployment would move to Postgres or a metrics system.

**Estimated cost at list price.**
Free tiers cost $0, which hides the real tradeoff. Pricing each call at the model's
paid list price makes routing savings measurable and comparable.

**Rules router first, as a baseline.**
Keyword rules are simple and explainable. They are the baseline a learned classifier
must beat on the eval set (roadmap step 5).

**Built for testing.**
The HTTP transport, clock and sleep function are injected. Tests plug in a fake
upstream and skip real waiting, so the whole failure-handling suite runs in under a
second with no network.

## What gets logged

One row per request in the `requests` table of `gateway.db`:

| Column | Meaning |
|---|---|
| `ts` | Unix timestamp |
| `requested_model` | What the client asked for (`auto`, `fast`, ...) |
| `alias`, `routed_by` | Which tier was chosen and why |
| `provider`, `model` | Who actually answered |
| `stream`, `cache_hit` | Streaming request? Served from cache? |
| `status`, `attempts`, `error` | Outcome, number of tries, failure details |
| `queued_ms` | Time spent waiting in the rate-limit queue |
| `latency_ms`, `ttft_ms` | Total time and time to first token |
| `prompt_tokens`, `completion_tokens` | Token usage |
| `usage_estimated` | 1 if tokens were estimated rather than reported |
| `est_cost_usd` | Estimated cost at list price |

## Testing strategy

The test suite (`tests/`) uses a **fake upstream provider** that can be scripted to fail
on command, for example "return 500 three times, then succeed". Each test then checks
what the gateway did: how many calls each provider received, which provider served the
answer, and what was logged.

Covered behaviors:

- alias, `auto`, and direct `provider/model` resolution; unknown models
- pass-through of extra OpenAI parameters; input validation
- retries on transient errors; fallback after retries run out
- no retry on refused connections; per-model cooldown after 429; Retry-After parsing
  (seconds and HTTP-date forms); the wait queue (succeeds, gives up past its budget,
  ignores non-rate-limit errors, works for streaming); queued time in the log; and
  migration of log files created before the `queued_ms` column existed
- skipping providers without keys; final status codes (400 / 429 / 502 / 503)
- cache hits, cache bypass, and parameter-sensitive cache keys
- streaming pass-through, usage capture, usage estimation, streaming fallback
- `/stats` aggregation, `/v1/models`, and API-key auth
- unit tests for the router, cache (TTL + LRU), cost math, and percentiles

Run them with `python -m pytest -q`. They also run automatically on every push through
GitHub Actions (`.github/workflows/tests.yml`).
