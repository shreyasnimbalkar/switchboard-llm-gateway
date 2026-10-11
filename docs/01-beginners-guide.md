# Part 1: The Project in Simple Words

## What is this?

Switchboard is an **LLM gateway**: a **smart receptionist for AI models**.

An app sends a question to one address. The gateway decides **which AI model should
answer it**, keeps working when a model fails, remembers answers it has already given,
and writes down what every answer cost and how long it took.

Apps never talk to the AI providers directly. They only talk to the gateway.

```
  your app  --->  Switchboard  --+-->  Groq        (fast, cheap)
                                 +-->  OpenRouter  (smarter)
                                 +-->  Gemini      (backup)
                                 +-->  Ollama      (runs on your own laptop)
```

## The problem it solves

Imagine a company whose AI chatbot answers **1 million questions a day**.

- Most questions are easy: *"What are your store hours?"*
- A few are hard: *"Debug this code"* or *"Compare these two contracts."*

Many companies send **every** question to their most expensive, smartest model.
That is like hiring a **surgeon to put on band-aids**. It works, but it wastes money.

They also hit four other problems:

| Problem | What goes wrong |
|---|---|
| A provider goes down | The whole app breaks |
| Too many requests ("rate limited") | The provider blocks you and the app breaks |
| The same question asked again and again | You pay for the same answer every time |
| No measurement | Nobody knows what each feature costs or how slow it is |

The gateway fixes all five.

### A worked example (hypothetical numbers)

Using the reference prices in this project's `config.yaml`:

- "Smart" model (Nemotron 3 Ultra): $0.50 per million input tokens, $2.20 per million output tokens
- "Fast" model (gpt-oss-20b): $0.018 per million input tokens, $0.09 per million output tokens

Say each question uses 500 input tokens and 200 output tokens, and there are
1,000,000 questions a day.

| Strategy | Daily cost |
|---|---|
| Send everything to the smart model | about $690 |
| Send everything to the fast model | about $27 (but hard questions get worse answers) |
| Route 80% to fast, 20% to smart | about $160 |

Routing saves about **77%** in this example, while hard questions still get the smart
model. The real savings depend on your traffic. Measuring them properly is what the
evaluation step on the roadmap is for.

## The hospital analogy

| At a hospital | In the gateway |
|---|---|
| A patient walks in | An app sends a question |
| The front desk checks how serious it is | The **router** decides: easy or hard? |
| A cold goes to a nurse, a heart problem to a specialist | Easy goes to the **fast** tier, hard to the **smart** tier |
| The specialist is busy, so the patient sees another one | **Fallback**: if a provider fails, try the next one |
| "We treated this exact case an hour ago" | **Cache**: same question, reuse the saved answer |
| Every visit goes in a logbook | **Logging**: every request saved with cost and time |
| A monthly report to management | The **/stats** page |

## How a request travels, step by step

Example: an app sends **"Write code to reverse a list"** and asks for model `auto`.

**Step 1. The request arrives.**
The app sends the question to `/v1/chat/completions`. This is the **same format as
OpenAI's API**, so any app built for the OpenAI API can use the gateway by changing one
URL.

**Step 2. Password check (optional).**
If a gateway password is set, requests without it are rejected. This matters once the
gateway is on the internet.

**Step 3. The router picks a tier.**
The app asked for `auto`, so the router reads the question:

- Contains words like *code*, *debug*, *step by step*, *analyze*? Use **smart**.
- Longer than 4,000 characters? Use **smart**.
- A long conversation (more than 6 user messages)? Use **smart**.
- Otherwise, use **fast**.

"Write **code** to reverse a list" contains "code", so it goes to **smart**.
(Apps can skip this by asking for `fast`, `smart`, or an exact model name.)

**Step 4. Check the cache.**
Has this *exact* question been asked recently? If yes, return the saved answer
instantly, for free. If no, continue.

**Step 5. Call the AI, with backup plans.**
"smart" is an ordered **list of backups**:

```
smart:  1. OpenRouter  (Nemotron 3 Ultra 550B)
        2. Groq        (gpt-oss-120b)
        3. Gemini      (3.5 Flash)
        4. Ollama      (Llama 3.2 3B, on your laptop)
```

The gateway tries number 1. What happens next depends on the result:

