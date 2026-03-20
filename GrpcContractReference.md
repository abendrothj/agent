# gRPC Contract Reference: Teammate v9.3

## Overview

The system uses **three core gRPC services** that define the contract between components:

| Service | Hostname | Role | Threat Model |
|---------|----------|------|--------------|
| **Muscle** | Win11 (3060Ti) | Stateless inference | Untrusted compute |
| **Vault** | Raspberry Pi | Orchestrator, gatekeeper, decisions | Trusted core |
| **Shadow** | Raspberry Pi | Dry-run validator, learner | Trusted core |

---

## 1. Muscle Service (Win11 gRPC Endpoint)

**File:** `internal/api/muscle.proto`

**Role:** Pure inference. Accepts prompts, returns tokens. Nothing else.

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

  Muscle Streams back:
  Response: TokenResponse {
    token: "def "
    token_index: 0
    is_complete: false
    metadata: { confidence: 0.95, ... }
    status: "ok"
  }
  
  Response: TokenResponse {
    token: "calculate"
    token_index: 1
    is_complete: false
    ...
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

Watchdog periodically pings Win11 to confirm Ollama is running.

---

## 2. Vault Service (Pi gRPC Endpoint)

**File:** `internal/api/vault.proto`

**Role:** Receives user requests, orchestrates reasoning, validates outputs, executes approved actions.

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
    type: PR_PROPOSED  // or DIRECT, ACTION_PENDING, ERROR
    response_id: "resp_001"
    content: "I've created PR #42 to fix this..."
    tier_classified: TIER_2
    approval: PENDING_SHADOW  // awaiting dry-run
    action: {
      action_type: "code_commit"
      description: "Merge fix for bug X"
      affected_files: ["src/fixes.py"]
      shadow_pr_link: "https://github.com/agent/repo/pull/999"
    }
  }
```

**Decision Logic Inside Vault:**
1. Classify risk tier (using `internal/core/risk` classifier)
2. If Tier 1-2: Decide immediately (low risk)
3. If Tier 3+: 
   - Route to Muscle for reasoning (via gRPC tunnel)
   - Validate response (check for injection, etc.)
   - Route to Shadow for dry-run
   - Wait for Shadow approval
   - Sign and merge PR
4. Execute approved actions (GitHub API, file writes, etc.)
5. Log everything (audit trail)

### Other RPCs:

- `ApproveAction(ApprovalRequest)` — Manually trigger (rarely used in permissive mode)
- `QueryPolicy(PolicyQueryRequest)` — Ask current rate limits, tier configs
- `Health(HealthCheck)` — Watchdog pings

---

## 3. Shadow Service (Pi gRPC Endpoint)

**File:** `internal/api/shadow.proto`

**Role:** Validate proposed code changes before merge (dry-run). Learn by comparing prediction vs. reality.

### Main RPC: `ValidateProposal`

```
Vault (client) → Shadow (server)
  Request: ValidationRequest {
    session_id: "xyz789"
    muscle_version: "v2.1.0"
    pr_title: "Add logging to generate_response()"
    file_diffs: [
      {
        file_path: "src/agent.py"
        operation: "modify"
        before_content: "def generate_response(...):\n  ..."
        after_content: "def generate_response(...):\n  log.info(...)\n  ..."
        risky_patterns: []  // no infinite loops, etc.
      }
    ]
    phase: PHASE_SANDBOX_TEST
  }

  Shadow Response: ValidationResponse {
    validation_id: "val_001"
    is_safe: true  // OK to merge
    confidence: 0.99
    validation_type: VALIDATION_PASSED
    issues: []  // no problems found
    test_results: ["✓ test_generate_response PASSED", ...]
    prediction: {
      will_agent_still_work: true
      predicted_behavior: "Agent generates responses with debug logging"
      predicted_correctness: 0.98
      new_capabilities: ["Enhanced debugging"]
    }
    shadow_pr_url: "https://github.com/agent/agent-shadow/pull/42"
  }
```

### Other RPCs:

- `RecordOutcome(OutcomeReport)` — After 24h observation, report actual results vs. predictions
- `QueryReadiness(ReadinessQuery)` — Is this Muscle version ready to go live?

---

## Data Flow: End-to-End

```
1. User (Mac/iPhone)
   └─→ Prompts via REST API

2. API Gateway (Pi)
   └─→ Calls Vault.ProcessPrompt(VaultPromptRequest)

3. Vault (Pi) receives request
   ├─→ Classifies risk tier
   ├─→ If Tier 1-2: Decide immediately
   └─→ If Tier 3-4:
       ├─→ Calls Muscle.GenerateResponse(PromptRequest)  [to Win11]
       │   └─→ Win11 streams back tokens
       ├─→ Validates Muscle response
       ├─→ Calls Shadow.ValidateProposal(ValidationRequest)  [on Pi]
       │   └─→ Shadow returns ValidationResponse
       └─→ If Shadow says safe:
           ├─→ Create PR
           ├─→ Call Shadow.RecordOutcome() after 24h
           └─→ Merge PR

4. Watchdog (Pi) monitors
   ├─→ Pings Vault.Health()
   ├─→ Pings Muscle.Health()  [to Win11]
   └─→ Auto-reverts if cavy metrics breach policies

5. User gets response via API
```

---

## Implementation Checklist

- [ ] Generate Go code from `.proto` files (`protoc --go_out=...`)
- [ ] Implement Muscle service (Ollama wrapper on Win11)
- [ ] Implement Vault service (orchestrator on Pi)
- [ ] Implement Shadow service (validator on Pi)
- [ ] Build API Gateway (REST → gRPC conversion on Pi)
- [ ] Set up gRPC mutual TLS (Vault ↔ Muscle tunnel)
- [ ] Implement Watchdog monitors
- [ ] Create audit logging middleware

---

## Security Properties

| Threat | Mitigated By |
|--------|--------------|
| Win11 compromise | Muscle is stateless; attacker gets inference tokens only |
| Code injection in prompts | Vault validates all Muscle outputs before execution |
| Infinite loops in generated code | Shadow dry-runs detect them before merge |
| Runaway self-modification | Vault enforces rate limits (max_changes_per_day) |
| Failed code in production | Watchdog auto-reverts if metrics breach thresholds |
| Pi compromise | Beyond scope; assume hardware security |

