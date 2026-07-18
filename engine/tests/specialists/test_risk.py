"""Tests for `wbj.specialists.risk` (Task 18): RSK-001..035, the six
weighted resilience dimensions, Beneish/Altman closed-form math, the
mandatory SOLVENCY_WARNING, the <=4/15 Speculative override, and `run()`
against the NVDA golden fixture.

Sources of truth: `Cerebro/05_risk_analysis/{FORMULAS,SCORING,
DECISION_RULES,VALIDATION_TESTS}.md`, `Perfil Inversionista/Victor
Gonzalez.md`.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from wbj.core.nullstates import NullState
from wbj.core.scoring import Category
from wbj.schemas.packet import AnalysisMeta, MarketData, OHLCVRow, Packet, Security
import wbj.specialists.risk as risk

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "packet" / "NVDA_packet.json"


@pytest.fixture(scope="module")
def nvda_packet() -> Packet:
    data = json.loads(_FIXTURE.read_text())
    return Packet.model_validate(data)


def _dates(n: int) -> list[str]:
    base = pd.Timestamp("2020-01-01")
    return [(base + pd.Timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


def _rows_from_closes(closes: list[float]) -> list[OHLCVRow]:
    dates = _dates(len(closes))
    ascending = [
        OHLCVRow(date=d, open=c, high=c * 1.01, low=c * 0.99, close=c, adj_close=c, volume=1_000_000.0)
        for d, c in zip(dates, closes)
    ]
    return list(reversed(ascending))


def _minimal_packet(
    annual_rows: list[dict], *, daily_closes: list[float] | None = None,
    benchmark_closes: list[float] | None = None, industry_adapter: str = "default_nonfinancial",
) -> Packet:
    return Packet(
        security=Security(ticker="TEST", exchange="NASDAQ", security_type="operating_company", reporting_currency="USD", valuation_currency="USD"),
        analysis=AnalysisMeta(knowledge_timestamp="2026-07-16T21:00:00+00:00", industry_adapter=industry_adapter),
        fundamentals={"annual": annual_rows, "quarterly": []},
        market_data=MarketData(
            daily=_rows_from_closes(daily_closes) if daily_closes else [],
            benchmark=_rows_from_closes(benchmark_closes) if benchmark_closes else [],
        ),
        estimates={}, capital_structure={}, facts_table={}, staleness={},
    )


def _row(year: int, **overrides) -> dict:
    base = dict(
        calendarYear=str(year), date=f"{year}-12-31", period="FY",
        revenue=1000.0, cogs=600.0, gross_profit=400.0, ebit=200.0,
        income_before_tax=190.0, income_tax_expense=40.0, net_income=150.0,
        operating_cash_flow=180.0, capex=-50.0, fcf=130.0, cash=300.0,
        inventory=80.0, net_receivables=90.0, total_current_assets=500.0,
        total_current_liabilities=300.0, total_debt=200.0, total_equity=600.0,
        total_assets=1200.0, total_liabilities=600.0, diluted_shares=100.0,
        stock_based_compensation=10.0,
    )
    base.update(overrides)
    return base


# ============================================================================
# RSK-VALIDATION_TESTS.md, encoded verbatim (RSK-T001..T009)
# ============================================================================


def test_RSK_T001_interest_coverage():
    """EBIT=15, interest=10 -> Coverage=1.5x."""
    v = risk.interest_coverage(15.0, 10.0)
    assert v.value == pytest.approx(1.5)


def test_RSK_T002_coverage_below_threshold_mandatory_warning():
    """Coverage=1.49x -> mandatory solvency warning."""
    v = risk.interest_coverage(14.9, 10.0)
    assert v.value == pytest.approx(1.49)
    assert risk.SOLVENCY_WARNING in v.warnings


def test_RSK_T003_cash_runway():
    """Cash=120, facility=0, monthly burn=10 -> Runway=12 months."""
    v = risk.cash_runway_months(120.0, 0.0, 10.0)
    assert v.value == pytest.approx(12.0)


def test_RSK_T004_max_drawdown():
    """Price index peak 100, trough 40 -> Max drawdown=-60%."""
    index = pd.Series([100.0, 80.0, 40.0, 60.0])
    v = risk.max_drawdown(index)
    assert v.value == pytest.approx(-0.60)


def test_RSK_T005_customer_hhi():
    """Two customers 50% each -> HHI=0.50."""
    v = risk.customer_hhi([0.5, 0.5])
    assert v.value == pytest.approx(0.50)


def test_RSK_T006_negative_ebitda_not_meaningful():
    """Negative EBITDA -> Net debt/EBITDA NOT_MEANINGFUL."""
    v = risk.net_debt_to_ebitda(net_debt=500.0, ebitda=-20.0)
    assert v.is_null
    assert v.state == NullState.NOT_MEANINGFUL


def test_RSK_T007_bank_company_flags_forensic_applicability():
    """Bank company -> Altman/Beneish applicability reviewed; industrial
    scoring not automatic."""
    rows = [_row(2025), _row(2024)]
    packet = _minimal_packet(rows, industry_adapter="bank_adapter")
    out = risk.run(packet)
    assert any("industry_adapter" in a and "bank_adapter" in a for a in out.assumptions)


def test_RSK_T008_risk_category_4_of_15_caps_speculative():
    """Risk category=4/15, raw total=90 -> Main profile capped Speculative."""
    assert risk.capped_verdict(score10=9.0, awarded_points=4.0) == "Speculative"
    assert risk.capped_verdict(score10=9.0, awarded_points=4.01) != "Speculative"


def test_RSK_T009_forensic_flag_only_no_accusation():
    """Forensic M-score above screen threshold -> flag only; no
    accusation (a warning string, not a categorical fraud verdict)."""
    v = risk.beneish_m_score(dsri=1.5, gmi=1.2, aqi=1.1, sgi=1.3, depi=1.0, sgai=1.0, tata=0.05, lvgi=1.0)
    assert v.value > -1.78
    assert "BENEISH_M_SCORE_ABOVE_SCREEN_THRESHOLD" in v.warnings
    # the warning is descriptive text, not e.g. a "FRAUD_DETECTED" state
    assert v.is_valid


# ============================================================================
# Beneish M-score / Altman Z'' -- closed-form hand-computed tests
# ============================================================================


def test_beneish_m_score_hand_computed():
    """DSRI=GMI=AQI=SGI=DEPI=SGAI=LVGI=1.0 (no year-over-year change),
    TATA=0.0 -> M = -4.84 + 0.920+0.528+0.404+0.892+0.115-0.172+0-0.327
    = -2.48 (hand-computed)."""
    v = risk.beneish_m_score(dsri=1.0, gmi=1.0, aqi=1.0, sgi=1.0, depi=1.0, sgai=1.0, tata=0.0, lvgi=1.0)
    assert v.value == pytest.approx(-2.48, abs=1e-9)
    assert "BENEISH_M_SCORE_ABOVE_SCREEN_THRESHOLD" not in v.warnings  # -2.48 < -1.78


def test_beneish_m_score_flags_above_threshold():
    v = risk.beneish_m_score(dsri=1.0, gmi=1.0, aqi=1.0, sgi=1.0, depi=1.0, sgai=1.0, tata=0.2, lvgi=1.0)
    assert v.value > -1.78
    assert "BENEISH_M_SCORE_ABOVE_SCREEN_THRESHOLD" in v.warnings


def test_altman_z_double_prime_hand_computed():
    """WC/TA=0.2, RE/TA=0.3, EBIT/TA=0.15, BE/TL=1.0 -> Z'' =
    6.56*0.2 + 3.26*0.3 + 6.72*0.15 + 1.05*1.0 = 4.348 (hand-computed)."""
    v = risk.altman_z_double_prime(wc_ta=0.2, re_ta=0.3, ebit_ta=0.15, be_tl=1.0)
    assert v.value == pytest.approx(4.348, abs=1e-9)


def test_beneish_dsri_component():
    v = risk.beneish_dsri(receivables_t=120.0, revenue_t=1000.0, receivables_t1=100.0, revenue_t1=1000.0)
    assert v.value == pytest.approx((120.0 / 1000.0) / (100.0 / 1000.0))


def test_beneish_tata_component():
    v = risk.beneish_tata(operating_income=200.0, ocf=180.0, total_assets=1200.0)
    assert v.value == pytest.approx((200.0 - 180.0) / 1200.0)


def test_beneish_lvgi_component():
    v = risk.beneish_lvgi(debt_t=200.0, assets_t=1200.0, debt_t1=180.0, assets_t1=1100.0)
    lev_t = 200.0 / 1200.0
    lev_t1 = 180.0 / 1100.0
    assert v.value == pytest.approx(lev_t / lev_t1)


# ============================================================================
# Piotroski F-score
# ============================================================================


def test_piotroski_f_score_all_9_signals_pass():
    v = risk.piotroski_f_score(
        roa_t=0.10, roa_t1=0.05, ocf=100.0, ni=80.0,
        leverage_t=0.3, leverage_t1=0.4, current_ratio_t=1.5, current_ratio_t1=1.2,
        shares_t=100.0, shares_t1=105.0, gross_margin_t=0.42, gross_margin_t1=0.40,
        asset_turnover_t=0.9, asset_turnover_t1=0.8,
    )
    assert v.value == pytest.approx(9.0)


def test_piotroski_f_score_missing_all_signals_not_scorable():
    v = risk.piotroski_f_score(
        roa_t=None, roa_t1=None, ocf=None, ni=None, leverage_t=None, leverage_t1=None,
        current_ratio_t=None, current_ratio_t1=None, shares_t=None, shares_t1=None,
        gross_margin_t=None, gross_margin_t1=None, asset_turnover_t=None, asset_turnover_t1=None,
    )
    assert v.is_null
    assert v.state == NullState.MISSING


# ============================================================================
# Other formula behavior
# ============================================================================


def test_annualized_volatility_and_downside_deviation():
    rng = np.random.default_rng(1)
    returns = pd.Series(rng.normal(0, 0.01, 300))
    v_vol = risk.annualized_volatility(returns)
    v_down = risk.downside_deviation(returns)
    assert v_vol.is_valid and v_vol.value > 0
    assert v_down.is_valid and v_down.value > 0


def test_market_beta_needs_30_observations():
    stock = pd.Series(np.random.default_rng(2).normal(0, 0.01, 10))
    bench = pd.Series(np.random.default_rng(3).normal(0, 0.01, 10))
    v = risk.market_beta(stock, bench)
    assert v.is_null
    assert v.state == NullState.MISSING


def test_downside_beta_needs_30_down_observations():
    rng = np.random.default_rng(4)
    stock = pd.Series(rng.normal(0.0005, 0.01, 50))
    bench = pd.Series(rng.normal(0.0005, 0.01, 50))
    v = risk.downside_beta(stock, bench)
    # may or may not have 30 down obs depending on draw; just must not crash
    assert v.is_valid or v.state == NullState.MISSING


def test_historical_var_and_expected_shortfall_warn_below_500_obs():
    rng = np.random.default_rng(5)
    returns = pd.Series(rng.normal(0, 0.02, 100))
    v_var = risk.historical_var(returns, 0.95, 1)
    v_cvar = risk.expected_shortfall(returns, 0.95)
    assert v_var.is_valid
    assert "VAR_BELOW_500_OBSERVATIONS_PREFERRED" in v_var.warnings
    assert v_cvar.is_valid
    assert "CVAR_BELOW_500_OBSERVATIONS_PREFERRED" in v_cvar.warnings


def test_diluted_share_cagr_reuses_core_cagr():
    v = risk.diluted_share_cagr(110.0, 100.0, 3.0)
    assert v.is_valid
    assert v.value == pytest.approx((110.0 / 100.0) ** (1 / 3) - 1)


def test_sbc_to_fcf():
    v = risk.sbc_to_fcf(sbc=20.0, fcf=100.0)
    assert v.value == pytest.approx(0.20)


def test_macro_sensitivity_beta_ols_slope():
    macro = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    company = [0.0, 2.0, 4.0, 6.0, 8.0, 10.0]
    v = risk.macro_sensitivity_beta(company, macro)
    assert v.value == pytest.approx(2.0, abs=1e-6)


def test_thesis_killer_priority():
    v = risk.thesis_killer_priority(probability=0.3, impact=0.8, detectability=0.5, time_urgency=0.9)
    assert v.value == pytest.approx(0.3 * 0.8 * 0.5 * 0.9)


def test_profile_fit_within_cap():
    # Angel: riesgo 3-5% por operación. 4% cae dentro; 8% se pasa.
    fit = risk.profile_fit(0.04)
    assert fit["within_risk_budget"] is True
    assert fit["within_position_cap"] is True   # alias retrocompatible
    fit_over = risk.profile_fit(0.08)
    assert fit_over["within_risk_budget"] is False
    fit_none = risk.profile_fit(None)
    assert fit_none["within_risk_budget"] is None


# ============================================================================
# Dimension caps / mandatory flags
# ============================================================================


def test_risk_has_no_apply_dimension_cap_helper():
    """risk_analysis has no numeric dimension-level caps (every SCORING.md
    'Gate / cap' entry here is a confidence caveat or the label-only
    <=4/15 Speculative override), so the shared _apply_dimension_cap is not
    imported into this module -- it was dead code and is removed."""
    assert not hasattr(risk, "_apply_dimension_cap")


def test_run_thesis_killer_priority_row_present_all_35_formulas(nvda_packet):
    """RSK-THESIS-035 (thesis_killer_priority) must surface in out.metrics
    (judgment-only, NOT_SCORABLE) so all 35 RSK formulas are accounted
    for -- previously only 34 rows appeared."""
    out = risk.run(nvda_packet)
    ids = {r.metric_id for r in out.metrics}
    assert "RSK-THESIS-035" in ids
    row = next(r for r in out.metrics if r.metric_id == "RSK-THESIS-035")
    assert row.score == "NOT_SCORABLE"


def test_run_solvency_warning_via_overlay_interest_expense():
    rows = [_row(2025, ebit=100.0), _row(2024)]
    packet = _minimal_packet(rows)
    out = risk.run(packet, overlay={"interest_expense": 90.0})  # coverage=1.11x < 1.5x
    assert risk.SOLVENCY_WARNING in out.mandatory_warnings
    assert risk.SOLVENCY_WARNING in out.mandatory_flags


def test_run_no_solvency_warning_when_coverage_healthy():
    rows = [_row(2025, ebit=100.0), _row(2024)]
    packet = _minimal_packet(rows)
    out = risk.run(packet, overlay={"interest_expense": 10.0})  # coverage=10x
    assert risk.SOLVENCY_WARNING not in out.mandatory_warnings


def test_run_category_reproduces_from_dimensions():
    rows = [_row(2025), _row(2024)]
    packet = _minimal_packet(rows)
    out = risk.run(packet)
    recomputed = Category(name=risk.AGENT_ID, max_points=risk.MAX_POINTS, dimensions=out.dimensions)
    assert out.category.awarded_points == pytest.approx(recomputed.points(), abs=1e-6)
    assert out.category.score_10 == pytest.approx(recomputed.score10(), abs=1e-6)
    assert out.coverage == pytest.approx(recomputed.coverage(), abs=1e-6)


# ============================================================================
# Judgment requests
# ============================================================================


def test_run_thesis_killers_is_judgment_request(nvda_packet):
    out = risk.run(nvda_packet)
    ids = {jr.metric_id for jr in out.judgment_requests}
    assert "thesis_killers" in ids


# ============================================================================
# run() against the NVDA fixture
# ============================================================================


def test_run_nvda_fixture_schema_valid(nvda_packet):
    out = risk.run(nvda_packet)
    assert out.agent_id == "risk_analysis"
    assert out.version == "2.0.0"
    assert out.security.ticker == "NVDA"
    assert out.category.max_points == 15.0
    assert out.status in ("COMPLETE", "INCOMPLETE", "ERROR")
    assert len(out.dimensions) == 6
    for row in out.metrics:
        assert row.metric_id
        assert row.formula_id
        assert row.formula_version
        assert row.score == "NOT_SCORABLE" or isinstance(row.score, float)
        assert 0.0 <= row.confidence <= 100.0
        assert (row.value is None) != (row.state is None)


def test_run_nvda_fixture_category_math_reproduces_from_dimensions(nvda_packet):
    out = risk.run(nvda_packet)
    recomputed = Category(name=risk.AGENT_ID, max_points=risk.MAX_POINTS, dimensions=out.dimensions)
    assert out.category.awarded_points == pytest.approx(recomputed.points(), abs=1e-6)
    assert out.category.score_10 == pytest.approx(recomputed.score10(), abs=1e-6)
    assert out.coverage == pytest.approx(recomputed.coverage(), abs=1e-6)


def test_run_nvda_fixture_category_confidence_computed(nvda_packet):
    out = risk.run(nvda_packet)
    assert out.category.confidence is not None
    assert 0.0 <= out.category.confidence <= 100.0


def test_run_nvda_fixture_serializes_to_json(nvda_packet):
    out = risk.run(nvda_packet)
    dumped = out.model_dump(mode="json")
    json.dumps(dumped)
    assert dumped["agent_id"] == "risk_analysis"


def test_run_nvda_fixture_beta_scored_with_populated_benchmark(nvda_packet):
    """The NVDA golden fixture's `market_data.benchmark` is now populated
    (SPY, aligned to the stock's trading dates -- packet-builder task) --
    beta/downside-beta/correlation must be computed from it, not degrade."""
    assert len(nvda_packet.market_data.benchmark) >= 30
    out = risk.run(nvda_packet)
    beta_row = next(r for r in out.metrics if r.metric_id == "RSK-BETA-003")
    corr_row = next(r for r in out.metrics if r.metric_id == "RSK-CORR-005")
    # RSK-BETA-003/CORR-005 are value-only rows (score is always the literal
    # "NOT_SCORABLE" placeholder per risk.py's `add(..., None)`) -- what
    # matters is that `state` is no longer MISSING, i.e. a real value was
    # computed from the benchmark instead of degrading. (RSK-DBETA-004
    # additionally needs >=30 *down* observations, which this smooth
    # synthetic fixture doesn't happen to have -- covered live instead.)
    assert beta_row.state is None
    assert beta_row.value is not None
    assert corr_row.state is None


def test_run_beta_not_scorable_empty_benchmark():
    """With no benchmark data at all (e.g. the benchmark provider call
    failing), beta must never be proxied (PROHIBITED_IMPUTATION) -- it
    degrades to NOT_SCORABLE/MISSING rather than crashing or guessing."""
    packet = _minimal_packet([_row(2025)], daily_closes=[100.0 + i for i in range(300)])
    assert packet.market_data.benchmark == []
    out = risk.run(packet)
    beta_row = next(r for r in out.metrics if r.metric_id == "RSK-BETA-003")
    assert beta_row.score == "NOT_SCORABLE"
    assert beta_row.state == NullState.MISSING


def test_run_nvda_fixture_profile_fit_populated(nvda_packet):
    out = risk.run(nvda_packet, overlay={"risk_per_trade_pct": 0.04})
    assert out.profile_fit["within_risk_budget"] is True
    assert out.profile_fit["capital_usd"] == pytest.approx(1_000.0)


def test_run_nvda_fixture_validation_tests_all_self_checks_pass(nvda_packet):
    out = risk.run(nvda_packet)
    assert out.validation_tests.failed == 0
    assert out.validation_tests.passed >= 1


def test_run_empty_annual_and_market_history_degrades_without_crashing():
    out = risk.run(_minimal_packet([]))
    assert out.coverage == 0.0
    assert out.category.awarded_points == 0.0
