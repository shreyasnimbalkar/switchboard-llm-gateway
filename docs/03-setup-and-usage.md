# Part 3: Setup and Usage

## What you need

| Item | Why | Where | Cost |
|---|---|---|---|
| Python 3.11+ | Runs the gateway | https://www.python.org/downloads | Free |
| Ollama | Runs a small model locally: the free, unlimited fallback | https://ollama.com/download | Free |
| Groq API key | Fast cloud models (the "fast" tier) | https://console.groq.com | Free tier, no card |
| OpenRouter API key | Many free models behind one key (the "smart" tier) | https://openrouter.ai | Free models, no card |
| Gemini API key (optional) | Extra backup provider | https://aistudio.google.com | Free tier |

Free tiers have strict rate limits and may use your prompts for training. Only send
test or synthetic data through them.

## Step 1: Install the Python environment

```
git clone https://github.com/shreyasnimbalkar/switchboard-llm-gateway.git
cd switchboard-llm-gateway
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
```

(On macOS/Linux use `.venv/bin/python` instead of `.venv\Scripts\python`.)

## Step 2: Install Ollama and download a model

1. Download and install Ollama from https://ollama.com/download.
2. Download the model (about 2 GB):

```
ollama pull llama3.2:3b
```

The first request after Ollama starts can take a minute or more while the model loads
into memory. Later requests take a few seconds.

## Step 3: Get free API keys

**Groq:** sign in at https://console.groq.com, open **API Keys**, click
**Create API Key**, and copy it (starts with `gsk_`). It is only shown once.

**OpenRouter:** sign in at https://openrouter.ai, open your profile menu, then
**Keys**, then **Create Key** (starts with `sk-or-`).

**Gemini (optional):** sign in at https://aistudio.google.com and click
**Get API key**, then **Create API key**.

## Step 4: Put the keys in `.env`

```
copy .env.example .env
notepad .env
```

Paste each key after its `=` sign, with no quotes or spaces:

```
GROQ_API_KEY=gsk_...
OPENROUTER_API_KEY=sk-or-...
GEMINI_API_KEY=
GATEWAY_API_KEY=
```

`.env` is listed in `.gitignore`, so it is never uploaded to GitHub.

## Step 5: Check every provider

```
.venv\Scripts\python scripts\check_providers.py
```

This sends one tiny prompt to every model in `config.yaml` and prints a table of which
ones work and, if not, why (missing key, wrong key, outdated model ID, not running).

## Step 6: Run the gateway

```
.venv\Scripts\uvicorn gateway.main:create_app --factory --reload
```

Then, in a second terminal, send some sample prompts:

```
.venv\Scripts\python scripts\demo.py
```

Useful pages while it runs:

- Interactive API docs: http://127.0.0.1:8000/docs
- Statistics: http://127.0.0.1:8000/stats
- Provider health: http://127.0.0.1:8000/health

## Using the gateway from code

Any OpenAI client works. Point it at the gateway:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="unused-locally")
reply = client.chat.completions.create(
    model="auto",   # or "fast", "smart", "local", or "groq/openai/gpt-oss-20b"
    messages=[{"role": "user", "content": "Explain recursion in one sentence."}],
)
print(reply.choices[0].message.content)
```

Or with plain HTTP:

```
curl http://127.0.0.1:8000/v1/chat/completions -H "Content-Type: application/json" \
  -d "{\"model\": \"auto\", \"messages\": [{\"role\": \"user\", \"content\": \"hi\"}]}"
```

## API reference

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/chat/completions` | Chat completion (OpenAI format), streaming supported |
| GET | `/v1/models` | Lists `auto`, the aliases, and every deployment |
| GET | `/health` | Which providers are configured; current cooldowns; cache size |
| GET | `/stats` | Requests, errors, cache hits, p50/p95 latency, tokens, est. cost |
| GET | `/logs/recent?limit=50` | The most recent logged requests |

`/health` is always public. If `GATEWAY_API_KEY` is set, every other endpoint requires
the header `Authorization: Bearer <GATEWAY_API_KEY>`.

### Model names you can request

| Model | Meaning |
|---|---|
| `auto` | The router chooses `fast` or `smart` |
| `fast` | Cheap, quick models first |
| `smart` | Stronger models first |
| `local` | Only the local Ollama model |
| `provider/model` | Exactly that model on that provider, no fallback |

### Response headers

| Header | Meaning |
|---|---|
| `X-Gateway-Route` | Why this tier was chosen (`rule:default`, `rule:keyword:code`, `explicit`, `direct`) |
| `X-Gateway-Alias` | The tier used |
| `X-Gateway-Provider` / `X-Gateway-Model` | Who actually answered |
| `X-Gateway-Attempts` | Number of provider calls it took |
| `X-Gateway-Queued-Ms` | Time spent waiting for a rate limit to clear (only when it waited) |
| `X-Gateway-Cache` | `hit` or `miss` |
| `X-Gateway-Est-Cost-USD` | Estimated cost at list price (non-streaming) |

Send `X-Gateway-Cache: bypass` to skip the cache for a request.

## Configuration (`config.yaml`)

| Section | What it controls |
|---|---|
| `providers` | Base URL, which env var holds the key, timeout, extra headers |
| `aliases` | Each tier's fallback chain, tried in order |
| `prices` | List price per million tokens, used for cost estimates |
| `routing` | Rules for `auto`: long-prompt length, conversation length, keywords |
| `reliability` | Retries, backoff base, default rate-limit cooldown, wait-queue budget |
| `cache` | Time-to-live and maximum entries |
| `db_path` | Where the SQLite request log is stored |

Provider model IDs change over time. If `check_providers.py` reports `bad_request` for a
model, find its current ID in the provider's dashboard and update `config.yaml`.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `skipped (no GROQ_API_KEY in .env)` | Key missing | Add it to `.env`; restart the gateway |
| `unreachable ... cannot connect` for Ollama | Ollama not running | Start the Ollama app |
| `model 'llama3.2:3b' not found` | Model not downloaded | `ollama pull llama3.2:3b` |
| `bad_request (404)` for a cloud model | Model ID retired or renamed | Update the ID in `config.yaml` |
| `auth (401)` | Key wrong or revoked | Create a new key |
| `rate_limited (429)` | Free-tier limit reached | Wait; the gateway falls back meanwhile |
| First request takes over a minute | Ollama loading the model | Normal; later requests are fast |
| `'ollama' is not recognized` in a terminal | Terminal opened before install | Open a new terminal |
