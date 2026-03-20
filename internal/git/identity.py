"""
Git Identity — manages the agent's SSH key pair and git config.

The agent has a real, persistent identity:
  - An RSA-4096 SSH key stored in a Docker volume (/run/git-identity/)
  - A dedicated git user.name + user.email
  - The public key is printed to logs on first boot so it can be added to the
    agent's GitHub account once (then it persists across container restarts)

Usage:
    identity = GitIdentity()
    await identity.initialize()
    print(identity.public_key)          # add this to the GitHub account
    identity.configure_repo(repo_path)  # sets user.name/email + SSH for a repo
"""
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

IDENTITY_DIR = Path(os.getenv("GIT_IDENTITY_DIR", "/run/git-identity"))
KEY_FILE     = IDENTITY_DIR / "id_rsa"
PUB_FILE     = IDENTITY_DIR / "id_rsa.pub"

# From env — defaulting to something recognisable in GitHub contribution graph
AGENT_GIT_NAME  = os.getenv("AGENT_GIT_NAME",  "teammate-agent")
AGENT_GIT_EMAIL = os.getenv("AGENT_GIT_EMAIL", "agent@teammate.local")


class GitIdentity:
    """Manages the agent's persistent SSH keypair and git author identity."""

    def __init__(self):
        self.public_key: str = ""
        self._ready = False

    async def initialize(self):
        IDENTITY_DIR.mkdir(parents=True, exist_ok=True)

        if not KEY_FILE.exists():
            logger.info("No SSH key found — generating RSA-4096 keypair for the agent")
            _run(
                ["ssh-keygen", "-t", "rsa", "-b", "4096",
                 "-C", AGENT_GIT_EMAIL, "-f", str(KEY_FILE), "-N", ""],
            )
            KEY_FILE.chmod(0o600)

        self.public_key = PUB_FILE.read_text().strip()
        self._ready = True
        logger.info(
            f"Agent git identity ready — name='{AGENT_GIT_NAME}' email='{AGENT_GIT_EMAIL}'\n"
            f"SSH public key (add to GitHub account):\n{self.public_key}"
        )

    def configure_repo(self, repo_path: str | Path):
        """Set author identity + SSH command inside a cloned repo directory."""
        if not self._ready:
            raise RuntimeError("GitIdentity.initialize() must be called first")
        p = Path(repo_path)
        _run(["git", "-C", str(p), "config", "user.name",  AGENT_GIT_NAME])
        _run(["git", "-C", str(p), "config", "user.email", AGENT_GIT_EMAIL])
        _run(["git", "-C", str(p), "config", "core.sshCommand",
              f"ssh -i {KEY_FILE} -o StrictHostKeyChecking=no"])

    @property
    def ssh_key_path(self) -> Path:
        return KEY_FILE


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout.strip()
