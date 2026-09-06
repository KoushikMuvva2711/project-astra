# Memory Design

**Status:** Draft v1
**Depends on:** `PRD.md`, `conversation-architecture.md`
**Informs:** `backend/app/memory/`, `backend/app/db/`

---

## 1. The central decision

Most agent-memory systems embed everything and retrieve by similarity. That is correct for *nuance* and catastrophic for *arithmetic*. If "how much did I spend on coffee this month" is answered by retrieving eight expense-shaped text chunks and asking a language model to add them, the answer will be plausible, confidently delivered, and sometimes wrong — and the user will not be able to tell which times.

Astra therefore splits memory by **the kind of question it answers**, not by the kind of data it holds:

| Question | Tier | Mechanism |
|---|---|---|
| "How much on coffee this month?" | Domain tables | `SELECT SUM(...)` |
| "What did I say about the rent situation?" | Episodic + vectors | Similarity search |
| "What is my protein target?" | Facts | Key lookup, current value |
| "What was I working on last Tuesday?" | Episodic | Time-ranged query |
| "Am I spending more since I started the gym?" | All three | SQL aggregate + fact dates + vector context |

Four tiers total: three storage tiers plus a vector index that spans two of them.

```
┌──────────────────────────────────────────────────────────┐
│ Tier 1 · EPISODIC     turns, immutable, append-only      │
│                       source of truth for everything     │
├──────────────────────────────────────────────────────────┤
│ Tier 2 · DOMAIN       typed tables, real constraints     │
│                       expenses, meals, tasks, workouts…  │
│                       ← all numeric answers come from here│
├──────────────────────────────────────────────────────────┤
│ Tier 3 · FACTS        (entity, predicate, value)          │
│                       bitemporal, confidence-scored       │
│                       ← "what is true about the user"     │
├──────────────────────────────────────────────────────────┤
│ VECTOR INDEX          over episodic summaries + fact text │
│                       ← "what was said about X"           │
└──────────────────────────────────────────────────────────┘
```

Tier 1 is the source of truth. Tiers 2 and 3 are **derived and rebuildable**. If fact extraction improves, the episodic log can be reprocessed to regenerate them. This is why turns are immutable and why every derived row carries `source_turn_id` — it makes the derivation reproducible rather than a one-way lossy transformation.

---

## 2. Tier 1 — Episodic

```sql
CREATE TABLE turns (
    id              BIGSERIAL PRIMARY KEY,
    session_id      UUID NOT NULL REFERENCES sessions(id),
    agent           TEXT NOT NULL,
    role            TEXT NOT NULL,           -- 'user' | 'agent'
    transcript      TEXT NOT NULL,
    asr_confidence  REAL,
    route_confidence REAL,
    route_method    TEXT,                    -- exact|phonetic|fuzzy|session|fallthrough
    audio_ref       TEXT,                    -- optional retained audio
    interrupted     BOOLEAN NOT NULL DEFAULT FALSE,
    tokens_in       INT, tokens_out INT, cost_minor INT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON turns (session_id, created_at);
CREATE INDEX ON turns (agent, created_at DESC);
```

Never updated after commit. `route_method` and `route_confidence` are stored specifically so routing failures can be mined from production data and fed back into the fixture corpus (`conversation-architecture.md` §4.4).

**Retention:** turns are kept forever — they are small. Audio is discarded after 7 days by default; it exists only for debugging ASR failures.

### Session summaries

Raw turns are too voluminous for long-range recall. On session close, a cheap model writes a 2–4 sentence summary, which is embedded and indexed.

```sql
CREATE TABLE session_summaries (
    session_id   UUID PRIMARY KEY REFERENCES sessions(id),
    agent        TEXT NOT NULL,
    summary      TEXT NOT NULL,
    embedding    VECTOR(768),
    turn_count   INT, period TSTZRANGE
);
```

Retrieval hits summaries first and drills into raw turns only when a summary looks relevant. This keeps recall cost roughly constant as history grows.

---

