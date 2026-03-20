# Staged Autonomy v9.3

Personal agent that separates **untrusted reasoning** (Win11 GPU) from **trusted decision-making** (Raspberry Pi). The Pi orchestrates, gates approvals, and holds all state and secrets. Win11 does stateless inference only.

## Architecture

```
Win11 (RTX 3060Ti)                    Raspberry Pi
───────────────────                   ────────────────────────────────────────
Muscle                                Vault :50051   ← Muscle connects here
 HuggingFace Transformers              LangGraph 9-node StateGraph
 Stateless inference                   Tier classifier + MFA gating
 GPU metric reporting                  LangGraph PostgresSaver checkpoints
 Activity-gated queuing                GraphRAG long-term memory query
        │                                    │
        │  mTLS gRPC                  Shadow :50053
        └────────────────────────────  24h baseline recorder
                                       Canary eligibility (semantic sim)

                                      Watchdog :50054
                                       GPU / latency / error-rate monitor
                                       Failure retrospective indexer
                                       Rollback trigger

                                      Sandbox :50055  (sandbox_net only)
                                       Ephemeral dry-run executor

                                      PostgreSQL + pgvector
                                       Vector memory, ledger, LangGraph checkpoints

                                      Redis    — rate-limit counters, sessions
                                      Neo4j    — GraphRAG real-time writes
```

## Risk Tier System

| Tier | Name     | Approval        | Shadow min | Sim threshold | Rate limit  |
|------|----------|-----------------|------------|---------------|-------------|
| 1    | Safe     | None            | —          | —             | 1 000 /hr   |
| 2    | Minor    | Self / cached   | —          | —             | 100 /hr     |
| 3    | Major    | Human + MFA     | 24 h       | 85 %          | 10 /hr      |
| 4    | Critical | Human + MFA     | 48 h       | 90 %          | 1 /hr       |

Tier 4 also maintains a 24 h auto-block cache for identical rejected prompts.

## File Structure

```
agent/
├── cmd/
│   ├── muscle/              # Win11 — HuggingFace inference service
│   │   ├── main.py          # Entry point
│   │   ├── grpc_server.py   # Accepts connections from Pi Vault
│   │   ├── hf_model.py      # HuggingFace Transformers wrapper
│   │   ├── vault_client.py  # Calls Pi Vault to propose actions
│   │   ├── activity_monitor.py
│   │   ├── config.py
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── vault/               # Pi — LangGraph orchestrator
│   │   ├── main.py
│   │   ├── grpc_server.py   # Port 50051
│   │   ├── langgraph_vault.py  # 9-node StateGraph
│   │   └── Dockerfile
│   ├── shadow/              # Pi — baseline recorder & canary gating
│   │   ├── main.py
│   │   ├── grpc_server.py   # Port 50053
│   │   └── Dockerfile
│   ├── watchdog/            # Pi — health monitor & retrospective indexer
│   │   ├── main.py
│   │   ├── grpc_server.py   # Port 50054
│   │   └── Dockerfile
│   └── sandbox-agent/       # Pi — ephemeral dry-run executor
│       ├── main.py
│       └── Dockerfile
├── internal/
│   ├── api/                 # .proto files + generated *_pb2.py stubs
│   ├── core/
│   │   ├── risk/            # Tier classifier
│   │   └── metrics/         # Canary / shadow evaluator
│   ├── memory/
│   │   ├── vector/          # pgvector semantic search
│   │   ├── ledger/          # Immutable action log
│   │   ├── context/         # Redis session manager
│   │   └── graph/           # GraphRAG + Neo4j client
│   ├── providers/           # GitHub integration
│   └── safety/              # Blocked-pattern validator
├── deployments/
│   ├── pi/
│   │   ├── docker-compose.yml   # All Pi services
│   │   └── .env.example
│   ├── win11/
│   │   ├── docker-compose.yml   # Muscle only
│   │   ├── .env.example
│   │   └── certs/               # Place muscle.crt, muscle.key, client.crt here
│   └── docker-compose.yml       # Dev all-in-one
├── graphrag_index/
│   ├── settings.yaml
│   ├── prompts/             # Entity extraction, community report, claim extraction
│   └── input/               # Documents staged for next index rebuild
├── policies/
│   ├── approval_rules.yaml
│   ├── canary_thresholds.yaml
│   └── rollback_triggers.yaml
├── configs/
│   └── base_policy.yaml
├── observability/
│   ├── prometheus.yml
│   ├── grafana_datasource.yml
│   └── dashboards/
│       └── vault.json       # Grafana: gRPC rates, GPU telemetry, rollbacks
├── scripts/
│   ├── gen_protos.sh        # Regenerate *_pb2.py from .proto files
│   ├── db_migrate.sh        # Apply schema + verify dirs
│   ├── emergency_rollback.sh
│   └── health_check.sh
└── tests/
    ├── conftest.py          # Shared AsyncMock fixtures
    ├── unit/
    │   ├── test_classifier.py      # Keyword scoring, tier thresholds
    │   ├── test_safety_validator.py # Blocked patterns, scope rules
    │   ├── test_shadow_logic.py    # Cosine similarity, age thresholds
    │   ├── test_watchdog_logic.py  # Rollback trigger thresholds
    │   ├── test_langgraph_nodes.py # Individual LangGraph node logic
    │   └── test_graph_client.py   # GraphRAG client (mocked Neo4j)
    └── integration/
        └── test_approval_flow.py  # In-process gRPC: T1 approve, T4 reject, T3 MFA
```

