"""
HTTP API Gateway — Slack slash command handler + liveness check.

The only external surface is /slack/command (verified by Slack HMAC-SHA256
signature). All other control flows to Vault go through gRPC from trusted
services inside the Docker network.

Exposure: Cloudflare Tunnel (cloudflared) forwards public HTTPS traffic to
this service on port 8080. The Pi LAN IP is never directly reachable.

Slack slash command syntax:
  /agent <prompt>          — Tier 1 (default)
  /agent t2: <prompt>      — Tier 2 (sandbox)
  /agent t3: <prompt>      — Tier 3 (production)
  /agent t4: <prompt>      — Tier 4 (infrastructure)
  /agent approve <id>      — Approve a pending action

Run (Docker handles this):
  uvicorn cmd.api.main:app --host 0.0.0.0 --port 8080
"""
import asyncio
import hashlib
import hmac
import os
import time
import uuid
import logging
from typing import Optional

import grpc
import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse

# Proto stubs — copied into the container alongside this file by Dockerfile
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import vault_pb2
import vault_pb2_grpc

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

VAULT_HOST = os.getenv("VAULT_HOST", "vault")
VAULT_PORT = int(os.getenv("VAULT_PORT", "50051"))

CERT_FILE           = os.getenv("CERT_FILE", "/run/certs/client.crt")
KEY_FILE            = os.getenv("KEY_FILE",  "/run/certs/client.key")
CA_CERT             = os.getenv("CA_CERT",   "/run/certs/muscle.crt")

SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")  # From Slack App settings

# ── gRPC channel ─────────────────────────────────────────────────────────────

def _build_channel() -> grpc.Channel:
    try:
        creds = grpc.ssl_channel_credentials(
            root_certificates=open(CA_CERT, "rb").read(),
            private_key=open(KEY_FILE, "rb").read(),
            certificate_chain=open(CERT_FILE, "rb").read(),
        )
        return grpc.secure_channel(f"{VAULT_HOST}:{VAULT_PORT}", creds)
    except FileNotFoundError:
        # No certs yet (dev mode) — fall back to insecure
        logger.warning("TLS certs not found — connecting to Vault without mTLS (dev only)")
        return grpc.insecure_channel(f"{VAULT_HOST}:{VAULT_PORT}")


_channel: Optional[grpc.Channel] = None

def get_stub() -> vault_pb2_grpc.VaultStub:
    global _channel
    if _channel is None:
        _channel = _build_channel()
    return vault_pb2_grpc.VaultStub(_channel)


# ── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Staged Autonomy — Slack Gateway",
    description="Slack slash command handler for /agent. Exposed via Cloudflare Tunnel.",
    version="1.0.0",
    # Hide docs from public — only /health and /slack/command are active
    docs_url=None,
    redoc_url=None,
)

TIER_MAP = {
    "tier1": vault_pb2.TIER_1,
    "tier2": vault_pb2.TIER_2,
    "tier3": vault_pb2.TIER_3,
    "tier4": vault_pb2.TIER_4,
    "1": vault_pb2.TIER_1,
    "2": vault_pb2.TIER_2,
    "3": vault_pb2.TIER_3,
    "4": vault_pb2.TIER_4,
}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Liveness — checks this service is up and Vault responds."""
    try:
        stub = get_stub()
        resp = stub.Health(vault_pb2.HealthCheck(watchdog_id="api-gateway"), timeout=5)
        return {
            "api": "ok",
            "vault": {
                "healthy": resp.healthy,
                "status": resp.status,
                "uptime_seconds": resp.uptime_seconds,
                "active_sessions": resp.active_sessions,
            },
        }
    except grpc.RpcError as e:
        return JSONResponse(status_code=503, content={
            "api": "ok",
            "vault": "unreachable",
            "error": e.details(),
        })


# ── Slack ─────────────────────────────────────────────────────────────────────

def _verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    """Verify Slack's HMAC-SHA256 request signature."""
    if not SLACK_SIGNING_SECRET:
        logger.warning("SLACK_SIGNING_SECRET not set — skipping signature verification")
        return True

    # Reject requests older than 5 minutes (replay protection)
    if abs(time.time() - float(timestamp)) > 300:
        return False

    base = f"v0:{timestamp}:{body.decode()}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        base.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _parse_tier(text: str) -> tuple[str, int]:
    """
    Extract tier prefix from Slack command text.
      't2: build the tests'  → tier2, 'build the tests'
      't3: deploy to prod'   → tier3, 'deploy to prod'
      'explain X'            → tier1, 'explain X'  (default)
    """
    prefixes = {
        "t1:": ("tier1", vault_pb2.TIER_1),
        "t2:": ("tier2", vault_pb2.TIER_2),
        "t3:": ("tier3", vault_pb2.TIER_3),
        "t4:": ("tier4", vault_pb2.TIER_4),
    }
    lower = text.strip().lower()
    for prefix, (name, proto_tier) in prefixes.items():
        if lower.startswith(prefix):
            return name, proto_tier, text[len(prefix):].strip()
    return "tier1", vault_pb2.TIER_1, text.strip()


