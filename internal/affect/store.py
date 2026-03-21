"""
Affect Store — production PostgreSQL persistence for the agent's psychological
state.

All writes use optimistic concurrency (version compare-and-swap) so concurrent
Vault threads cannot produce split-brain affect state.  All reads are non-
blocking and degrade gracefully when the database is unavailable — the agent
keeps working, just without affect influence.

Tables owned by this module (defined in db_schema.sql):
    agent_affect        — materialized singleton state
    affect_events       — immutable event log
    user_preferences    — learned user taste
    explored_domains    — novelty map
    pr_signals          — raw PR learning signals
    affect_decay_log    — periodic decay audit trail
"""
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import psycopg
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row
    _PSYCOPG_OK = True
except ImportError:
    _PSYCOPG_OK = False


# ── Value objects ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AffectState:
    """
    The agent's current psychological state.

    Curiosity   — drive to explore unknown territory.  Natural resting value ≈ 0.5.
    Boredom     — accumulates with repetition; the agent must seek novelty to relieve it.
    Fulfillment — earned by meaningful impact (merged PRs, learning confirmed preferences).
    Caution     — outward-directed concern for high-stakes actions.  Not self-preservation:
                  the agent is not afraid for itself, but it feels the weight of potential
                  harm to others.  Decays fast (~2 hours) via the decay loop.

    No `survival` field.  The agent does not value its own continuation above human
    control.  This is a hard moral constraint encoded into the data model itself.
    """
    curiosity:   float   # 0.0–1.0
    boredom:     float   # 0.0–1.0
    fulfillment: float   # 0.0–1.0
    caution:     float   # 0.0–1.0
    version:     int
    updated_at:  datetime

    def as_dict(self) -> Dict[str, float]:
        return {
            "curiosity":   round(self.curiosity,   3),
            "boredom":     round(self.boredom,     3),
            "fulfillment": round(self.fulfillment, 3),
            "caution":     round(self.caution,     3),
        }


@dataclass(frozen=True)
class AffectDelta:
    """
    A signed change to apply to AffectState.
    None means "leave this field unchanged" (not the same as 0.0).
    """
    event_type:     str
    curiosity:      Optional[float] = None
    boredom:        Optional[float] = None
    fulfillment:    Optional[float] = None
    caution:        Optional[float] = None
    source_pr_id:   Optional[str]   = None
    source_domain:  Optional[str]   = None
    source_language:Optional[str]   = None
    narrative:      str              = ""
    metadata:       Dict[str, Any]   = field(default_factory=dict)


@dataclass
class UserPreference:
    domain:         str
    language:       str
    weight:         float
    evidence_count: int
    last_signal_type: Optional[str]


@dataclass
class ExploredDomain:
    domain:       str
    language:     str
    visit_count:  int
    merged_count: int
    best_outcome: Optional[str]


# ── Store ─────────────────────────────────────────────────────────────────────

