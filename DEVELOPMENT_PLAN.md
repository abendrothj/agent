# Teammate v9.3: Full Development Plan

**Objective:** Build a self-improving personal agent system across Raspberry Pi (trusted core), Win11 3060Ti (untrusted compute), and remote control via Mac/iPhone.

**Completion Target:** Production-ready system capable of:
- Autonomous code generation and self-modification
- GitHub PR creation/approval workflow
- Real-time learning from execution feedback
- Remote access from anywhere
- Sub-24h iteration cycles (Shadow validation → merge → learn)

---

## Phase 1: Foundation & Infrastructure ✅ (COMPLETE)

### 1.1 gRPC Contracts [DONE]
- ✅ `internal/api/muscle.proto` — Win11 inference interface
- ✅ `internal/api/vault.proto` — Pi orchestrator interface
- ✅ `internal/api/shadow.proto` — Pi validator interface
- ✅ `GrpcContractReference.md` — Data flow documentation

**Deliverable:** All three proto files, fully documented with RPC signatures and security notes.

---

## Phase 2: Win11 Muscle Service (Untrusted Compute)

### 2.1 Ollama Wrapper & gRPC Server
**Location:** `cmd/muscle/` (Windows/Go)

**Sub-tasks:**
- [ ] 2.1.1 Install Ollama on Win11, configure 3060Ti CUDA support
- [ ] 2.1.2 Implement Go gRPC server: `GenerateResponse()` RPC
  - Stream tokens from Ollama inference
  - No filesystem writes beyond /tmp cache
  - No network access except to Pi (gRPC tunnel)
  - Validate `PromptRequest` (reject if > 50KB)
- [ ] 2.1.3 Implement `Health()` RPC for liveness checks
- [ ] 2.1.4 Implement request timeout (10s per token stream)
- [ ] 2.1.5 Isolation: Run Muscle in Windows sandbox/AppContainer if possible
- [ ] 2.1.6 Logging: All requests logged (no secrets) to shared audit volume

**Test Plan:**
- Local: Send valid PromptRequest, verify token stream
- Local: Send oversized prompt, verify rejection
- Security: Attempt filesystem access from RPC, verify blocked
- Network: Confirm only Pi connection allowed

**Success Criteria:**
- Muscle responds to 100 consecutive prompts without crash
- Inference latency: < 2s per token (depends on model size)
- Zero filesystem persistence after shutdown

---

## Phase 3: Raspberry Pi Core Services

### 3.1 Vault Service (Orchestrator & Gatekeeper)
**Location:** `cmd/vault/` (Linux/Go)

**Sub-tasks:**
- [ ] 3.1.1 Implement core data structures:
  - `RiskClassifier` — Tier 1-4 classification logic
  - `RateLimiter` — max_changes_per_day enforcement
  - `SecretManager` — GitHub PAT, API keys (encrypted at rest)
- [ ] 3.1.2 Implement `ProcessPrompt()` RPC
  - Classify risk tier
  - Route to Muscle for reasoning (if needed)
  - Validate/sanitize Muscle output
  - Call Shadow for dry-run (if Tier 3+)
  - Execute approved actions
  - Log everything
- [ ] 3.1.3 GitHub integration layer:
  - Client for agent's own GitHub repo
  - Create PR with diff
  - Merge PR with commit message
  - Manage branch lifecycle
- [ ] 3.1.4 Implement rate limiting:
  - Max 5 self-modifications per day (configurable)
  - Per-domain daily limits
  - Cooldown between Tier 4 actions
- [ ] 3.1.5 Cryptographic signing:
  - Sign PRs with Vault's private key
  - Shadow verifies before dry-run
  - Watchdog verifies before execution
- [ ] 3.1.6 File system access:
  - Agent codebase directory (read/write)
  - Config directory (read-only mount)
  - Audit logs directory (append-only)
  - Secrets directory (encrypted)

**Internal Dependencies:**
- `internal/core/risk/classifier.go` — Risk tier logic
- `internal/core/metrics/ratelimit.go` — Governance enforcement
- `internal/providers/github.go` — GitHub API wrapper
- `internal/safety/validator.go` — Muscle response validation