## Setup

### On the Pi

1. **Install certificates** (see [PI_SETUP.md](PI_SETUP.md)):
   ```bash
   sudo mkdir -p /opt/teammate-vault/certs
   sudo install -m 600 client.crt /opt/teammate-vault/certs/
   sudo install -m 600 client.key /opt/teammate-vault/certs/
   sudo install -m 600 muscle.crt /opt/teammate-vault/certs/
   ```

2. **Generate proto stubs** (once, or after any `.proto` change):
   ```bash
   pip install grpcio-tools>=1.60.0
   bash scripts/gen_protos.sh
   ```

3. **Create networks and start services**:
   ```bash
   docker network create vault_net
   docker network create sandbox_net
   cp deployments/pi/.env.example deployments/pi/.env
   # Edit .env: set MUSCLE_HOST, passwords
   bash scripts/db_migrate.sh
   docker compose --env-file deployments/pi/.env \
     -f deployments/pi/docker-compose.yml up -d
   ```

4. **Verify**:
   ```bash
   bash scripts/health_check.sh
   ```

### On Win11 (Muscle)

1. **Install certificates** (see [WIN11_SETUP.md](WIN11_SETUP.md)) — place in `deployments/win11/certs/`:
   - `muscle.crt` — Win11's TLS certificate
   - `muscle.key` — Win11's private key (never copy to Pi)
   - `client.crt` — Pi's certificate (trust anchor)

2. **Generate proto stubs** (Windows):
   ```cmd
   pip install grpcio-tools
   python -m grpc_tools.protoc -I internal/api --python_out=internal/api --grpc_python_out=internal/api internal/api/muscle.proto internal/api/vault.proto
   ```

3. **Start Muscle**:
   ```cmd
   cp deployments/win11/.env.example deployments/win11/.env
   # Edit .env: set VAULT_API_URL to your Pi's LAN IP
   docker compose --env-file deployments/win11/.env -f deployments/win11/docker-compose.yml up -d
   ```

## Memory System

### Vector Memory (PostgreSQL + pgvector)
Semantic search over PR history and failure patterns. 1024-dim embeddings.
Used by: Shadow (baseline similarity), Watchdog (retrospective storage).

### GraphRAG (Microsoft graphrag + Neo4j)
Two-layer long-term memory:
- **Neo4j** (`memory_store:7687`) — real-time writes; every failure retrospective and baseline indexed immediately as a `:Document` node
- **graphrag parquet index** — LLM-enhanced community detection over PR / failure entities

Rebuild index (requires `OPENAI_API_KEY`):
```bash
graphrag index --root graphrag_index
# incremental:
graphrag index --root graphrag_index --update
```

### Ledger (immutable action log)
Every approval, rejection, rollback written to PostgreSQL. Never deleted.

### Context (Redis, TTL-based)
Short-term session state and per-tier rate-limit counters.

