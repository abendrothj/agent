"""
Vault gRPC server — binds port 50051 on Pi.

Implements:
  vault.api.Vault service (vault.proto):
    ProcessPrompt   → VaultService.process_request()
    ApproveAction   → VaultService.resume_after_mfa()
    QueryPolicy     → VaultService.get_state()
    Health          → liveness check with Prometheus metrics

Import strategy: generated pb2 stubs live in internal/api/.
The sys.path insertion below makes bare `import vault_pb2` work
without restructuring the whole package layout.
"""

import sys
import asyncio
import time
import logging
import os
from concurrent import futures
from pathlib import Path

import grpc
from prometheus_client import Counter, Histogram, start_http_server

# Make protobuf stubs importable from internal/api/
_PROTO_DIR = Path(__file__).resolve().parent.parent.parent / "internal" / "api"
if str(_PROTO_DIR) not in sys.path:
    sys.path.insert(0, str(_PROTO_DIR))

import vault_pb2          # noqa: E402  (generated, not in repo until gen_protos.sh runs)
import vault_pb2_grpc     # noqa: E402

logger = logging.getLogger(__name__)

# ── Prometheus metrics ──────────────────────────────────────────────────────
_REQUESTS_TOTAL = Counter(
    "vault_grpc_requests_total",
    "Total gRPC requests received by Vault",
    ["method", "status"],
)
_REQUEST_LATENCY = Histogram(
    "vault_grpc_request_duration_seconds",
    "gRPC request latency",
    ["method"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

_START_TIME = time.time()


class VaultServicer(vault_pb2_grpc.VaultServicer):
    """
    Adapts VaultService business logic to the vault.proto contract.
    All heavy lifting lives in VaultService / LangGraphVault.
    """

    def __init__(self, vault_service):
        self._svc = vault_service

    # ── ProcessPrompt ────────────────────────────────────────────────────────
    async def ProcessPrompt(self, request, context):
        start = time.monotonic()
        try:
            approved, reason, tier = await self._svc.process_request(
                request_id=request.session_id,
                prompt=request.prompt,
                system_context=request.domain,
                scope="local",
                session_id=request.session_id,
            )
            latency = time.monotonic() - start
            _REQUEST_LATENCY.labels("ProcessPrompt").observe(latency)

            if reason.startswith("PENDING_MFA:"):
                resp_type = vault_pb2.ACTION_PENDING
                approval_state = vault_pb2.PENDING_HUMAN
            elif approved:
                resp_type = vault_pb2.DIRECT
                approval_state = vault_pb2.APPROVED
            else:
                resp_type = vault_pb2.DIRECT
                approval_state = vault_pb2.REJECTED

            _REQUESTS_TOTAL.labels("ProcessPrompt", "ok").inc()
            return vault_pb2.VaultResponse(
                type=resp_type,
                response_id=request.session_id,
                content=reason,
                tier_classified=tier.value,
                approval=approval_state,
            )

        except Exception as exc:
            latency = time.monotonic() - start
            _REQUEST_LATENCY.labels("ProcessPrompt").observe(latency)
            _REQUESTS_TOTAL.labels("ProcessPrompt", "error").inc()
            logger.error("ProcessPrompt failed: %s", exc, exc_info=True)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))

    # ── ApproveAction ────────────────────────────────────────────────────────
    async def ApproveAction(self, request, context):
        start = time.monotonic()
        try:
            approved, reason, tier = await self._svc.resume_after_mfa(
                request_id=request.response_id,
                human_approved=True,
            )
            latency = time.monotonic() - start
            _REQUEST_LATENCY.labels("ApproveAction").observe(latency)
            _REQUESTS_TOTAL.labels("ApproveAction", "ok").inc()
            return vault_pb2.ExecutionResponse(
                success=approved,
                execution_id=request.response_id,
                result=reason,
            )

        except Exception as exc:
            latency = time.monotonic() - start
            _REQUEST_LATENCY.labels("ApproveAction").observe(latency)
            _REQUESTS_TOTAL.labels("ApproveAction", "error").inc()
            logger.error("ApproveAction failed: %s", exc, exc_info=True)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))

    # ── QueryPolicy ──────────────────────────────────────────────────────────
    async def QueryPolicy(self, request, context):
        start = time.monotonic()
        try:
            state = await self._svc.get_state()
            latency = time.monotonic() - start
            _REQUEST_LATENCY.labels("QueryPolicy").observe(latency)
            _REQUESTS_TOTAL.labels("QueryPolicy", "ok").inc()
            return vault_pb2.PolicyState(
                changes_today=state.get("approvals_total", 0),
                max_changes_per_day=1000,
                tier_3_pending=0,
                tier_4_pending=0,
                last_update=str(state.get("timestamp_ms", "")),
            )

        except Exception as exc:
            latency = time.monotonic() - start
            _REQUEST_LATENCY.labels("QueryPolicy").observe(latency)
            _REQUESTS_TOTAL.labels("QueryPolicy", "error").inc()
            logger.error("QueryPolicy failed: %s", exc, exc_info=True)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))

    # ── Health ───────────────────────────────────────────────────────────────
    async def Health(self, request, context):
        _REQUESTS_TOTAL.labels("Health", "ok").inc()
        uptime = int(time.time() - _START_TIME)
        return vault_pb2.HealthStatus(
            healthy=True,
            status="ready",
            uptime_seconds=uptime,
            active_sessions=0,
            last_decision="",
        )


