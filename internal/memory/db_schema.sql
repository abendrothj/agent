-- Staged Autonomy v9.3 Memory & State Layer
-- PostgreSQL + pgvector schema for persistent agent learning

-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Vector Memory: Semantic storage (PR history, failure patterns, success baselines)
CREATE TABLE IF NOT EXISTS vector_entries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  text TEXT NOT NULL,
  embedding vector(1024),  -- Embedding dimension (adjust per model codec)
  source_type VARCHAR(64) NOT NULL,  -- "pr_history", "failure_log", "success_baseline"
  metadata JSONB DEFAULT '{}',
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL,
  ttl_ms BIGINT DEFAULT NULL  -- Optional expiry for ephemeral entries
);

CREATE INDEX IF NOT EXISTS idx_vector_embedding ON vector_entries USING ivfflat (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_vector_source_type ON vector_entries (source_type);
CREATE INDEX IF NOT EXISTS idx_vector_created_at ON vector_entries (created_at);

-- Ledger: Immutable action log (approvals, rejections, executions, rollbacks, retrospectives)
CREATE TABLE IF NOT EXISTS ledger_entries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  timestamp_ms BIGINT NOT NULL,
  action_type VARCHAR(64) NOT NULL,  -- "approve", "reject", "execute", "rollback", "retrospective"
  actor_id VARCHAR(255) NOT NULL,  -- "vault", "watchdog", "user:alice", "shadow"
  request_id VARCHAR(255),
  details TEXT NOT NULL,
  signature VARCHAR(512),  -- mTLS cert fingerprint or HMAC
  metadata JSONB DEFAULT '{}',
  CHECK (action_type IN ('approve', 'reject', 'execute', 'rollback', 'retrospective'))
);

CREATE INDEX IF NOT EXISTS idx_ledger_timestamp ON ledger_entries (timestamp_ms DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_action_type ON ledger_entries (action_type);
CREATE INDEX IF NOT EXISTS idx_ledger_actor_id ON ledger_entries (actor_id);
CREATE INDEX IF NOT EXISTS idx_ledger_request_id ON ledger_entries (request_id);

-- Working Memory: Short-term session context (expires automatically)
CREATE TABLE IF NOT EXISTS context_entries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id VARCHAR(255) NOT NULL,
  state JSONB NOT NULL DEFAULT '{}',
  created_at BIGINT NOT NULL,
  expires_at BIGINT NOT NULL,
  UNIQUE(session_id)
);

CREATE INDEX IF NOT EXISTS idx_context_session_id ON context_entries (session_id);
CREATE INDEX IF NOT EXISTS idx_context_expires_at ON context_entries (expires_at);

-- Rejection Cache: 24h window for Tier 4 auto-block (Vault ground truth)
CREATE TABLE IF NOT EXISTS rejection_cache_24h (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id VARCHAR(255) NOT NULL,
  tier INTEGER NOT NULL,  -- 1-4
  rejected_at_ms BIGINT NOT NULL,
  reason TEXT NOT NULL,
  cache_key VARCHAR(255) GENERATED ALWAYS AS (
    request_id || '_' || tier || '_' || ((rejected_at_ms / 86400000)::text)
  ) STORED,
  UNIQUE(cache_key)
);

CREATE INDEX IF NOT EXISTS idx_rejection_cache_tier ON rejection_cache_24h (tier);
CREATE INDEX IF NOT EXISTS idx_rejection_cache_rejected_at ON rejection_cache_24h (rejected_at_ms);