## gRPC Ports

| Service   | Pi Port | Metrics Port | Description                    |
|-----------|---------|--------------|--------------------------------|
| Vault     | 50051   | 8000         | Orchestrator — Muscle connects |
| Shadow    | 50053   | 8001         | Baseline recorder              |
| Watchdog  | 50054   | 8002         | Health monitor                 |
| Sandbox   | 50055   | —            | Dry-run executor               |
| Muscle    | 50051   | —            | Win11 inference (own port)     |

## Observability

- **Prometheus** — scrapes all three Pi services every 15 s
- **Grafana** at `:3000` — Vault throughput, latency p50/p99, GPU temperature gauge, VRAM gauge, rollback events
- Metrics: `vault_grpc_requests_total`, `shadow_grpc_requests_total`, `watchdog_gpu_temp_celsius`, `watchdog_rollbacks_total`

## Testing

Tests run entirely on macOS (or any dev machine) — no Pi, no DB, no gRPC server needed.

```bash
# Install test dependencies
pip install pytest pytest-asyncio

# All unit tests (pure logic, no I/O)
pytest tests/unit/ -v

# Integration tests (in-process gRPC, skips if proto stubs not generated)
pytest tests/integration/ -v

# Run everything
pytest tests/ -v
```

Unit tests cover:
- **`test_classifier.py`** — keyword scoring engine (1 vs 2 hits), `_score_keywords`, all tier boundaries, policy helpers
- **`test_safety_validator.py`** — every blocked pattern, scope allowlist per tier, violation counter throttle, length guard
- **`test_shadow_logic.py`** — `_cosine_similarity` with orthogonal/identical/known-angle vectors, canary sim thresholds (T4=0.90, T3=0.85), baseline age thresholds (T4=48h, T3=24h)
- **`test_watchdog_logic.py`** — exact threshold values pinned, boundary tests for all four trigger conditions (error rate > 0.10, latency > 5000ms, GPU temp > 85°C, VRAM < 512MB)

## Policies

| File | Purpose |
|------|---------|
| `policies/approval_rules.yaml` | MFA / signature requirements per tier |
| `policies/canary_thresholds.yaml` | Success metrics for canary promotion |
| `policies/rollback_triggers.yaml` | Error / latency abort conditions |
| `configs/base_policy.yaml` | Global immutable policy |

## Emergency Procedures

```bash
# Immediate rollback
bash scripts/emergency_rollback.sh

# Database rebuild (destroys all memory)
docker compose -f deployments/pi/docker-compose.yml down -v
bash scripts/db_migrate.sh
docker compose -f deployments/pi/docker-compose.yml up -d
```

## Security Notes

- **Vault is ground truth** — all decisions are immutable in the ledger
- **Muscle is stateless** — no credentials or decision state on Win11
- **mTLS required** — Pi certs in `/opt/teammate-vault/certs/`, Win11 certs in `./certs/`; services fall back to insecure only when cert files are missing (dev mode)
- **Tier 4 cache** — identical rejections auto-block for 24 h (policy immutable)
- **sandbox_net isolation** — Sandbox container has no access to vault_net secrets

## References

- [PI_SETUP.md](PI_SETUP.md) — certificate installation on Pi
- [WIN11_SETUP.md](WIN11_SETUP.md) — Win11 CUDA + certificate setup
- [GrpcContractReference.md](GrpcContractReference.md) — full proto API reference
- `internal/api/*.proto` — service contracts
- `internal/memory/db_schema.sql` — PostgreSQL schema

## Overview

This is a production-ready, self-improving personal agent with staged autonomy architecture. The system separates reasoning (untrusted Muscle on Win11) from decision-making (trusted Vault on Pi), with comprehensive memory persistence, validation, and rollback safety systems.

## Architecture

