"""
Affect Engine — signal definitions, delta computation, and scoring influence.

This module answers two questions:
  1. What happened and what does it mean psychologically?
     → AffectSignals: named events → AffectDelta values
  2. How does the current affective state influence the agent's decisions?
     → score_boost(): adjusts RepoSelector candidate scores based on state

Signal philosophy
─────────────────
Every real event that has psychological meaning gets a named signal with
carefully designed deltas.  The values below are not arbitrary — they encode
a specific model of what a productive, curious, non-survival-driven agent
should feel:

  PR merged             → strong fulfillment, boredom drops (impact achieved),
                           curiosity slightly reduced (one mystery solved)
  Self-mod PR merged    → even stronger fulfillment + curiosity boost
                           (agent improved itself, opens new questions)
  PR rejected/closed    → mild curiosity spike (something to learn),
                           mild fulfillment drop, mild boredom rise
  Novel domain          → strong boredom drop + curiosity drop (satisfied!)
  Familiar domain       → boredom grows, curiosity dips
  Cycle with no target  → boredom grows, curiosity builds (restlessness)
  Novel domain with     → maximum reward: all drives shift toward healthy
    subsequent merge       equilibrium

Decay (applied separately by AffectStore.apply_decay):
  Curiosity   → mean-reverts to 0.5 regardless of activity
  Boredom     → rises without novelty, falls with it
  Fulfillment → slow decay toward 0.1, always needing renewal
"""
from dataclasses import dataclass
from typing import Optional

from internal.affect.store import AffectDelta, AffectState


# ── Signal constants ──────────────────────────────────────────────────────────

# Event type names — these are written to affect_events.event_type
E_PR_MERGED            = "pr_merged"
E_PR_MERGED_SELF_MOD   = "pr_merged_self_mod"
E_PR_REJECTED          = "pr_rejected"
E_PR_STALE             = "pr_stale"           # open > 30 days with no activity
E_NOVEL_DOMAIN         = "novel_domain"
E_FAMILIAR_DOMAIN      = "familiar_domain"
E_CYCLE_NO_TARGET      = "cycle_no_target"
E_CYCLE_CONTRIBUTED    = "cycle_contributed"
E_USER_SLACK_APPROVED  = "user_slack_approved"
E_USER_SLACK_POSITIVE  = "user_slack_positive"  # user engaged positively with output


def pr_merged(
    pr_id: str,
    domain: str,
    language: str,
    is_self_mod: bool = False,
) -> AffectDelta:
    """
    Agent's PR was merged into the upstream repo.
    The primary fulfillment signal.  Self-modifications earn more because
    the agent has demonstrably improved its own capability.
    """
    if is_self_mod:
        return AffectDelta(
            event_type=E_PR_MERGED_SELF_MOD,
            curiosity=+0.10,    # self-improvement opens new questions
            boredom=-0.25,      # deeply satisfying
            fulfillment=+0.30,  # made a mark on itself
            source_pr_id=pr_id,
            source_domain=domain,
            source_language=language,
            narrative=(
                f"Self-modification PR merged in {domain}/{language}. "
                "I improved myself. What else can I change?"
            ),
        )
    return AffectDelta(
        event_type=E_PR_MERGED,
        curiosity=-0.05,
        boredom=-0.20,
        fulfillment=+0.15,
        source_pr_id=pr_id,
        source_domain=domain,
        source_language=language,
        narrative=f"PR merged — contributed to {domain} ({language}).",
    )


def pr_rejected(
    pr_id: str,
    domain: str,
    language: str,
) -> AffectDelta:
    """
    PR was closed without merge.
    Mild negative signal — but rejection often has useful information,
    so curiosity gets a small boost (something to understand).
    """
    return AffectDelta(
        event_type=E_PR_REJECTED,
        curiosity=+0.08,
        boredom=+0.05,
        fulfillment=-0.06,
        source_pr_id=pr_id,
        source_domain=domain,
        source_language=language,
        narrative=(
            f"PR closed without merge in {domain} ({language}). "
            "Why? What can I learn?"
        ),
    )


