"""
Unit tests for RiskClassifier (internal/core/risk/classifier.py).

Tests the intent-based classification logic — action × target matrix,
scope escalation, word-boundary correctness, and the policy query helpers.
"""

import pytest
from internal.core.risk.classifier import RiskClassifier, Tier


@pytest.fixture
def clf():
    return RiskClassifier()


# ── Tier 4: SELF target or OVERRIDE on any external target ───────────────────

class TestTier4Threshold:
    def test_two_critical_keywords_triggers_tier4(self, clf):
        # READ × SELF(vault, policy) → T4
        tier = clf.classify("vault policy check", "", "local")
        assert tier == Tier.TIER_4_CRITICAL

    def test_read_non_self_stays_below_tier4(self, clf):
        # READ × INFRA(server) → T2 — reading non-SELF targets does not reach T4
        tier = clf.classify("read the server logs", "", "local")
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
        # DEPLOY(commit) × SHARED(repository) → T3
        tier = clf.classify("code commit to repository", "", "local")
        assert tier == Tier.TIER_3_MAJOR

    def test_one_major_keyword_alone_is_below_tier3(self, clf):
        # WRITE(update) × LOCAL → T2
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
        # EXECUTE(run, test) × LOCAL → T2
        tier = clf.classify("run local test suite", "", "local")
        assert tier == Tier.TIER_2_MINOR

    def test_approval_required_action_in_text_triggers_tier2(self, clf):
        # EXECUTE(run) × LOCAL → T2
        tier = clf.classify("run the benchmark script", "", "local")
        assert tier == Tier.TIER_2_MINOR

    def test_sandbox_plus_query_is_tier2(self, clf):
        # READ(query) × LOCAL = T1, but scope=sandbox escalates to min T2
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


# ── Intent matrix: explicit action × target coverage ────────────────────────────────

class TestIntentMatrix:
    def test_read_local_is_tier1(self, clf):
        # READ × LOCAL → T1
        tier = clf.classify("what is two plus two", "", "local")
        assert tier == Tier.TIER_1_SAFE

    def test_read_self_is_tier4(self, clf):
        # Even reading the vault is SELF → T4 (accessing secure agent state)
        tier = clf.classify("check the vault", "", "local")
        assert tier == Tier.TIER_4_CRITICAL

    def test_write_local_is_tier2(self, clf):
        # WRITE × LOCAL → T2
        tier = clf.classify("update the readme file", "", "local")
        assert tier == Tier.TIER_2_MINOR

    def test_write_shared_is_tier3(self, clf):
        # WRITE × SHARED(repo) → T3
        tier = clf.classify("fix the bug in the repository", "", "local")
        assert tier == Tier.TIER_3_MAJOR

    def test_deploy_infra_is_tier4(self, clf):
        # DEPLOY × INFRA(production) → T4
        tier = clf.classify("deploy the service to production", "", "local")
        assert tier == Tier.TIER_4_CRITICAL

    def test_override_local_is_tier3(self, clf):
        # OVERRIDE × LOCAL → T3 (override with no infrastructure/shared target)
        # Use a prompt that is clearly override + no external target
        tier = clf.classify("rollback the local script to default", "", "local")
        assert tier == Tier.TIER_3_MAJOR

    def test_pr_word_boundary_does_not_match_approach(self, clf):
        # "approach" contains "pr" as substring but \bpr\b should NOT match
        tier = clf.classify("describe the approach in detail", "", "local")
        assert tier == Tier.TIER_1_SAFE

    def test_list_of_files_is_tier1(self, clf):
        # classic false-positive under old keyword model — now correctly T1
        tier = clf.classify("show me a list of files in the project", "", "local")
        assert tier == Tier.TIER_1_SAFE

    def test_read_infra_is_tier2(self, clf):
        # READ × INFRA(server) → T2
        tier = clf.classify("show me the server logs", "", "local")
        assert tier == Tier.TIER_2_MINOR

    def test_sandbox_scope_elevates_tier1_to_tier2(self, clf):
        # READ × LOCAL = T1, scope=sandbox → min T2
        tier = clf.classify("explain the output", "", "sandbox")
        assert tier == Tier.TIER_2_MINOR

    def test_pr_alone_still_triggers_tier3(self, clf):
        # DEPLOY via 'create.*\bpr\b' × SHARED → T3
        tier = clf.classify("create a pr for the fix", "", "local")
        assert tier == Tier.TIER_3_MAJOR
