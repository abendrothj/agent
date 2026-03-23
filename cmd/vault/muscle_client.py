"""
MuscleClient — used by the autonomy loop to call the Win11 inference engine.

Handles:
  - mTLS gRPC connection to Muscle
  - Streaming token generation
"""
import asyncio
import logging
import os
import sys
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import grpc
    _GRPC_OK = True
except ImportError:
    grpc = None  # type: ignore
    _GRPC_OK = False

try:
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

    async def _connect(self):
        """Return a live MuscleStub or None if Muscle is unreachable."""
        if not _GRPC_OK or not _PROTO_OK:
            logger.warning("[muscle] grpc or proto stubs not available")
            return None
        try:
            ch   = self._build_channel()
            stub = muscle_pb2_grpc.MuscleStub(ch)
            r    = stub.Health(muscle_pb2.HealthRequest(session_id="vault-probe"), timeout=4)
            if r.healthy:
                logger.info("[muscle] Muscle online")
                return stub
            logger.warning(f"[muscle] Muscle at {self.MUSCLE_HOST}:{self.MUSCLE_PORT} unhealthy")
            return None
        except FileNotFoundError as exc:
            logger.warning(f"[muscle] cert not found ({exc})")
            return None
        except Exception as exc:
            logger.warning(f"[muscle] unreachable: {exc}")
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
