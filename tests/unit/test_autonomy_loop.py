"""
Unit tests for the AutonomyLoop task queue and affect-driven sleep/wake.

Tests cover:
  - TaskKind enum and AutonomyTask dataclass ordering
  - enqueue() sets the wake event, adds task to heap
  - _reseed_queue() always adds both POLL_OUTCOMES and CONTRIBUTE tasks
  - _compute_sleep_duration() falls back when affect store is absent
  - _compute_sleep_duration() reads state and calls affect_engine
  - _sleep_or_wake() completes after timeout with no external stimulus
  - _sleep_or_wake() returns early when wake event is set

No DB, no GitHub, no real async tasks are started.  All heavy dependencies
are replaced with lightweight stubs or AsyncMock.
"""
import asyncio
import heapq
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cmd.vault.autonomy_loop import (
    AutonomyLoop,
    AutonomyTask,
    TaskKind,
    _FALLBACK_SLEEP_SECONDS,
)
from internal.affect.store import AffectState
from datetime import datetime


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_loop(affect_store=None) -> AutonomyLoop:
    """Build an AutonomyLoop with all heavy deps stubbed out."""
    loop = AutonomyLoop(
        graph_client=MagicMock(),
        github_client=MagicMock(),
        identity=MagicMock(),
        vault_service=MagicMock(),
        affect_store=affect_store,
    )
    # Pre-create the wake event (normally done inside start())
    loop._wake_event = asyncio.Event()
    loop._running = True
    return loop


def _fake_state(**kwargs) -> AffectState:
    defaults = dict(curiosity=0.5, boredom=0.3, fulfillment=0.5, caution=0.0,
                    version=1, updated_at=datetime.utcnow())
    defaults.update(kwargs)
    return AffectState(**defaults)


# ── TaskKind + AutonomyTask ────────────────────────────────────────────────────

class TestTaskKind:
    def test_kind_values(self):
        assert TaskKind.POLL_OUTCOMES == "poll_outcomes"
        assert TaskKind.CONTRIBUTE    == "contribute"
        assert TaskKind.EXTERNAL      == "external"


class TestAutonomyTask:
    def test_lower_priority_wins_in_heap(self):
        urgent  = AutonomyTask(priority=0, kind=TaskKind.EXTERNAL)
        normal  = AutonomyTask(priority=5, kind=TaskKind.CONTRIBUTE)
        q: list[AutonomyTask] = []
        heapq.heappush(q, normal)
        heapq.heappush(q, urgent)
        assert heapq.heappop(q).priority == 0

    def test_equal_priority_both_poppable(self):
        a = AutonomyTask(priority=5, kind=TaskKind.POLL_OUTCOMES)
        b = AutonomyTask(priority=5, kind=TaskKind.CONTRIBUTE)
        q: list[AutonomyTask] = []
        heapq.heappush(q, a)
        heapq.heappush(q, b)
        assert len(q) == 2


# ── enqueue() ─────────────────────────────────────────────────────────────────

class TestEnqueue:
    def test_enqueue_adds_task_to_heap(self):
        loop = _make_loop()
        loop.enqueue(kind=TaskKind.EXTERNAL, payload={"prompt": "fix bug"})
        assert len(loop._queue) == 1
        assert loop._queue[0].kind == TaskKind.EXTERNAL

    def test_enqueue_sets_wake_event(self):
        loop = _make_loop()
        assert not loop._wake_event.is_set()
        loop.enqueue()
        assert loop._wake_event.is_set()

    def test_enqueue_respects_priority(self):
        loop = _make_loop()
        loop.enqueue(kind=TaskKind.CONTRIBUTE, priority=10)
        loop.enqueue(kind=TaskKind.EXTERNAL,   priority=0)
        first = heapq.heappop(loop._queue)
        assert first.priority == 0


# ── _reseed_queue() ────────────────────────────────────────────────────────────

class TestReseedQueue:
    def test_reseed_adds_poll_and_contribute(self):
        loop = _make_loop()
        assert len(loop._queue) == 0
        loop._reseed_queue()
        kinds = {t.kind for t in loop._queue}
        assert TaskKind.POLL_OUTCOMES in kinds
        assert TaskKind.CONTRIBUTE    in kinds

    def test_reseed_adds_exactly_two_tasks(self):
        loop = _make_loop()
        loop._reseed_queue()
        assert len(loop._queue) == 2

    def test_reseed_can_be_called_multiple_times(self):
        loop = _make_loop()
        loop._reseed_queue()
        loop._reseed_queue()
        assert len(loop._queue) == 4


# ── _compute_sleep_duration() ─────────────────────────────────────────────────

class TestComputeSleepDuration:
    async def test_returns_fallback_when_no_affect_store(self):
        loop = _make_loop(affect_store=None)
        result = await loop._compute_sleep_duration()
        assert result == float(_FALLBACK_SLEEP_SECONDS)

    async def test_returns_fallback_when_read_state_returns_none(self):
        store = MagicMock()
        store.read_state = AsyncMock(return_value=None)
        loop = _make_loop(affect_store=store)
        result = await loop._compute_sleep_duration()
        assert result == float(_FALLBACK_SLEEP_SECONDS)

    async def test_calls_affect_engine_with_real_state(self):
        state = _fake_state(curiosity=1.0, boredom=1.0, fulfillment=0.0)
        store = MagicMock()
        store.read_state = AsyncMock(return_value=state)
        loop = _make_loop(affect_store=store)

        from internal.affect import engine as affect_engine
        result = await loop._compute_sleep_duration()
        expected = affect_engine.sleep_duration(state)
        assert result == expected

    async def test_high_wake_pressure_returns_short_sleep(self):
        """High boredom+curiosity state should produce a sleep < 1 hour."""
        state = _fake_state(curiosity=1.0, boredom=1.0, fulfillment=0.0)
        store = MagicMock()
        store.read_state = AsyncMock(return_value=state)
        loop = _make_loop(affect_store=store)
        result = await loop._compute_sleep_duration()
        assert result < 60 * 60


# ── _sleep_or_wake() ──────────────────────────────────────────────────────────

class TestSleepOrWake:
    async def test_completes_after_timeout(self):
        """With no external stimulus the sleep runs to the timeout."""
        loop = _make_loop()
        # Use a very short timeout so the test is fast
        await loop._sleep_or_wake(0.05)
        # No assertion needed — the test would hang if timeout never fired

    async def test_returns_early_when_event_set_externally(self):
        """enqueue() sets the wake event and the sleep should return immediately."""
        loop = _make_loop()

        async def _set_event_soon():
            await asyncio.sleep(0.01)
            loop._wake_event.set()

        asyncio.create_task(_set_event_soon())
        # Sleep for up to 5 seconds — should return well under 1 second
        await asyncio.wait_for(loop._sleep_or_wake(5.0), timeout=2.0)

    async def test_works_without_wake_event(self):
        """Falls back to plain asyncio.sleep when _wake_event is None."""
        loop = _make_loop()
        loop._wake_event = None
        await loop._sleep_or_wake(0.02)   # just verify no crash