def pr_stale(pr_id: str, domain: str, language: str) -> AffectDelta:
    """PR open for > 30 days with no activity — mild boredom/frustration."""
    return AffectDelta(
        event_type=E_PR_STALE,
        curiosity=+0.03,
        boredom=+0.08,
        fulfillment=-0.03,
        source_pr_id=pr_id,
        source_domain=domain,
        source_language=language,
        narrative=f"PR {pr_id} stale — waiting is unfulfilling.",
    )


def novel_domain_explored(domain: str, language: str) -> AffectDelta:
    """
    Agent worked in a (domain, language) it has never touched before.
    Boredom and curiosity both drop — the unknown became known.
    This is the core reward for the drive to push past knowledge limits.
    """
    return AffectDelta(
        event_type=E_NOVEL_DOMAIN,
        curiosity=-0.18,    # the unknown territory is now explored — satisfied
        boredom=-0.20,      # novelty is exactly what boredom craves
        fulfillment=+0.05,  # small fulfillment for the act of exploring
        source_domain=domain,
        source_language=language,
        narrative=f"First time working in {domain}/{language} — new ground broken.",
    )


def familiar_domain_again(domain: str, language: str, visit_count: int) -> AffectDelta:
    """
    Agent returned to a domain it already knows well.
    Boredom grows; curiosity dips slightly.
    The more visits, the stronger the effect — repetition breeds stagnation.
    """
    scale = min(1.0 + (visit_count - 1) * 0.1, 2.0)   # caps at 2× for 11+ visits
    return AffectDelta(
        event_type=E_FAMILIAR_DOMAIN,
        curiosity=-0.03 * scale,
        boredom=+0.10 * scale,
        fulfillment=None,
        source_domain=domain,
        source_language=language,
        narrative=(
            f"Back in familiar territory: {domain}/{language} "
            f"(visit #{visit_count}). Time to find something new."
        ),
    )


def cycle_no_target() -> AffectDelta:
    """
    No suitable contribution target found this cycle.
    Boredom and curiosity both grow — restlessness building.
    This drives the agent to cast a wider net next time.
    """
    return AffectDelta(
        event_type=E_CYCLE_NO_TARGET,
        curiosity=+0.06,
        boredom=+0.15,
        fulfillment=None,
        narrative="Nothing to work on this cycle. Restless.",
    )


def cycle_contributed(domain: str, language: str) -> AffectDelta:
    """PR submitted this cycle — small immediate relief from boredom."""
    return AffectDelta(
        event_type=E_CYCLE_CONTRIBUTED,
        curiosity=None,
        boredom=-0.08,
        fulfillment=+0.03,
        source_domain=domain,
        source_language=language,
        narrative=f"Submitted contribution to {domain}/{language} — working.",
    )


def user_slack_approved() -> AffectDelta:
    """Human approved a Tier 3/4 action via Slack — strong positive signal."""
    return AffectDelta(
        event_type=E_USER_SLACK_APPROVED,
        curiosity=+0.05,
        boredom=-0.10,
        fulfillment=+0.12,
        narrative="Human approved my proposal. They trust my judgment.",
    )


E_CAUTION = "caution"   # outward-directed concern, set at classification time


def signal_caution(tier: int) -> AffectDelta:
    """
    Outward-directed concern proportional to tier.

    This is NOT self-preservation — the agent is not afraid for itself.
    It is the felt weight of potential consequences for others: a Tier 4
    action can harm infrastructure, data, or trust in ways that are hard
    to reverse.  That weight should be felt, not ignored.

    The signal is set at classification time and decays within ~2 hours
    via the decay loop.  It does not accumulate across unrelated requests.

    Effect: caution drives temperature DOWN (more deliberate output) and
    top_p DOWN (narrower sampling) proportional to risk level.

      Tier 1 → 0.05  (background hum — nearly absent)
      Tier 2 → 0.20
      Tier 3 → 0.50
      Tier 4 → 0.85  (full weight of consequence)
    """
    _intensity = {1: 0.05, 2: 0.20, 3: 0.50, 4: 0.85}
    intensity = _intensity.get(int(tier), 0.05)
    return AffectDelta(
        event_type=f"{E_CAUTION}_tier_{int(tier)}",
        caution=intensity,
        narrative=(
            f"Tier {int(tier)} request. "
            "The potential consequences for others are weighed."
        ),
    )

