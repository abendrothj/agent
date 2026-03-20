# Staged Autonomy v9.3 - Complete Implementation Guide

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
