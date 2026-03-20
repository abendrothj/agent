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
