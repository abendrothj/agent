"""
MCPContextProvider — the Thalamus

Human Biology Mapping
─────────────────────
  Peripheral nervous system (PNS)  → MCP protocol layer
  Sensory receptors                → MCP servers (git=proprioception, files=tactile, fetch=vision)
  Afferent neurons                 → asyncio tasks calling call_tool()
  Thalamus (sensory routing)       → MCPContextProvider.gather()
  Primary sensory cortex           → node_sense_context  (LangGraph, new)
  Association cortex               → node_classify        (LangGraph, existing)
  Working memory (prefrontal WM)   → VaultState.mcp_context

In biology, the thalamus:
  - Receives almost all sensory signals before they reach the cortex
  - Filters noise, enhances signal-to-noise ratio
  - Has a hard gating window — signals that don't arrive in time are dropped
  - The thalamo-cortical feedback loop creates selective attention

Here:
  - MCP servers produce structured sensory signals
  - gather() relays them to the prefrontal cortex (LangGraph)
  - A 2-second timeout gates the window — if the signal doesn't arrive,
    cognition proceeds without it (the agent is not paralysed by slow senses)
  - Falls back to direct Python implementations when MCP servers are absent

Cost: completely free. The mcp SDK, mcp-server-git, and stdlib file reading
carry no per-request fees. All transport is local stdio.
"""

import asyncio
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MCP_AVAILABLE = False
try:
    from mcp import ClientSession                           # type: ignore
    from mcp.client.stdio import stdio_client              # type: ignore
    from mcp.client.stdio import StdioServerParameters     # type: ignore
    _MCP_AVAILABLE = True
except ImportError:
    pass

_GIT_SERVER_AVAILABLE = False
try:
    import importlib.util
    if importlib.util.find_spec("mcp_server_git") is not None:
        _GIT_SERVER_AVAILABLE = True
except Exception:
    pass


# ── MCP context dataclass ────────────────────────────────────────────────────

@dataclass
class MCPContext:
    """
    Multi-modal sensory context assembled by the thalamus.

    Each field corresponds to a different sensory modality:
      git_summary   → proprioception  (where is the body in space? what has changed?)
      file_snippets → tactile input   (what does the relevant code feel like?)

    The formatted string is injected into VaultState.mcp_context and passed
    to node_classify as an enriched system_context prefix.
    """
    git_summary: str = ""
    file_snippets: list = field(default_factory=list)  # list[str]

    def format(self) -> Optional[str]:
        """Render for injection into system_context. None if nothing gathered."""
        parts: list[str] = []
        if self.git_summary.strip():
            parts.append(f"<mcp:git>\n{self.git_summary.strip()}\n</mcp:git>")
        for snippet in self.file_snippets:
            if snippet.strip():
                parts.append(f"<mcp:file>\n{snippet.strip()}\n</mcp:file>")
        if not parts:
            return None
        return (
            "## Peripheral Context (MCP — sensory input to prefrontal cortex)\n"
            + "\n\n".join(parts)
        )


# ── Provider ─────────────────────────────────────────────────────────────────