**Test Plan:**
- Unit: Test RiskClassifier on sample prompts
- Unit: Test RateLimiter enforcement
- Integration: Full loop: prompt → Muscle → Vault → decision
- Integration: GitHub PR creation/merge

**Success Criteria:**
- Vault routes low-risk queries directly (no latency from Shadow)
- Vault enforces rate limits without false positives
- All decisions are cryptographically signed
- Audit log captures 100% of actions

---

### 3.2 Shadow Service (Dry-Run Validator)
**Location:** `cmd/shadow/` (Linux/Go)

**Sub-tasks:**
- [ ] 3.2.1 Code analysis layer:
  - Static analysis: Parse generated code, detect syntax errors
  - Pattern detection: Flag risky operations (rm -rf, infinite loops, etc.)
  - Type checking: Validate Python/Go type signatures
- [ ] 3.2.2 Sandbox execution:
  - Spin up isolated Docker container per validation
  - Run generated code with mock data
  - Capture stdout/stderr
  - Timeout: 30s max execution
  - Kill container after validation
- [ ] 3.2.3 Prediction engine:
  - Compare Shadow prediction vs. Vault decision
  - Log prediction accuracy
  - Surface trends (accuracy degrading = signal to update)
- [ ] 3.2.4 24-hour observation tracker:
  - Per Muscle version: track how long it's been observable
  - Canary eligibility: 24h+ AND accuracy >= 98%
  - Surface readiness status via `QueryReadiness()`
- [ ] 3.2.5 Learning loop:
  - Accept `RecordOutcome()` with actual execution results
  - Update prediction accuracy metrics
  - Flag newly discovered issue patterns

**Internal Dependencies:**
- `internal/core/metrics/shadow_metrics.go` — Prediction tracking
- Test environment setup (mock APIs, test data)

**Test Plan:**
- Unit: Analyze sample code, detect risky patterns
- Integration: Dry-run a simple code change, verify sandbox isolation
- Learning: Record outcome, verify accuracy metric update
- Monitoring: Simulate 24h observation period

**Success Criteria:**
- Shadow detects obvious bugs before merge (syntax, type errors)
- Shadow prediction accuracy >= 95% on first batch
- Shadow blocks infinite loops, unhandled exceptions
- Readiness tracker reports accurate canary eligibility

---

### 3.3 Watchdog Service (Safety Net & Auto-Rollback)
**Location:** `cmd/watchdog/` (Linux/Go)

**Sub-tasks:**
- [ ] 3.3.1 Health monitoring:
  - Periodic `Vault.Health()` checks
  - Periodic `Muscle.Health()` checks (to Win11)
  - Alert if either unreachable > 5 min
- [ ] 3.3.2 Metrics collection:
  - Pull canary metrics (error rate, latency, throughput)
  - Compare against `policies/rollback_triggers.yaml`
  - If any threshold breached: trigger rollback
- [ ] 3.3.3 Git rollback automation:
  - Detect failed commit hash
  - Run `git revert <hash>`
  - Force-push to main
  - Alert Pi operator
- [ ] 3.3.4 Post-mortem logging:
  - Capture why rollback triggered
  - Store before/after metrics
  - Link to Shadow prediction (was it a miss?)

**Internal Dependencies:**
- `policies/rollback_triggers.yaml` — Thresholds configuration
- Git client library
- Metrics collection from observability stack

**Test Plan:**
- Integration: Simulate metric threshold breach, verify rollback
- Integration: Verify health checks detect service down
- Chaos: Kill Muscle, verify Watchdog alerts

**Success Criteria:**
- Watchdog detects failures within 60s
- Rollback completes within 2 min
- Zero false positive rollbacks over 7 days

---

## Phase 4: Governance & Policies

### 4.1 Risk Tier Classifier
**Location:** `internal/core/risk/classifier.go` (Logic)