def _load_tls_credentials():
    """
    Load mTLS credentials from env-configured cert paths.
    Env precedence (highest → lowest):
      VAULT_CERT_FILE  → CERT_FILE  → /opt/teammate-vault/certs/client.crt
      VAULT_KEY_FILE   → KEY_FILE   → /opt/teammate-vault/certs/client.key
      VAULT_CA_CERT    → CA_CERT    → /opt/teammate-vault/certs/muscle.crt
    Falls back to insecure if any cert file is absent (dev mode).
    See PI_SETUP.md for certificate installation instructions.
    """
    cert_file = os.getenv("VAULT_CERT_FILE", os.getenv("CERT_FILE", "/opt/teammate-vault/certs/client.crt"))
    key_file  = os.getenv("VAULT_KEY_FILE",  os.getenv("KEY_FILE",   "/opt/teammate-vault/certs/client.key"))
    ca_cert   = os.getenv("VAULT_CA_CERT",   os.getenv("CA_CERT",    "/opt/teammate-vault/certs/muscle.crt"))

    for path in (cert_file, key_file, ca_cert):
        if not Path(path).exists():
            logger.warning("TLS cert missing at %s — starting WITHOUT mTLS (dev mode)", path)
            return None

    with open(cert_file, "rb") as f:
        cert = f.read()
    with open(key_file, "rb") as f:
        key = f.read()
    with open(ca_cert, "rb") as f:
        ca = f.read()

    return grpc.ssl_server_credentials(
        [(key, cert)],
        root_certificates=ca,
        require_client_auth=True,
    )


async def start_grpc_server(vault_service, host: str, port: int) -> grpc.aio.Server:
    """
    Bind the Vault gRPC server on host:port.
    Returns the running server so the caller can call await server.wait_for_termination().
    """
    prom_port = int(os.getenv("VAULT_METRICS_PORT", "8000"))
    try:
        start_http_server(prom_port)
        logger.info("Prometheus metrics on :%d", prom_port)
    except OSError:
        logger.warning("Prometheus port %d already in use — skipping", prom_port)

    server = grpc.aio.server(futures.ThreadPoolExecutor(max_workers=10))
    servicer = VaultServicer(vault_service)
    vault_pb2_grpc.add_VaultServicer_to_server(servicer, server)

    credentials = _load_tls_credentials()
    listen_addr = f"{host}:{port}"
    if credentials:
        server.add_secure_port(listen_addr, credentials)
        logger.info("Vault gRPC listening (mTLS) on %s", listen_addr)
    else:
        server.add_insecure_port(listen_addr)
        logger.info("Vault gRPC listening (insecure) on %s", listen_addr)

    await server.start()
    return server