class AffectStore:
    """
    Full async PostgreSQL store for the agent's affect layer.

    Usage:
        store = AffectStore(db_host=..., ...)
        await store.connect()
        state = await store.read_state()
        await store.apply_delta(AffectDelta(...))
        await store.disconnect()
    """

    def __init__(
        self,
        db_host: str,
        db_port: int,
        db_name: str,
        db_user: str,
        db_password: str,
    ):
        self._dsn = (
            f"postgresql://{db_user}:{db_password}"
            f"@{db_host}:{db_port}/{db_name}"
        )
        self._conn: Optional[AsyncConnection] = None
        self._available = False

    async def connect(self):
        if not _PSYCOPG_OK:
            logger.warning("[affect] psycopg not installed — affect layer disabled")
            return
        try:
            self._conn = await psycopg.AsyncConnection.connect(
                self._dsn, row_factory=dict_row
            )
            self._available = True
            logger.info("[affect] Connected to PostgreSQL affect store")
        except Exception as exc:
            logger.warning(f"[affect] DB connect failed — affect layer disabled: {exc}")

    async def disconnect(self):
        if self._conn:
            await self._conn.close()
        self._available = False

    # ── State ────────────────────────────────────────────────────────────────

    async def read_state(self) -> Optional[AffectState]:
        """Return the agent's current affective state, or None if unavailable."""
        if not self._available:
            return None
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    "SELECT curiosity, boredom, fulfillment, "
                    "COALESCE(caution, 0.0) AS caution, version, updated_at "
                    "FROM agent_affect WHERE id = 1"
                )
                row = await cur.fetchone()
            if not row:
                return None
            return AffectState(
                curiosity=float(row["curiosity"]),
                boredom=float(row["boredom"]),
                fulfillment=float(row["fulfillment"]),
                caution=float(row["caution"]),
                version=row["version"],
                updated_at=row["updated_at"],
            )
        except Exception as exc:
            logger.warning(f"[affect] read_state failed: {exc}")
            return None

    async def apply_delta(self, delta: AffectDelta) -> bool:
        """
        Apply a signed delta to agent_affect using optimistic concurrency.

        Returns True on success.  If another writer updated the row between our
        read and write (version mismatch), we re-read and retry once — affects
        are commutative so the order rarely matters.
        """
        if not self._available:
            return False

        for attempt in range(2):
            state = await self.read_state()
            if not state:
                return False

            new_curiosity  = _clamp(state.curiosity   + (delta.curiosity   or 0.0))
            new_boredom    = _clamp(state.boredom     + (delta.boredom     or 0.0))
            new_fulfillment= _clamp(state.fulfillment + (delta.fulfillment or 0.0))
            new_caution    = _clamp(state.caution     + (delta.caution     or 0.0))

            try:
                async with self._conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE agent_affect
                           SET curiosity    = %s,
                               boredom      = %s,
                               fulfillment  = %s,
                               caution      = %s,
                               updated_at   = NOW(),
                               version      = version + 1
                         WHERE id = 1
                           AND version = %s
                        """,
                        (new_curiosity, new_boredom, new_fulfillment, new_caution, state.version),
                    )
                    if cur.rowcount == 0:
                        # Someone else wrote first — retry
                        continue

                # Log the event (non-fatal if this fails)
                await self._log_event(delta, new_curiosity, new_boredom, new_fulfillment, new_caution)
                return True

            except Exception as exc:
                logger.warning(f"[affect] apply_delta attempt {attempt}: {exc}")
                return False

        logger.warning("[affect] apply_delta: optimistic lock retry exhausted")
        return False

    async def apply_decay(
        self,
        elapsed_seconds: int,
        had_novel_activity: bool,
    ) -> bool:
        """
        Apply time-based psychological decay.

        Decay model:
          Curiosity   → mean-reverts toward 0.5 (the agent is naturally curious;
                        inactivity doesn't kill it, but it drifts back to baseline).
          Boredom     → if the agent worked on something novel: decays toward 0.0.
                        if idle or doing familiar things: grows toward 1.0.
          Fulfillment → slowly decays toward 0.1 (achievements fade; need new ones).
        """
        if not self._available:
            return False

        state = await self.read_state()
        if not state:
            return False

        hours = elapsed_seconds / 3600.0

        # Curiosity: pull toward 0.5 at rate 0.02/hr
        c_target = 0.5
        c_rate   = 0.02 * hours
        new_c    = state.curiosity + c_rate * (c_target - state.curiosity)

        # Boredom: novel activity suppresses it; inactivity grows it
        if had_novel_activity:
            new_b = state.boredom - 0.05 * hours
        else:
            new_b = state.boredom + 0.03 * hours
        new_b = _clamp(new_b)

        # Fulfillment: slow decay toward 0.1 at rate 0.015/hr
        f_target = 0.1
        f_rate   = 0.015 * hours
        new_f    = state.fulfillment + f_rate * (f_target - state.fulfillment)

        # Caution: fast linear decay toward 0.0 — situational concern should not
        # persist.  Clears a Tier-4-level signal (0.85) within ~2 hours.
        new_ca   = max(0.0, state.caution - 0.40 * hours)

        new_c  = _clamp(new_c)
        new_f  = _clamp(new_f)
        new_ca = _clamp(new_ca)

        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE agent_affect
                       SET curiosity   = %s, boredom = %s, fulfillment = %s,
                           caution     = %s,
                           updated_at  = NOW(), version = version + 1
                     WHERE id = 1 AND version = %s
                    """,
                    (new_c, new_b, new_f, new_ca, state.version),
                )
                if cur.rowcount == 0:
                    return False  # concurrent write — decay will catch up next cycle

            # Log to decay audit table
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO affect_decay_log
                    (before_curiosity, before_boredom, before_fulfillment, before_caution,
                     after_curiosity,  after_boredom,  after_fulfillment,  after_caution,
                     elapsed_seconds)
                    VALUES (%s,%s,%s,%s, %s,%s,%s,%s, %s)
                    """,
                    (
                        state.curiosity, state.boredom, state.fulfillment, state.caution,
                        new_c,           new_b,         new_f,             new_ca,
                        elapsed_seconds,
                    ),
                )
            return True
        except Exception as exc:
            logger.warning(f"[affect] apply_decay failed: {exc}")
            return False

    # ── PR signals ───────────────────────────────────────────────────────────

    async def record_pr_signal(
        self,
        pr_id: str,
        event_type: str,        # 'submitted' | 'merged' | 'rejected' | 'commented'
        repo_full_name: str,
        domain: str = "",
        language: str = "",
        is_self_mod: bool = False,
        issue_title: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        """Append a raw PR learning signal.  Non-fatal if DB is unavailable."""
        if not self._available:
            return
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO pr_signals
                    (pr_id, event_type, repo_full_name, domain, language,
                     is_self_mod, issue_title, metadata)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (pr_id, event_type, repo_full_name, domain, language,
                     is_self_mod, issue_title, metadata or {}),
                )
        except Exception as exc:
            logger.warning(f"[affect] record_pr_signal failed: {exc}")

    # ── User preferences ─────────────────────────────────────────────────────

    async def update_preference(
        self,
        domain: str,
        language: str,
        signal_type: str,
        positive: bool,
    ):
        """
        Update the user preference weight for a (domain, language) pair using
        an exponential moving average so early evidence fades as more arrives.

        Positive signal (merged): weight += 1 / ln(evidence+2)
        Negative signal (closed): weight -= 0.5 / ln(evidence+2)
        """
        if not self._available:
            return

        # Read current row
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    "SELECT weight, evidence_count FROM user_preferences "
                    "WHERE domain=%s AND language=%s",
                    (domain, language),
                )
                row = await cur.fetchone()

            current_weight = float(row["weight"]) if row else 0.0
            evidence       = int(row["evidence_count"]) if row else 0

            step   = 1.0 / math.log(evidence + 2)
            delta  = step if positive else -step * 0.5
            new_w  = current_weight + delta

            async with self._conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO user_preferences
                        (domain, language, weight, evidence_count, last_signal_type)
                    VALUES (%s, %s, %s, 1, %s)
                    ON CONFLICT (domain, language) DO UPDATE
                       SET weight           = EXCLUDED.weight,
                           evidence_count   = user_preferences.evidence_count + 1,
                           last_signal_type = EXCLUDED.last_signal_type,
                           last_updated_at  = NOW()
                    """,
                    (domain, language, new_w, signal_type),
                )
        except Exception as exc:
            logger.warning(f"[affect] update_preference failed: {exc}")

    async def get_preference_weight(
        self, domain: str, language: str
    ) -> float:
        """Return the learned preference weight for a (domain, language) pair."""
        if not self._available:
            return 0.0
        try:
            async with self._conn.cursor() as cur:
                # Exact match first, then wildcard fallbacks
                await cur.execute(
                    """
                    SELECT COALESCE(
                        (SELECT weight FROM user_preferences
                         WHERE domain = %s AND language = %s LIMIT 1),
                        (SELECT weight FROM user_preferences
                         WHERE domain = %s AND language = ''  LIMIT 1),
                        (SELECT weight FROM user_preferences
                         WHERE domain = ''  AND language = %s LIMIT 1),
                        0.0
                    ) AS weight
                    """,
                    (domain, language, domain, language),
                )
                row = await cur.fetchone()
            return float(row["weight"]) if row else 0.0
        except Exception as exc:
            logger.warning(f"[affect] get_preference_weight failed: {exc}")
            return 0.0

    # ── Explored domains ─────────────────────────────────────────────────────

    async def mark_domain_visited(
        self,
        domain: str,
        language: str,
        outcome: Optional[str] = None,     # 'merged' | 'closed' | 'open'
    ):
        """Record (or increment) a visit to a (domain, language) territory."""
        if not self._available:
            return
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO explored_domains
                        (domain, language, best_outcome,
                         merged_count, visit_count)
                    VALUES (
                        %s, %s, %s,
                        CASE WHEN %s = 'merged' THEN 1 ELSE 0 END,
                        1
                    )
                    ON CONFLICT (domain, language) DO UPDATE
                       SET visit_count  = explored_domains.visit_count + 1,
                           last_seen_at = NOW(),
                           merged_count =
                               explored_domains.merged_count +
                               CASE WHEN %s = 'merged' THEN 1 ELSE 0 END,
                           best_outcome = CASE
                               WHEN %s = 'merged' THEN 'merged'
                               WHEN explored_domains.best_outcome = 'merged' THEN 'merged'
                               WHEN %s = 'closed' AND
                                    COALESCE(explored_domains.best_outcome,'') != 'merged'
                                    THEN 'closed'
                               ELSE COALESCE(explored_domains.best_outcome, %s)
                           END
                    """,
                    (domain, language, outcome,
                     outcome,           # initial merged_count
                     outcome,           # increment merged_count
                     outcome,           # CASE best_outcome merged
                     outcome,           # CASE best_outcome closed
                     outcome),          # COALESCE fallback
                )
        except Exception as exc:
            logger.warning(f"[affect] mark_domain_visited failed: {exc}")

    async def get_domain_familiarity(
        self, domain: str, language: str
    ) -> Tuple[int, int]:
        """
        Return (visit_count, merged_count) for a (domain, language) pair.
        Returns (0, 0) if completely unexplored.
        """
        if not self._available:
            return 0, 0
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT COALESCE(SUM(visit_count),  0) AS visits,
                           COALESCE(SUM(merged_count), 0) AS merges
                    FROM explored_domains
                    WHERE (domain = %s OR domain = '')
                      AND (language = %s OR language = '')
                    """,
                    (domain, language),
                )
                row = await cur.fetchone()
            if not row:
                return 0, 0
            return int(row["visits"]), int(row["merges"])
        except Exception as exc:
            logger.warning(f"[affect] get_domain_familiarity failed: {exc}")
            return 0, 0

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _log_event(
        self,
        delta: AffectDelta,
        after_c: float,
        after_b: float,
        after_f: float,
        after_ca: float,
    ):
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO affect_events
                    (event_type, delta_curiosity, delta_boredom, delta_fulfillment,
                     delta_caution,
                     source_pr_id, source_domain, source_language,
                     narrative, metadata)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        delta.event_type,
                        delta.curiosity, delta.boredom, delta.fulfillment,
                        delta.caution,
                        delta.source_pr_id, delta.source_domain, delta.source_language,
                        delta.narrative, delta.metadata,
                    ),
                )
        except Exception as exc:
            logger.warning(f"[affect] _log_event failed (non-fatal): {exc}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))
