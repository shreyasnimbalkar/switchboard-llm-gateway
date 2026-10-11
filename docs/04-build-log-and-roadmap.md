# Part 4: What Was Built, and Why

## Build log

### Step 1: Core proxy

**What:** An OpenAI-compatible `/v1/chat/completions` endpoint that forwards requests to
Groq, OpenRouter, Gemini or Ollama, with streaming support.

**Why:** Being OpenAI-compatible means any existing app or SDK can use the gateway by
changing one URL. Without that, nobody would adopt it.

### Step 2: Request logging

**What:** Every request is recorded in SQLite with tokens, estimated cost, total latency
and time to first token. `/stats` summarizes it per provider, tier and routing rule.

**Why:** You cannot claim savings or speed you have not measured. This log is the data
source for the evaluation and dashboard in steps 5 and 6.

### Step 3: Reliability

**What:** Retries with exponential backoff for temporary errors, fallback chains across
providers, a cooldown for rate-limited models, and fallback for streaming
requests before the first byte.

**Why:** Free tiers rate-limit often and providers have outages. The gateway's job is to
keep apps working when that happens.

### Step 3b: Rate-limit upgrade

**What:** Three changes, made while preparing Switchboard to carry long agent benchmark
runs on free tiers:

1. **Per-model cooldowns.** The first version cooled down a whole *provider* after a
   429. Measuring Groq's free tier showed its limits are *per model* (1,000 requests a
   day and 8,000 tokens a minute each), so one busy model was wrongly blocking the
   others. Cooldowns are now tracked per `provider/model`.
2. **Retry-After.** The cooldown now uses the provider's own `Retry-After` header
   (seconds or HTTP-date) instead of a fixed 30 seconds.
3. **Wait queue.** If every option is rate-limited, the request waits for the first
   one to free up (up to `max_queue_wait_s`, 90s) instead of failing. Waiting time is
   returned in `X-Gateway-Queued-Ms` and logged in a new `queued_ms` column; old log
   files are migrated automatically.

**Why:** An agent conversation that fails halfway because of a 20-second rate limit is
wasted work. Waiting is the better trade for long-running clients.

**Verified:** 13 new tests (52 total). Against the real Groq API, 6 parallel
~2,400-token requests were sent to one model, more than its 8,000 tokens/minute limit:
5 went straight through (about 0.7 to 1 second each) and 1 was rate-limited. Groq asked
for a wait of about 16 seconds, the gateway queued it, and it then succeeded. Before the
change, that request would have failed.

### Extras added along the way

| Feature | Why |
|---|---|
| `auto` routing with keyword rules | The baseline for comparing smarter routers later |
| Exact-match cache | Repeated questions become instant and free |
| `X-Gateway-*` response headers | Shows exactly what happened for each request |
| Optional gateway API key | Required before putting the gateway on the internet |
| `scripts/check_providers.py` | One command to verify every key and model ID |
| `scripts/demo.py` | A quick live demonstration of routing, caching and cost |
| Offline test suite + GitHub Actions | Proves the failure handling works, on every push |

### Verification so far

- The automated test suite passes (52 tests at the time of writing).
- The gateway was started for real with no keys and Ollama not running. It returned a
  clear error naming each missing piece.
- With Ollama running `llama3.2:3b` locally and no cloud keys, the demo sent 5 prompts:

| Prompt | Routed to | Reason | Outcome |
|---|---|---|---|
| "What is the capital of Japan?" | fast | `rule:default` | Answered ("Tokyo") |
| "Write code for a ... palindrome" | smart | `rule:keyword:code` | Answered with working Python |
| "Give me a synonym for 'happy'." | fast | `rule:default` | Answered ("joyful") |
| "Explain why the sky is blue, step by step." | smart | `rule:keyword:step by step` | Answered |
| "What is the capital of Japan?" (again) | fast | `rule:default` | **Cache hit** |

Result: 0 errors, p95 latency 3.8 seconds on a laptop GPU (GTX 1650). With no cloud keys,
both tiers fell back to the local model, which is the fallback design working as
intended.

### Connecting real providers

