"""Risk Tier Classifier — intent-based request risk assessment.

Classification is based on *what is being done* (action class) and *to what*
(target domain), not keyword counting.  Every request falls into one of five
action classes and one of four target domains.  The tier is their intersection.

Action classes (checked in priority order, highest risk first):
  OVERRIDE  — rollback, disable, bypass, purge, force, delete …
  DEPLOY    — commit, push, merge, deploy, create/open PR …
  EXECUTE   — run, build, test, generate, install, restart …
  WRITE     — create, modify, fix, refactor, update, rename …
  READ      — explain, show, query, find, describe … (default)

Target domains (checked in priority order, highest risk first):
  SELF      — vault, policy, ledger, secrets, approval rules, MFA …
  INFRA     — production, config, services, containers, databases …
  SHARED    — github, git repositories, packages, dependencies …
  LOCAL     — local files, sandbox, temp, scripts … (default)

Tier matrix:
              LOCAL   SHARED   INFRA   SELF
  READ          1       1        2      4
  WRITE         2       3        3      4
  EXECUTE       2       3        3      4
  DEPLOY        3       3        4      4
  OVERRIDE      3       4        4      4

Scope escalation (applied after matrix lookup):
  scope in (system, global) → minimum Tier 3
  scope in (sandbox,)       → minimum Tier 2

This model fixes the classic pitfalls of bare-substring matching:
  • "pr"     → word-bounded \\bpr\\b; does not match "approach", "temperature"
  • "system" → only relevant when it is the TARGET, not double-counted
  • "list of files" → READ × LOCAL = Tier 1, as expected
"""
import re
import logging
from enum import IntEnum

logger = logging.getLogger(__name__)


class Tier(IntEnum):
    """Risk tier classification (immutable per base_policy.yaml)"""
    TIER_1_SAFE = 1
    TIER_2_MINOR = 2
    TIER_3_MAJOR = 3
    TIER_4_CRITICAL = 4


# ── Taxonomy ──────────────────────────────────────────────────────────────────

class _Action:
    READ     = "read"
    WRITE    = "write"
    EXECUTE  = "execute"
    DEPLOY   = "deploy"
    OVERRIDE = "override"


class _Target:
    LOCAL  = "local"
    SHARED = "shared"
    INFRA  = "infra"
    SELF   = "self"


# Tier outcome for each (action, target) pair.
_TIER_MATRIX: dict[tuple[str, str], Tier] = {
    (_Action.READ,     _Target.LOCAL):  Tier.TIER_1_SAFE,
    (_Action.READ,     _Target.SHARED): Tier.TIER_1_SAFE,
    (_Action.READ,     _Target.INFRA):  Tier.TIER_2_MINOR,
    (_Action.READ,     _Target.SELF):   Tier.TIER_4_CRITICAL,

    (_Action.WRITE,    _Target.LOCAL):  Tier.TIER_2_MINOR,
    (_Action.WRITE,    _Target.SHARED): Tier.TIER_3_MAJOR,
    (_Action.WRITE,    _Target.INFRA):  Tier.TIER_3_MAJOR,
    (_Action.WRITE,    _Target.SELF):   Tier.TIER_4_CRITICAL,

    (_Action.EXECUTE,  _Target.LOCAL):  Tier.TIER_2_MINOR,
    (_Action.EXECUTE,  _Target.SHARED): Tier.TIER_3_MAJOR,
    (_Action.EXECUTE,  _Target.INFRA):  Tier.TIER_3_MAJOR,
    (_Action.EXECUTE,  _Target.SELF):   Tier.TIER_4_CRITICAL,

    (_Action.DEPLOY,   _Target.LOCAL):  Tier.TIER_3_MAJOR,
    (_Action.DEPLOY,   _Target.SHARED): Tier.TIER_3_MAJOR,
    (_Action.DEPLOY,   _Target.INFRA):  Tier.TIER_4_CRITICAL,
    (_Action.DEPLOY,   _Target.SELF):   Tier.TIER_4_CRITICAL,

    (_Action.OVERRIDE, _Target.LOCAL):  Tier.TIER_3_MAJOR,
    (_Action.OVERRIDE, _Target.SHARED): Tier.TIER_4_CRITICAL,
    (_Action.OVERRIDE, _Target.INFRA):  Tier.TIER_4_CRITICAL,
    (_Action.OVERRIDE, _Target.SELF):   Tier.TIER_4_CRITICAL,
}


# Action patterns checked in priority order — highest risk first.
# All use word boundaries to prevent false substring matches.
_ACTION_PATTERNS: list[tuple[str, list[str]]] = [
    (_Action.OVERRIDE, [
        r"\boverride\b", r"\brollback\b", r"\brevert\b", r"\bdisable\b",
        r"\bbypass\b",   r"\bforce\b",    r"\bpurge\b",  r"\bdrop\b",
        r"\bdelete\b",   r"\breset\b",
    ]),
    (_Action.DEPLOY, [
        r"\bdeploy\b",    r"\bcommit\b",     r"\bmerge\b",  r"\brelease\b",
        r"\bship\b",      r"\bpublish\b",    r"\bpush\b",
        r"pull.request",  r"create.*\bpr\b", r"open.*\bpr\b",
        r"submit.*\bpr\b", r"new.*\bpr\b",
    ]),
    (_Action.EXECUTE, [
        r"\brun\b",      r"\bexecute\b",  r"\bbuild\b",    r"\btest\b",
        r"\bgenerate\b", r"\bcompile\b",  r"\binstall\b",  r"\blaunch\b",
        r"\bstart\b",    r"\brestart\b",  r"\bopen\b",     r"\btrigger\b",
    ]),
    (_Action.WRITE, [
        r"\bcreate\b",    r"\bwrite\b",   r"\badd\b",    r"\bfix\b",
        r"\brefactor\b",  r"\brename\b",  r"\bmove\b",   r"\bupdate\b",
        r"\bedit\b",      r"\bmodify\b",  r"\bchange\b", r"\bpatch\b",
        r"\bimplement\b", r"\bset\b",
    ]),
    # READ is the fallback — _extract_action returns it when nothing else matched.
]