async def _call_vault_and_reply(prompt: str, tier: int, user_id: str,
                                session_id: str, response_url: str):
    """Run Vault call in background and POST result back to Slack."""
    try:
        stub = get_stub()
        resp = stub.ProcessPrompt(vault_pb2.VaultPromptRequest(
            user_id=user_id,
            session_id=session_id,
            prompt=prompt,
            domain="coding",
            tier=tier,
        ), timeout=120)

        if resp.error:
            text = f":x: Vault error: {resp.error}"
        elif resp.type == vault_pb2.ACTION_PENDING:
            text = (
                f":hourglass: Action pending approval\n"
                f"*ID:* `{resp.response_id}`\n"
                f"*Action:* {resp.action.description}\n"
                f"*Risk:* {resp.action.risk_level}\n"
                f"To approve: `/agent approve {resp.response_id}`"
            )
        elif resp.type == vault_pb2.PR_PROPOSED:
            text = (
                f":twisted_rightwards_arrows: PR proposed\n"
                f"{resp.content}\n"
                + (f"<{resp.action.shadow_pr_link}|View PR>" if resp.action.shadow_pr_link else "")
            )
        else:
            text = resp.content or "_(empty response)_"

    except grpc.RpcError as e:
        text = f":x: gRPC error `{e.code()}`: {e.details()}"
    except Exception as e:
        text = f":x: Unexpected error: {e}"

    async with httpx.AsyncClient() as client:
        await client.post(response_url, json={
            "response_type": "in_channel",
            "text": text,
        })


@app.post("/slack/command")
async def slack_command(
    request: Request,
    text: str = Form(default=""),
    user_id: str = Form(default="slack_user"),
    user_name: str = Form(default=""),
    response_url: str = Form(default=""),
):
    """
    Handles Slack slash commands sent to /agent.

    Set up in Slack App:
      Slash Command: /agent
      Request URL:   http://<PI_IP>:8080/slack/command
      (or via ngrok/Tailscale for external access)

    Usage:
      /agent explain the vault classifier
      /agent t2: run the test suite
      /agent t3: merge PR #42
      /agent approve abc-123
    """
    # Verify Slack signature
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not _verify_slack_signature(body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    text = text.strip()
    if not text:
        return {"response_type": "ephemeral", "text": "Usage: `/agent <prompt>` or `/agent t2: <prompt>`"}

    # Handle approve subcommand
    if text.lower().startswith("approve "):
        response_id = text.split(" ", 1)[1].strip()
        try:
            stub = get_stub()
            resp = stub.ApproveAction(vault_pb2.ApprovalRequest(
                response_id=response_id,
                reason=f"Approved via Slack by {user_name or user_id}",
            ), timeout=30)
            msg = f":white_check_mark: Approved `{response_id}`" if resp.success else f":x: Failed: {resp.error}"
        except grpc.RpcError as e:
            msg = f":x: {e.details()}"
        return {"response_type": "in_channel", "text": msg}

    tier_name, tier_proto, prompt = _parse_tier(text)
    session_id = str(uuid.uuid4())

    # Respond immediately (Slack requires < 3s) — do actual work async
    if response_url:
        asyncio.create_task(_call_vault_and_reply(
            prompt=prompt,
            tier=tier_proto,
            user_id=user_id,
            session_id=session_id,
            response_url=response_url,
        ))
        return {
            "response_type": "ephemeral",
            "text": f":hourglass_flowing_sand: Processing `{tier_name}` request... (I'll post the result here)",
        }

    # Fallback: synchronous (no response_url — shouldn't happen with real Slack)
    tier_name2, tier_proto2, prompt2 = _parse_tier(text)
    try:
        stub = get_stub()
        resp = stub.ProcessPrompt(vault_pb2.VaultPromptRequest(
            user_id=user_id, session_id=session_id,
            prompt=prompt2, domain="coding", tier=tier_proto2,
        ), timeout=120)
        return {"response_type": "in_channel", "text": resp.content or resp.error or "_(no response)_"}
    except grpc.RpcError as e:
        raise HTTPException(status_code=502, detail=e.details())