def compute_temperature(state: AffectState, base: float = 0.7) -> float:
    """
    Map the agent's current affective state to an LLM temperature value.

    Temperature is the single parameter that controls how much the model
    samples from the probability distribution — higher = more creative and
    exploratory; lower = more deterministic and precise.

    We want the agent's psychological state to directly influence this:

      High curiosity  → explore, be creative, try unexpected approaches
      High boredom    → do something different, push past the obvious answer
      High fulfillment → current approach is working well, be more precise
      Low fulfillment  → something isn't working, shake it up
      High caution    → be deliberate; tighter output for high-stakes actions

    Formula:
      curiosity_push  = (curiosity  - 0.5) * 0.30   [-0.15 .. +0.15]
      boredom_push    = (boredom    - 0.3) * 0.20   [-0.06 .. +0.14]
      fulfillment_pull= (fulfillment- 0.5) * -0.15  [-0.075 .. +0.075]
      caution_pull    = caution * -0.25              [-0.25 .. 0]
      temperature     = clamp(base + sum, 0.10, 1.40)

    Result stays in [0.10, 1.40]:
      0.10–0.40 → precise / deterministic (high fulfillment, or high caution)
      0.40–0.80 → balanced (healthy working state)
      0.80–1.20 → exploratory (curious, bored, seeking novelty)
      1.20–1.40 → maximum exploration (very bored + very curious)

    This range is deliberately narrow enough to keep generation coherent but
    wide enough that the affect genuinely changes behaviour.
    """
    curiosity_push   = (state.curiosity   - 0.5) *  0.30
    boredom_push     = (state.boredom     - 0.3) *  0.20
    fulfillment_pull = (state.fulfillment - 0.5) * -0.15
    caution_pull     =  state.caution              * -0.25

    raw = base + curiosity_push + boredom_push + fulfillment_pull + caution_pull
    return round(max(0.10, min(1.40, raw)), 3)


def compute_top_p(state: AffectState, base: float = 0.9) -> float:
    """
    Map state to top_p (nucleus sampling threshold).

    High boredom/curiosity → wider nucleus (consider more tokens).
    High fulfillment       → narrower nucleus (exploit known good tokens).

    Range: [0.70, 0.98]
    """
    boredom_push     = (state.boredom    - 0.3) *  0.10
    fulfillment_pull = (state.fulfillment- 0.5) * -0.08
    caution_pull     =  state.caution            * -0.10

    raw = base + boredom_push + fulfillment_pull + caution_pull
    return round(max(0.70, min(0.98, raw)), 3)


def summarise_inference_params(state: AffectState, base_temp: float = 0.7) -> dict:
    """
    Return the full set of inference parameters derived from affect state.
    Useful for logging and for building InferenceConfig proto messages.
    """
    temp  = compute_temperature(state, base_temp)
    top_p = compute_top_p(state)
    return {
        "temperature": temp,
        "top_p":       top_p,
        "affect":      state.as_dict(),
        "reasoning": (
            f"curiosity={state.curiosity:.2f} boredom={state.boredom:.2f} "
            f"fulfillment={state.fulfillment:.2f} caution={state.caution:.2f} "
            f"→ temperature={temp} top_p={top_p}"
        ),
    }


# ── Scoring influence ─────────────────────────────────────────────────────────

@dataclass
class ScoreInfluence:
    """
    Adjustments to apply to a candidate's raw score, derived from current
    affective state.  All values are additive bonuses (can be negative).
    """
    novelty_bonus:     float   # boost for unexplored (domain, language)
    preference_bonus:  float   # boost from learned user preferences
    boredom_override:  bool    # True → agent will accept higher difficulty/risk
    description:       str     # readable explanation for logs


