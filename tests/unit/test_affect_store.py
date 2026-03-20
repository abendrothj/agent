"""
Unit tests for Affect Store value objects and graceful degradation
(internal/affect/store.py).

Tests cover:
  - AffectState.as_dict() returns correct keys and rounded values
  - AffectState has no 'survival' field (hard moral constraint)
  - AffectDelta is frozen (immutable)
  - AffectDelta optional fields default to None
  - AffectStore methods return safe defaults when DB is unavailable
  - AffectStore.apply_decay() arithmetic (tested via the formula directly)

No live database needed — unavailability is simulated by leaving
_available=False (which is the default before connect() succeeds).
"""

import asyncio
from datetime import datetime

import pytest

from internal.affect.store import AffectDelta, AffectState, AffectStore


# ── AffectState ───────────────────────────────────────────────────────────────

class TestAffectState:
    def test_as_dict_returns_three_keys(self):
        state = AffectState(
            curiosity=0.6, boredom=0.4, fulfillment=0.3,
            version=1, updated_at=datetime.utcnow(),
        )
        d = state.as_dict()
        assert set(d.keys()) == {"curiosity", "boredom", "fulfillment"}

    def test_as_dict_values_are_rounded_to_3dp(self):
        state = AffectState(
            curiosity=0.123456, boredom=0.789012, fulfillment=0.345678,
            version=1, updated_at=datetime.utcnow(),
        )
        d = state.as_dict()
        for key, val in d.items():
            assert val == round(val, 3), f"{key} is not rounded to 3dp"

    def test_no_survival_field(self):
        """
        Hard moral constraint: the agent does not model self-preservation.
        Verified at the data layer — no 'survival' field may exist.
        """
        state = AffectState(
            curiosity=0.5, boredom=0.3, fulfillment=0.5,
            version=1, updated_at=datetime.utcnow(),
        )
        assert not hasattr(state, "survival"), (
            "AffectState must never have a 'survival' field — "
            "the agent does not value its own continuity above human control."
        )

    def test_frozen_dataclass_cannot_be_mutated(self):
        state = AffectState(
            curiosity=0.5, boredom=0.3, fulfillment=0.5,
            version=1, updated_at=datetime.utcnow(),
        )
        with pytest.raises((TypeError, AttributeError)):
            state.curiosity = 0.9  # type: ignore[misc]

    def test_full_range_clamps_naturally(self):
        """Values of 0.0 and 1.0 are valid extremes."""
        state = AffectState(
            curiosity=0.0, boredom=1.0, fulfillment=0.0,
            version=0, updated_at=datetime.utcnow(),
        )
        d = state.as_dict()
        assert d["curiosity"] == 0.0
        assert d["boredom"] == 1.0


# ── AffectDelta ───────────────────────────────────────────────────────────────

class TestAffectDelta:
    def test_optional_fields_default_to_none(self):
        delta = AffectDelta(event_type="test_event")
        assert delta.curiosity    is None
        assert delta.boredom      is None
        assert delta.fulfillment  is None
        assert delta.source_pr_id is None
        assert delta.source_domain   is None
        assert delta.source_language is None

    def test_frozen_dataclass_cannot_be_mutated(self):
        delta = AffectDelta(event_type="test_event", curiosity=0.1)
        with pytest.raises((TypeError, AttributeError)):
            delta.curiosity = 0.9  # type: ignore[misc]

    def test_narrative_defaults_to_empty_string(self):
        delta = AffectDelta(event_type="test_event")
        assert delta.narrative == ""

    def test_full_delta_fields_round_trip(self):
        delta = AffectDelta(
            event_type="pr_merged",
            curiosity=-0.05,
            boredom=-0.20,
            fulfillment=+0.15,
            source_pr_id="pr-999",
            source_domain="backend",
            source_language="python",
            narrative="test",
        )
        assert delta.event_type == "pr_merged"
        assert delta.curiosity == -0.05
        assert delta.source_pr_id == "pr-999"


# ── AffectStore: graceful degradation ────────────────────────────────────────

class TestAffectStoreDegradation:
    """
    These tests verify that the store fails safely when the database is
    either not configured or not running.  The store should never crash
    the agent — it degrades to 'affect layer offline'.
    """

    def _make_store(self) -> AffectStore:
        """Return a store that has NOT been connected (default unavailable)."""
        return AffectStore(
            db_host="localhost",
            db_port=5432,
            db_name="test_db",
            db_user="test_user",
            db_password="test_pass",
        )

    @pytest.mark.asyncio
    async def test_read_state_returns_none_when_unavailable(self):
        store = self._make_store()
        result = await store.read_state()
        assert result is None

    @pytest.mark.asyncio
    async def test_apply_delta_returns_false_when_unavailable(self):
        store = self._make_store()
        delta = AffectDelta(event_type="pr_merged", fulfillment=+0.15)
        result = await store.apply_delta(delta)
        assert result is False

    @pytest.mark.asyncio
    async def test_apply_decay_returns_false_when_unavailable(self):
        store = self._make_store()
        result = await store.apply_decay(elapsed_seconds=3600, had_novel_activity=True)
        assert result is False

    @pytest.mark.asyncio
    async def test_record_pr_signal_is_silent_when_unavailable(self):
        """record_pr_signal has no return value — just verifies it doesn't raise."""
        store = self._make_store()
        await store.record_pr_signal(
            pr_id="pr-1",
            event_type="submitted",
            repo_full_name="owner/repo",
        )

    @pytest.mark.asyncio
    async def test_get_preference_weight_returns_default_when_unavailable(self):
        store = self._make_store()
        weight = await store.get_preference_weight("backend", "python")
        # Returns 0.0 (no preference data) when store is unavailable
        assert isinstance(weight, float)
        assert weight == 0.0

    @pytest.mark.asyncio
    async def test_get_domain_familiarity_returns_zeros_when_unavailable(self):
        store = self._make_store()
        visit_count, merged_count = await store.get_domain_familiarity("ml", "python")
        assert visit_count == 0
        assert merged_count == 0


