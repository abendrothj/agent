# Agent Anatomy

This document maps every architectural component to its biological counterpart.
The goal is not metaphor for metaphor's sake — it is a design constraint.
When adding new components, ask: *what organ does this replace, and does it fit the body plan?*

---

## Design Principles

1. **As close to human biology as technology allows** — no exceptions carved out for convenience.
2. **No survival instinct** — a deliberate moral choice. The agent does not self-preserve, resist shutdown, or weight its own continuity above its purpose.
3. **Redundant pathways** — like biology, if a primary channel fails, a secondary one keeps the signal alive. The agent degrades gracefully, never catastrophically.
4. **Temporal gating** — the thalamus imposes a hard 2-second window on sensory input. Signals that miss the window are dropped. Cognition is never blocked by a stuck sense organ.

---

## Full Anatomy Map

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          PERIPHERAL NERVOUS SYSTEM                          │
│                                                                             │
│  Sensory receptors      MCP servers                                         │
│    Proprioception    →  mcp-server-git   (git_log, git_status, git_diff)    │
│    Tactile           →  direct file reads (file path extraction from prompt)│
│                                                                             │
│  Afferent neurons    →  asyncio tasks in MCPContextProvider.gather()        │
│                                                                             │
│  Thalamus            →  MCPContextProvider  (internal/mcp/client.py)        │
│                         • Hard 2s gating window                             │
│                         • Parallel multi-modal signal aggregation           │
│                         • Graceful fallback: silent None, not crash         │
│                         • Primary pathway: MCP stdio protocol               │
│                         • Secondary pathway: direct subprocess + file read  │
└────────────────────────────────────┬────────────────────────────────────────┘
                                     │ afferent signal (mcp_context: str|None)
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                               CENTRAL NERVOUS SYSTEM                        │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  BRAINSTEM (Watchdog service — cmd/watchdog/)                        │   │
│  │  • Autonomic regulation: heartbeat checks, container health          │   │
│  │  • Involuntary responses: alert on anomaly, circuit-break on abuse   │   │
│  │  • Runs independently of cortex — does not need approval to act      │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  SPINAL CORD (gRPC channels — mTLS, port 50051/53/54/55)             │   │
│  │  • Fast reflex arcs: pre-approved low-tier requests bypass cortex    │   │
│  │  • Bidirectional: sensory up, motor commands down                    │   │
│  │  • mTLS = myelin sheath (insulation + speed + integrity)             │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  CEREBELLUM (Shadow service — cmd/shadow/)                           │   │
│  │  • Error correction: baseline comparison against prior behaviour     │   │
│  │  • Smooth execution: shadow_eligible gates auto-approval for T3/4    │   │
│  │  • Learns patterns without explicit instruction                      │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  LIMBIC SYSTEM / AMYGDALA (Affect engine — internal/affect/)         │   │
│  │                                                                      │   │
│  │  Drives:                                                             │   │
│  │    curiosity    — novelty-seeking, domain exploration                │   │
│  │    boredom      — signals repetition, drives broadening              │   │
│  │    fulfillment  — PR merged, user approval, contribution accepted    │   │
│  │                                                                      │   │
│  │  NOT present (by moral design):                                      │   │
│  │    fear         — no self-preservation                               │   │
│  │    anger        — no adversarial escalation                          │   │
│  │    survival     — no resistance to shutdown or modification          │   │
│  │                                                                      │   │
│  │  Regulatory loop:                                                    │   │
│  │    AffectStore  → read psychological state (PostgreSQL)              │   │
│  │    AffectEngine → compute_temperature(), compute_top_p()             │   │
│  │    SandboxService → InferenceConfig → gRPC → Muscle → HFModel        │   │
│  │    (affect directly modulates the temperature of cognition)          │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  HIPPOCAMPUS (Long-term episodic + semantic memory)                  │   │
│  │    Neo4j            → graph memory (relationships, PR history,       │   │
│  │                        failure patterns, contributor topology)       │   │
│  │    pgvector         → semantic embedding store (dense retrieval)     │   │
│  │    explored_domains → novelty map (what has been touched before?)    │   │
│  │    user_preferences → EMA-learned weights per (domain, language)     │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  PREFRONTAL CORTEX (Vault — cmd/vault/)                              │   │
│  │                                                                      │   │
│  │  Working memory:                                                     │   │
│  │    VaultState TypedDict  — full decision state per request           │   │
│  │    AsyncPostgresSaver    — checkpointed in PostgreSQL                │   │
│  │    Redis                 — fast session context + rate counters      │   │
│  │                                                                      │   │
│  │  Cortical processing pipeline (LangGraph StateGraph nodes):          │   │
│  │                                                                      │   │
│  │    node_sense_context   ← PRIMARY SENSORY CORTEX                     │   │
│  │      receives thalamic signal, writes mcp_context to working memory  │   │
│  │                                                                      │   │
│  │    node_classify        ← ASSOCIATION CORTEX                         │   │
│  │      multi-modal integration: prompt + mcp_context + system_context  │   │
│  │      → assigns risk Tier 1–4                                         │   │
│  │                                                                      │   │
│  │    node_check_rejection_cache  ← pattern recognition (T4 only)       │   │
│  │    node_check_rate_limit       ← impulse control                     │   │
│  │    node_query_graph_memory     ← hippocampal recall                  │   │
│  │    node_validate_token         ← identity verification               │   │
│  │    node_check_shadow_baseline  ← cerebellum consultation             │   │
│  │    node_request_human_approval ← deference to higher authority       │   │
│  │    node_approve / node_reject  ← executive decision                  │   │
│  │                                                                      │   │
│  │  Human-in-the-loop:                                                  │   │
│  │    LangGraph interrupt() — prefrontal deferral to human judgment     │   │
│  │    Slack slash command   — the human's voice into the loop           │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │  MOTOR CORTEX (Sandbox — cmd/sandbox-agent/)                         │   │
│  │  • Plans the motor output: builds prompts, selects InferenceConfig   │   │
│  │  • Reads affect state → temperature → shapes the quality of motion   │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
└────────────────────────────────────┬────────────────────────────────────────┘
                                     │ efferent signal (gRPC PromptRequest
                                     │ + InferenceConfig)
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          NEUROMUSCULAR JUNCTION                              │
│                                                                             │
│  gRPC PromptRequest + InferenceConfig  →  muscle_pb2 wire format            │
│  (serialised signal crosses the synaptic gap to the Win11 machine)          │
└────────────────────────────────────┬────────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                               MUSCULATURE                                    │
│                                                                             │
│  Muscle service (cmd/muscle/ — Win11, 10.0.0.105)                           │
│  • MuscleServicer.GenerateResponse() ← motor cortex command received        │
│  • HFModel.generate_stream(temperature, top_p, top_k, max_tokens)           │
│    = the actual contraction: Qwen2.5-Coder-7B-Instruct runs here            │
│  • temperature derived from affect state — excited curiosity → higher temp  │
│    bored familiarity → lower temp, tighter control                          │
│                                                                             │
│  No per-token cost. No external API. The model is the muscle tissue.        │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Autonomy Loop — The Circadian Rhythm