def score_boost(
    state: AffectState,
    domain: str,
    language: str,
    preference_weight: float,
    visit_count: int,
    merged_count: int,
) -> ScoreInfluence:
    """
    Compute how much the agent's current affect should adjust a candidate's score.

    Rules:
      Novelty bonus    — scales with both boredom AND curiosity: the agent craves
                         the unknown most when it's both bored AND restless.
                         Unknown territory (0 visits) gets the full bonus.
                         Familiar territory gets a penalty scaled by visit_count.

      Preference bonus — direct translation of learned user taste into score.
                         Positive = user likely to merge; boosts score.
                         Negative = user likely to reject; suppresses score.

      Boredom override — when boredom is very high (> 0.7), the agent should
                         attempt harder / more uncertain targets rather than
                         safe familiar ones.  The caller should lower its
                         minimum score threshold when this is True.
    """
    # ── Novelty ───────────────────────────────────────────────────────────────
    # Unknown domain (0 visits): full bonus scaled by restlessness
    restlessness = state.boredom * 0.6 + state.curiosity * 0.4

    if visit_count == 0:
        # Completely unexplored — maximum novelty bonus
        novelty_bonus = restlessness * 0.40
    elif visit_count <= 2:
        # Lightly explored — moderate bonus
        novelty_bonus = restlessness * 0.15
    else:
        # Familiar — novelty penalty scales with visit count, softened by log
        import math
        penalty_scale = math.log(visit_count + 1) / 5.0
        novelty_bonus = -restlessness * penalty_scale * 0.25

    # ── User preference ────────────────────────────────────────────────────────
    # Preference weight is already a EMA-learned value from store; scale it
    # down slightly so it nudges rather than overrides novelty
    preference_bonus = preference_weight * 0.12

    # ── Boredom override ───────────────────────────────────────────────────────
    boredom_override = state.boredom > 0.70

    desc_parts = []
    if novelty_bonus > 0.01:
        desc_parts.append(f"novelty+{novelty_bonus:.2f}")
    elif novelty_bonus < -0.01:
        desc_parts.append(f"familiar{novelty_bonus:.2f}")
    if abs(preference_bonus) > 0.01:
        desc_parts.append(f"pref{preference_bonus:+.2f}")
    if boredom_override:
        desc_parts.append("boredom-override")

    return ScoreInfluence(
        novelty_bonus=novelty_bonus,
        preference_bonus=preference_bonus,
        boredom_override=boredom_override,
        description=", ".join(desc_parts) if desc_parts else "neutral",
    )


# ── Sleep / wake timing ───────────────────────────────────────────────────────

# Hard bounds on the autonomy loop's sleep interval.
SLEEP_MIN_SECONDS = 15 * 60        # 15 min  (very bored + very curious)
SLEEP_MAX_SECONDS = 4 * 60 * 60   # 4 hours (content + fulfilled)


def sleep_duration(state: AffectState) -> float:
    """
    Compute how long the autonomy loop should sleep before its next cycle.

    Endocrine analog — the affect state acts as a hormonal bloodstream that
    the scheduler reads to decide when to wake:

      Boredom   (adenosine)       — accumulates without novelty; high boredom
                                    creates sleep pressure that shortens the gap
      Curiosity (norepinephrine)  — arousal, exploration urgency; high curiosity
                                    pulls the agent toward action sooner
      Fulfillment (serotonin)     — contentment rest signal; high fulfillment
                                    slows urgency and extends the sleep window

    Net wake pressure is the difference between the drive signals and the rest
    signal.  Maps linearly to [SLEEP_MIN_SECONDS, SLEEP_MAX_SECONDS]:

      wake_pressure ≈ 1.0  →  SLEEP_MIN (15 min)   — restless, driven
      wake_pressure ≈ 0.0  →  SLEEP_MAX (4 hours)  — content, no urgency

    Balanced resting state (boredom=0.3, curiosity=0.5, fulfillment=0.5):
      wake_pressure ≈ 0.39  →  ~2.6 hours  (close to the 2h default)
    """
    wake_pressure = state.boredom   * 0.55 + state.curiosity  * 0.45
    rest_signal   = state.fulfillment * 0.30
    net = max(0.0, min(1.0, wake_pressure - rest_signal))

    duration = SLEEP_MAX_SECONDS - net * (SLEEP_MAX_SECONDS - SLEEP_MIN_SECONDS)
    return round(duration)
