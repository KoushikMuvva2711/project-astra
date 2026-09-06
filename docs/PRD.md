# Project Astra — Product Requirements Document

**Status:** Draft v1
**Owner:** Koushik
**Last updated:** 2026-08-14

---

## 1. Summary

Astra is a voice-first personal AI constellation: six agents with distinct personalities, responsibilities, and memories, addressed by name in natural speech. It runs on the user's own hardware, stores all data locally, and is operated primarily through an iPhone.

It is not a chatbot with modes. Each agent is a separate reasoning context with its own tools, its own private memory namespace, and its own voice. A central coordinator, Astraea, holds global memory and moves information between the others.

---

## 2. Problem statement

Personal life admin is fragmented across a dozen apps that do not talk to each other. A finance app knows about grocery spending; a nutrition app knows about protein intake; neither knows the other exists. The user is the integration layer, and that costs continuous attention.

Existing AI assistants fail this in three specific ways:

1. **No persistence.** Each conversation starts cold. A project discussed on Monday is gone by Thursday.
2. **No specialisation.** One general assistant with one tone cannot be both a demanding academic mentor and an encouraging fitness coach without the personality collapsing into neutral mush.
3. **No data ownership.** Health, financial, and personal data sits on someone else's servers under someone else's terms.

Astra's bet is that these three are solvable together, and that solving them together is what makes the system feel different in use rather than merely convenient.

---

## 3. Goals

### 3.1 Product goals

| # | Goal | Measure of success |
|---|------|--------------------|
| G1 | Speaking to a named agent reaches that agent | ≥97% correct routing on real speech, including mis-transcriptions |
| G2 | Logging a fact by voice is faster than opening an app | ≤8s from raising the phone to confirmed write |
| G3 | Agents remember across sessions indefinitely | Nova can resume a project after 30 days with no re-briefing |
| G4 | Information crosses domains without being re-entered | A grocery expense logged to Vega is visible to Lyra and Selene without restatement |
| G5 | The system is trustworthy about numbers | 100% of reported financial totals reconcile exactly against logged entries |
| G6 | All data is user-owned and exportable | Full JSON + Markdown export; system runs with no outbound network |

### 3.2 Non-goals (v1)

Explicitly out of scope, recorded so they are not re-litigated mid-build:

- Multi-user or family accounts. Single user, single tenant.
- A native iOS app. Not buildable without a Mac (see §7).
- Always-on ambient listening inside our own software. Siri handles wake-word.
- Autonomous action without confirmation — no agent sends messages, moves money, or purchases anything.
- Being a general-purpose assistant. If a request does not fall in a specialist's domain, Astraea answers briefly or declines. Astra is not a replacement for a search engine.

---

## 4. Users and context

**Single user.** Indian, INR, based in India. Building this while pursuing AI engineering mastery and preparing for higher studies abroad within 24 months. Owns an iPhone and an Apple Watch. Develops on a Windows PC with an NVIDIA GPU. Has no Mac.

Usage is expected to be **many short voice interactions** (logging an expense, a meal, a thought) plus **a few long ones** (weekly review, project planning session). The system must be excellent at the short ones — they are the load-bearing case. A five-second interaction that works every time is worth more than an impressive twenty-minute one.

---

## 5. The constellation

| Agent | Domain | Character | Primary interaction |
|-------|--------|-----------|---------------------|
| **Astraea** | Coordination, global memory, briefings | Calm, strategic, economical with words | Daily briefing, weekly review, cross-domain questions |
| **Lyra** | Fitness, nutrition, recovery | Energetic, encouraging, disciplined | Meal logging, workout programming, recovery reads |
| **Vega** | Finance, budgeting, investments | Precise, analytical, unsentimental | Expense logging, spend reports, bill reminders |
| **Nova** | Personal projects, code, execution | Efficient, focused, terse | Project resumption, task planning, brainstorming |
| **Athena** | Career, higher studies, AI mastery | Wise, demanding, long-horizon | Curriculum, GRE/TOEFL, applications, paper schedule |
| **Selene** | Home, calendar, life admin | Organised, warm, quietly competent | Reminders, groceries, inventory, documents |

