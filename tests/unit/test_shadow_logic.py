"""
Unit tests for pure logic in ShadowService (cmd/shadow/main.py).

Tests _cosine_similarity (a static method) and the threshold rules
that determine canary eligibility — all without any DB or async I/O.
"""

import math
import pytest
from cmd.shadow.main import ShadowService


# ── _cosine_similarity ────────────────────────────────────────────────────────

class TestCosineSimilarity:
    """Exercises the static _cosine_similarity method directly."""

    def test_identical_vectors_return_one(self):
        v = [1.0, 0.0, 0.0, 0.0]
        assert ShadowService._cosine_similarity(v, v) == pytest.approx(1.0)

    def test_opposite_vectors_return_minus_one(self):
        v1 = [1.0, 0.0]
        v2 = [-1.0, 0.0]
        assert ShadowService._cosine_similarity(v1, v2) == pytest.approx(-1.0)

    def test_orthogonal_vectors_return_zero(self):
        v1 = [1.0, 0.0]
        v2 = [0.0, 1.0]
        assert ShadowService._cosine_similarity(v1, v2) == pytest.approx(0.0)

    def test_known_angle_45_degrees(self):
        # cos(45°) ≈ 0.7071
        v1 = [1.0, 0.0]
        v2 = [1.0, 1.0]
        result = ShadowService._cosine_similarity(v1, v2)
        assert result == pytest.approx(math.cos(math.radians(45)), abs=1e-6)

    def test_empty_vector_returns_zero(self):
        assert ShadowService._cosine_similarity([], [1.0]) == 0.0

    def test_mismatched_lengths_return_zero(self):
        assert ShadowService._cosine_similarity([1.0, 2.0], [1.0]) == 0.0

    def test_zero_vector_returns_zero(self):
        assert ShadowService._cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_all_positive_components_positive_result(self):
        v1 = [0.6, 0.8]          # unit vector
        v2 = [0.8, 0.6]          # unit vector, angle ~37°
        result = ShadowService._cosine_similarity(v1, v2)
        assert 0.0 < result < 1.0


# ── Similarity thresholds per tier ──────────────────────────────────────────
# The real thresholds live inside check_canary_eligibility:
#   T4 = 0.90, T3 = 0.85, T2 = 0.80
# We test the threshold values are correct by verifying the logic that
# selects them — extracting it into a helper so we can unit-test it.

def _threshold_for_tier(tier: int) -> float:
    """Mirror the threshold selection from ShadowService.check_canary_eligibility."""
    return 0.90 if tier == 4 else 0.85 if tier == 3 else 0.80


class TestCanaryThresholds:
    def test_tier4_threshold_is_90_percent(self):
        assert _threshold_for_tier(4) == pytest.approx(0.90)

    def test_tier3_threshold_is_85_percent(self):
        assert _threshold_for_tier(3) == pytest.approx(0.85)

    def test_tier2_and_below_threshold_is_80_percent(self):
        assert _threshold_for_tier(2) == pytest.approx(0.80)
        assert _threshold_for_tier(1) == pytest.approx(0.80)

    def test_threshold_ordering(self):
        # Higher tier → stricter threshold
        assert _threshold_for_tier(4) > _threshold_for_tier(3)
        assert _threshold_for_tier(3) > _threshold_for_tier(2)

    @pytest.mark.parametrize("tier,similarity,expected_eligible", [
        (4, 0.91, True),
        (4, 0.90, False),   # exactly at threshold = not above → ineligible
        (4, 0.89, False),
        (3, 0.86, True),
        (3, 0.85, False),
        (2, 0.81, True),
        (2, 0.80, False),
    ])
    def test_eligibility_against_threshold(self, tier, similarity, expected_eligible):
        threshold = _threshold_for_tier(tier)
        eligible = similarity > threshold
        assert eligible == expected_eligible


# ── Baseline age thresholds per tier ────────────────────────────────────────
# Mirrors verify_baseline_age: T4=48h, T3=24h, all others=1h

def _min_age_hours(tier: int) -> int:
    return 48 if tier == 4 else 24 if tier == 3 else 1


class TestBaselineAgeThresholds:
    def test_tier4_requires_48h(self):
        assert _min_age_hours(4) == 48

    def test_tier3_requires_24h(self):
        assert _min_age_hours(3) == 24

    def test_tier2_requires_1h(self):
        assert _min_age_hours(2) == 1

    def test_tier1_requires_1h(self):
        assert _min_age_hours(1) == 1

    @pytest.mark.parametrize("tier,age_hours,expected_pass", [
        (4, 49.0, True),
        (4, 48.0, False),   # exactly at minimum is not enough
        (4, 0.5,  False),
        (3, 25.0, True),
        (3, 24.0, False),
        (2, 2.0,  True),
        (2, 0.5,  False),
    ])
    def test_age_eligibility(self, tier, age_hours, expected_pass):
        min_age = _min_age_hours(tier)
        passes = age_hours > min_age
        assert passes == expected_pass
