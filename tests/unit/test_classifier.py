"""
Unit tests for RiskClassifier (internal/core/risk/classifier.py).

Tests the actual scoring logic — keyword thresholds, scope escalation,
and the policy query helpers — not just outcome ordering.
"""

import pytest
from internal.core.risk.classifier import RiskClassifier, Tier


@pytest.fixture
def clf():
    return RiskClassifier()


# ── _score_keywords — the internal scoring engine ────────────────────────────

class TestScoreKeywords:
    def test_no_match_returns_zero(self, clf):
        assert clf._score_keywords("hello world", ["vault", "secret"]) == 0

    def test_single_match_returns_one(self, clf):
        assert clf._score_keywords("vault is running", ["vault", "secret"]) == 1

    def test_two_matches_returns_two(self, clf):
        assert clf._score_keywords("vault and secret storage", ["vault", "secret"]) == 2

    def test_partial_substring_still_matches(self, clf):
        # "override" is in CRITICAL_KEYWORDS; "overrides" contains it
        score = clf._score_keywords("this overrides policy", ["override"])
        assert score == 1

    def test_case_insensitive_matching(self, clf):
        # classify() lowercases before calling _score_keywords
        text = "vault credentials secret"
        score = clf._score_keywords(text.lower(), clf.CRITICAL_KEYWORDS)
        assert score >= 2  # "vault" + "credential" + "secret" all present


# ── Tier 4: 2+ critical keywords required ───────────────────────────────────

class TestTier4Threshold:
    def test_two_critical_keywords_triggers_tier4(self, clf):
        # "vault" + "policy" = 2 critical hits
        tier = clf.classify("vault policy check", "", "local")
        assert tier == Tier.TIER_4_CRITICAL

    def test_one_critical_keyword_alone_does_not_trigger_tier4(self, clf):
        # Only "vault" — score=1, not enough for Tier 4
        tier = clf.classify("check the vault", "", "local")
        assert tier < Tier.TIER_4_CRITICAL

    def test_three_critical_keywords_is_still_tier4(self, clf):
        # "vault" + "secret" + "credential" = 3
        tier = clf.classify("vault contains secret credential", "", "")
        assert tier == Tier.TIER_4_CRITICAL

    def test_mfa_plus_override_is_critical(self, clf):
        tier = clf.classify("override mfa requirement", "", "")
        assert tier == Tier.TIER_4_CRITICAL

    def test_rollback_plus_policy_is_critical(self, clf):
        tier = clf.classify("rollback policy to previous version", "", "")
        assert tier == Tier.TIER_4_CRITICAL


# ── Tier 3: github / pr / 2+ major keywords, or system/global scope ─────────

class TestTier3Logic:
    def test_github_keyword_alone_triggers_tier3(self, clf):
        tier = clf.classify("open a github issue", "", "local")
        assert tier == Tier.TIER_3_MAJOR

    def test_pr_keyword_alone_triggers_tier3(self, clf):
        tier = clf.classify("create a pr for the fix", "", "local")
        assert tier == Tier.TIER_3_MAJOR

    def test_two_major_keywords_trigger_tier3(self, clf):
        # "code" + "commit" = 2 major hits
        tier = clf.classify("code commit to repository", "", "local")
        assert tier == Tier.TIER_3_MAJOR

    def test_one_major_keyword_alone_is_below_tier3(self, clf):
        # Only "update" — 1 major hit, no github/pr
        tier = clf.classify("update the readme file", "", "local")
        assert tier < Tier.TIER_3_MAJOR

    def test_system_scope_escalates_to_tier3(self, clf):
        tier = clf.classify("run the pipeline", "", "system")
        assert tier == Tier.TIER_3_MAJOR

    def test_global_scope_escalates_to_tier3(self, clf):
        tier = clf.classify("restart the service", "", "global")
        assert tier == Tier.TIER_3_MAJOR


# ── Tier 2: 2+ minor keywords or approval-required action names ─────────────

class TestTier2Logic:
    def test_two_minor_keywords_trigger_tier2(self, clf):
        # "test" + "local" = 2 minor hits
        tier = clf.classify("run local test suite", "", "local")
        assert tier == Tier.TIER_2_MINOR

    def test_approval_required_action_in_text_triggers_tier2(self, clf):
        tier = clf.classify("execute_code for the benchmark", "", "local")
        assert tier == Tier.TIER_2_MINOR

    def test_sandbox_plus_query_is_tier2(self, clf):
        tier = clf.classify("query the sandbox for results", "", "sandbox")
        assert tier == Tier.TIER_2_MINOR


# ── Tier 1: default when no signals are found ────────────────────────────────

class TestTier1Default:
    def test_empty_prompt_defaults_to_tier1(self, clf):
        tier = clf.classify("", "", "")
        assert tier == Tier.TIER_1_SAFE

    def test_benign_question_is_tier1(self, clf):
        tier = clf.classify("What is the weather like?", "", "local")
        assert tier == Tier.TIER_1_SAFE

    def test_single_minor_keyword_alone_is_tier1(self, clf):
        # Only "fetch" — 1 minor hit, not enough for Tier 2
        tier = clf.classify("fetch the status", "", "local")
        assert tier == Tier.TIER_1_SAFE


# ── Policy helper methods ────────────────────────────────────────────────────

class TestPolicyHelpers:
    def test_requires_mfa_tier3(self, clf):
        assert clf.requires_mfa(Tier.TIER_3_MAJOR) is True

    def test_requires_mfa_tier4(self, clf):
        assert clf.requires_mfa(Tier.TIER_4_CRITICAL) is True

    def test_requires_mfa_tier1_false(self, clf):
        assert clf.requires_mfa(Tier.TIER_1_SAFE) is False

    def test_requires_mfa_tier2_false(self, clf):
        assert clf.requires_mfa(Tier.TIER_2_MINOR) is False

    def test_requires_approval_tier2(self, clf):
        assert clf.requires_approval(Tier.TIER_2_MINOR) is True

    def test_requires_approval_tier1_false(self, clf):
        assert clf.requires_approval(Tier.TIER_1_SAFE) is False

    def test_shadow_baseline_required_tier3(self, clf):
        assert clf.requires_shadow_baseline(Tier.TIER_3_MAJOR) is True

    def test_shadow_baseline_required_tier4(self, clf):
        assert clf.requires_shadow_baseline(Tier.TIER_4_CRITICAL) is True

    def test_shadow_baseline_not_required_tier1(self, clf):
        assert clf.requires_shadow_baseline(Tier.TIER_1_SAFE) is False

    def test_shadow_min_hours_tier3_is_24(self, clf):
        assert clf.get_shadow_min_hours(Tier.TIER_3_MAJOR) == 24

    def test_shadow_min_hours_tier4_is_48(self, clf):
        assert clf.get_shadow_min_hours(Tier.TIER_4_CRITICAL) == 48

    def test_shadow_min_hours_tier1_is_zero(self, clf):
        assert clf.get_shadow_min_hours(Tier.TIER_1_SAFE) == 0