| Result | What the gateway does |
|---|---|
| It works | Done |
| Timeout or server error (a temporary glitch) | Wait 0.5s and retry, then 1s and retry, then move on |
| "Too many requests" (429) | Put that provider in a 30-second time-out, move to the next one now |
| Cannot connect at all | Move to the next one immediately (retrying will not help) |
| No API key set for it | Skip it |

**Step 6. Send the answer back, with labels.**
The answer goes back with extra **headers** that say what happened:

```
X-Gateway-Route:        rule:keyword:code   <- why it was routed this way
X-Gateway-Alias:        smart               <- which tier
X-Gateway-Provider:     openrouter          <- who actually answered
X-Gateway-Attempts:     1                   <- how many tries it took
X-Gateway-Cache:        miss                <- served from cache?
X-Gateway-Est-Cost-USD: 0.00004120          <- estimated cost
```

**Step 7. Write it in the logbook.**
Every request is saved to a small database file (`gateway.db`): which model answered,
tokens used, estimated cost, total time, time until the first word, and any errors.

**Step 8. Reports.**
`/stats` shows totals and breakdowns: requests, errors, cache hits, typical and slow
response times (p50 / p95), and cost, per provider, per tier, and per routing rule.

## What happens when things go wrong

**Scenario A: Groq is having an outage.**
A "fast" request goes to Groq. Groq returns server errors three times in a row. The
gateway gives up on Groq and sends the same request to the next backup, Gemini. The user
still gets an answer; the header says `X-Gateway-Provider: gemini` and
`X-Gateway-Attempts: 4`.

**Scenario B: you hit Groq's free-tier limit.**
Groq replies "429 Too Many Requests, retry after 16 seconds". The gateway does *not*
retry right away (that would make it worse). It puts **that one model** in a 16-second
time-out and moves on to the next option. Groq's other models keep working, because
Groq counts each model's limit separately.

If *every* option is in a time-out, the gateway does not give up. It **waits in a queue**
until the first one frees up (up to 90 seconds), then tries again. The header
`X-Gateway-Queued-Ms` says how long the request waited.

**Scenario C: no keys at all.**
Every cloud provider is skipped because it has no key, and Ollama answers everything.
This is how the project ran on day one, before any keys were added.

**Scenario D: everything is down.**
The gateway returns an error that lists every attempt and why it failed, for example:

```
groq/openai/gpt-oss-20b: skipped (no GROQ_API_KEY in .env)
ollama/llama3.2:3b: unreachable - cannot connect
```

## Streaming

Streaming means the answer appears **word by word**, like in ChatGPT, instead of all at
once at the end. The gateway supports it.

The tricky part: once the first word has been sent to the user, you cannot switch to a
different provider mid-sentence. So the gateway makes sure a provider has **accepted**
the request before it sends anything to the user. That way, streaming requests get the
same backup plans as normal requests.

## About "estimated cost"

On free tiers the real bill is $0. To still measure savings, the gateway calculates what
each request **would cost** at the model's normal paid price (the `prices` section in
`config.yaml`). That is the same way companies estimate cost when they plan.

## Key ideas, in plain words

| Term | Meaning |
|---|---|
| LLM | Large language model: the AI that reads and writes text |
| API | A way for one program to talk to another over the internet |
| API key | A password that identifies you to an AI provider |
| Token | A small piece of text, roughly 3/4 of a word. Providers charge per token |
| Provider | A company that hosts AI models (Groq, OpenRouter, Google, ...) |
| Alias / tier | A nickname like "fast" or "smart" that maps to a list of real models |
| Deployment | One specific model on one specific provider, e.g. `groq/openai/gpt-oss-20b` |
| Router | The part that decides which tier handles a request |
| Fallback | Trying the next option when one fails |
| Retry with backoff | Trying again after waiting longer each time (0.5s, 1s, 2s, ...) |
| Rate limit (429) | The provider saying "slow down, too many requests" |
| Cooldown | Temporarily skipping a model that rate-limited you |
| Retry-After | A header where the provider says exactly how long to wait |
| Wait queue | Holding a request until a rate-limited model frees up, instead of failing |
| Cache | Saved answers that can be reused |
| Latency | How long a response takes |
| p50 / p95 | Half of requests are faster than p50; 95% are faster than p95 |
| TTFT | Time to first token: how long until the first word appears |
| Streaming | Sending the answer word by word as it is generated |
| SSE | Server-Sent Events: the format used to stream the words |
| Ollama | Free software that runs AI models on your own computer |
| Eval set | A list of test questions with known good answers, used to score quality |