```
┌─ Win11 (GPU) ─────────────────────────────────────┐
│  Muscle: Stateless LLM reasoning (no state/creds) │
│  - Generates responses via HuggingFace Transformers
│  - Streams tokens via gRPC
│  - Reports GPU metrics
│  - Activity-gated queuing (user active = queue requests)
└──────────────────┬─────────────────────────────────┘
                   │ mTLS gRPC (50051)
                   │
┌─ Raspberry Pi ───┴─────────────────────────────────┐
│                                                     │
│  Vault (50052): Orchestrator & approval gating     │
│  - Classifies requests into 4-tier risk model      │
│  - Enforces approval rules and MFA                 │
│  - Manages immutable ledger                        │
│  - 24-hour Tier 4 rejection cache (auto-block)     │
│                                                     │
│  Shadow (50053): Prediction validator               │
│  - Records 24h baseline predictions                │
│  - Evaluates semantic similarity (canary eligibility)
│  - Manages 48h burn-in for Tier 4                  │
│                                                     │
│  Watchdog (50054): Health monitor                   │
│  - Monitors error rates, latency, GPU thermal      │
│  - Writes failure retrospectives to memory         │
│  - Triggers rollbacks on threshold breaches        │
│                                                     │
│  Sandbox (50055): Dry-run execution               │
│  - Runs ephemeral requests without approval        │
│  - Collects metrics for canary validation          │
│  - Integrated with gRPC to Muscle                  │
│                                                     │
│  PostgreSQL + pgvector: Persistent memory         │
│  - Vector embeddings: PR history, failure patterns │
│  - Ledger: Immutable action log (decisions)       │
│  - Baselines: Shadow predictions for canary       │
│                                                     │
│  Redis: Session context (short-term working mem)   │
│  - Rate limit tracking                             │
│  - Session state                                   │
│                                                     │
└─────────────────────────────────────────────────────┘
```

## Risk Tier System

**Tier 1 (SAFE)**: Read-only queries, no approval needed
- Auto-approved
- Rate limit: 1000 req/hour
- Example: "List git commits"

**Tier 2 (MINOR)**: Local testing, minimal scope
- Requires approval (can be self-approved)
- Rate limit: 100 req/hour
- Example: "Run unit tests in sandbox"

**Tier 3 (MAJOR)**: Code changes, requires 24h shadow baseline
- Requires human approval + MFA
- 24-hour Shadow baseline minimum
- Semantic similarity check (85%+)
- Rate limit: 10 req/hour
- Example: "Create GitHub PR with code changes"

**Tier 4 (CRITICAL)**: System-level changes, 24h rejection cache
- Requires dual human approval + MFA
- 48-hour Shadow baseline minimum
- Semantic similarity check (90%+)
- **Identifier rejection cache**: If same request rejected in Tier 4, auto-block for 24h (immutable policy)
- Watchdog review required before canary
- Rate limit: 1 req/hour
- Example: "Modify Vault approval rules"

## Deployment

### Quick Start (macOS/Linux with Pi)

1. **Ensure dependencies**:
   ```bash
   # On Pi
   sudo apt-get install -y docker.io docker-compose postgresql-client redis-tools netcat
   sudo usermod -aG docker $(whoami)
   ```

2. **Initialize database**:
   ```bash
   cd /path/to/agent
   bash scripts/db_migrate.sh
   ```

3. **Launch services**:
   ```bash
   docker-compose up -d
   ```

4. **Verify health**:
   ```bash
   bash scripts/health_check.sh
   ```

### Production Setup (Kubernetes)

See `deployments/k8s/` for Kubernetes manifests:
- NetworkPolicy: Enforce service isolation
- Secrets: mTLS cert rotation
- Persistent volumes: PostgreSQL + pgvector
- StatefulSet: Ordered service startup

## Memory System

### Vector Memory (PostgreSQL + pgvector)

Semantic search across PR history and failure patterns:

```sql
-- 1024-dim embeddings (HuggingFace codec)
SELECT id, text, similarity_score
FROM vector_entries
WHERE embedding <-> query_embedding < 0.3  -- cosine similarity
ORDER BY similarity_score DESC
LIMIT 10;
```

Used by: Shadow (baseline similarity), Watchdog (retrospective storage).

### GraphRAG Memory (Knowledge Graph)

Relationship-aware long-term memory over codebase history:

```
Entities:       file, function, pull_request, failure, deployment
Relationships:  caused, fixed, modified, introduced, approved, rejected

Local query:  "What failures are linked to changes in auth.py?"
Global query: "What are the most common root causes of rollbacks?"
```

