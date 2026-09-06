# Where things stand

Written at handoff, 2026-08-15. Read this first when picking the project back up.

## Working right now

Everything below is verified live against the running stack, not just unit-tested.

```
297 tests passing · ruff clean · alembic check clean · 39 tables
```

**Phase 0 — infrastructure.** Docker Compose with Postgres 17 + pgvector 0.8.6 and
Redis. FastAPI on :8000 behind a bearer token that the app refuses to boot without.
Postgres and Redis bind to loopback only.

**Phase 1 — the vertical slice.** Verified end to end:

```
Vega, I spent 250 on auto     -> Logged. 250 rupees, auto_taxi.
Vega, paid 820 for groceries  -> Logged. 820 rupees, groceries.
actually make that 900        -> Updated. 900 rupees, groceries.   [no wake name]
Vega, spent eighteen on chai  -> Was that 18 rupees or 80 rupees?
Vega, how much this month?    -> 1,330 rupees across 3 entries.
```

The total reconciles exactly: the superseded 820 is excluded, the ambiguous chai
was never written. Logging groceries to Vega produced hints for Selene and Lyra
without either agent being invoked.

**Lyra's domain**, also verified live:

```
Lyra, two rotis and dal                  -> 262 kcal, 13 g protein
Lyra, I had two eggs and a protein shake -> 276 kcal, 36.6 g protein
Lyra, did legs today                     -> Logged. That's 1 this week.
Lyra, how are you doing today?           -> logs nothing (correctly)
```

## The one thing capping quality

The local model is `qwen2.5:0.5b`, chosen only because the PG Wi-Fi cannot sustain
a larger download. It is fine for deterministic paths and useless for judgement.
Observed failures, all real:

- confirmed a save that never happened
- narrated a figure it never read
- emitted raw tool-call JSON into the spoken channel
- collapsed to one stock sentence for every input

Each is now blocked structurally rather than by prompting — see
`app/orchestration/graph.py` guards and `app/orchestration/intents.py`. That is
why Vega and Lyra work well despite the model.

**When a capable model is configured**, revisit `_prefer_tool_wording()` in
`graph.py`. It currently makes the tool's message authoritative on deterministic
turns because the model's phrasing is net-negative. With a real model the model's
own wording is better and that override should be relaxed.

Astraea, Nova, Athena and Selene are wired but will sound poor until then —
their value is judgement, and judgement is exactly what a 0.5B cannot supply.

## Next, in order

1. **Selene's tools** (task #11) — reminders, inventory, grocery list. The
   inventory hint from Vega's grocery events already arrives; nothing consumes it yet.
2. **Nova's tools** (task #12) — the `project_sessions` resumption substrate.
3. **Athena's tools** (task #13) — curriculum, study log, applications.
4. **Phase 2, voice** — blocked on network: `faster-whisper large-v3-turbo` is
   ~1.6 GB and will not download over the PG Wi-Fi. Piper voices are small enough
   to fetch. Do not start this assuming the ASR model will arrive.

## Network constraint

Sustained TLS transfers fail on this connection: ~400 MB succeeds in about 80
seconds, anything past ~1 GB dies with `tls: bad record MAC` or `curl: (56)`.

Range requests *are* honoured, so `scripts/fetch_model.sh` (resumable `curl -C -`)
works where `ollama pull` does not — the latter restarts from zero on every drop.

**Never run two fetchers at once.** Concurrent writers to the same file corrupt it
and make progress appear to go backwards. That cost hours once already.

## Running it

```bash
docker compose up -d
```

```bash
docker compose exec api alembic upgrade head
```

```bash
docker compose exec api pytest tests/ -q
```

Talk to it:

```bash
curl -s -X POST localhost:8000/converse -H "Authorization: Bearer $ASTRA_DEVICE_TOKEN" -H "Content-Type: application/json" -d '{"text":"Vega, spent 250 on auto"}'
```

The curator normally drains in the background; force it with
`POST /converse/curate`.
