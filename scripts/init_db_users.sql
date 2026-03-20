-- Additional DB users for Shadow, Watchdog, Sandbox services
-- Executed automatically on postgres container first startup

CREATE USER shadow WITH PASSWORD 'shadow_secure_pass';
CREATE USER watchdog WITH PASSWORD 'watchdog_secure_pass';
CREATE USER sandbox WITH PASSWORD 'sandbox_secure_pass';

-- Grant connection rights to agent_memory
GRANT CONNECT ON DATABASE agent_memory TO shadow;
GRANT CONNECT ON DATABASE agent_memory TO watchdog;
GRANT CONNECT ON DATABASE agent_memory TO sandbox;

-- Shadow: read vector + write baseline, no ledger writes
GRANT SELECT ON vector_entries, baseline_predictions TO shadow;
GRANT INSERT, SELECT ON vector_entries, baseline_predictions TO shadow;
GRANT SELECT ON ledger_entries TO shadow;
GRANT INSERT ON ledger_entries TO shadow;

-- Watchdog: read all, write retrospectives + ledger
GRANT SELECT ON ALL TABLES IN SCHEMA public TO watchdog;
GRANT INSERT ON vector_entries, ledger_entries, retrospectives, execution_metrics TO watchdog;

-- Sandbox: read/write execution metrics and ledger
GRANT SELECT ON ledger_entries TO sandbox;
GRANT INSERT ON ledger_entries, execution_metrics TO sandbox;

-- Vault (already the superuser for this DB): full access is implicit
