# Conversation Architecture

**Status:** Draft v1
**Depends on:** `PRD.md`
**Informs:** `backend/app/orchestration/`, `backend/app/api/ws/`

---

## 1. Scope

How an utterance becomes a response: the turn lifecycle, how the wake-name selects an agent, how sessions hold context across turns, how agents hand off, and the wire protocol between phone and backend.

Memory internals are deliberately excluded — see `memory-design.md`. This document treats memory as two calls: `recall(agent, query, session)` before reasoning, and `commit(turn, outputs)` after.

---

## 2. Core model

Three nested scopes, each with a different lifetime. Confusing them is the most common source of bugs in multi-agent systems, so they are named explicitly and kept separate in code.

```
Conversation  ─ the permanent record. Never ends. One per user.
  └── Session ─ a contiguous stretch of interaction with a focused agent.
                Opens on a wake-name, closes on timeout or explicit end.
      └── Turn ─ one utterance and one response.
```

- A **Turn** is atomic and immutable once committed. It is the unit of provenance.
- A **Session** holds the *active agent* and the working context. It is what makes "Vega, spent 250 on auto" followed by "actually make that 280" work.
- The **Conversation** is the append-only history that all long-term memory is derived from.

**Session lifetime:** opens when a wake-name is resolved; stays open through follow-up turns; closes after **90 seconds** of silence, on an explicit close ("thanks, done"), or when a different wake-name is spoken — which closes the current session and opens a new one.

The 90-second window is a deliberate trade-off. Too short and natural pauses break continuity mid-thought; too long and an unrelated later utterance gets misattributed to a stale agent. It is configurable and expected to need tuning against real use.

---

## 3. Turn lifecycle

```
 1. CAPTURE      client records audio, streams to server
 2. TRANSCRIBE   faster-whisper → partial transcripts → final transcript
 3. ROUTE        resolve wake-name → target agent (§4)
 4. ADMIT        open/continue/switch session; create Turn record
 5. RECALL       memory.recall(agent, transcript, session) → context bundle
 6. REASON       agent graph: LLM + tools, streaming tokens
 7. SPEAK        Piper synthesises per sentence boundary, streams audio
 8. COMMIT       persist turn, domain writes, emit events
 9. CURATE       (async, off-path) Astraea extracts + propagates
```

Steps 1–7 are on the latency path and are the only ones the user waits for. Steps 8–9 must never block the response. Step 9 in particular runs in a separate worker entirely.

**Step 7 begins before step 6 finishes.** Synthesis starts at the first sentence boundary in the token stream rather than waiting for a complete response. This is the single largest latency win available and is a requirement, not an optimisation — see §8.

---

## 4. Wake-name routing

The hardest correctness problem in the system, because it fails in front of the user and it fails on *speech*, which is noisy by nature.

### 4.1 The problem

Names arrive through a speech recogniser. Observed and anticipated corruptions:

| Agent | Likely transcriptions |
|---|---|
| Astraea | astraea, astraya, astrea, astria, astra, extra |
| Lyra | lyra, lira, leera, liara, liar, lyre |
| Vega | vega, vaga, bega, viga, vegas |
| Nova | nova, nofa, noba, nowa, no va |
| Athena | athena, athina, atena, aetna, athene |
| Selene | selene, saleen, celine, seline, sailene |

String equality resolves none of the interesting cases. `celine` and `Selene` are the same word spoken; they share no useful prefix.

### 4.2 Resolution algorithm

Cascade, cheapest first, first confident match wins:

1. **Exact match** on the first token, case-folded. Covers the majority of clean input.
2. **Phonetic match** — Double Metaphone of the first token against precomputed codes for the six names. `celine` and `selene` both encode to `SLN`. This is what catches the hard cases.
3. **Fuzzy match** — normalised Damerau-Levenshtein ≥ 0.82 against each name. Catches `athina`/`athena` where phonetics differ slightly.
4. **Two-token window** — repeat 1–3 on the first two tokens joined, catching `no va` → `nova`.
5. **Fallthrough** — no confident match: route to Astraea, flagged `unrouted`.

Scoring returns a confidence in [0,1]. Below a floor of **0.7**, treat as fallthrough. If the top two candidates are within **0.05** of each other, this is *ambiguous*: do not pick. Astraea takes the turn and asks which agent was meant.

**Never guess between two agents.** Silently routing to the wrong agent writes data into the wrong namespace, which is a correctness failure with a persistent tail — a mis-filed expense is discovered weeks later. Asking costs one turn.

### 4.3 Session-scoped exception

If a session is open and the utterance carries no confident wake-name, it belongs to the **active agent**, not Astraea. Follow-ups ("make that 280", "actually it was yesterday") have no name and must not be hijacked.