**Sub-tasks:**
- [ ] 4.1.1 Tier 1 (Read/Search/Diff):
  - Prompt analysis → classify as discovery only
  - Examples: "List files in /src", "Show git diff"
  - No approval needed, immediate response
- [ ] 4.1.2 Tier 2 (Build/Test/Lint):
  - Sandbox-isolated execution
  - Examples: "Run pytest", "Check linter"
  - Approval: No (low risk, no side effects)
- [ ] 4.1.3 Tier 3 (Deploy/Merge/Tag):
  - Production changes
  - Examples: "Merge feature branch", "Tag release"
  - Approval: Shadow required (24h observation or fast-pass)
- [ ] 4.1.4 Tier 4 (Firewall/IAM/Billing/Secrets):
  - High-impact infrastructure
  - Examples: "Update GitHub permissions", "Modify API key rotation"
  - Approval: Would require human in strict mode, but permissive mode auto-approves after Shadow

**Rules File:** `policies/approval_rules.yaml`
```yaml
tier_1_keywords: [read, search, diff, ls, cat, grep]
tier_2_keywords: [test, lint, build, check, dry-run]
tier_3_keywords: [deploy, merge, commit, tag, push]
tier_4_keywords: [firewall, iam, billing, secrets, permissions, rotate]
```

**Test Plan:**
- Unit: Classify 20 diverse prompts, verify tier predictions
- Edge case: Ambiguous prompts ("Generate code") — should default to what?

**Success Criteria:**
- Classifies 95% of prompts with correct tier on first pass
- Rare misclassifications don't break system (Shadow catches them)

---

### 4.2 Rate Limiting & Governance
**Location:** `internal/core/metrics/ratelimit.go` (Logic)

**Sub-tasks:**
- [ ] 4.2.1 Implement per-day limits:
  - max_changes_per_day: 5
  - Tracked per calendar day (UTC midnight)
  - Reset at 00:00 UTC
- [ ] 4.2.2 Per-tier limits:
  - Tier 3: max 2 per day
  - Tier 4: max 1 per day (if ever needed)
- [ ] 4.2.3 Cooldown windows:
  - After Tier 4 action: 6h cooldown before next Tier 4
  - After rollback: 2h cooldown on related code
- [ ] 4.2.4 PR scope validation:
  - Reject PRs with > 500 lines changed (enforce single-task)
  - Reject PRs touching > 5 files (encourage focused changes)

**Rules File:** `policies/governance.yaml`
```yaml
max_changes_per_day: 5
tier_3_max_per_day: 2
tier_4_max_per_day: 1
tier_4_cooldown_hours: 6
max_pr_lines: 500
max_pr_files: 5
```

**Test Plan:**
- Unit: Simulate 5 changes, verify 6th is rejected
- Unit: Verify cooldown windows block actions

**Success Criteria:**
- Zero over-limit changes allowed
- Accurate daily reset
- No race conditions on concurrent requests

---

### 4.3 Canary & Rollback Thresholds
**Location:** `policies/rollback_triggers.yaml`

**Sub-tasks:**
- [ ] 4.3.1 Define error rate thresholds:
  - Canary max error increase: 0.1% (from baseline)
  - If observed: trigger rollback
- [ ] 4.3.2 Define latency thresholds:
  - Canary max p95 latency: 250ms
  - If exceeded: trigger rollback
- [ ] 4.3.3 Define availability thresholds:
  - Min availability: 99%
  - If below: trigger rollback

**Config:**
```yaml
canary_max_error_increase: 0.001  # 0.1%
canary_max_latency_p95_ms: 250
canary_min_availability: 0.99
observation_window: 120s
```

**Test Plan:**
- Integration: Simulate metric breach, verify rollback triggered

**Success Criteria:**
- Rollback triggers within 60s of threshold breach
- No premature rollbacks from normal variance

---

## Phase 5: API Gateway & Remote Access

### 5.1 REST API Gateway
**Location:** `cmd/vault/` (extends Vault service)