Built with Microsoft's `graphrag` package. Index lives in `graphrag_index/`.
Vault queries GraphRAG in the `query_graph_memory` node before approving T3/T4.
Watchdog automatically stages every failure retrospective for the next index rebuild.

Rebuild index:
```bash
graphrag index --root graphrag_index
# or incremental:
graphrag index --root graphrag_index --update
```

### Ledger (Immutable Action Log)

Every approval, rejection, and execution is recorded:

```sql
-- Example queries
SELECT * FROM ledger_entries
WHERE action_type = 'approve' AND actor_id = 'vault'
ORDER BY timestamp_ms DESC
LIMIT 100;
```

### Context (Redis, TTL-based)

Short-term working memory with automatic expiry:

```python
await context.create_session("session-123", ttl_seconds=3600)
await context.set_state("session-123", "request_count", 5)
```

## Safety Policies

All policies are defined in YAML and mounted read-only:

- **`policies/approval_rules.yaml`**: MFA/signature requirements per tier
- **`policies/canary_thresholds.yaml`**: Success metrics for promotion
- **`policies/rollback_triggers.yaml`**: Error/latency abort conditions
- **`configs/base_policy.yaml`**: Global immutable policy (service duties, memory access, tier definitions)

See each file for complete tier-specific configuration.

## gRPC Services

### Vault Service (50052)

```protobuf
service Vault {
  rpc RequestApproval(RequestApprovalRequest) returns (RequestApprovalResponse);
  rpc GetState(GetStateRequest) returns (GetStateResponse);
}
```

### Memory Service (integrated)

```protobuf
service Memory {
  rpc SemanticSearch(SemanticSearchRequest) returns (SemanticSearchResponse);
  rpc QueryLedger(LedgerQueryRequest) returns (LedgerQueryResponse);
  rpc WriteVector(WriteVectorRequest) returns (WriteVectorResponse);
  rpc WriteLedger(WriteLedgerRequest) returns (WriteLedgerResponse);
}
```

### Shadow Service (50053)

```protobuf
service Sandbox {
  rpc RunDryOp(RunDryOpRequest) returns (RunDryOpResponse);
  rpc RecordBaseline(RecordBaselineRequest) returns (RecordBaselineResponse);
  rpc CheckCanaryEligibility(CheckCanaryEligibilityRequest) returns (CheckCanaryEligibilityResponse);
}
```

## Emergency Procedures

### Manual Rollback

```bash
bash scripts/emergency_rollback.sh
```

This:
1. Stops all services
2. Clears Redis cache
3. Verifies Muscle health
4. Restarts services in dependency order

### Database Recovery

PostgreSQL uses persistent volumes. To rebuild:

```bash
docker-compose down -v  # Remove volumes
bash scripts/db_migrate.sh
docker-compose up -d
```

## File Structure

```
agent/
├── cmd/
│   ├── muscle/          # Win11 inference service (HuggingFace Transformers)
│   ├── vault/           # Pi orchestrator + LangGraph approval gating
│   │   ├── main.py            # Service bootstrap
│   │   └── langgraph_vault.py # LangGraph StateGraph (9 nodes)
│   ├── shadow/          # Pi prediction validator
│   ├── watchdog/        # Pi health monitor + GraphRAG indexer
│   └── sandbox-agent/   # Pi dry-run executor
├── internal/
│   ├── api/             # Protobuf contract definitions
│   ├── core/
│   │   ├── risk/        # Tier classifier
│   │   └── metrics/     # Canary/shadow evaluator
│   ├── memory/
│   │   ├── vector/      # pgvector semantic search
│   │   ├── ledger/      # Immutable action log
│   │   ├── context/     # Redis session manager
│   │   └── graph/       # GraphRAG knowledge graph client
│   ├── providers/       # GitHub integration
│   └── safety/          # Policy validator
├── graphrag_index/      # GraphRAG index root
│   ├── settings.yaml      # GraphRAG configuration
│   └── input/             # Documents staged for indexing
├── policies/            # YAML policy definitions
│   ├── approval_rules.yaml
│   ├── canary_thresholds.yaml
│   └── rollback_triggers.yaml
├── configs/
│   └── base_policy.yaml # Global immutable policy
├── deployments/
│   ├── docker-compose.yml
│   ├── k8s/             # Kubernetes manifests
│   └── terraform/       # Cloud infrastructure
├── scripts/
│   ├── emergency_rollback.sh
│   ├── db_migrate.sh
│   └── health_check.sh
└── observability/       # Dashboards, logs, traces
    ├── dashboards/
    ├── logs/
    └── tracing/
```