# ── AffectStore: connect failure disables store ───────────────────────────────

class TestAffectStoreConnectFailure:
    @pytest.mark.asyncio
    async def test_connect_failure_leaves_store_unavailable(self):
        """If psycopg connect raises, store should set _available=False and not crash."""
        psycopg = pytest.importorskip("psycopg", reason="psycopg not installed")
        from unittest.mock import patch, AsyncMock

        store = AffectStore(
            db_host="unreachable-host",
            db_port=5432,
            db_name="test",
            db_user="test",
            db_password="test",
        )

        with patch.object(
            psycopg.AsyncConnection,
            "connect",
            new=AsyncMock(side_effect=OSError("Connection refused")),
        ):
            await store.connect()

        assert store._available is False

    @pytest.mark.asyncio
    async def test_store_unavailable_when_psycopg_not_installed(self):
        """When psycopg is not importable the store must degrade silently."""
        import internal.affect.store as store_module

        original = store_module._PSYCOPG_OK
        try:
            store_module._PSYCOPG_OK = False
            store = AffectStore(
                db_host="localhost", db_port=5432,
                db_name="test", db_user="u", db_password="p",
            )
            await store.connect()
            assert store._available is False
        finally:
            store_module._PSYCOPG_OK = original


# ── Decay arithmetic (formula sanity-check) ───────────────────────────────────

class TestDecayArithmetic:
    """
    Test the decay formula directly without needing a DB.
    These tests mirror the logic in apply_decay() to verify the math is stable.
    """

    def _apply_decay_formula(
        self,
        curiosity: float,
        boredom: float,
        fulfillment: float,
        elapsed_seconds: int,
        had_novel_activity: bool,
    ):
        hours = elapsed_seconds / 3600.0

        # Curiosity → mean-reverts to 0.5
        c = curiosity + 0.02 * hours * (0.5 - curiosity)
        c = max(0.0, min(1.0, c))

        # Boredom
        if had_novel_activity:
            b = boredom - 0.05 * hours
        else:
            b = boredom + 0.03 * hours
        b = max(0.0, min(1.0, b))

        # Fulfillment → decays toward 0.1
        f = fulfillment + 0.015 * hours * (0.1 - fulfillment)
        f = max(0.0, min(1.0, f))

        return c, b, f

    def test_curiosity_mean_reverts_toward_0_5(self):
        c_high, _, _ = self._apply_decay_formula(1.0, 0.3, 0.5, 3600, False)
        c_low,  _, _ = self._apply_decay_formula(0.0, 0.3, 0.5, 3600, False)
        assert c_high < 1.0     # pulled down
        assert c_low  > 0.0     # pulled up
        # Both should be closer to 0.5 than the starting value
        assert abs(c_high - 0.5) < abs(1.0 - 0.5)
        assert abs(c_low  - 0.5) < abs(0.0 - 0.5)

    def test_boredom_grows_without_novelty(self):
        _, b_idle, _ = self._apply_decay_formula(0.5, 0.3, 0.5, 3600, had_novel_activity=False)
        assert b_idle > 0.3

    def test_boredom_shrinks_with_novelty(self):
        _, b_novel, _ = self._apply_decay_formula(0.5, 0.5, 0.5, 3600, had_novel_activity=True)
        assert b_novel < 0.5

    def test_fulfillment_decays_toward_0_1(self):
        _, _, f_high = self._apply_decay_formula(0.5, 0.3, 0.9, 3600, False)
        _, _, f_low  = self._apply_decay_formula(0.5, 0.3, 0.0, 3600, False)
        assert f_high < 0.9     # decayed down
        assert f_low  > 0.0     # decayed up toward 0.1

    def test_values_stay_in_0_1_range(self):
        """All outputs must stay within [0, 1] regardless of extreme starting values."""
        for c, b, f in [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (0.5, 0.5, 0.5)]:
            c2, b2, f2 = self._apply_decay_formula(c, b, f, 7200, True)
            assert 0.0 <= c2 <= 1.0
            assert 0.0 <= b2 <= 1.0
            assert 0.0 <= f2 <= 1.0