**Sub-tasks:**
- [ ] 5.1.1 REST endpoints:
  - `POST /prompt` — Send prompt, get response
  - `GET /status` — Get agent status (rate limits, Muscle health, etc.)
  - `GET /history/<session_id>` — Get conversation history
  - `POST /rollback/<commit_hash>` — Manual override (admin only)
- [ ] 5.1.2 Authentication & authorization:
  - API key scheme (generated for Mac/iPhone)
  - Rate limits per API key: 100 req/min
  - Log all API access (audit trail)
- [ ] 5.1.3 Session management:
  - Track conversation state across requests
  - Timeout: 1h idle session
  - Clear sensitive data on logout

**API Spec (OpenAPI/Swagger):**
```yaml
POST /prompt:
  requestBody:
    prompt: string
    domain: "coding" | "research" | "writing"
  responses:
    200:
      response_id: string
      type: "direct" | "pr_proposed" | "action_pending"
      content: string
      approval_state: string
```

**Test Plan:**
- Local: Test all endpoints with valid requests
- Security: Test auth bypass attempts, rate limit exceed
- Remote: Test from Mac, verify latency acceptable

**Success Criteria:**
- All endpoints respond within 500ms (excluding Muscle latency)
- Rate limiting works correctly
- Sessions persist across reconnects

---

### 5.2 Client SDKs
**Platforms:** macOS, iOS

**Sub-tasks:**
- [ ] 5.2.1 Python/CLI client:
  - `agent prompt "What should I work on?"`
  - `agent status`
  - `agent history`
- [ ] 5.2.2 Build simple web UI (optional Phase 2):
  - Prompt input field
  - Real-time response streaming
  - Status dashboard (rate limits, health)

**Test Plan:**
- Functional: Send prompts from Mac terminal
- Network: Test over WiFi, verify reconnect handling

**Success Criteria:**
- CLI client works from anywhere on network
- Streaming responses appear real-time

---

## Phase 6: GitHub Integration & Self-Modification

### 6.1 GitHub API Integration
**Location:** `internal/providers/github.go`

**Sub-tasks:**
- [ ] 6.1.1 Agent's own repository:
  - Create or use existing GitHub repo for agent code
  - Agent generates PAT (Personal Access Token) once, store encrypted on Pi
  - PAT scoped to: repo, workflow (PR creation only)
- [ ] 6.1.2 PR creation workflow:
  - Branch: `feature/auto-<session_id>-<timestamp>`
  - Commit message: "Auto-generated: [proposal from Muscle]"
  - PR title: "Self-improve: [description]"
  - PR body: Links to Shadow validation, metrics
- [ ] 6.1.3 PR auto-merge (if Shadow approves):
  - Wait for Shadow validation pass
  - Auto-merge (squash commit)
  - Delete feature branch
  - Log merge details
- [ ] 6.1.4 Revert automation:
  - If Watchdog triggers rollback: auto-revert commit
  - Push to main
  - Create incident issue

**Test Plan:**
- Integration: Create PR to agent repo, verify structure
- Integration: Shadow validates, verify auto-merge
- Integration: Trigger rollback, verify revert

**Success Criteria:**
- PRs created with correct metadata
- Auto-merge works reliably
- Revert completes without conflicts

---

### 6.2 Self-Improvement Loop
**Location:** Docs + workflow automation

**Sub-tasks:**
- [ ] 6.2.1 Learning from execution:
  - After PR merge: run agent's new code for 24h
  - Shadow logs predicted vs. actual behavior
  - If divergence: agent notices and proposes fix
  - Agent creates new PR to "fix the fixer"
- [ ] 6.2.2 Capability evolution:
  - Agent tracks what it CAN do (capabilities list)
  - Agent periodically asks: "What new capability would be useful?"
  - Agent proposes code changes to expand capabilities
  - Gated by Shadow validation (prevents runaway expansion)
- [ ] 6.2.3 Metrics-driven improvement:
  - Agent observes: "My code is generating 50% success on X"
  - Agent proposes: "I'll add error handling for X"
  - Agent creates PR, Shadow validates, merge if safe

**Test Plan:**
- Scenario: Agent generates code, execution fails, agent fixes it
- Scenario: Agent learns new capability, validates it works

