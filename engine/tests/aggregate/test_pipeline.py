"""Tests for the classification orchestrator — `wbj.aggregate.pipeline`.

This is the one module that runs packet -> 6 specialists -> apply_overrides ->
apply_gates -> ProfileResult end to end. The tests pin two things:

1. It classifies the golden NVDA packet without crashing, and the mechanical-only
   run (no judgment overlay) lands on a coverage-limited gate — Business/Market/Risk
   qualitative coverage is 0 without the Claude overlay, so override 6 fires. That
   is the system being honest, and the forward-test staying quiet until judged.
2. The `Classification` it returns is internally consistent: raw_total is the sum of
   the reported category points, and every override/point/confidence is exposed for
   audit.
"""

import json
from pathlib import Path

import pytest

from wbj.aggregate.gates import CATEGORY_ORDER
from wbj.aggregate.overrides import OVERRIDE_6_COVERAGE_GATE_INELIGIBLE
from wbj.aggregate.pipeline import Classification, classify
from wbj.schemas.packet import Packet

_PACKET_JSON = Path(__file__).parent.parent / "fixtures" / "packet" / "NVDA_packet.json"


@pytest.fixture
def nvda_packet() -> Packet:
    return Packet.model_validate(json.loads(_PACKET_JSON.read_text()))


def test_classify_returns_a_classification(nvda_packet):
    c = classify(nvda_packet)
    assert isinstance(c, Classification)
    assert c.profile.label
    assert set(c.outputs) == set(CATEGORY_ORDER)


def test_raw_total_is_the_sum_of_reported_points(nvda_packet):
    c = classify(nvda_packet)
    assert c.profile.raw_score == pytest.approx(sum(c.points.as_dict().values()))


def test_mechanical_only_run_is_coverage_limited(nvda_packet):
    """Without the judgment overlay, the qualitative categories score 0 coverage,
    so the coverage override fires and no favorable gate can pass."""
    c = classify(nvda_packet)
    override_ids = {o.id for o in c.overrides}
    assert OVERRIDE_6_COVERAGE_GATE_INELIGIBLE in override_ids
    assert c.profile.passed_gates == []


def test_points_and_confidences_expose_every_category(nvda_packet):
    c = classify(nvda_packet)
    assert set(c.points.as_dict()) == set(CATEGORY_ORDER)
    assert set(c.confidences.as_dict()) == set(CATEGORY_ORDER)
    # points never exceed their category maxima
    maxima = {"business": 20, "financial": 15, "market": 20,
              "technical": 20, "risk": 15, "valuation": 10}
    for name, pts in c.points.as_dict().items():
        assert 0.0 <= pts <= maxima[name]