## Development Workflow

1. **Process requests**:
   ```python
   vault = VaultService()
   await vault.initialize()
   # LangGraph StateGraph runs automatically:
   approved, reason, tier = await vault.process_request(
       request_id="req-123",
       prompt="...",
       system_context="...",
       approval_token="mfa:abc123",
   )

   # Resume after human MFA pause (if reason starts with PENDING_MFA:):
   approved, reason, tier = await vault.resume_after_mfa(
       request_id="req-123",
       human_approved=True,
   )
   ```

2. **Record baselines**:
   ```python
   shadow = ShadowService()
   await shadow.initialize()
   vector_id = await shadow.record_baseline(request_id, prompt, response, embedding, tier)
   ```

3. **Monitor health**:
   ```python
   watchdog = WatchdogService()
   await watchdog.initialize()
   should_rollback, reason = await watchdog.monitor_metrics(...)
   ```

## Testing

### Unit Tests
```bash
pytest internal/core/test_classifier.py -v
pytest internal/core/test_metrics.py -v
```

### Integration Tests
```bash
# Start services
docker-compose up -d

# Run integration tests
pytest tests/integration/ -v
```

### Full System Test
```bash
bash scripts/health_check.sh          # Verify all services responding
python tests/system_acceptance.py     # End-to-end workflow
```

## Monitoring & Observability

### Prometheus Metrics
- `vault_approvals_total`: Total approvals issued
- `vault_rejections_24h`: Rejections in last 24h
- `watchdog_rollbacks_total`: Rollback count
- `shadow_baseline_age_hours`: Current baseline age

### Logs
- JSON-formatted logs to stdout (docker-compose)
- Structured logging: `{"timestamp", "level", "service", "message", "metadata"}`

### Traces (OpenTelemetry)
- Request flow across Vault → Shadow → Muscle
- Decision latency tracking
- Semantic search performance

## Known Limitations & Future Work

- **mTLS**: Currently simplified; production needs full certificate chain validation
- **GraphRAG LLM**: Requires an OpenAI API key for entity extraction during index builds; swap for a local LLM once graphrag supports offline models
- **GraphRAG rebuild**: Index rebuild is LLM-call intensive; run nightly or on major memory writes (not on every retrospective)
- **Muscle Communication**: gRPC client to Win11 needs full proto compilation before production use
- **Distributed Ledger**: Single PostgreSQL; future versions could use a replicated setup for high availability

## Security Notes

- **Vault is ground truth**: All decisions immutable in ledger
- **Muscle is stateless**: No credentials or decision state on Win11
- **24h cache enforced**: Tier 4 identical rejections auto-block (policy immutable)
- **mTLS required**: All inter-service communication encrypted + authenticated
- **No external network**: Services communicate only within local network
- **Persistent volumes**: Memory survives service restarts

## Support & Troubleshooting

### Services won't start
```bash
docker-compose logs vault
docker-compose logs postgres
```

### Muscle not responding
```bash
# From Pi
nc -zv <MUSCLE_HOST> 50051
```

### Memory queries slow
- Check PostgreSQL: `docker-compose logs postgres`
- Verify pgvector index: `SELECT * FROM pg_stat_user_indexes WHERE indexname LIKE '%embedding%';`
- Increase shared_buffers or effective_cache_size in docker-compose.yml

### Rate limiting triggered
- Query Redis: `redis-cli HGETALL context:session-123`
- Clear: `redis-cli FLUSHDB` (development only)

## References

- **Risk Tier System**: See `policies/approval_rules.yaml`
- **Canary Rules**: See `policies/canary_thresholds.yaml`
- **Rollback Logic**: See `policies/rollback_triggers.yaml`
- **Base Policy**: See `configs/base_policy.yaml`
- **gRPC Contracts**: See `internal/api/*.proto`
- **Database Schema**: See `internal/memory/db_schema.sql`