**Success Criteria:**
- Self-improvement loop completes in < 24h
- Agent fixes bugs in generated code autonomously
- New capabilities properly validated before merge

---

## Phase 7: Observability & Monitoring

### 7.1 Audit Logging
**Location:** `observability/logs/`

**Sub-tasks:**
- [ ] 7.1.1 Append-only audit log:
  - Every Vault decision logged
  - Every Muscle call logged
  - Every Shadow validation logged
  - Every action executed logged
  - Format: JSON, with timestamp, trace ID, decision rationale
- [ ] 7.1.2 Log retention:
  - Keep 30 days of detailed logs
  - Archive older logs
  - Exportable for analysis

**Implementation:** Simple JSON line file, rotated daily.

**Success Criteria:**
- 100% decision capture
- Logs enable full audit trail reconstruction

---

### 7.2 Health Dashboard (Optional Phase 2)
**Location:** `observability/dashboards/`

**Metrics to expose:**
- Muscle availability (% uptime)
- Vault request latency (p50, p95, p99)
- Shadow validation success rate
- Self-modification frequency (PRs/day)
- Canary metrics (error rate, latency)
- Watchdog rollback frequency

**Implementation:** Prometheus metrics + Grafana dashboard.

---

## Phase 8: Deployment & Operations

### 8.1 Container Setup
**Location:** `deployments/docker-compose.yml` (local dev/staging)

**Sub-tasks:**
- [ ] 8.1.1 Multi-container compose:
  - Muscle (Win11 — skip in compose, manual setup)
  - Vault (Pi service)
  - Shadow (Pi service)
  - Watchdog (Pi service)
  - Volumes: agent codebase, configs (RO), secrets (encrypted)
- [ ] 8.1.2 Network settings:
  - Internal network for services
  - Vault→Muscle tunnel (mTLS gRPC)
  - Exposed REST API port (8080 externally)
  - No direct external access to Vault/Shadow/Watchdog
- [ ] 8.1.3 Secrets management:
  - GitHub PAT encrypted at rest
  - Load from environment (Pi trusted admin)
  - Never log secrets

**Test Plan:**
- Local: docker-compose up, verify all services healthy

**Success Criteria:**
- All services start cleanly
- Health checks pass
- mTLS tunnel established between Pi and Win11

---

### 8.2 Kubernetes Deployment (Optional Phase 2)
**Location:** `deployments/k8s/`

For production Pi cluster (if scaling to multiple Pis).

---

### 8.3 Operational Runbook
**Location:** `scripts/`

**Sub-tasks:**
- [ ] 8.3.1 Emergency rollback script: `emergency_rollback.sh`
  - Immediate revert of latest commit
  - Kill all services, restart from known-good state
- [ ] 8.3.2 Token rotation script: `rotate_approval_tokens.sh`
  - If GitHub PAT expires, rotate safely
  - Update encrypted secret store
- [ ] 8.3.3 Backup/restore procedures
  - Backup agent codebase
  - Backup audit logs
  - Restore procedures tested

**Success Criteria:**
- Emergency rollback takes < 5 min
- Token rotation doesn't interrupt service

---

## Phase 9: Testing & Validation

### 9.1 Unit Tests
**Location:** `*_test.go` (throughout codebase)

**Target:** 80% coverage on core logic
- Risk tier classifier
- Rate limiter
- Shadow validation
- GitHub integration

---

### 9.2 Integration Tests
**Location:** `tests/integration/`

**Scenarios:**
1. End-to-end: Mac prompt → Vault → Muscle → response → display
2. Self-modify: Muscle proposes code change → Shadow validates → PR merges
3. Rollback: Merged code fails → Watchdog reverts → logs incident
4. Learning: Execution diverges from Shadow prediction → agent notices and fixes

**Test Plan:**
- Each scenario: verify all steps execute correctly
- Each scenario: verify audit logs capture everything
- Each scenario: verify no unhandled exceptions

---

### 9.3 Chaos Engineering
**Location:** `tests/chaos/`