Personalities are enforced through system prompts, response-length budgets, and voice parameters. They are **specified as testable artifacts** (see `docs/agents/`), not left to prompt improvisation — a personality that drifts between sessions is a bug.

---

## 6. Functional requirements

### 6.1 Voice interaction

- **FR-V1** The user addresses an agent by name as the first word of an utterance; that agent handles the turn.
- **FR-V2** Name resolution is robust to speech-recognition error. `Lyra` → *Lira / Leera / Liar*; `Vega` → *Vaga / Bega*; `Astraea` → *Astraya / Astrea / Astra*. Phonetic matching, not string equality.
- **FR-V3** If no name is confidently detected, Astraea takes the turn. The system never silently guesses between two agents.
- **FR-V4** Within an active session, follow-up turns stay with the current agent without repeating the name.
- **FR-V5** Each agent speaks with a distinct, consistent voice.
- **FR-V6** The user can interrupt a spoken response; playback stops and generation is cancelled.
- **FR-V7** Push-to-talk is the primary input. Hands-free invocation is via Siri Shortcuts.
- **FR-V8** End-of-speech to first audio ≤2.5s at p50, ≤4s at p95.

### 6.2 Memory

- **FR-M1** Every turn is persisted immutably with timestamp, agent, and transcript.
- **FR-M2** Structured facts are extracted into typed domain tables — not only embeddings.
- **FR-M3** Facts are bitemporal: superseding a fact preserves the prior value and its validity window.
- **FR-M4** Each agent reads and writes its own namespace. Astraea reads all namespaces.
- **FR-M5** Cross-domain propagation is automatic and asynchronous, and never delays a response.
- **FR-M6** Contradictions between facts are detected and surfaced in the daily briefing.
- **FR-M7** Every derived record links to the turn that produced it (full provenance).

### 6.3 Per-agent capability (MVP level)

**Vega** — voice expense logging with automatic categorisation; INR; monthly and category reports computed in SQL; budgets; recurring bill and rent reminders; subscription tracking.

**Lyra** — meal logging with Indian food support; calorie and macro tracking; workout logging and programming; recovery commentary. *Apple Health ingestion lands in Phase 4.*

**Nova** — project registry; session continuity ("where did I leave off"); task and sprint planning; brainstorming with retained context. *Git indexing lands in Phase 5.*

**Athena** — 24-month roadmap toward higher studies and AI engineering mastery; adaptive weekly learning plans; paper reading schedule; GRE/TOEFL milestones; application and scholarship tracking.

**Selene** — reminders; grocery lists; home inventory; document and warranty tracking; packing lists. *Native calendar/reminders write access lands in Phase 5.*

**Astraea** — daily briefing; weekly review; cross-domain queries; conflict surfacing; notification prioritisation; long-term goal tracking.

### 6.4 Trust and correctness

- **FR-T1** Monetary values are stored as integer minor units. No floating point on any financial path.
- **FR-T2** Numeric answers are computed by database query, never inferred by a language model from retrieved text.
- **FR-T3** Any reported figure can be traced to its source utterances on request.
- **FR-T4** When an agent is uncertain about a logged value, it confirms rather than assuming.

> FR-T2 is the single most important requirement in this document. An assistant that is charming but wrong about money is worse than no assistant, because it is trusted. Retrieval-augmented generation is used for *nuance and recall*; arithmetic and aggregation go to SQL. Agents are given separate tools for these and the prompts are explicit about which to reach for.

### 6.5 Privacy and ownership

- **FR-P1** Self-hostable via Docker on the user's own hardware.
- **FR-P2** Speech recognition and synthesis run locally. Raw audio never leaves the machine.
- **FR-P3** Cloud LLM use is configurable, per-agent, and disableable; the system remains functional offline via Ollama.
- **FR-P4** Full export to JSON and Markdown.
- **FR-P5** Encrypted backups.
- **FR-P6** The public ingress is authenticated; no unauthenticated endpoint exposes user data.

---

## 7. Platform constraint and its consequences

