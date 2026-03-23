"""
MuscleClient — used by the autonomy loop to call the Win11 inference engine.

Handles:
  - Wake-on-LAN when Win11 is sleeping
  - mTLS gRPC connection to Muscle
  - Streaming token generation
"""
import asyncio
import logging
import os
import socket
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import grpc
    _GRPC_OK = True
except ImportError:
    grpc = None  # type: ignore
    _GRPC_OK = False

try:
    import sys
    sys.path.insert(0, "/app/cmd/muscle")
    import muscle_pb2
    import muscle_pb2_grpc
    _PROTO_OK = True
except ImportError:
    muscle_pb2 = None  # type: ignore
    muscle_pb2_grpc = None  # type: ignore
    _PROTO_OK = False


class MuscleClient:
    """
    Vault-side client for the Win11 Muscle inference service.

    Usage:
        client = MuscleClient()
        code = await client.generate(session_id, prompt, system_context)
    """

    MUSCLE_HOST    = os.getenv("MUSCLE_HOST",            "10.0.0.105")
    MUSCLE_PORT    = int(os.getenv("MUSCLE_PORT",        "50051"))
    MUSCLE_TLS_SN  = os.getenv("MUSCLE_TLS_SERVER_NAME", "win11-muscle")
    CERT_FILE      = os.getenv("CERT_FILE",              "/run/certs/client.crt")
    KEY_FILE       = os.getenv("KEY_FILE",               "/run/certs/client.key")
    MUSCLE_CA_CERT = os.getenv("MUSCLE_CA_CERT",         "/run/certs/muscle.crt")
    WIN11_MAC      = os.getenv("WIN11_MAC",              "")
    WAKE_TIMEOUT_S = int(os.getenv("MUSCLE_WAKE_TIMEOUT", "120"))

    def _send_wol(self) -> None:
        """Broadcast a Wake-on-LAN magic packet to WIN11_MAC."""
        if not self.WIN11_MAC:
            logger.warning("[muscle] WIN11_MAC not set — cannot send WoL")
            return
        try:
            mac_bytes = bytes.fromhex(self.WIN11_MAC.replace(":", "").replace("-", ""))
            magic = b"\xff" * 6 + mac_bytes * 16
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.sendto(magic, ("255.255.255.255", 9))
                s.sendto(magic, ("10.0.0.255", 9))  # directed subnet broadcast
            logger.info(f"[muscle] WoL magic packet sent to {self.WIN11_MAC}")
        except Exception as exc:
            logger.warning(f"[muscle] WoL send failed: {exc}")

    def _build_channel(self):
        with open(self.CERT_FILE,      "rb") as f: cert = f.read()
        with open(self.KEY_FILE,       "rb") as f: key  = f.read()
        with open(self.MUSCLE_CA_CERT, "rb") as f: ca   = f.read()
        creds = grpc.ssl_channel_credentials(
            root_certificates=ca, private_key=key, certificate_chain=cert
        )
        return grpc.secure_channel(
            f"{self.MUSCLE_HOST}:{self.MUSCLE_PORT}",
            creds,
            options=[("grpc.ssl_target_name_override", self.MUSCLE_TLS_SN)],
        )

    async def _probe(self, stub) -> bool:
        try:
            r = stub.Health(
                muscle_pb2.HealthRequest(session_id="vault-probe"), timeout=4
            )
            return bool(r.healthy)
        except Exception:
            return False

    async def _connect(self):
        """
        Return a live MuscleStub, sending WoL and waiting if Win11 is asleep.
        Returns None if Muscle is not reachable after WAKE_TIMEOUT_S seconds.
        """
        if not _GRPC_OK or not _PROTO_OK:
            logger.warning("[muscle] grpc or proto stubs not available — skipping")
            return None
        try:
            ch   = self._build_channel()
            stub = muscle_pb2_grpc.MuscleStub(ch)
            if await self._probe(stub):
                logger.info("[muscle] Muscle already online")
                return stub

            # Not reachable — send WoL and poll
            logger.info(
                f"[muscle] Muscle unreachable at {self.MUSCLE_HOST}:{self.MUSCLE_PORT} "
                f"— sending WoL, waiting up to {self.WAKE_TIMEOUT_S}s"
            )
            self._send_wol()
            deadline = time.monotonic() + self.WAKE_TIMEOUT_S
            while time.monotonic() < deadline:
                await asyncio.sleep(5)
                try:
                    ch   = self._build_channel()
                    stub = muscle_pb2_grpc.MuscleStub(ch)
                    if await self._probe(stub):
                        logger.info("[muscle] Muscle came online after WoL")
                        return stub
                except Exception:
                    pass
            logger.warning("[muscle] Muscle did not respond within wake timeout")
            return None
        except FileNotFoundError as exc:
            logger.warning(f"[muscle] cert not found ({exc}) — cannot connect")
            return None
        except Exception as exc:
            logger.warning(f"[muscle] connect error: {exc}")
            return None

    async def generate(
        self,
        session_id: str,
        prompt: str,
        system_context: str = "",
        max_tokens: int = 2048,
        temperature: float = 0.7,
        top_p: float = 0.9,
    ) -> str:
        """
        Stream tokens from Muscle and return the full generated text.
        Returns an empty string if Muscle is unavailable.
        """
        stub = await self._connect()
        if stub is None:
            return ""
        try:
            config = muscle_pb2.InferenceConfig(
                temperature=temperature,
                top_p=top_p,
                top_k=40,
                max_tokens=max_tokens,
                stream=True,
            )
            req = muscle_pb2.PromptRequest(
                session_id=session_id,
                prompt=prompt,
                system_context=system_context,
                config=config,
                action_intent="code_gen",
            )
            tokens: list[str] = []
            async for resp in stub.GenerateResponse(req):
                if resp.status == "error":
                    logger.error(f"[muscle] inference error: {resp.error_msg}")
                    break
                if resp.token:
                    tokens.append(resp.token)
            result = "".join(tokens)
            logger.info(
                f"[muscle] generation complete — {len(tokens)} tokens "
                f"({len(result)} chars)"
            )
            return result
        except Exception as exc:
            logger.error(f"[muscle] generate failed: {exc}")
            return ""
