"""
Watchdog gRPC server — binds port 50054 on Pi.

Implements:
  watchdog.v1.Watchdog service (watchdog.proto):
    ReportMetrics → WatchdogService.monitor_metrics()
    Health        → WatchdogService.check_system_health()

Win11 Muscle pushes GPU/latency metrics after each inference call.
Watchdog decides whether to trigger rollback and writes retrospectives.
"""

import sys
import asyncio
import time
import logging
import os
from concurrent import futures
from pathlib import Path

import grpc
from prometheus_client import Counter, Histogram, Gauge, start_http_server

_PROTO_DIR = Path(__file__).resolve().parent.parent.parent / "internal" / "api"
if str(_PROTO_DIR) not in sys.path:
    sys.path.insert(0, str(_PROTO_DIR))

import watchdog_pb2          # noqa: E402
import watchdog_pb2_grpc     # noqa: E402

logger = logging.getLogger(__name__)

# ── Prometheus metrics ──────────────────────────────────────────────────────
_METRICS_TOTAL = Counter(
    "watchdog_metrics_reports_total",
    "Total MetricsReport RPCs received",
    ["outcome"],  # "ack" | "rollback"
)
_GPU_TEMP = Gauge("watchdog_gpu_temp_celsius", "Latest reported GPU temperature")
_GPU_MEM = Gauge("watchdog_gpu_memory_available_mb", "Latest reported GPU VRAM headroom")
_LATENCY_P99 = Gauge("watchdog_latency_p99_ms", "Latest reported P99 latency")
_ROLLBACKS = Counter("watchdog_rollbacks_total", "Total rollbacks triggered")
_REQUEST_LATENCY = Histogram(
    "watchdog_grpc_request_duration_seconds",
    "gRPC request latency",
    ["method"],
)

_START_TIME = time.time()


class WatchdogServicer(watchdog_pb2_grpc.WatchdogServicer):
    """Adapts WatchdogService onto watchdog.proto RPCs."""

    def __init__(self, watchdog_service):
        self._svc = watchdog_service

    # ── ReportMetrics ────────────────────────────────────────────────────────
    async def ReportMetrics(self, request, context):
        start = time.monotonic()
        try:
            # Update gauges for Grafana dashboards
            _GPU_TEMP.set(request.gpu_temp_c)
            _GPU_MEM.set(request.gpu_memory_available_mb)
            _LATENCY_P99.set(request.latency_p99_ms)

            should_rollback, reason = await self._svc.monitor_metrics(
                request_id=request.request_id,
                error_rate=request.error_rate,
                latency_p99_ms=request.latency_p99_ms,
                gpu_temp_c=request.gpu_temp_c,
                gpu_memory_available_mb=request.gpu_memory_available_mb,
            )

            outcome = "rollback" if should_rollback else "ack"
            _METRICS_TOTAL.labels(outcome).inc()
            if should_rollback:
                _ROLLBACKS.inc()

            _REQUEST_LATENCY.labels("ReportMetrics").observe(time.monotonic() - start)
            return watchdog_pb2.MetricsResponse(
                acknowledged=True,
                should_rollback=should_rollback,
                reason=reason if should_rollback else "",
            )

        except Exception as exc:
            _REQUEST_LATENCY.labels("ReportMetrics").observe(time.monotonic() - start)
            logger.error("ReportMetrics failed: %s", exc, exc_info=True)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))

    # ── Health ───────────────────────────────────────────────────────────────
    async def Health(self, request, context):
        start = time.monotonic()
        try:
            state = await self._svc.check_system_health()
            uptime = int(time.time() - _START_TIME)
            healthy = state.get("status") == "healthy"

            _REQUEST_LATENCY.labels("Health").observe(time.monotonic() - start)
            return watchdog_pb2.WatchdogHealthStatus(
                healthy=healthy,
                active_alerts=0,
                retrospectives_written=state.get("ledger_entries", 0),
                rollbacks_triggered=0,
                status=state.get("status", "unknown"),
                uptime_seconds=uptime,
            )

        except Exception as exc:
            _REQUEST_LATENCY.labels("Health").observe(time.monotonic() - start)
            logger.error("Health check failed: %s", exc, exc_info=True)
            await context.abort(grpc.StatusCode.INTERNAL, str(exc))


def _load_tls_credentials():
    # Env precedence: WATCHDOG_CERT_FILE → CERT_FILE → /opt/teammate-vault/certs/client.crt
    # See PI_SETUP.md for certificate installation instructions.
    cert_file = os.getenv("WATCHDOG_CERT_FILE", os.getenv("CERT_FILE", "/opt/teammate-vault/certs/client.crt"))
    key_file  = os.getenv("WATCHDOG_KEY_FILE",  os.getenv("KEY_FILE",   "/opt/teammate-vault/certs/client.key"))
    ca_cert   = os.getenv("WATCHDOG_CA_CERT",   os.getenv("CA_CERT",    "/opt/teammate-vault/certs/muscle.crt"))

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


async def start_grpc_server(watchdog_service, host: str, port: int) -> grpc.aio.Server:
    prom_port = int(os.getenv("WATCHDOG_METRICS_PORT", "8002"))
    try:
        start_http_server(prom_port)
        logger.info("Prometheus metrics on :%d", prom_port)
    except OSError:
        logger.warning("Prometheus port %d already in use — skipping", prom_port)

    server = grpc.aio.server(futures.ThreadPoolExecutor(max_workers=10))
    servicer = WatchdogServicer(watchdog_service)
    watchdog_pb2_grpc.add_WatchdogServicer_to_server(servicer, server)

    credentials = _load_tls_credentials()
    listen_addr = f"{host}:{port}"
    if credentials:
        server.add_secure_port(listen_addr, credentials)
        logger.info("Watchdog gRPC listening (mTLS) on %s", listen_addr)
    else:
        server.add_insecure_port(listen_addr)
        logger.info("Watchdog gRPC listening (insecure) on %s", listen_addr)

    await server.start()
    return server