When the free API keys were added, `check_providers.py` showed that every key worked
but **every cloud model ID in the original config had been retired or moved off the
free tier**. The fix was to query each provider's live model list, test candidates,
and pick replacements on speed and list price:

| Tier | Chosen model | Why |
|---|---|---|
| fast | Groq `openai/gpt-oss-20b` | Under 0.5s, and the cheapest list price tested |
| fast backup | Gemini `gemini-3.1-flash-lite` | Fast (under 1s), different provider |
| smart | OpenRouter `nvidia/nemotron-3-ultra-550b-a55b:free` | Largest free model available |
| smart backups | Groq `openai/gpt-oss-120b`, Gemini `gemini-3.5-flash` | Strong models on other providers |

A model that answered faster (`qwen3.8-27b`) was rejected for the fast tier because its
list price was about 10x higher per request, which would defeat the purpose of a cheap
tier. Lesson: provider model catalogs change quickly, so model IDs belong in config and
need a health check.

The same demo through the real providers:

| Prompt | Served by | Est. cost |
|---|---|---|
| "What is the capital of Japan?" | Groq gpt-oss-20b | $0.000005 |
| "Write code for a ... palindrome" | OpenRouter Nemotron 3 Ultra | $0.001336 |
| "Give me a synonym for 'happy'." | Groq gpt-oss-20b | $0.000005 |
| "Explain why the sky is blue, step by step." | OpenRouter Nemotron 3 Ultra | $0.001334 |
| "What is the capital of Japan?" (again) | Cache | $0 |

Observations:

- The fast tier answered with a median latency of about 0.5 seconds; streaming worked,
  with the first token after about 0.66 seconds.
- A smart-tier request cost about **270x** more than a fast-tier one at list prices,
  which is why routing easy prompts away from it matters.
- The smart tier took 17 to 29 seconds for long answers on the free tier. Free-tier
  capacity is slow, and long answers take longer. This is a real tradeoff to measure in
  the evaluation step.

## Roadmap

### Step 4: Semantic cache

**What:** Reuse answers for questions that *mean* the same thing, not just identical
text. "What's the capital of Japan?" and "Japan's capital city?" should match.

**How:** Turn each question into an **embedding** (a list of numbers that captures its
meaning). For a new question, find the most similar cached one using cosine similarity.
If the similarity is above a threshold, reuse its answer. Move the cache to Redis so it
survives restarts.

**Why:** More cache hits mean lower cost and latency. The threshold is a real tradeoff:
too loose and users get wrong answers, too strict and nothing matches. Measuring that
tradeoff is part of the step.

### Step 5: Learned router

**What:** Replace keyword rules with a small classifier that predicts whether a prompt
needs the smart tier.

**How:** Label a set of prompts by whether the fast model's answer was good enough,
then train a lightweight classifier on embeddings of those prompts.

**Why:** Keywords are crude. "Write a haiku about code" is easy but contains "code".
A learned router should send fewer easy prompts to the expensive tier.

### Step 6: Evaluation and dashboard

**What:** A set of about 200 prompts with reference answers, scored automatically, and
a dashboard of quality versus cost versus p95 latency for each routing strategy:
always-smart, always-fast, rules, and learned.

**Why:** This produces the headline result: *"routing cut estimated cost by X% while
keeping Y% of quality."*

### Step 7: Deploy and write up

**What:** Host the gateway publicly (with `GATEWAY_API_KEY` set), add the results and
charts to the README, and publish a short write-up.

**Why:** A live demo and real numbers are what make the project credible.

## Known limitations

| Limitation | Impact | Planned fix |
|---|---|---|
| Cache is in memory | Lost on restart; not shared between instances | Redis (step 4) |
| Keyword router is crude | Some easy prompts go to the smart tier, and vice versa | Learned router (step 5) |
| Token counts are sometimes estimated | When a provider omits usage, cost is approximate | Flagged with `usage_estimated` in the log |
| Costs are estimates at list price | Not a real bill | Prices are configurable in `config.yaml` |
| SQLite log | Fine for one instance, not for a fleet | Postgres or a metrics system if scaled |
| Cooldown state is per process | Multiple instances would not share it | Shared store (Redis) if scaled |
| Free-tier limits | Large eval runs must be spread out | Fallback chains spread the load |