## 3. Tier 2 — Domain tables

Ordinary relational tables with real constraints. Representative example:

```sql
CREATE TABLE expenses (
    id            BIGSERIAL PRIMARY KEY,
    amount_minor  BIGINT NOT NULL CHECK (amount_minor > 0),   -- paise
    currency      CHAR(3) NOT NULL DEFAULT 'INR',
    category      TEXT NOT NULL REFERENCES expense_categories(slug),
    merchant      TEXT,
    note          TEXT,
    occurred_at   TIMESTAMPTZ NOT NULL,
    source_turn   BIGINT REFERENCES turns(id),
    confidence    REAL NOT NULL DEFAULT 1.0,
    superseded_by BIGINT REFERENCES expenses(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON expenses (occurred_at DESC);
CREATE INDEX ON expenses (category, occurred_at DESC);
```

Four properties that generalise to every domain table:

- **`amount_minor BIGINT`.** Integer paise. No float touches a financial path, ever. `CHECK > 0` because a negative expense is a refund and belongs in its own row with its own semantics, not a sign flip.
- **`source_turn`.** Full provenance. Any total Vega reports decomposes into the sentences that produced it. This is what makes "why is that number wrong?" answerable.
- **`confidence`.** Below a threshold the agent confirms before committing. ASR mishears amounts — "eighteen" and "eighty" are one phoneme apart, and the difference is ₹62.
- **`superseded_by`.** Corrections do not mutate. "Actually make that 280" writes a new row and points the old one at it. History survives; totals filter `WHERE superseded_by IS NULL`.

Full table inventory: `expenses`, `expense_categories`, `budgets`, `savings_goals`, `investments`, `subscriptions`, `bills`, `meals`, `meal_items`, `foods`, `workouts`, `exercise_sets`, `health_samples`, `body_metrics`, `projects`, `project_sessions`, `tasks`, `repo_index`, `learning_tracks`, `curriculum_items`, `study_sessions`, `papers`, `applications`, `reminders`, `inventory_items`, `documents`, `warranties`, `trips`.

### The query tool

Agents do not write raw SQL. Each gets **parameterised query tools** scoped to its namespace:

```python
vega.spend_summary(period, group_by="category")
vega.spend_total(category=None, since=None, until=None)
lyra.macro_totals(date_range)
```

Reasons this is not free-form SQL: injection surface, the model inventing columns, and unbounded queries. The tools return structured results the agent narrates but does not recompute. **The prompt states explicitly that figures are quoted verbatim from tool output and never arithmetic'd by the model.**

---

## 4. Tier 3 — Facts

Durable assertions about the user that are not naturally tabular. "Rent is ₹18,000, due on the 1st." "Target is 150g protein daily." "Prefers morning workouts." "Applying to ETH Zurich for Fall 2028."

```sql
CREATE TABLE memory_facts (
    id           BIGSERIAL PRIMARY KEY,
    namespace    TEXT NOT NULL,             -- finance|health|work|learning|home|global
    entity       TEXT NOT NULL,             -- usually 'user'
    predicate    TEXT NOT NULL REFERENCES fact_predicates(slug),
    value        JSONB NOT NULL,
    confidence   REAL NOT NULL DEFAULT 1.0,
    source_turn  BIGINT REFERENCES turns(id),
    -- valid time: when true in the world
    valid_from   TIMESTAMPTZ NOT NULL,
    valid_to     TIMESTAMPTZ,               -- NULL = still true
    -- transaction time: when we knew it
    recorded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    retracted_at TIMESTAMPTZ,               -- NULL = still believed
    embedding    VECTOR(768)
);
CREATE INDEX ON memory_facts (namespace, entity, predicate)
    WHERE valid_to IS NULL AND retracted_at IS NULL;
```

### Why two time axes

`valid_from`/`valid_to` record when a fact was true *in the world*. `recorded_at`/`retracted_at` record when the system *believed* it. These come apart constantly and the difference is not academic:

> On 20 March the user says "my rent went up to 18,000 last month." Valid time starts in February. Transaction time starts in March.

With one axis you must choose between "rent was 18000 from February" (losing the fact that January's budget was computed on old information, making the budget look wrong in hindsight) and "rent was 18000 from March" (which is simply false). Two axes answer both "what is my rent history" and "what did the system believe in January" — the second being how you debug a budget that looked wrong at the time.

Corrections close `valid_to`. Mistakes set `retracted_at`. **Nothing is ever deleted.**

### Predicate cardinality

```sql
CREATE TABLE fact_predicates (
    slug        TEXT PRIMARY KEY,
    cardinality TEXT NOT NULL,   -- 'single' | 'multi'
    value_type  TEXT NOT NULL,   -- money|number|string|date|enum|json
    namespace   TEXT NOT NULL
);
```

Declaring cardinality is what makes conflict detection mechanical instead of a judgement call. `monthly_rent` is `single` — two open values is a contradiction by definition. `dietary_restriction` is `multi` — several open values is normal. Without this the system either misses real conflicts or nags about non-conflicts, and the second trains the user to ignore it.

---

## 5. Namespaces and access control

| Namespace | Owner | Read | Write |
|---|---|---|---|
| `finance` | Vega | Vega, Astraea | Vega |
| `health` | Lyra | Lyra, Astraea | Lyra |
| `work` | Nova | Nova, Astraea | Nova |
| `learning` | Athena | Athena, Astraea | Athena |
| `home` | Selene | Selene, Astraea | Selene |
| `global` | Astraea | **all** | Astraea |

`global` holds the shared profile — name, location, timezone, household, long-term goals, important people — plus curator-derived cross-domain facts. Every agent reads it; only Astraea writes it.

**Enforced in the repository layer**, not the prompt. `MemoryStore` is constructed with an agent identity and physically cannot return rows outside that agent's read set. A confused Lyra asking for finance facts gets an empty result and a logged warning, not private data. Prompt-level scoping holds only while the model cooperates; this holds regardless.

---

## 6. Recall — assembling context

`recall(agent, transcript, session)` returns a bundle under a **2,000-token budget**, allocated:

| Component | Budget | Source |
|---|---|---|
| Global profile | 250 | `global` facts, cached |
| Agent facts (current) | 400 | open facts in namespace |
| Session working memory | 500 | recent turns, verbatim |
| Semantic recall | 500 | vector over summaries + facts |
| Curator hints | 200 | unconsumed cross-agent hints |
| Domain preview | 150 | e.g. last 3 expenses for Vega |

Budgets are hard. Overflow drops the lowest-priority component rather than truncating mid-item — a half-included fact is worse than an omitted one because the model treats fragments as complete.

Semantic recall scores `0.6 × similarity + 0.3 × recency_decay + 0.1 × confidence`. Recency decay is a 30-day half-life. Older material must be substantially more relevant to displace recent material, which matches how the user actually refers back to things.

**Speculative prefetch:** recall starts on the ASR *partial* transcript, before finalisation, and is discarded if the final transcript diverges materially. Buys 100–150ms on the critical path at the cost of some wasted queries — a good trade on a local database.

---

## 7. The curator

Astraea's asynchronous background process. **It never runs on the response path.**

```
agent commits turn
   └─▶ XADD astra:events  {type, agent, payload, turn_id}
                │
                ▼   (separate worker)
        ┌───────────────┐
        │ 1. EXTRACT    │ facts from turn (cheap model, structured out)
        │ 2. RECONCILE  │ vs existing → new / supersede / ignore
        │ 3. DETECT     │ cardinality violations → conflicts
        │ 4. PROPAGATE  │ derive global facts, emit hints
        │ 5. SUMMARISE  │ on session close
        └───────────────┘
```

Redis Streams with a consumer group, not pub/sub — the curator must be able to crash and resume from its cursor without losing events. Memory that silently drops writes is worse than no memory.

### Event schema

```python
{
  "type": "expense.logged",
  "agent": "vega",
  "turn_id": 4821,
  "occurred_at": "2026-08-14T18:22:00+05:30",
  "payload": {"category": "groceries", "amount_minor": 82000, "merchant": None}
}
```

Types: `expense.logged`, `meal.logged`, `workout.logged`, `health.synced`, `task.created`, `task.completed`, `project.resumed`, `study.logged`, `reminder.set`, `inventory.changed`, `goal.updated`, `fact.asserted`.

### Propagation rules

Declarative, in `backend/app/memory/propagation.py` — not hardcoded in the curator:

```python
Rule(on="expense.logged",
     when=lambda e: e.payload["category"] == "groceries",
     emit=[Hint("selene", "inventory_likely_replenished", ttl_days=3),
           Hint("lyra",   "groceries_purchased",          ttl_days=3)])

Rule(on="health.synced",
     when=lambda e: e.payload.get("sleep_hours", 9) < 6,
     emit=[Hint("lyra",    "poor_sleep_adjust_intensity", ttl_days=1),
           Hint("astraea", "recovery_risk",              ttl_days=1)])
```

This is the requested behaviour made literal: telling Vega about groceries reaches Selene and Lyra without either being invoked.

**Hints expire.** A three-day-old grocery hint is stale and its continued presence is noise. TTL is per-rule, and hints are consumed once read into a context bundle.

### Conflict detection

After reconciliation, for every `single`-cardinality predicate with more than one open fact:

```sql
INSERT INTO memory_conflicts (namespace, entity, predicate, fact_ids, detected_at, state)
```

Conflicts surface in Astraea's **daily briefing**, never mid-conversation. Interrupting an expense log to adjudicate a rent discrepancy is exactly the wrong moment. Resolution options: keep newer, keep older, both valid over different windows, or user-supplied value.

---

## 8. Consolidation over time

Without maintenance, retrieval quality degrades as volume grows — the classic failure of long-lived memory systems. Three scheduled jobs:

| Job | Cadence | Action |
|---|---|---|
| Session summarisation | on close | 2–4 sentence summary, embedded |
| Weekly rollup | Sunday | week digest per namespace; raw turns stay, summaries collapse |
| Fact consolidation | monthly | merge near-duplicate facts, decay confidence on unreinforced ones, close obviously stale open facts |

Confidence decay is gentle — 2% monthly for facts never restated. A preference asserted once eighteen months ago and never repeated should rank below one mentioned last week, without being erased.

---

## 9. Export and backup

- **JSON** — complete relational dump, all tiers, restorable.
- **Markdown** — human-readable: daily journals from turns, monthly finance reports, project histories, learning logs. This is the format that makes the data genuinely *owned* rather than merely exportable — a JSON dump nobody can read is technically compliant and practically useless.
- **Backups** — nightly `pg_dump`, age-encrypted, retained 30 daily / 12 monthly. Restore is verified by an actual scripted restore into a scratch database. An unverified backup is not a backup.

---

## 10. Failure semantics

Restating the rule from `conversation-architecture.md` §10 because it lands hardest here: **recall failures degrade; write failures shout.**

- Vector index unavailable → proceed on facts and session context, note reduced recall once.
- Fact extraction fails → turn is still committed; extraction retries from the episodic log later. This is why Tier 1 is the source of truth and Tiers 2–3 are rebuildable.
- Domain write fails → the agent must **not** confirm. "I didn't get that saved" is recoverable; a false "logged" is not, because the user stops tracking it themselves.
- Curator falls behind → acceptable and self-correcting. Hints arrive late; nothing is lost.

---

## 11. Open questions

1. Embedding model — local (`bge-small`, `nomic-embed-text` via Ollama) or hosted? Local is preferred for privacy; needs a quality check against real recall queries.
2. Should raw audio be retained longer than 7 days for ASR fine-tuning later?
3. Is monthly the right consolidation cadence, or should it be volume-triggered?
4. Does the user want to see and edit facts directly — a memory inspector UI — or is voice-only correction sufficient?