**Failure scenarios to test:**
- Win11 Muscle goes down
- Pi loses GitHub connectivity
- Canary metrics spike
- PR merge conflicts
- Duplicate concurrent requests

**Success Criteria:**
- System recovers gracefully from each failure
- Audit log explains what happened
- No data corruption

---

## Phase 10: Documentation

### 10.1 Architecture Docs
- [x] `GrpcContractReference.md` — API contracts
- [ ] `ARCHITECTURE.md` — System design, threat model, security properties
- [ ] `DEVELOPMENT.md` — How to extend/modify the system

### 10.2 Operational Docs
- [ ] `DEPLOYMENT.md` — How to run on Pi + Win11
- [ ] `TROUBLESHOOTING.md` — Common issues and fixes
- [ ] `RUNBOOK.md` — Emergency procedures

### 10.3 API Docs
- [ ] OpenAPI/Swagger spec for REST API
- [ ] gRPC service documentation

---

## Timeline & Sequencing

| Phase | Estimated Effort | Dependencies | Target Date |
|-------|------------------|--------------|-------------|
| 1. gRPC Contracts | 1 day | None | ✅ Done |
| 2. Win11 Muscle | 2-3 days | Phase 1 | Week 1 |
| 3. Pi Core Services | 5-7 days | Phase 1, 2 | Week 2-3 |
| 4. Governance & Policies | 2-3 days | Phase 3 | Week 3 |
| 5. API Gateway & Remote | 2-3 days | Phase 3 | Week 3-4 |
| 6. GitHub Integration | 2-3 days | Phase 3, 5 | Week 4 |
| 7. Observability | 1-2 days | Phase 3 | Week 4 |
| 8. Deployment | 1-2 days | Phase 3, 7 | Week 4 |
| 9. Testing & Validation | 3-5 days | All | Week 5 |
| 10. Documentation | 2-3 days | All | Week 5 |
| **Total** | **~25-35 days** | — | **8-10 weeks** |

---

## Success Criteria (Final Product)

### Functionality
- [ ] Agent accepts prompts from Mac/iPhone, responds with generated content
- [ ] Agent creates PRs to its own code
- [ ] Agent merges PRs safely (Shadow validates first)
- [ ] Agent learns from execution feedback
- [ ] Agent reverts failed changes autonomously (Watchdog)

### Performance
- [ ] Prompt-to-response latency: < 10s (including Muscle inference)
- [ ] Shadow validation: < 30s
- [ ] PR merge: < 1 min
- [ ] Rollback: < 5 min

### Reliability
- [ ] Uptime: 99%+ (excluding planned maintenance)
- [ ] Zero data loss
- [ ] Zero security breaches (in permissive mode, low risk by design)
- [ ] Graceful recovery from service failures

### Safety
- [ ] Infinite loops detected and blocked (Shadow)
- [ ] Rate limits enforced (max 5 changes/day)
- [ ] Failed changes reverted automatically (Watchdog)
- [ ] All actions audited (100% capture)

---

## Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| Win11 compromise | All state on Pi, Muscle stateless compute-only |
| Code injection via prompts | Vault validates Muscle outputs before execution |
| Runaway self-modification | Shadow dry-runs, Watchdog monitors, rate limits |
| Loss of audit trail | Append-only logging, backup daily |
| GitHub PAT compromise | Encrypted at rest, minimal scoping, rotation procedure |
| Concurrent request race | Use file locks on shared state (agent codebase) |

---

## Notes for Implementation

1. **Start with single-prompt, single-response** — Don't build conversation history first. Get end-to-end working.
2. **Test each phase independently** — Don't integrate everything at once.
3. **Gradual rollout** — Run Shadow in parallel mode first (log predictions, don't block). Then enable blocking.
4. **Monitor Shadow accuracy** — If prediction accuracy < 90%, pause auto-merge pending investigation.
5. **Keep secrets minimal** — Only GitHub PAT and maybe one API key. Avoid other credentials.
6. **Audit first, optimize later** — Comprehensive logging is non-negotiable.

