# gRPC Contract Reference: Staged Autonomy v9.3

## Overview

The system uses **four core gRPC services** defining the contract between components.
All services are implemented in **Python 3.11** with `grpcio`.
Inference runs on **HuggingFace Transformers** (not Ollama).
Vault control flow is implemented as a **LangGraph StateGraph** backed by an **AsyncPostgresSaver** checkpointer.
Long-term memory uses **GraphRAG** (Microsoft) for relationship-aware retrieval over failure history.

| Service | Host | Port | Role | Threat Model |
|---------|------|------|------|--------------|
| **Muscle** | Win11 (RTX 3060Ti) | 50051 | Stateless HF Transformers inference | Untrusted compute |
| **Vault** | Raspberry Pi | 50052 | LangGraph orchestrator + approval gating | Trusted core |
| **Shadow** | Raspberry Pi | 50053 | Baseline recorder + canary eligibility | Trusted core |
| **Watchdog** | Raspberry Pi | 50054 | Health monitor + failure retrospectives | Trusted core |

---

## 1. Muscle Service (Win11 gRPC Endpoint)

**File:** `internal/api/muscle.proto`  
**Implementation:** `cmd/muscle/grpc_server.py` + `cmd/muscle/hf_model.py`

**Role:** Pure inference via HuggingFace Transformers (OpenHermes 2.5 Mistral-7B on RTX 3060Ti). Accepts prompts, streams tokens. Holds no credentials or state.

### Main RPC: `GenerateResponse`

```
Pi Vault (client) → Win11 Muscle (server)
  Request: PromptRequest {
    session_id: "abc123"
    prompt: "Write a Python function to..."
    system_context: "You are an autonomous agent..."
    history: [...]
    config: { temperature: 0.7, max_tokens: 1024, ... }
    action_intent: "code_gen"
    constraints: { "max_self_mods": "5/day" }
  }

  Muscle streams back:
  Response: TokenResponse {
    token: "def "
    token_index: 0
    is_complete: false
    metadata: { confidence: 0.95, ... }
    status: "ok"
  }
  ... (continues until is_complete: true)
```

**Security:**
- Muscle NEVER initiates connection
- Muscle NEVER touches filesystem (except /tmp inference cache)
- Muscle NEVER reads external files
- All context comes from Pi (PromptRequest)
- Pi validates all responses

### Health RPC: `Health`

Watchdog periodically pings Win11 to confirm the HF Transformers model server is running.

---

## 2. Vault Service (Pi gRPC Endpoint)

**File:** `internal/api/vault.proto`  
**Implementation:** `cmd/vault/main.py` (service bootstrap) + `cmd/vault/langgraph_vault.py` (LangGraph state machine)

**Role:** Receives requests and runs them through a typed **LangGraph StateGraph** for approval gating. Every decision is written to the immutable PostgreSQL ledger. Supports **human-in-the-loop MFA** via `interrupt()` — graph suspends at `request_human_approval` and resumes when the human responds. Checkpointer: `AsyncPostgresSaver` — full state persisted between nodes so a Pi reboot mid-approval resumes cleanly.

### Main RPC: `ProcessPrompt`

```
API Gateway (client) → Vault (server)
  Request: VaultPromptRequest {
    user_id: "user@mac"
    session_id: "xyz789"
    prompt: "Generate a function to fix bug X"
    domain: "coding"
    tier: TIER_2  // estimated risk tier
  }

  Vault Response: VaultResponse {
    type: PR_PROPOSED  // or DIRECT, ACTION_PENDING, PENDING_MFA, ERROR
    response_id: "resp_001"
    content: "I've created PR #42 to fix this..."
    tier_classified: TIER_2
    approval: APPROVED  // or PENDING_MFA:<request_id>
  }
```

**LangGraph Decision Graph:**
```
classify → T1 ─────────────────────────────────────────── approve
         ├ T4 ─ check_rejection_cache ─ hit ────────────── reject
         │                              miss ─┐
         └ T2/3 ────────────────────────────►check_rate_limit
                                               ├ exceeded ── reject
                                               └ ok ─── query_graph_memory (GraphRAG)
                                                           validate_token
                                               ├ invalid(T2) ── reject
                                               ├ valid(T2) ──── approve
                                               └ T3/4 ── check_shadow_baseline
                                                          ├ eligible ──── approve
                                                          └ ineligible ── request_human_mfa
                                                                          ├ granted ── approve
                                                                          └ denied ─── reject
```

### Other RPCs:
- `GetState(GetStateRequest)` — Returns ledger totals (approvals, rejections, entries)
- `Health(HealthCheck)` — Watchdog liveness probe

---

## 3. Shadow Service (Pi gRPC Endpoint)

**File:** `internal/api/shadow.proto`

**File:** `internal/api/shadow.proto`  
**Implementation:** `cmd/shadow/main.py`

