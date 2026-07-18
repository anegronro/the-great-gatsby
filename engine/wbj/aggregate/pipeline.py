"""Classification pipeline: the packet -> final profile orchestrator.

This is the one module that connects the 6 frozen Cerebro specialist
outputs to the main-agent aggregation (`apply_overrides` then
`apply_gates`), producing the single `ProfileResult` a report -- or the
paper forward-test (`wbj.papertest`) -- consumes. Nothing else in the
engine runs this end to end: `dashboard.py` builds a *health* view (per-area
HEALTHY/WATCH/WEAK), which is deliberately not the gate profile.

Flow (Cerebro `00_main_agent/SCORING_AND_GATES.md`):

    packet
      -> run the 6 specialists            (business..valuation, frozen)
      -> optional judgment overlay        (fills Business/Market/Risk coverage)
      -> apply_overrides(AggregateInputs) (the 7 mandatory overrides)
      -> apply_gates(raw, points, ...)    (the profile gates)
      -> ProfileResult                    (one final label + audit trail)

It never executes anything or touches money; it only classifies. The
`Classification` it returns bundles the profile with the raw specialist
outputs and the aggregation inputs so a caller (report or forward-test) has
the full audit trail behind the label.
"""

from __future__ import annotations

from dataclasses import dataclass

from wbj.aggregate.gates import (
    CategoryConfidences,
    CategoryPoints,
    ProfileResult,
    apply_gates,
    raw_total,
)
from wbj.aggregate.overrides import AggregateInputs, Override, apply_overrides
from wbj.schemas.packet import Packet
from wbj.specialists.common import SpecialistOutput
from wbj.specialists.financial import _annual_rows, _num
import wbj.specialists.business as biz_mod
import wbj.specialists.financial as fin_mod
import wbj.specialists.market as mkt_mod
import wbj.specialists.risk as risk_mod
import wbj.specialists.technical as tech_mod
import wbj.specialists.valuation as val_mod

# (category_key, module) in the fixed Cerebro category order.
_SPECIALISTS = [
    ("business", biz_mod),
    ("financial", fin_mod),
    ("market", mkt_mod),
    ("technical", tech_mod),
    ("risk", risk_mod),
    ("valuation", val_mod),
]

__all__ = ["Classification", "classify"]


@dataclass(frozen=True)
class Classification:
    """The final profile plus every input behind it, so a caller never has
    to re-run the specialists to audit the label.

    - `profile`: the single `ProfileResult` (its `.label` is the gate, e.g.
      "Momentum Candidate" / "Avoid / Wait").
    - `outputs`: the 6 frozen `SpecialistOutput`s, keyed by category.
    - `overrides`: the mandatory overrides that fired.
    - `points` / `confidences`: what fed the gates.
    """

    profile: ProfileResult
    outputs: dict[str, SpecialistOutput]
    overrides: list[Override]
    points: CategoryPoints
    confidences: CategoryConfidences


def _pre_profit(packet: Packet) -> bool:
    """True when the latest annual net income is a loss.

    Reads the packet through the financial specialist's own extraction
    helpers (`_annual_rows`/`_num`) -- one source of truth for "net income"
    -- rather than re-deriving it. `apply_gates` uses this only to scope the
    Speculative "pre-profit low-confidence terminal value" bullet.
    """
    rows = _annual_rows(packet)
    ni = [_num(r, "net_income") for r in rows]
    ni = [x for x in ni if x is not None]
    return bool(ni) and ni[-1] < 0


def classify(
    packet: Packet,
    *,
    overlay=None,
    judge: bool = False,
    settings=None,
) -> Classification:
    """Run the full packet -> profile pipeline and return the `Classification`.

    `overlay` (optional): pre-computed judgment answers to merge before
    aggregating. `judge` (optional): when True and a `settings` with an
    `anthropic_api_key` is given, calls the Claude judgment agent to answer
    the specialists' open qualitative questions first -- this is what fills
    Business/Market/Risk coverage so the coverage override (override 6) does
    not force every name to gate-ineligible. Both default off, so the pure
    mechanical classification runs with no API calls.
    """
    from wbj.overlay.merge import collect_requests, merge_overlay

    # 1. Run the 6 specialists (frozen outputs), in fixed order.
    outputs: list[SpecialistOutput] = [mod.run(packet) for _key, mod in _SPECIALISTS]

    # 2. Qualitative judgment overlay (fills the coverage the code can't score).
    if judge and settings is not None and getattr(settings, "anthropic_api_key", None):
        from wbj.judge import answer_judgments

        judgments = answer_judgments(packet, collect_requests(outputs), settings)
        if judgments:
            overlay = (overlay or []) + judgments
    if overlay:
        outputs = merge_overlay(outputs, overlay)

    # 3. Re-key by category (agent_id is e.g. "business_analysis").
    by_key = {o.agent_id.split("_")[0]: o for o in outputs}

    # 4. Aggregation inputs (+ facts-table for override 7).
    inputs = AggregateInputs(
        business=by_key["business"],
        financial=by_key["financial"],
        market=by_key["market"],
        technical=by_key["technical"],
        risk=by_key["risk"],
        valuation=by_key["valuation"],
        facts_table=packet.facts_table,
    )
    overrides = apply_overrides(inputs)

    # 5. Category points + confidences (a missing value scores 0, never
    #    silently favorable).
    def _pts(o: SpecialistOutput) -> float:
        return o.category.awarded_points or 0.0

    def _conf(o: SpecialistOutput) -> float:
        return o.category.confidence or 0.0

    points = CategoryPoints(
        business=_pts(inputs.business),
        financial=_pts(inputs.financial),
        market=_pts(inputs.market),
        technical=_pts(inputs.technical),
        risk=_pts(inputs.risk),
        valuation=_pts(inputs.valuation),
    )
    confidences = CategoryConfidences(
        business=_conf(inputs.business),
        financial=_conf(inputs.financial),
        market=_conf(inputs.market),
        technical=_conf(inputs.technical),
        risk=_conf(inputs.risk),
        valuation=_conf(inputs.valuation),
    )

    # 6. Gates. `runway_unfunded` has no packet signal (documented in
    #    gates.py) -> honest False; `pre_profit` + valuation flags scope the
    #    Speculative terminal-value bullet.
    profile = apply_gates(
        raw_total(points),
        points,
        confidences,
        overrides,
        pre_profit=_pre_profit(packet),
        valuation_mandatory_flags=list(inputs.valuation.mandatory_flags),
    )

    return Classification(
        profile=profile,
        outputs=by_key,
        overrides=overrides,
        points=points,
        confidences=confidences,
    )
