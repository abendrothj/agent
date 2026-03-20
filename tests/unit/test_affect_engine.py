"""
Unit tests for the Affect Engine (internal/affect/engine.py).

Tests cover:
  - All named signal functions return correct AffectDelta field values
  - familiar_domain_again scale capping at 2× for 11+ visits
  - compute_temperature formula and boundary clamping
  - compute_top_p formula and boundary clamping
  - summarise_inference_params returns all expected keys

No DB or async I/O — everything is pure computation.
"""

import pytest

from internal.affect.store import AffectDelta, AffectState
from internal.affect import engine as affect_engine
from internal.affect.engine import (
    compute_temperature,
    compute_top_p,
    summarise_inference_params,
    score_boost,
    pr_merged,
    pr_rejected,
    pr_stale,
    novel_domain_explored,
    familiar_domain_again,
    cycle_no_target,
    cycle_contributed,
    user_slack_approved,
)
from datetime import datetime


# ── Helpers ───────────────────────────────────────────────────────────────────

def _state(curiosity=0.5, boredom=0.3, fulfillment=0.5) -> AffectState:
    return AffectState(
        curiosity=curiosity,
        boredom=boredom,
        fulfillment=fulfillment,
        version=1,
        updated_at=datetime.utcnow(),
    )


# ── Signal: pr_merged ─────────────────────────────────────────────────────────

class TestPrMergedSignal:
    def test_regular_merge_increases_fulfillment(self):
        delta = pr_merged("pr-1", "backend", "python")
        assert delta.fulfillment is not None
        assert delta.fulfillment > 0
        assert delta.boredom is not None
        assert delta.boredom < 0       # satisfaction reduces boredom

    def test_self_mod_merge_higher_fulfillment(self):
        regular = pr_merged("pr-1", "infra", "python", is_self_mod=False)
        self_mod = pr_merged("pr-2", "infra", "python", is_self_mod=True)
        assert regular.fulfillment is not None
        assert self_mod.fulfillment is not None
        assert self_mod.fulfillment > regular.fulfillment
        assert self_mod.curiosity is not None
        assert self_mod.curiosity > 0   # self-improvement opens new questions

    def test_merge_carries_pr_metadata(self):
        delta = pr_merged("pr-42", "web", "typescript")
        assert delta.source_pr_id == "pr-42"
        assert delta.source_domain == "web"
        assert delta.source_language == "typescript"

    def test_narrative_is_non_empty(self):
        delta = pr_merged("pr-1", "ml", "python")
        assert delta.narrative


# ── Signal: pr_rejected ───────────────────────────────────────────────────────

class TestPrRejectedSignal:
    def test_rejection_boosts_curiosity_slightly(self):
        """Rejection should trigger a 'what can I learn?' curiosity bump."""
        delta = pr_rejected("pr-3", "backend", "go")
        assert delta.curiosity is not None
        assert delta.curiosity > 0

    def test_rejection_reduces_fulfillment(self):
        delta = pr_rejected("pr-3", "backend", "go")
        assert delta.fulfillment is not None
        assert delta.fulfillment < 0


# ── Signal: pr_stale ─────────────────────────────────────────────────────────

class TestPrStaleSignal:
    def test_stale_increases_boredom(self):
        delta = pr_stale("pr-5", "devops", "yaml")
        assert delta.boredom is not None
        assert delta.boredom > 0

    def test_stale_has_low_magnitude(self):
        """Staleness is mild — not as strong as a rejection."""
        delta_stale  = pr_stale("pr-5", "devops", "yaml")
        delta_reject = pr_rejected("pr-5", "devops", "yaml")
        assert delta_stale.curiosity is not None
        assert delta_reject.curiosity is not None
        assert abs(delta_stale.curiosity) <= abs(delta_reject.curiosity)


# ── Signal: novel_domain_explored ────────────────────────────────────────────

class TestNovelDomainSignal:
    def test_novel_domain_drops_boredom(self):
        delta = novel_domain_explored("ml", "python")
        assert delta.boredom is not None
        assert delta.boredom < 0

    def test_novel_domain_drops_curiosity(self):
        """Unknown territory became known — curiosity satisfied, not raised."""
        delta = novel_domain_explored("ml", "python")
        assert delta.curiosity is not None
        assert delta.curiosity < 0

    def test_novel_domain_carries_metadata(self):
        delta = novel_domain_explored("gamedev", "rust")
        assert delta.source_domain == "gamedev"
        assert delta.source_language == "rust"


# ── Signal: familiar_domain_again ────────────────────────────────────────────

class TestFamiliarDomainSignal:
    def test_first_revisit_mild_boredom_increase(self):
        delta = familiar_domain_again("web", "js", visit_count=1)
        assert delta.boredom is not None
        assert delta.boredom > 0
        assert abs(delta.boredom) < 0.3    # mild on first revisit

    def test_many_revisits_stronger_boredom(self):
        delta_1  = familiar_domain_again("web", "js", visit_count=1)
        delta_11 = familiar_domain_again("web", "js", visit_count=11)
        assert delta_1.boredom is not None
        assert delta_11.boredom is not None
        assert delta_11.boredom > delta_1.boredom

    def test_scale_caps_at_2x_for_high_visit_counts(self):
        """After 11+ visits the scale is capped at 2.0 — no unbounded growth."""
        delta_11 = familiar_domain_again("web", "js", visit_count=11)
        delta_50 = familiar_domain_again("web", "js", visit_count=50)
        # Both should produce the same absolute deltas (cap applies)
        assert delta_11.boredom == pytest.approx(delta_50.boredom)

    def test_fulfillment_is_none_for_familiar(self):
        """Fulfillment is left unchanged for routine revisits."""
        delta = familiar_domain_again("web", "js", visit_count=1)
        assert delta.fulfillment is None


