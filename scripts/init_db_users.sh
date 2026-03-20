#!/bin/bash
# init_db_users.sh — Create per-service DB users with passwords from environment.
#
# Executed automatically by the PostgreSQL entrypoint on first container start
# (mounted as /docker-entrypoint-initdb.d/02-users.sh).
# Runs as the postgres superuser, AFTER the main DB and the vault user exist.
#
# Required env vars (set on the postgres container):
#   SHADOW_DB_PASSWORD    WATCHDOG_DB_PASSWORD    SANDBOX_DB_PASSWORD
# They are passed via the postgres service environment in docker-compose.

set -e

# Fallback to safe placeholders so the script doesn't crash in dev if vars unset
SHADOW_PASS="${SHADOW_DB_PASSWORD:-shadow_secure_pass}"
WATCHDOG_PASS="${WATCHDOG_DB_PASSWORD:-watchdog_secure_pass}"
SANDBOX_PASS="${SANDBOX_DB_PASSWORD:-sandbox_secure_pass}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL

  -- Per-service database users
  DO \$\$
  BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'shadow') THEN
      CREATE USER shadow WITH PASSWORD '${SHADOW_PASS}';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'watchdog') THEN
      CREATE USER watchdog WITH PASSWORD '${WATCHDOG_PASS}';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sandbox') THEN
      CREATE USER sandbox WITH PASSWORD '${SANDBOX_PASS}';
    END IF;
  END
  \$\$;

  GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO shadow, watchdog, sandbox;

  -- Shadow: read/write vector entries and baselines, append-only ledger
  GRANT SELECT, INSERT ON vector_entries, baseline_predictions TO shadow;
  GRANT SELECT, INSERT ON ledger_entries TO shadow;

  -- Watchdog: read all, write retrospectives + ledger + metrics
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO watchdog;
  GRANT INSERT ON vector_entries, ledger_entries, retrospectives, execution_metrics TO watchdog;

  -- Sandbox: append ledger + execution metrics
  GRANT SELECT ON ledger_entries TO sandbox;
  GRANT INSERT ON ledger_entries, execution_metrics TO sandbox;

EOSQL

echo "init_db_users.sh: per-service users created successfully."