Precedence within an open session:

```
confident different name  → close session, switch agent
confident same name       → continue session
no confident name         → continue session with active agent
ambiguous between two     → ask; do not switch
```

Note the asymmetry: with **no** session open, an unnamed utterance goes to Astraea; with a session open, it stays with the active agent. Astraea is the default only in the absence of context.

### 4.4 Testing

Routing has a fixture corpus at `backend/tests/fixtures/wake_names.yaml`: every variant in §4.1, each mapped to its expected agent and confidence band, plus negative cases (`"astrology"`, `"a laser"`, `"novel"`) that must *not* route. The corpus grows from real mis-transcriptions in production logs — every routing failure observed in use is added as a regression case.

---

## 5. Agent execution

### 5.1 Graph shape

LangGraph, one supervisor with six subgraphs:

```
                    ┌──────────┐
   transcript ─────▶│  router  │
                    └────┬─────┘
                         ▼
                   ┌───────────┐
                   │  admit    │  session open/switch/continue
                   └─────┬─────┘
                         ▼
                   ┌───────────┐
                   │  recall   │  memory context bundle
                   └─────┬─────┘
                         ▼
          ┌──────────────┴──────────────┐
          ▼    (one per agent)          ▼
    ┌───────────┐                 ┌───────────┐
    │  agent    │◀───┐            │  astraea  │
    │  reason   │    │ tool loop  │  reason   │
    └─────┬─────┘────┘            └─────┬─────┘
          └──────────────┬──────────────┘
                         ▼
                   ┌───────────┐
                   │  respond  │  stream tokens → TTS
                   └─────┬─────┘
                         ▼
                   ┌───────────┐
                   │  commit   │  persist + emit events
                   └───────────┘
```

Each agent subgraph is identical in structure and differs only in configuration: system prompt, tool allowlist, model assignment, memory namespace, voice profile, response-length budget. **Adding an agent adds a config entry, not a code path.** This is the extensibility requirement from the PRD made concrete.

### 5.2 Agent configuration

```python
AgentSpec(
    name="vega",
    display_name="Vega",
    system_prompt=...,          # from docs/agents/vega.md
    tools=[...],                # allowlist; enforced, not advisory
    model="cloud:standard",     # logical name, resolved by registry
    namespace="finance",
    voice=VoiceProfile(...),
    max_response_tokens=180,    # personality is partly enforced by brevity
    fallback_model="local:qwen",
)
```

The tool allowlist is **enforced at the graph boundary**, not merely described in the prompt. Lyra cannot write to `expenses` even if a confused model tries. Namespace isolation is checked at the memory layer, not trusted to prompting. Prompt-level guardrails are guidance for a cooperative model; boundary enforcement is what holds when the model misbehaves.

### 5.3 Tool loop

Standard reason-act cycle, capped at **6 iterations**, cancellation-aware at every await. Tool results are appended to working context. Exceeding the cap returns a degraded response acknowledging the failure — it never silently truncates and presents a partial answer as complete.

### 5.4 Response budget

Voice punishes verbosity in a way text does not: the user cannot skim. Each agent has a token budget, and the budget is part of the personality — Vega at 180 tokens is clipped and factual; Athena at 400 has room to be discursive. Budgets are enforced by generation parameters *and* stated in the prompt.

---

## 6. Cross-agent handoff

Two mechanisms, and it matters that they are distinct.

### 6.1 Implicit propagation (the common case)

An agent completes a turn, writes its domain data, emits an event. Astraea's curator consumes it asynchronously and updates global memory plus any relevant agent's context. **The user sees nothing.** No agent announces the transfer.

```
"Vega, paid 820 for groceries"
  → expense row (category=groceries)
  → event: expense.logged{category:groceries, amount:82000}
  → curator: fact(user, purchased_groceries, 2026-08-14)
  → hints: selene(inventory likely replenished), lyra(food available)
```

Next time Selene is asked about groceries, that context is present. Neither Selene nor Lyra was invoked.

### 6.2 Explicit delegation (rare)

An agent is asked something outside its domain. It does **not** attempt an answer. It returns a delegation intent; the supervisor routes to the right agent, which responds in its own voice.

```
User → "Vega, am I eating enough protein?"
Vega → delegate(lyra)
Lyra → responds, in Lyra's voice
```

The response is spoken by Lyra, and the transition is audible because the voice changes. This is intentional: the user should always know which agent is speaking. An agent answering outside its competence — Vega improvising nutrition advice — would be both wrong and a break in character.

Astraea is the exception. She may answer across domains, because correlation is her job.