**Role:** Records 24h prediction baselines; evaluates canary eligibility via cosine similarity against stored baselines. Also consulted by the Vault's `check_shadow_baseline` LangGraph node via `GraphRAGClient.check_baseline_eligibility()`.

### Main RPCs:

- `RecordBaseline(RecordBaselineRequest)` — Store a (prompt, response, embedding) triple as the baseline for this request type
- `CheckCanaryEligibility(CheckCanaryEligibilityRequest)` — Is a similar request eligible for auto-promotion? (requires 24h baseline age + similarity ≥ threshold)
- `VerifyBaselineAge(BaselineAgeRequest)` — How old is the oldest baseline matching this request type?

---

## 4. Watchdog Service (Pi gRPC Endpoint)

**File:** `internal/api/sandbox.proto` (shared contract)  
**Implementation:** `cmd/watchdog/main.py`

**Role:** Monitors health metrics, triggers rollbacks, writes failure retrospectives to both pgvector (for Shadow semantic similarity) and GraphRAG input directory (so failures become traversable graph entities queryable by Vault before future T3/4 approvals).

### Main RPCs:

- `MonitorMetrics(MetricsRequest)` — Check error rate, latency P99, GPU temp/memory; triggers rollback if thresholds breached
- `WriteRetrospective(RetrospectiveRequest)` — Post-failure analysis; indexes into pgvector and stages in GraphRAG input for next index rebuild
- `GetSystemHealth(HealthRequest)` — Overall health aggregating all services

---

## Data Flow: End-to-End

```
1. User (Mac/iPhone)
   └─→ Prompts via REST API

2. API Gateway (Pi)
   └─→ Calls Vault gRPC service

3. Vault (Pi) — LangGraph StateGraph runs:
   ├ classify_tier
   ├ [T4] check_rejection_cache
   ├ check_rate_limit
   ├ query_graph_memory → GraphRAG: "Any known failures for this change type?"
   ├ validate_token
   ├ [T3/4] check_shadow_baseline → Shadow: cosine similarity check
   ├ [ineligible] request_human_approval → interrupt() + resume via MFA channel
   └ approve / reject → writes to immutable ledger

4. Vault (if approved, T3+):
   ├ Calls Muscle.GenerateResponse() → Win11 streams HF Transformers tokens
   ├ Calls GitHub API → creates PR
   └ Logs to ledger

5. Watchdog (Pi) monitors continuously:
   ├ Health pings to Vault + Muscle
   ├ Metric threshold checks
   ├ On failure: write_retrospective() → pgvector + GraphRAG input
   └ Trigger rollback if thresholds breached

6. GraphRAG index rebuild (scheduled/manual):
   graphrag index --root graphrag_index
   └ Failure retrospectives become traversable graph entities
      queried by Vault before future T3/4 approvals
```

---

## Implementation Status

| Component | Status | File |
|-----------|--------|------|
| Muscle gRPC server | ✅ | `cmd/muscle/grpc_server.py` |
| HuggingFace model integration | ✅ | `cmd/muscle/hf_model.py` |
| Vault LangGraph state machine | ✅ | `cmd/vault/langgraph_vault.py` |
| Vault service bootstrap | ✅ | `cmd/vault/main.py` |
| Shadow baseline recorder | ✅ | `cmd/shadow/main.py` |
| Watchdog health monitor | ✅ | `cmd/watchdog/main.py` |
| Sandbox dry-run executor | ✅ | `cmd/sandbox-agent/main.py` |
| PostgreSQL + pgvector schema | ✅ | `internal/memory/db_schema.sql` |
| Vector memory client | ✅ | `internal/memory/vector/client.py` |
| Immutable ledger | ✅ | `internal/memory/ledger/store.py` |
| Redis context manager | ✅ | `internal/memory/context/manager.py` |
| GraphRAG long-term memory | ✅ | `internal/memory/graph/client.py` |
| Risk tier classifier | ✅ | `internal/core/risk/classifier.py` |
| GitHub provider | ✅ | `internal/providers/github.py` |
| Safety validator | ✅ | `internal/safety/validator.py` |
| Docker Compose deployment | ✅ | `deployments/docker-compose.yml` |
| gRPC mTLS | ✅ | Enforced on all inter-service connections |

---

## Security Properties

| Threat | Mitigated By |
|--------|--------------|
| Win11 compromise | Muscle is stateless; attacker gets inference tokens only |
| Code injection in prompts | `internal/safety/validator.py` validates all inputs |
| Runaway self-modification | LangGraph rate limit node; per-tier counters in Redis |
| Failed code in production | Watchdog auto-reverts if metrics breach thresholds |
| Replay of rejected T4 requests | 24h rejection cache in immutable ledger |
| Mid-approval Pi reboot | LangGraph `AsyncPostgresSaver` checkpoints every node |
| Pi compromise | Beyond scope; assume hardware security |