# ── Signal: cycle signals ─────────────────────────────────────────────────────

class TestCycleSignals:
    def test_no_target_increases_boredom_and_curiosity(self):
        delta = cycle_no_target()
        assert delta.boredom is not None
        assert delta.boredom > 0
        assert delta.curiosity is not None
        assert delta.curiosity > 0    # restlessness builds

    def test_contributed_reduces_boredom(self):
        delta = cycle_contributed("auth", "python")
        assert delta.boredom is not None
        assert delta.boredom < 0

    def test_slack_approved_boosts_fulfillment(self):
        delta = user_slack_approved()
        assert delta.fulfillment is not None
        assert delta.fulfillment > 0
        assert delta.boredom is not None
        assert delta.boredom < 0


# ── compute_temperature ───────────────────────────────────────────────────────

class TestComputeTemperature:
    def test_baseline_state_returns_base_temperature(self):
        """c=0.5, b=0.3, f=0.5 produce zero-sum deltas → temperature == base."""
        state = _state(curiosity=0.5, boredom=0.3, fulfillment=0.5)
        assert compute_temperature(state, base=0.7) == pytest.approx(0.7, abs=1e-3)

    def test_high_curiosity_raises_temperature(self):
        low  = compute_temperature(_state(curiosity=0.0))
        high = compute_temperature(_state(curiosity=1.0))
        assert high > low

    def test_high_boredom_raises_temperature(self):
        low  = compute_temperature(_state(boredom=0.0))
        high = compute_temperature(_state(boredom=1.0))
        assert high > low

    def test_high_fulfillment_lowers_temperature(self):
        low_f = compute_temperature(_state(fulfillment=0.0))
        high_f = compute_temperature(_state(fulfillment=1.0))
        assert high_f < low_f

    def test_temperature_never_below_minimum(self):
        """Extreme low state must stay above 0.10."""
        state = _state(curiosity=0.0, boredom=0.0, fulfillment=1.0)
        assert compute_temperature(state) >= 0.10

    def test_temperature_never_above_maximum(self):
        """Extreme high state must stay below 1.40."""
        state = _state(curiosity=1.0, boredom=1.0, fulfillment=0.0)
        assert compute_temperature(state) <= 1.40

    def test_result_rounded_to_3dp(self):
        state = _state(curiosity=0.123456, boredom=0.456789, fulfillment=0.789012)
        temp = compute_temperature(state)
        assert temp == round(temp, 3)


# ── compute_top_p ─────────────────────────────────────────────────────────────

class TestComputeTopP:
    def test_high_boredom_widens_nucleus(self):
        low  = compute_top_p(_state(boredom=0.0))
        high = compute_top_p(_state(boredom=1.0))
        assert high > low

    def test_high_fulfillment_narrows_nucleus(self):
        low_f  = compute_top_p(_state(fulfillment=0.0))
        high_f = compute_top_p(_state(fulfillment=1.0))
        assert high_f < low_f

    def test_top_p_lower_bound(self):
        state = _state(curiosity=0.0, boredom=0.0, fulfillment=1.0)
        assert compute_top_p(state) >= 0.70

    def test_top_p_upper_bound(self):
        state = _state(curiosity=1.0, boredom=1.0, fulfillment=0.0)
        assert compute_top_p(state) <= 0.98


# ── summarise_inference_params ────────────────────────────────────────────────

class TestSummariseInferenceParams:
    def test_returns_all_required_keys(self):
        params = summarise_inference_params(_state())
        assert "temperature" in params
        assert "top_p" in params
        assert "affect" in params
        assert "reasoning" in params

    def test_affect_dict_has_correct_keys(self):
        params = summarise_inference_params(_state())
        affect = params["affect"]
        assert set(affect.keys()) == {"curiosity", "boredom", "fulfillment"}

    def test_reasoning_mentions_state_values(self):
        state = _state(curiosity=0.75, boredom=0.40, fulfillment=0.20)
        params = summarise_inference_params(state)
        assert "curiosity" in params["reasoning"]
        assert "boredom" in params["reasoning"]


# ── score_boost ───────────────────────────────────────────────────────────────

class TestScoreBoost:
    def test_novel_domain_gets_positive_novelty_bonus(self):
        state = _state(curiosity=0.8, boredom=0.7)
        influence = score_boost(
            state=state,
            domain="gamedev",
            language="rust",
            preference_weight=0.5,
            visit_count=0,     # never visited
            merged_count=0,
        )
        assert influence.novelty_bonus > 0

    def test_familiar_domain_gets_lower_novelty_bonus(self):
        state = _state(curiosity=0.8, boredom=0.7)
        novel = score_boost(state, "gamedev", "rust", 0.5, 0, 0)
        familiar = score_boost(state, "gamedev", "rust", 0.5, 20, 10)
        assert novel.novelty_bonus >= familiar.novelty_bonus

    def test_boredom_override_true_when_very_bored(self):
        state = _state(boredom=0.85)
        influence = score_boost(state, "web", "js", 0.5, 5, 2)
        assert influence.boredom_override is True

    def test_boredom_override_false_when_engaged(self):
        state = _state(boredom=0.2)
        influence = score_boost(state, "web", "js", 0.5, 5, 2)
        assert influence.boredom_override is False