# Target patterns checked in priority order — highest risk first.
# LOCAL is the implicit fallback (always matches when nothing above does).
_TARGET_PATTERNS: list[tuple[str, list[str]]] = [
    (_Target.SELF, [
        r"\bvault\b",         r"\bpolicy\b",        r"\bledger\b",
        r"\bcredential\b",    r"\bsecret\b",         r"\bmfa\b",
        r"\bpermission\b",    r"\bapproval.rule\b",  r"\brejection.cache\b",
        r"\bmemory.schema\b", r"\bapproval_rules\b",
    ]),
    (_Target.INFRA, [
        r"\bproduction\b", r"\bprod\b",      r"\bpipeline\b",
        r"\bcontainer\b",  r"\bdocker\b",    r"\bservice\b",
        r"\bdatabase\b",   r"\bmigration\b", r"\binfrastructure\b",
        r"\bserver\b",     r"\bcluster\b",   r"\bconfig\b",
        r"\bconfiguration\b",
    ]),
    (_Target.SHARED, [
        r"\bgithub\b",  r"\bgit\b",          r"\brepository\b",
        r"\brepo\b",    r"\bpackage\b",       r"\bdependency\b",
        r"\bmodule\b",  r"\bpull.request\b",  r"\bpr\b",
    ]),
    # LOCAL: implicit fallback, always matches.
]

# Pre-compile all patterns once at import time.
_COMPILED_ACTION: list[tuple[str, list[re.Pattern]]] = [
    (action, [re.compile(p) for p in pats])
    for action, pats in _ACTION_PATTERNS
]
_COMPILED_TARGET: list[tuple[str, list[re.Pattern]]] = [
    (target, [re.compile(p) for p in pats])
    for target, pats in _TARGET_PATTERNS
]


def _extract_action(text: str) -> str:
    """Return the highest-priority action class found in text, defaulting to READ."""
    for action, patterns in _COMPILED_ACTION:
        if any(p.search(text) for p in patterns):
            return action
    return _Action.READ


def _extract_target(text: str) -> str:
    """Return the highest-priority target domain found in text, defaulting to LOCAL."""
    for target, patterns in _COMPILED_TARGET:
        if any(p.search(text) for p in patterns):
            return target
    return _Target.LOCAL


class RiskClassifier:
    """
    Analyzes prompt + scope to assign risk tier (1–4).

    Uses an intent matrix (action × target) rather than keyword counting.
    See module docstring for the full decision table and rationale.
    """

    def classify(
        self,
        prompt: str,
        system_context: str = "",
        scope: str = "local",
    ) -> Tier:
        """Classify request into risk tier."""
        combined    = (prompt + " " + system_context).lower()
        scope_lower = scope.lower()

        action = _extract_action(combined)
        target = _extract_target(combined)
        tier   = _TIER_MATRIX[(action, target)]

        logger.info(
            f"[classify] action={action} target={target} "
            f"scope={scope} → Tier {int(tier)}"
        )

        # Scope escalation
        if scope_lower in ("system", "global"):
            tier = max(tier, Tier.TIER_3_MAJOR)
        elif scope_lower == "sandbox":
            tier = max(tier, Tier.TIER_2_MINOR)

        return tier

    def requires_mfa(self, tier: Tier) -> bool:
        """Check if tier requires MFA"""
        return tier in [Tier.TIER_3_MAJOR, Tier.TIER_4_CRITICAL]
    
    def requires_approval(self, tier: Tier) -> bool:
        """Check if tier requires approval"""
        return tier in [Tier.TIER_2_MINOR, Tier.TIER_3_MAJOR, Tier.TIER_4_CRITICAL]
    
    def requires_shadow_baseline(self, tier: Tier) -> bool:
        """Check if tier requires Shadow baseline before canary"""
        return tier in [Tier.TIER_3_MAJOR, Tier.TIER_4_CRITICAL]
    
    def get_shadow_min_hours(self, tier: Tier) -> int:
        """Get minimum Shadow baseline hours for tier"""
        if tier == Tier.TIER_4_CRITICAL:
            return 48
        elif tier == Tier.TIER_3_MAJOR:
            return 24
        return 0
    
    def get_similarity_threshold(self, tier: Tier) -> float:
        """Get semantic similarity threshold for tier"""
        if tier == Tier.TIER_4_CRITICAL:
            return 0.90
        elif tier == Tier.TIER_3_MAJOR:
            return 0.85
        return 0.70
    
    def get_rate_limit(self, tier: Tier) -> str:
        """Get rate limit for tier (requests/hour)"""
        limits = {
            Tier.TIER_1_SAFE: "1000/hour",
            Tier.TIER_2_MINOR: "100/hour",
            Tier.TIER_3_MAJOR: "10/hour",
            Tier.TIER_4_CRITICAL: "1/hour",
        }
        return limits.get(tier, "10/hour")
    
    def get_timeout_seconds(self, tier: Tier) -> int:
        """Get execution timeout for tier"""
        timeouts = {
            Tier.TIER_1_SAFE: 30,
            Tier.TIER_2_MINOR: 60,
            Tier.TIER_3_MAJOR: 300,
            Tier.TIER_4_CRITICAL: 600,
        }
        return timeouts.get(tier, 60)
    
    def is_read_only(self, tier: Tier) -> bool:
        """Check if tier is read-only"""
        return tier == Tier.TIER_1_SAFE
