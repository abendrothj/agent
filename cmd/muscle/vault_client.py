"""
VaultClient — Muscle (Win11) calls Pi Vault for every action proposal.

This thin async client wraps vault_pb2_grpc.VaultStub and exposes the
three methods Muscle needs:
  propose_action()   → ProcessPrompt
  approve_action()   → ApproveAction
  query_policy()     → QueryPolicy

Import path: vault_pb2 / vault_pb2_grpc are generated into internal/api/
by scripts/gen_protos.sh.  The Muscle Dockerfile copies the relevant
*_pb2.py files adjacent to main.py so `import vault_pb2` works directly.
"""

import sys
import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Optional, Tuple

import grpc

# Proto stubs: generated into internal/api/, copied to cmd/muscle/ by Dockerfile
# When running locally (not inside Docker), add internal/api to sys.path.
_PROTO_DIR = Path(__file__).resolve().parent.parent.parent / "internal" / "api"
if str(_PROTO_DIR) not in sys.path:
    sys.path.insert(0, str(_PROTO_DIR))

import vault_pb2          # noqa: E402
import vault_pb2_grpc     # noqa: E402

logger = logging.getLogger(__name__)


class VaultClient:
    """
    Async gRPC client for the Pi Vault service.

    Usage:
        client = VaultClient()
        await client.connect()
        approved, reason, tier = await client.propose_action(
            prompt="write a function that ...",
            domain="coding",
            session_id="req-001",
        )
        await client.close()
    """

    def __init__(
        self,
        vault_url: Optional[str] = None,
        cert_file: Optional[str] = None,
        key_file: Optional[str] = None,
        ca_cert: Optional[str] = None,
    ):
        self._url = vault_url or os.getenv("VAULT_API_URL", "localhost:50051")
        # Win11 holds: muscle.crt (its identity) + muscle.key (its private key)
        # Pi's client.crt is used as the trusted CA to verify the Pi's certificate.
        # See WIN11_SETUP.md for cert installation path (default: ./certs/ beside main.py).
        self._cert_file = cert_file or os.getenv("MUSCLE_CERT_FILE", os.getenv("CERT_FILE", "./certs/muscle.crt"))
        self._key_file  = key_file  or os.getenv("MUSCLE_KEY_FILE",  os.getenv("KEY_FILE",  "./certs/muscle.key"))
        self._ca_cert   = ca_cert   or os.getenv("VAULT_CA_CERT",    os.getenv("CA_CERT",   "./certs/client.crt"))
        self._channel: Optional[grpc.aio.Channel] = None
        self._stub: Optional[vault_pb2_grpc.VaultStub] = None

    async def connect(self):
        """Open a gRPC channel to Vault.  Uses mTLS if cert files exist."""
        credentials = self._build_credentials()
        if credentials:
            self._channel = grpc.aio.secure_channel(self._url, credentials)
            logger.info("VaultClient connected (mTLS) to %s", self._url)
        else:
            self._channel = grpc.aio.insecure_channel(self._url)
            logger.warning("VaultClient connected (insecure) to %s", self._url)

        self._stub = vault_pb2_grpc.VaultStub(self._channel)

    async def close(self):
        if self._channel:
            await self._channel.close()
            self._channel = None
            self._stub = None

    # ── Public API ────────────────────────────────────────────────────────────

    async def propose_action(
        self,
        prompt: str,
        domain: str = "coding",
        session_id: Optional[str] = None,
        user_id: str = "muscle",
        tier: int = 0,
    ) -> Tuple[bool, str, int]:
        """
        Send an action proposal to Vault for approval.

        Returns:
            (approved, reason, tier_classified)
            approved=False + reason starting with "PENDING_MFA:" means
            human approval is required — call approve_action() after.
        """
        self._require_connected()
        sid = session_id or str(uuid.uuid4())
        request = vault_pb2.VaultPromptRequest(
            user_id=user_id,
            session_id=sid,
            prompt=prompt,
            domain=domain,
            tier=tier,
        )
        try:
            response: vault_pb2.VaultResponse = await self._stub.ProcessPrompt(request)
        except grpc.aio.AioRpcError as err:
            logger.error("ProcessPrompt RPC failed: %s", err)
            return False, f"Vault RPC error: {err.details()}", 0

        approved = response.approval == vault_pb2.APPROVED
        return approved, response.content, response.tier_classified

    async def approve_action(
        self,
        response_id: str,
        approver_signature: str = "",
        reason: str = "human approved",
    ) -> Tuple[bool, str]:
        """
        Confirm a pending MFA approval request on Vault.

        Returns: (success, result_message)
        """
        self._require_connected()
        request = vault_pb2.ApprovalRequest(
            response_id=response_id,
            approver_signature=approver_signature,
            reason=reason,
        )
        try:
            response: vault_pb2.ExecutionResponse = await self._stub.ApproveAction(request)
        except grpc.aio.AioRpcError as err:
            logger.error("ApproveAction RPC failed: %s", err)
            return False, f"Vault RPC error: {err.details()}"

        return response.success, response.result

    async def query_policy(self, user_id: str = "") -> dict:
        """
        Query current Vault governance state.

        Returns a dict with keys: changes_today, max_changes_per_day,
        tier_3_pending, tier_4_pending, last_update.
        """
        self._require_connected()
        request = vault_pb2.PolicyQueryRequest(user_id=user_id)
        try:
            response: vault_pb2.PolicyState = await self._stub.QueryPolicy(request)
        except grpc.aio.AioRpcError as err:
            logger.error("QueryPolicy RPC failed: %s", err)
            return {}

        return {
            "changes_today": response.changes_today,
            "max_changes_per_day": response.max_changes_per_day,
            "tier_3_pending": response.tier_3_pending,
            "tier_4_pending": response.tier_4_pending,
            "last_update": response.last_update,
        }

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _require_connected(self):
        if self._stub is None:
            raise RuntimeError("VaultClient.connect() has not been called")

    def _build_credentials(self) -> Optional[grpc.ChannelCredentials]:
        for path in (self._cert_file, self._key_file, self._ca_cert):
            if not Path(path).exists():
                logger.debug("TLS cert not found at %s — using insecure channel", path)
                return None

        with open(self._cert_file, "rb") as f:
            cert = f.read()
        with open(self._key_file, "rb") as f:
            key = f.read()
        with open(self._ca_cert, "rb") as f:
            ca = f.read()

        return grpc.ssl_channel_credentials(
            root_certificates=ca,
            private_key=key,
            certificate_chain=cert,
        )
