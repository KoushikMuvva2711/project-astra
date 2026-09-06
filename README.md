# Project Astra

A voice-first personal AI constellation. Six agents with distinct personalities,
private working memory, and a shared memory layer, addressed by name in natural
speech. Local-first, self-hosted, single-user.

```
Astraea   chief of staff — coordination, global memory, briefings
Lyra      fitness & nutrition
Vega      finance
Nova      work & personal projects
Athena    career & learning
Selene    home & life admin
```

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Docker, Postgres+pgvector, Redis, FastAPI skeleton, core schema | **done** |
| 1 | Text-first slice: router + Astraea + Vega + memory | **done** |
| 2 | Voice pipeline (faster-whisper, Piper, WS streaming, barge-in) | not started |
| 3 | All six agents, briefings | not started |
| 4 | PWA install, Web Push, Siri Shortcuts, Health ingestion | not started |
| 5 | Deep integrations (Git, calendar, documents) | not started |

## Documentation

Read in this order:

1. [docs/PRD.md](docs/PRD.md) — what this is, what it isn't, why
2. [docs/conversation-architecture.md](docs/conversation-architecture.md) — turn lifecycle, wake-name routing, wire protocol
3. [docs/memory-design.md](docs/memory-design.md) — the four memory tiers and the curator
4. [docs/agents/](docs/agents/) — per-agent personality specs

## Platform note

There is no Mac in this project, so there is no native iOS app. The iPhone
client is an installable PWA plus iOS Shortcuts. Hands-free invocation comes from
one Siri Shortcut per agent name — "Hey Siri, Vega" — which is a better outcome
than a custom wake-word engine, since iOS would not permit a third-party app to
listen continuously in the background anyway.

See [docs/PRD.md §7](docs/PRD.md) for the full cost/benefit of that constraint.

## Setup

**Prerequisites:** Docker Desktop with WSL2, an NVIDIA GPU (for Phase 2 onward),
and Python 3.12 if you want to run tooling outside the container.

### 1. Configure

```bash
cp .env.example .env
```

Generate a real device token and put it in `.env`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

The app refuses to start with the placeholder token. This is deliberate — the
backend is fronted by a public tunnel, and a default credential there is an open
door to health and financial data.

### 2. Start

```bash
docker compose up -d
```

### 3. Migrate

```bash
docker compose exec api alembic upgrade head
```

Creates 18 tables and 40 indexes, and seeds 6 agents, 21 expense categories, and
33 fact predicates.

Confirm the models and the migrated schema agree — this should report no
operations, and is worth running after any model change:

```bash
docker compose exec api alembic check
```

### 4. Verify

```bash
curl -s localhost:8000/health/live
```

Then, with your token:

```bash
curl -s -H "Authorization: Bearer $ASTRA_DEVICE_TOKEN" localhost:8000/health/ready
```

Expect `postgres: ok`, a pgvector version, `redis: ok`, and `migration: 0002`.

### 5. Expose to the phone

The PWA needs a stable HTTPS origin — microphone access, service workers, and
Web Push all require a secure context, so `http://<lan-ip>:8000` will not work.

```bash
cloudflared tunnel --url http://localhost:8000
```

For a permanent hostname, create a named tunnel and put Cloudflare Access in
front of it. The device token is the floor, not the ceiling.

## Development

Run the tooling outside Docker against the same code:

```bash
cd backend && python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"
```

```bash
cd backend && .venv/Scripts/python -m pytest -q tests/test_health.py tests/test_config.py
```

Schema integration tests need a live Postgres, so they run in the container.
They write freely and roll back, so they never pollute the database:

```bash
docker compose exec api pytest tests/ -q
```

```bash
cd backend && .venv/Scripts/python -m ruff check app
```

Preview a migration's SQL without touching a database:

```bash
cd backend && .venv/Scripts/python -m alembic upgrade head --sql
```

## Design commitments

These are load-bearing. Changing one changes the system's character.

**Numbers come from SQL, never from a model.** Every figure an agent states is
quoted verbatim from a query tool. Language models are used for nuance and
recall, never for arithmetic over retrieved text. Money is stored as integer
paise; no float touches a financial path. See PRD FR-T2.

**Routing never guesses between two agents.** If the top two wake-name candidates
score within 0.05, the system asks instead of picking. A misrouted turn writes
data into the wrong namespace and is discovered weeks later.

**Namespace isolation is enforced in the repository layer**, not in prompts.
`MemoryStore` is constructed with an agent identity and cannot return rows
outside that agent's read set. Prompt-level scoping only holds while the model
cooperates.

**Nothing is ever deleted.** Corrections close a validity window; mistakes set a
retraction timestamp; superseded rows point at their replacement.

**Recall failures degrade, write failures shout.** An agent that cannot remember
says so and continues. An agent must never confirm a write that did not happen.

## Licence

TBD.