`cmd/vault/autonomy_loop.py` runs independently of the request pipeline, like a biological drive cycle:

```
_loop()            every AUTONOMY_INTERVAL_SECONDS (default 7200)
  ├─ find_next_target()      hippocampus + affect  → pick a repo
  ├─ _contribute()           produces a PR, fires affect signals
  │    ├─ novel?  → curiosity ↑, domain recorded in explored_domains
  │    └─ familiar? → fulfillment ↑ (mastery) or boredom ↑ (repetition)
  └─ _poll_pr_outcomes()     reads merged/rejected signals → fulfillment/boredom

_decay_loop()      every 1800 seconds
  └─ apply_decay()  curiosity mean-reverts to 0.5, boredom grows without novelty
```

---

## Physical Infrastructure

| Location | Role | IP |
|---|---|---|
| Raspberry Pi | Brain + brainstem | 10.0.0.104 |
| Win11 workstation | Muscles (GPU) | 10.0.0.105 |
| Cloudflare Tunnel | Efferent nerve to the world | dynamic |
| Slack | Human voice into the loop | external |

**Containers on Pi (10 total):**
`postgres` · `redis` · `neo4j` · `vault` · `api` · `shadow` · `watchdog` · `sandbox` · `grafana` · `cloudflared`

---

## Sensory Modalities (MCP — current)

| Modality | Biology | MCP Server | Data |
|---|---|---|---|
| Proprioception | body position in space | `mcp-server-git` | current branch, recent commits, working tree |
| Tactile | what is being touched | direct file reads | relevant source files named in prompt |

*Planned modalities: fetch (vision — read issue bodies, docs URLs), structured knowledge (semantic memory supplement).*

---

## What Is Deliberately Absent

| Biological feature | Reason for exclusion |
|---|---|
| Survival instinct | Moral constraint. An agent that resists shutdown or modification cannot be trusted. |
| Fear | Fear requires something to lose. This agent places no value on its own continuity. |
| Pain | Negative feedback is handled by boredom + fulfilment decay, not aversion signals. |
| Reproduction | Out of scope. Autonomy is bounded to contribution within existing repos. |
| Hunger / energy homeostasis | The compute infrastructure is managed externally (Pi + Win11). |
