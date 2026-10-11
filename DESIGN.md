# Design notes

## Request flow

1. `main.py` validates the body with Pydantic. `messages` must be non-empty; unknown
   OpenAI fields (`top_p`, `tools`, ...) are kept and passed through.
2. `engine.resolve()` turns the requested model into a fallback chain:
   - `auto`: the router picks `fast` or `smart` and records why (`rule:keyword:code`)
   - an alias: its chain from `config.yaml`
   - `provider/model`: a one-item chain, no fallback
   - anything else: 404 listing the valid options
3. Non-streaming requests check the exact-match cache. The key is a SHA-256 of the
   whole request minus `stream`, so a different temperature or system prompt is a
   different entry. Clients can skip it with `X-Gateway-Cache: bypass`.
4. `_call_with_fallback()` walks the chain. It skips models without an API key and
   models that are cooling down, retries transient errors, and falls back on the rest.
5. Token usage comes from the provider when it reports it; otherwise it is estimated
   at about 4 characters per token and the row is flagged `usage_estimated`.
6. One row goes to SQLite, the response goes into the cache, and the client gets the
   body plus `X-Gateway-*` headers.

Streaming works in two phases. `open_stream()` runs the same fallback loop but only
opens the upstream stream and checks its status. `relay()` then passes the SSE events
through untouched while measuring time to first token. Fallback is only possible in
the first phase: once tokens reach the client, switching providers would produce a
half-and-half answer, so a mid-stream failure becomes an error event instead.

## Failure handling

The provider adapter turns every failure into a `ProviderError` with a kind:

| Kind | Cause | What the gateway does |
|---|---|---|
| transient | timeout, network error, 5xx | retry the same model (0.5 s, 1 s), then fall back |
| unreachable | connection refused (Ollama not running) | fall back at once |
| rate_limited | 429 | cool that model down, fall back at once |
| auth | 401, 403 | fall back |
| bad_request | 400, 404, 422 (often a retired model ID) | fall back, no retry |
| unconfigured | no API key in `.env` | skip without calling |

When the whole chain fails, the status code is 503 if nothing was callable, 400 if
every attempt was a bad request, 429 if every attempt was rate-limited, and 502
otherwise. The error body lists every attempt and why it failed.

## Decisions

**One adapter for every provider.** Groq, OpenRouter, Gemini and Ollama all expose an
OpenAI-compatible `/chat/completions`, so adding a provider is a config change.

**Retry only what retrying can fix.** Timeouts and 5xx errors are often temporary. A
refused connection is not going to fix itself in half a second, and retrying a 429
straight away only makes the limit last longer.

**Cooldowns are per model.** Groq's free tier allows 1,000 requests a day and 8,000
tokens a minute for each model separately. The first version cooled down the whole
provider after a 429, so one busy model blocked models that still had capacity.

**Use Retry-After.** Per-minute token limits usually clear within seconds and the 429
response says exactly when (as seconds or an HTTP date). The 30 s default only applies
when the header is missing.

**Queue when everything is rate-limited.** Agent conversations and benchmark runs would
rather wait 20 seconds than lose a half-finished task. If every model in the chain is
cooling down, the gateway sleeps until the earliest one frees up and tries again, up to
`max_queue_wait_s`. It never waits for a cooldown that ends after that budget, and it
never queues for errors other than 429.

**SQLite for the log.** No setup, one file, and SQL for analysis. Fine for one
instance; a fleet would need Postgres or a metrics pipeline.

**Estimated cost at list price.** Free tiers bill $0, which hides the trade-off the
gateway exists to manage. Pricing each call at the model's paid list price makes the
savings from routing measurable.

**A rules router first.** Keyword rules are easy to explain and give a baseline that a
learned router has to beat.

**Testable by construction.** The HTTP transport, clock and sleep function are all
injected, so the tests use a fake upstream and a fake clock. A 16-second queue wait is
checked to the millisecond without the test taking 16 seconds.

## What gets logged

Each request is one row in `gateway.db` (`requests` table): requested model, alias and
routing reason, the provider and model that answered, stream and cache flags, status,
attempts, latency, time to first token, prompt and completion tokens, whether usage
was estimated, estimated cost, error details and time spent queued. New columns are
added to old log files automatically on startup.