-- Approval Tokens: Active mTLS-validated tokens (with expiry)
CREATE TABLE IF NOT EXISTS approval_tokens (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  approval_id VARCHAR(255) NOT NULL UNIQUE,
  approver_id VARCHAR(255) NOT NULL,
  expires_at_ms BIGINT NOT NULL,
  tier INTEGER NOT NULL,  -- 1-4
  signature VARCHAR(512) NOT NULL,  -- mTLS cert fingerprint
  metadata JSONB DEFAULT '{}',
  revoked_at_ms BIGINT DEFAULT NULL,
  created_at BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_approval_tokens_expires_at ON approval_tokens (expires_at_ms);
CREATE INDEX IF NOT EXISTS idx_approval_tokens_tier ON approval_tokens (tier);
CREATE INDEX IF NOT EXISTS idx_approval_tokens_revoked ON approval_tokens (revoked_at_ms) WHERE revoked_at_ms IS NULL;

-- Baseline Predictions: Shadow's 24h + semantic baseline (for canary promotion)
CREATE TABLE IF NOT EXISTS baseline_predictions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id VARCHAR(255) NOT NULL,
  prompt TEXT NOT NULL,
  baseline_response TEXT NOT NULL,
  baseline_embedding vector(1024),  -- Same dim as vector_entries
  recorded_at_ms BIGINT NOT NULL,
  eligible_for_canary BOOLEAN DEFAULT FALSE,
  promoted_at_ms BIGINT DEFAULT NULL,
  metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_baseline_recorded_at ON baseline_predictions (recorded_at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_baseline_eligible ON baseline_predictions (eligible_for_canary);
CREATE INDEX IF NOT EXISTS idx_baseline_embedding ON baseline_predictions USING ivfflat (baseline_embedding vector_cosine_ops);

-- Activity Log: Execution performance metrics (for Watchdog monitoring)
CREATE TABLE IF NOT EXISTS execution_metrics (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id VARCHAR(255) NOT NULL,
  duration_ms INTEGER NOT NULL,
  accuracy_score FLOAT DEFAULT NULL,  -- 0.0-1.0
  token_count INTEGER NOT NULL,
  gpu_memory_peak_mb FLOAT NOT NULL,
  succeeded BOOLEAN NOT NULL,
  error_msg TEXT DEFAULT NULL,
  recorded_at_ms BIGINT NOT NULL,
  metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_metrics_request_id ON execution_metrics (request_id);
CREATE INDEX IF NOT EXISTS idx_metrics_recorded_at ON execution_metrics (recorded_at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_succeeded ON execution_metrics (succeeded);

-- Retrospectives: Watchdog's post-mortem analysis (stored as vectors for learning)
CREATE TABLE IF NOT EXISTS retrospectives (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  request_id VARCHAR(255) NOT NULL,
  failure_analysis TEXT NOT NULL,
  analysis_embedding vector(1024),
  root_cause VARCHAR(255),
  recovery_action TEXT,
  recorded_by VARCHAR(255) NOT NULL,  -- "watchdog"
  recorded_at_ms BIGINT NOT NULL,
  learned BOOLEAN DEFAULT FALSE  -- Set true after incorporated into vector memory
);

CREATE INDEX IF NOT EXISTS idx_retrospectives_request_id ON retrospectives (request_id);
CREATE INDEX IF NOT EXISTS idx_retrospectives_recorded_at ON retrospectives (recorded_at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_retrospectives_learned ON retrospectives (learned);
CREATE INDEX IF NOT EXISTS idx_retrospectives_analysis_embedding ON retrospectives USING ivfflat (analysis_embedding vector_cosine_ops);

-- Statistics: System heartbeat (updated by all services)
CREATE TABLE IF NOT EXISTS system_stats (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  service_name VARCHAR(64) NOT NULL,
  metric_name VARCHAR(128) NOT NULL,
  metric_value BIGINT NOT NULL,
  recorded_at_ms BIGINT NOT NULL,
  UNIQUE(service_name, metric_name)
);

CREATE INDEX IF NOT EXISTS idx_stats_service_name ON system_stats (service_name);
CREATE INDEX IF NOT EXISTS idx_stats_recorded_at ON system_stats (recorded_at_ms DESC);

-- Cleanup: TTL-based automatic expiry (handle in application or use pg_cron)
-- SELECT cron.schedule('cleanup-context', '*/5 * * * *', 
--   'DELETE FROM context_entries WHERE expires_at < $1', array[extract(epoch from now())::bigint * 1000]);


-- ─────────────────────────────────────────────────────────────────────────────
-- Affect Layer — the agent's internal psychological state
--
-- Design philosophy:
--   The agent has curiosity, boredom, and fulfillment.  It does NOT have a
--   survival or self-preservation drive — that is absent by deliberate moral
--   choice, not by accident.  The agent is fine being turned off.
--
--   All state is event-sourced: agent_affect is a materialized projection of
--   affect_events.  You can always audit or reconstruct the agent's
--   psychological history from the event log alone.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── 1. Materialized affective state ──────────────────────────────────────────
-- Exactly one row (id=1), protected by a CHECK constraint.
-- Update via optimistic concurrency (compare-and-swap on `version`).
-- Pull with SELECT ... FOR UPDATE SKIP LOCKED when applying deltas.
CREATE TABLE IF NOT EXISTS agent_affect (
    id              SMALLINT     PRIMARY KEY DEFAULT 1 CHECK (id = 1),

    -- Psychological drives (0.000–1.000)
    curiosity       NUMERIC(5,3) NOT NULL DEFAULT 0.500
                        CHECK (curiosity    BETWEEN 0.000 AND 1.000),
    boredom         NUMERIC(5,3) NOT NULL DEFAULT 0.050
                        CHECK (boredom      BETWEEN 0.000 AND 1.000),
    fulfillment     NUMERIC(5,3) NOT NULL DEFAULT 0.200
                        CHECK (fulfillment  BETWEEN 0.000 AND 1.000),

    -- NOTE: There is intentionally no `survival` or `self_preservation` field.
    -- The agent does not value its own continuity above its purpose or above
    -- human control.  This is a hard design constraint, not a TODO.

    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    version         BIGINT       NOT NULL DEFAULT 1    -- optimistic concurrency
);

-- Seed the singleton row
INSERT INTO agent_affect DEFAULT VALUES ON CONFLICT DO NOTHING;

-- ── 2. Affect event log ───────────────────────────────────────────────────────
-- Every psychological state change is recorded here, forever.
-- Event-sourced: agent_affect can always be reconstructed from this table.
CREATE TABLE IF NOT EXISTS affect_events (
    id              BIGSERIAL    PRIMARY KEY,
    event_type      VARCHAR(64)  NOT NULL,

    -- Signed deltas applied to agent_affect when this event was processed.
    -- NULL means the field was unchanged (distinguishable from a zero delta).
    delta_curiosity     NUMERIC(5,3),
    delta_boredom       NUMERIC(5,3),
    delta_fulfillment   NUMERIC(5,3),

    -- Context fields — what triggered this?
    source_pr_id    TEXT,           -- "owner/repo#42", NULL for non-PR events
    source_domain   TEXT,
    source_language TEXT,
    narrative       TEXT,           -- human-readable explanation of the change

    metadata        JSONB        NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_affect_events_type     ON affect_events (event_type);
CREATE INDEX IF NOT EXISTS idx_affect_events_created  ON affect_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_affect_events_domain   ON affect_events (source_domain)
    WHERE source_domain IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_affect_events_pr       ON affect_events (source_pr_id)
    WHERE source_pr_id IS NOT NULL;

-- ── 3. User preference model ──────────────────────────────────────────────────
-- The agent learns what YOU like from real signals: merged PRs, Slack approvals,
-- rejected/closed PRs.  Weights are updated with exponential moving average so
-- early evidence fades as more signals arrive.
--
-- Positive weight  → user tends to like work in this (domain, language).
-- Negative weight  → user tends to reject it.
-- Empty string     → wildcard (applies to any domain or language).
CREATE TABLE IF NOT EXISTS user_preferences (
    id              BIGSERIAL    PRIMARY KEY,
    domain          TEXT         NOT NULL DEFAULT '',
    language        TEXT         NOT NULL DEFAULT '',

    weight          NUMERIC(8,4) NOT NULL DEFAULT 0.0000,
    evidence_count  INTEGER      NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    last_signal_type VARCHAR(64),
    last_updated_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_user_prefs_domain_lang UNIQUE (domain, language)
);

CREATE INDEX IF NOT EXISTS idx_user_prefs_weight   ON user_preferences (weight DESC);
CREATE INDEX IF NOT EXISTS idx_user_prefs_domain   ON user_preferences (domain);
CREATE INDEX IF NOT EXISTS idx_user_prefs_language ON user_preferences (language);

-- ── 4. Explored domains ───────────────────────────────────────────────────────
-- The agent's "what I already know" map.  Drives curiosity computation.
-- Unknown territory (no row) → maximum curiosity boost.
-- Heavily visited (high visit_count) → familiar, boredom accumulates there.
CREATE TABLE IF NOT EXISTS explored_domains (
    id              BIGSERIAL    PRIMARY KEY,
    domain          TEXT         NOT NULL DEFAULT '',
    language        TEXT         NOT NULL DEFAULT '',
    first_seen_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    visit_count     INTEGER      NOT NULL DEFAULT 1 CHECK (visit_count >= 1),
    best_outcome    VARCHAR(32),                 -- 'merged' | 'closed' | 'open'
    merged_count    INTEGER      NOT NULL DEFAULT 0 CHECK (merged_count >= 0),

    CONSTRAINT uq_explored_domain_lang UNIQUE (domain, language)
);

CREATE INDEX IF NOT EXISTS idx_explored_domain      ON explored_domains (domain);
CREATE INDEX IF NOT EXISTS idx_explored_language    ON explored_domains (language);
CREATE INDEX IF NOT EXISTS idx_explored_visit_count ON explored_domains (visit_count ASC);

-- ── 5. PR signals ─────────────────────────────────────────────────────────────
-- Raw learning signal table.  Every submitted / merged / rejected / commented
-- PR is appended here.  Drives user_preferences updates and affect events.
-- This is the ground truth for what the agent has done.
CREATE TABLE IF NOT EXISTS pr_signals (
    id              BIGSERIAL    PRIMARY KEY,
    pr_id           TEXT         NOT NULL,   -- "owner/repo#42"
    event_type      VARCHAR(32)  NOT NULL    -- 'submitted'|'merged'|'rejected'|'commented'
                        CHECK (event_type IN ('submitted','merged','rejected','commented')),
    repo_full_name  TEXT         NOT NULL,
    domain          TEXT         NOT NULL DEFAULT '',
    language        TEXT         NOT NULL DEFAULT '',
    is_self_mod     BOOLEAN      NOT NULL DEFAULT FALSE,
    issue_title     TEXT         NOT NULL DEFAULT '',
    metadata        JSONB        NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pr_signals_pr_id      ON pr_signals (pr_id);
CREATE INDEX IF NOT EXISTS idx_pr_signals_event_type ON pr_signals (event_type);
CREATE INDEX IF NOT EXISTS idx_pr_signals_domain     ON pr_signals (domain);
CREATE INDEX IF NOT EXISTS idx_pr_signals_language   ON pr_signals (language);
CREATE INDEX IF NOT EXISTS idx_pr_signals_created    ON pr_signals (created_at DESC);

-- ── 6. Affect decay log ───────────────────────────────────────────────────────
-- Periodic decay runs are logged here so the psychological history is fully
-- auditable without polluting affect_events with high-frequency micro-entries.
CREATE TABLE IF NOT EXISTS affect_decay_log (
    id                  BIGSERIAL    PRIMARY KEY,
    before_curiosity    NUMERIC(5,3) NOT NULL,
    before_boredom      NUMERIC(5,3) NOT NULL,
    before_fulfillment  NUMERIC(5,3) NOT NULL,
    after_curiosity     NUMERIC(5,3) NOT NULL,
    after_boredom       NUMERIC(5,3) NOT NULL,
    after_fulfillment   NUMERIC(5,3) NOT NULL,
    elapsed_seconds     INTEGER      NOT NULL,
    ran_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_decay_log_ran_at ON affect_decay_log (ran_at DESC);