---

## 7. Wire protocol

WebSocket at `/ws/converse`, JSON control frames plus binary audio.

### Client → server

| Type | Payload | Notes |
|---|---|---|
| `session.start` | `{device_id, auth}` | First frame |
| `audio.chunk` | binary | Opus/WebM, ~100ms |
| `audio.end` | `{}` | End of utterance |
| `text.input` | `{text}` | Typed input; skips ASR |
| `cancel` | `{reason}` | Barge-in or user abort |
| `session.close` | `{}` | Explicit end |

### Server → client

| Type | Payload | Notes |
|---|---|---|
| `asr.partial` | `{text}` | Interim, for display only |
| `asr.final` | `{text, confidence}` | Committed transcript |
| `route.resolved` | `{agent, confidence}` | UI switches identity immediately |
| `token` | `{text}` | Streamed response text |
| `tts.chunk` | binary | Audio, sentence-aligned |
| `tts.end` | `{}` | Playback complete |
| `turn.complete` | `{turn_id}` | Committed |
| `error` | `{code, message, recoverable}` | |

`route.resolved` is emitted **as soon as routing completes**, before reasoning begins. The UI changes colour, name, and avatar at that instant. This makes the system feel fast even when the response is not yet ready, and — more importantly — it surfaces mis-routing immediately, while the user can still cancel, instead of after a wrong answer has been spoken.

---

## 8. Latency

Budget from end-of-speech to first audible syllable, target p50 ≤2.5s:

| Stage | Budget | Notes |
|---|---|---|
| ASR finalise | 300ms | faster-whisper GPU, partials already streamed |
| Route | 5ms | in-process, no I/O |
| Recall | 150ms | vector + SQL, parallel |
| LLM first token | 600–1200ms | dominant term; cloud network included |
| Sentence boundary | 100–300ms | enough tokens to form a clause |
| TTS first chunk | 200ms | Piper, GPU |
| **Total** | **~1.4–2.2s** | |

Levers, in order of preference if the budget is missed:

1. **Speak on the first clause, not the first sentence.** Cuts 100–200ms.
2. **Prefetch recall during ASR.** Partial transcripts are usually enough to start retrieval; run it speculatively and discard on mismatch.
3. **Cheaper model for high-frequency, low-nuance turns.** Expense logging does not need a frontier model. Route by intent complexity.
4. **Cache the opening phrase per agent.** Pre-synthesised acknowledgements ("Got it.") play instantly while real generation proceeds. Use sparingly — it becomes obvious and grating if overused.

Lever 3 is where the real headroom is. A large fraction of turns are simple logging, and treating them like open-ended reasoning wastes both latency and money.

---

## 9. Interruption

Barge-in is what separates a voice interface that feels alive from one that feels like an IVR menu.

```
client: energy over threshold during playback for >250ms
      → stop playback immediately
      → send cancel{reason:"barge_in"}
server: cancel generation task
      → stop TTS stream
      → commit partial turn, marked interrupted
      → begin capturing new utterance
```

Requirements this places on the implementation: every node in the graph must be cancellation-aware at each `await`; a cancelled turn is still **committed** with an `interrupted` flag (the user said something, and that is part of the record even though the response was cut off); and partial tool side effects must not be left half-applied — domain writes are transactional and roll back on cancellation.

The 250ms threshold and energy gate exist to avoid triggering on the user's own TTS bleeding through the microphone, coughs, and background noise. Expect to tune this against real hardware.

---

## 10. Error handling

| Failure | Behaviour |
|---|---|
| ASR returns empty/low confidence | Ask once for repetition; do not guess |
| Routing ambiguous | Astraea asks which agent |
| LLM provider unavailable | Fall back to local model, note the degradation once |
| Tool raises | Agent sees the error and may retry once, then reports honestly |
| DB write fails | Fail loudly. Never confirm a write that did not happen |
| Memory recall fails | Proceed with degraded context, flag it in the response |

The distinction that matters: **recall failures degrade gracefully; write failures do not.** If Vega cannot remember last month's spending it can say so and continue. If Vega says "logged" and nothing was written, the user's mental model diverges from reality, and every subsequent total is wrong. Silent write failure is the worst outcome in the system and is treated accordingly.

---

## 11. Open questions

1. Is 90s the right session timeout? Instrument and tune.
2. Should ambiguous routing ask, or pick the higher-scoring agent and offer an undo? Asking is safer; undo is faster. Start with asking.
3. Multi-agent turns — "Astraea, ask Lyra and Vega about..." — deferred past v1, but the graph should not preclude it.
4. Does barge-in need server-side VAD as a backstop, or is client energy detection sufficient?