The user has no Mac and none planned. SwiftUI, HealthKit, App Intents, and custom widgets cannot be compiled. The iPhone client is therefore an **installable PWA plus iOS Shortcuts**.

**What this costs:**

| Capability | Native plan | Actual | Assessment |
|---|---|---|---|
| Hands-free wake-name | Custom wake-word engine | Siri Shortcut per agent name | **Better.** Apple does the always-on listening; iOS would not permit us to do it well regardless. |
| Apple Health data | Continuous HealthKit stream | Scheduled Shortcuts automation POSTing samples | **Acceptable.** Batched hourly/daily instead of live. Sufficient for coaching. |
| Notifications | APNs | Web Push (iOS 16.4+, installed PWA) | **Equivalent** in practice. |
| Home/Lock Screen widgets | WidgetKit | None | **Lost.** Partly mitigated by Shortcuts on the home screen. |
| Background listening in-app | Possible with entitlements | None | **Lost**, superseded by Siri. |
| On-device ASR latency | ~instant | Network round trip to local GPU | **Slightly worse**, within the latency budget. |

Two hard dependencies follow: the PWA must be served over a **stable public HTTPS origin** (microphone, service workers, and Web Push all require secure context), and Web Push requires the PWA to be **installed to the home screen** — it does not work in a Safari tab.

---

## 8. Non-functional requirements

- **Latency** — §6.1 FR-V8.
- **Availability** — best-effort single-node. Data durability matters; uptime does not. A missed briefing is acceptable; a lost expense is not.
- **Durability** — nightly encrypted backup; Postgres is the single source of truth. Redis holds only ephemeral state and may be lost without consequence.
- **Maintainability** — one developer, evenings and weekends. Every added service must justify its operational cost. This drove the choice of pgvector over a separate vector database and the removal of Vosk.
- **Extensibility** — adding a seventh agent should require a spec file, a prompt, a tool allowlist, and a voice assignment. No changes to the orchestration core.
- **Cost** — cloud LLM spend tracked per agent and visible; a hard monthly ceiling triggers fallback to local models.

---

## 9. Success criteria for v1

The system is a success if, ninety days after MVP:

1. The user logs expenses by voice in preference to any other method.
2. Nova has successfully resumed a project after a gap of two weeks or more without re-briefing.
3. At least one Astraea cross-domain correlation has been useful and unprompted.
4. Zero financial reporting discrepancies.
5. The user has not felt the need to open the finance or nutrition apps it replaces.

Criterion 3 is the real test. The first four are competent software. The third is the thing that makes it a constellation rather than six separate tools.

---

## 10. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Wake-name mis-routing frustrates the user into abandoning voice | Fatal to the premise | Phonetic matching + fixture corpus from day one; fall through to Astraea rather than guess |
| Latency makes voice feel worse than typing | High | Strict budget; stream TTS from first sentence boundary; local ASR/TTS on GPU |
| Personalities collapse into a single neutral voice | Medium-high — kills the constellation feel | Personality specs as testable artifacts with length budgets and lexical constraints |
| Memory bloat degrades retrieval quality over time | Medium, grows with age | Summarisation tiers, fact consolidation, relevance decay |
| Cloud LLM cost drift | Medium | Per-agent cost tracking, cheap models for routing/curation, hard ceiling |
| Scope — six agents plus six integrations by one developer | High | Agents decoupled from integrations; agents ship first, integrations land incrementally |
| Public tunnel exposes personal data | Severe if realised | Device-token auth minimum, Cloudflare Access preferred; no unauthenticated data endpoints |

---

## 11. Open questions

1. Per-agent model assignment — which agents justify a frontier model versus a cheap one?
2. Authentication mechanism on the tunnel: device token, mTLS, or Cloudflare Access?
3. Backup destination and encryption key custody.
4. Does Astraea's daily briefing arrive as a push notification, or wait to be asked for?

---

## 12. References

- `docs/conversation-architecture.md` — turn lifecycle, routing, session model
- `docs/memory-design.md` — three-tier memory, curator, conflict detection
- `docs/agents/` — per-agent personality and capability specifications
- `docs/adr/` — architecture decision records
