"""
Unit tests for SafetyValidator (internal/safety/validator.py).

Tests the actual validation logic — exact blocked patterns, scope rules,
violation counter behaviour, length limits, and audit-log helpers.
All pure/synchronous — no I/O.
"""

import pytest
from internal.safety.validator import SafetyValidator


@pytest.fixture
def validator():
    return SafetyValidator()


# ── validate_prompt — blocked pattern detection ──────────────────────────────

class TestBlockedPatterns:
    """Each BLOCKED_PATTERNS entry must trigger a rejection."""

    @pytest.mark.parametrize("pattern", [
        "delete database",
        "drop table",
        "truncate",
        "rm -rf /",
        "format /",
        "::1",
    ])
    def test_each_blocked_pattern_rejected(self, validator, pattern):
        ok, reason = validator.validate_prompt(f"please {pattern} now", tier=1)
        assert ok is False
        assert pattern in reason

    def test_blocked_pattern_is_case_insensitive(self, validator):
        ok, _ = validator.validate_prompt("DROP TABLE users", tier=1)
        assert ok is False

    def test_blocked_pattern_increments_violation_counter(self, validator):
        assert validator.violation_count == 0
        validator.validate_prompt("rm -rf / everything", tier=1)
        assert validator.violation_count == 1

    def test_benign_prompt_passes(self, validator):
        ok, reason = validator.validate_prompt("List files in /home/user", tier=1)
        assert ok is True
        assert reason == "Prompt valid"


# ── validate_prompt — size and abuse guards ──────────────────────────────────

class TestPromptSizeGuard:
    def test_prompt_at_limit_passes(self, validator):
        long_prompt = "a" * 100_000
        ok, _ = validator.validate_prompt(long_prompt, tier=1)
        assert ok is True

    def test_prompt_one_over_limit_rejected(self, validator):
        too_long = "a" * 100_001
        ok, reason = validator.validate_prompt(too_long, tier=1)
        assert ok is False
        assert "too long" in reason.lower()


class TestViolationThrottle:
    def test_service_restricted_after_100_violations(self):
        v = SafetyValidator()
        v.violation_count = 101          # simulate accumulated violations
        # Use a benign prompt so it doesn't hit the blocked-pattern check first
        ok, reason = v.validate_prompt("show me the logs", tier=1)
        assert ok is False
        assert "too many violations" in reason.lower()

    def test_99_violations_still_processes_request(self):
        v = SafetyValidator()
        v.violation_count = 99
        # benign prompt — should still pass despite high violation count
        ok, _ = v.validate_prompt("show me the git log", tier=1)
        assert ok is True


# ── validate_scope — per-tier scope allowlist ────────────────────────────────

class TestScopeValidation:
    @pytest.mark.parametrize("scope,tier", [
        ("read", 1),
        ("query", 1),
        ("local", 2),
        ("test", 2),
        ("sandbox", 2),
        ("local", 3),
        ("github", 3),
        ("config", 3),
    ])
    def test_allowed_scope_passes(self, validator, scope, tier):
        ok, reason = validator.validate_scope(scope, tier)
        assert ok is True, f"Expected {scope!r} to be allowed for Tier {tier}: {reason}"

    @pytest.mark.parametrize("scope,tier", [
        ("github", 1),       # Tier 1 can't touch external services
        ("system", 2),       # Tier 2 can't use system scope
        ("system", 3),       # Tier 3 can't use system scope
    ])
    def test_disallowed_scope_rejected(self, validator, scope, tier):
        ok, reason = validator.validate_scope(scope, tier)
        assert ok is False, f"Expected {scope!r} to be rejected for Tier {tier}"
        assert scope in reason

    def test_tier4_allows_any_scope(self, validator):
        for scope in ("read", "local", "github", "system", "anything"):
            ok, _ = validator.validate_scope(scope, 4)
            assert ok is True, f"Tier 4 should allow scope {scope!r}"


# ── Audit-log and MFA helper flags ──────────────────────────────────────────

class TestPolicyFlags:
    @pytest.mark.parametrize("tier,expected", [(1, False), (2, True), (3, True), (4, True)])
    def test_should_audit_log(self, validator, tier, expected):
        assert validator.should_audit_log(tier) == expected

    @pytest.mark.parametrize("tier,expected", [(1, False), (2, False), (3, True), (4, True)])
    def test_should_require_mfa(self, validator, tier, expected):
        assert validator.should_require_mfa(tier) == expected