class MCPContextProvider:
    """
    Thalamus — filters and routes peripheral sensory signals into the
    LangGraph decision pipeline before risk classification fires.

    Usage:
        provider = MCPContextProvider(workspace_path="/workspace")
        await provider.initialize()
        ctx = await provider.gather("please refactor internal/auth/jwt.py")
        # ctx is an Optional[str] ready to set on VaultState["mcp_context"]
    """

    def __init__(
        self,
        workspace_path: str,
        enabled: bool = True,
        timeout_seconds: float = 2.0,
        max_file_lines: int = 60,
        max_files: int = 3,
    ) -> None:
        self._workspace = Path(workspace_path).resolve()
        self._enabled = enabled
        self._timeout = timeout_seconds
        self._max_file_lines = max_file_lines
        self._max_files = max_files

    async def initialize(self) -> None:
        """No persistent connections needed — each gather() is self-contained."""
        logger.info(
            "[mcp/thalamus] initialized  workspace=%s  mcp=%s  git_server=%s",
            self._workspace,
            _MCP_AVAILABLE,
            _GIT_SERVER_AVAILABLE,
        )

    async def gather(self, prompt: str) -> Optional[str]:
        """
        Thalamic relay: collect multi-modal sensory context with a hard timeout.

        If any sensory channel is slow or unavailable, the others still
        contribute — the brain doesn't wait for one stuck sense organ.
        """
        if not self._enabled:
            return None

        try:
            async with asyncio.timeout(self._timeout):
                git_task = asyncio.create_task(self._git_context())
                file_task = asyncio.create_task(self._file_context(prompt))
                git_result, file_result = await asyncio.gather(
                    git_task, file_task, return_exceptions=True
                )

            ctx = MCPContext()
            if isinstance(git_result, str):
                ctx.git_summary = git_result
            elif isinstance(git_result, Exception):
                logger.debug("[mcp/git] %s", git_result)

            if isinstance(file_result, list):
                ctx.file_snippets = file_result
            elif isinstance(file_result, Exception):
                logger.debug("[mcp/files] %s", file_result)

            formatted = ctx.format()
            if formatted:
                logger.debug("[mcp/thalamus] context gathered (%d chars)", len(formatted))
            return formatted

        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("[mcp/thalamus] sensory timeout (%.1fs) — proceeding without context", self._timeout)
            return None
        except Exception as exc:
            logger.warning("[mcp/thalamus] gather failed: %s", exc)
            return None

    # ── Git proprioception ───────────────────────────────────────────────────

    async def _git_context(self) -> str:
        """
        Proprioception — where is the agent in its own history?
        Knows: current branch, recent commits, unstaged modifications.

        Tries the real MCP git server first (full protocol compliance);
        falls back to direct subprocess calls if the server isn't installed.
        This mirrors the biological principle of redundant pathways: if the
        primary afferent nerve is cut, the secondary pathway keeps signalling.
        """
        if _MCP_AVAILABLE and _GIT_SERVER_AVAILABLE:
            try:
                return await self._git_via_mcp_server()
            except Exception as exc:
                logger.debug("[mcp/git] MCP server path failed: %s — using direct", exc)

        # Spinal reflex fallback: direct subprocess git
        return await asyncio.get_event_loop().run_in_executor(None, self._git_subprocess)

    async def _git_via_mcp_server(self) -> str:
        """
        Connect to mcp-server-git via stdio transport.
        The MCP protocol here acts as a well-defined afferent nerve channel:
          call_tool("git_log")    → episodic memory (what happened?)
          call_tool("git_status") → current body state (what changed?)
        """
        params = StdioServerParameters(
            command="python",
            args=["-m", "mcp_server_git", "--repository", str(self._workspace)],
        )
        lines: list[str] = []
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}

                if "git_log" in tools:
                    result = await session.call_tool("git_log", {"max_count": 10})
                    if result.content:
                        lines.append(f"Recent commits:\n{result.content[0].text}")

                if "git_status" in tools:
                    result = await session.call_tool("git_status", {})
                    if result.content:
                        lines.append(f"Working tree:\n{result.content[0].text}")

                if "git_diff" in tools:
                    result = await session.call_tool("git_diff", {"staged": False})
                    diff_text = result.content[0].text if result.content else ""
                    if diff_text.strip():
                        # Truncate large diffs — only show first 40 lines
                        diff_lines = diff_text.splitlines()[:40]
                        lines.append(f"Unstaged diff (first 40 lines):\n" + "\n".join(diff_lines))

        return "\n\n".join(lines)

    def _git_subprocess(self) -> str:
        """
        Spinal reflex fallback — minimal git information via subprocess.
        Fast, always available, requires only git in PATH.
        """
        def run(*args: str) -> str:
            try:
                r = subprocess.run(
                    ["git", *args],
                    cwd=str(self._workspace),
                    capture_output=True,
                    text=True,
                    timeout=4,
                )
                return r.stdout.strip() if r.returncode == 0 else ""
            except Exception:
                return ""

        branch = run("branch", "--show-current")
        log    = run("log", "--oneline", "-10")
        status = run("status", "--short")

        lines: list[str] = []
        if branch:
            lines.append(f"Branch: {branch}")
        if log:
            lines.append(f"Recent commits:\n{log}")
        if status:
            lines.append(f"Modified files:\n{status}")

        return "\n".join(lines)

    # ── Tactile / file context ───────────────────────────────────────────────

    async def _file_context(self, prompt: str) -> list[str]:
        """
        Tactile input — read relevant code files referenced in the prompt.

        Biology: somatosensory cortex receives signals from skin receptors
        about what the hands are touching. Here, we extract file/module
        references from the prompt and surface the relevant code.

        This gives node_classify real signal: "this request touches
        internal/auth/jwt.py which handles token signing — elevated risk".
        """
        paths = self._extract_file_references(prompt)
        if not paths:
            return []

        snippets: list[str] = []
        loop = asyncio.get_event_loop()
        for path in paths[:self._max_files]:
            try:
                snippet = await loop.run_in_executor(None, self._read_file_snippet, path)
                if snippet:
                    snippets.append(snippet)
            except Exception as exc:
                logger.debug("[mcp/files] could not read %s: %s", path, exc)

        return snippets

    def _extract_file_references(self, prompt: str) -> list[str]:
        """
        Parse the prompt for file paths, module imports, and file-like tokens.
        Returns absolute paths that exist in the workspace.
        """
        candidates: list[str] = []

        # Direct path patterns: internal/foo/bar.py, cmd/vault/main.py
        for match in re.finditer(r"[\w./]+\.(?:py|ts|go|js|json|yaml|yml|sh|sql)", prompt):
            candidates.append(match.group(0))

        # Python module patterns: from internal.foo import bar → internal/foo.py
        for match in re.finditer(r"(?:from|import)\s+([\w.]+)", prompt):
            module = match.group(1).replace(".", "/")
            candidates.extend([f"{module}.py", f"{module}/__init__.py"])

        # Deduplicate, resolve relative to workspace
        seen: set[str] = set()
        resolved: list[str] = []
        for c in candidates:
            c_clean = c.lstrip("/")
            if c_clean in seen:
                continue
            seen.add(c_clean)
            full = self._workspace / c_clean
            if full.is_file():
                resolved.append(str(full))

        return resolved

    def _read_file_snippet(self, path: str) -> str:
        """Read the first N lines of a file for context."""
        try:
            lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
            snippet_lines = lines[:self._max_file_lines]
            rel = Path(path).relative_to(self._workspace)
            header = f"# {rel}  ({len(lines)} lines total, showing first {len(snippet_lines)})"
            return header + "\n" + "\n".join(snippet_lines)
        except Exception:
            return ""
