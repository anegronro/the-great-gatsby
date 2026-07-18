"""Risk and Resilience specialist (Task 18): RSK-VOL-001..RSK-THESIS-035
(`FORMULAS.md`) and the six weighted risk dimensions (15 pts, higher =
safer).

Sources of truth (`Cerebro/05_risk_analysis/`):
- `FORMULAS.md`: the 35 registered formulas.
- `DECISION_RULES.md`: the direction rule (invert raw risk before
  scoring), quantitative resilience anchors, the mandatory
  `SOLVENCY_WARNING`, forensic-screen rules, thesis-killer schema, and the
  `<=4/15 -> Speculative` risk override.
- `SCORING.md`: the six weighted dimensions and their per-dimension
  gate/cap column.
- `OUTPUT_SCHEMA.md`: `RiskOutput`'s extension fields.
- `Perfil Inversionista/Angel Negron.md`: this is the only specialist
  that reads the investor profile (per CLAUDE.md) -- `PROFILE` below is a
  literal, dated transcription of that file's position-sizing/horizon
  facts (3-5% risk per trade, <1 year horizon, $1,000 capital,
  aggressive/speculative style), not re-parsed from markdown at runtime.

## Direction rule

Every dimension score here is *resilience*, not raw risk: `DECISION_RULES
.md`'s "Direction rule" -- "Raw risk measures must be inverted before
assigning 0-10 dimension scores." A HIGH score always means LOWER risk.

## Dataset coverage vs. `Packet`

`Packet.fundamentals.annual` carries `revenue`, `cogs`/`gross_profit`,
`ebit`, `net_income`, `operating_cash_flow`, `total_assets`,
`total_liabilities`, `total_equity`, `total_debt`, `net_receivables`,
`inventory`, `diluted_shares`, `stock_based_compensation`, `fcf` -- enough
for `RSK-ACCR-020`, `RSK-DSRI-021/GMI-022/SGI-024/TATA-028/LVGI-027`,
`RSK-PIO-031` (Piotroski, all 9 signals computable from two years of these
fields), `RSK-DIL-032`, `RSK-SBC-033`, and (with `overlay["cash_burn"]`
for the negative-FCF case) `RSK-RUN-015`. It carries no PP&E,
depreciation, SG&A, retained-earnings, or interest-expense field --
`RSK-AQI-023`/`RSK-DEPI-025`/`RSK-SGAI-026` (so also the full
`RSK-MSCR-029` Beneish M-score, an 8-term additive formula requiring all
eight components), `RSK-ALT-030` (needs retained earnings), and
`RSK-ICOV-011`/`RSK-FCC-012` (need interest expense) are `overlay`-only,
mirroring `financial.py`'s `overlay["interest_expense"]`/`overlay["wacc"]`
precedent -- MISSING, not guessed, without it. Concentration data
(`RSK-CUST-017..019`), the maturity schedule (`RSK-MAT-016`), the macro-
sensitivity series (`RSK-CYC-034`), and the valuation-compression signal
(`margin_of_safety`, sourced from the *valuation* specialist's packet per
`SCORING.md`: "use the valuation-agent packet; do not duplicate valuation
score") are likewise `overlay`-only. `RSK-BETA-003`/`RSK-DBETA-004`
additionally need `packet.market_data.benchmark`, which (per the task-17
brief's note, applicable here too) may be empty -- and `beta` is on
`wbj.core.nullstates.PROHIBITED_IMPUTATION` ("never proxied", per the
task-18 brief): both degrade to `MISSING` honestly, never estimated from
another security's beta or a sector average.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from wbj.core.confidence import confidence as _confidence_formula
from wbj.core.formulas import cagr as _cagr
from wbj.core.nullstates import EvidenceClass, NullState, Value
from wbj.core.scoring import Category, Dimension, anchor_score
from wbj.schemas.packet import Packet
from wbj.specialists.common import (
    CategoryStats,
    JudgmentRequest,
    MetricRow,
    SecurityRef,
    SpecialistOutput,
    ValidationTestsSummary,
    status_from_coverage,
)

__all__ = [
    "AGENT_ID",
    "MAX_POINTS",
    "DIM_FINANCING",
    "DIM_CONCENTRATION",
    "DIM_EXECUTION_QUALITY",
    "DIM_REGULATORY_MACRO",
    "DIM_VALUATION_COMPRESSION",
    "DIM_VOLATILITY_DRAWDOWN",
    "DIMENSION_NAMES",
    "SOLVENCY_WARNING",
    "PROFILE",
    "RiskOutput",
    "verdict",
    "capped_verdict",
    "run",
    "profile_fit",
    # formula functions
    "annualized_volatility",
    "downside_deviation",
    "market_beta",
    "downside_beta",
    "market_correlation",
    "max_drawdown",
    "drawdown_recovery_sessions",
    "historical_var",
    "expected_shortfall",
    "worst_overnight_gap",
    "interest_coverage",
    "fixed_charge_coverage",
    "net_debt_to_ebitda",
    "net_debt_to_fcf",
    "cash_runway_months",
    "maturity_wall_coverage",
    "customer_hhi",
    "product_hhi",
    "geographic_hhi",
    "accrual_ratio",
    "beneish_dsri",
    "beneish_gmi",
    "beneish_aqi",
    "beneish_sgi",
    "beneish_depi",
    "beneish_sgai",
    "beneish_lvgi",
    "beneish_tata",
    "beneish_m_score",
    "altman_z_double_prime",
    "piotroski_f_score",
    "diluted_share_cagr",
    "sbc_to_fcf",
    "macro_sensitivity_beta",
    "thesis_killer_priority",
]

_VERSION = "2.0.0"
AGENT_ID = "risk_analysis"
MAX_POINTS = 15.0

DIM_FINANCING = "financing_and_balance_sheet_risk"
DIM_CONCENTRATION = "competition_and_concentration_risk"
DIM_EXECUTION_QUALITY = "execution_and_earnings_quality_risk"
DIM_REGULATORY_MACRO = "regulatory_legal_and_macro_risk"
DIM_VALUATION_COMPRESSION = "valuation_compression_risk"
DIM_VOLATILITY_DRAWDOWN = "volatility_and_drawdown_risk"

DIMENSION_NAMES = (
    DIM_FINANCING, DIM_CONCENTRATION, DIM_EXECUTION_QUALITY,
    DIM_REGULATORY_MACRO, DIM_VALUATION_COMPRESSION, DIM_VOLATILITY_DRAWDOWN,
)

DIMENSION_MAX_POINTS: dict[str, float] = {
    DIM_FINANCING: 3.0, DIM_CONCENTRATION: 3.0, DIM_EXECUTION_QUALITY: 3.0,
    DIM_REGULATORY_MACRO: 2.0, DIM_VALUATION_COMPRESSION: 2.0, DIM_VOLATILITY_DRAWDOWN: 2.0,
}
assert sum(DIMENSION_MAX_POINTS.values()) == MAX_POINTS

SOLVENCY_WARNING = "SOLVENCY_WARNING: Operating earnings do not provide a comfortable interest buffer."

# `Perfil Inversionista/Angel Negron.md`, transcribed 2026-07 (dated;
# re-check the source file if the profile changes). Only risk.py reads
# this file, per CLAUDE.md's orchestration notes.
#
# Angel's sizing rule differs in KIND from a concentration cap: his profile
# answers "max % per position" with a RISK-per-trade budget of 3-5% of capital
# ($30-50 on $1,000), and states that rule governs over the >10% loss tolerance.
# So the sizing band here is `risk_per_trade_pct`, not a 30-60% position cap.
PROFILE: dict[str, Any] = {
    "objective": "capital_growth_and_income",
    "horizon_years": (0, 1),          # <1 año; retiro parcial en 6-12 meses
    "max_loss_tolerance": "governed_by_risk_rule",
    "style": "aggressive_speculative",
    "capital_usd": 1_000.0,
    "risk_per_trade_pct": (0.03, 0.05),
    "geography": "us_only",
    "excludes": ("forex",),
}


def _ok(x: float, unit: str, **lineage: object) -> Value:
    return Value.of(x, unit=unit, evidence_class=lineage.pop("evidence_class", EvidenceClass.C), **lineage)


def _null(state: NullState, unit: str, *warnings: str) -> Value:
    return Value.null(state, unit=unit, warnings=list(warnings))


# ============================================================================
# RSK-VOL-001..RSK-GAP-010: market and drawdown risk
# ============================================================================


def annualized_volatility(returns: pd.Series) -> Value:
    """Annualized volatility (RSK-VOL-001): `stdev(daily log returns) *
    sqrt(252)`."""
    valid = returns.dropna()
    if len(valid) < 2:
        return _null(NullState.MISSING, "pct_annualized", "VOLATILITY_INSUFFICIENT_HISTORY")
    return _ok(float(valid.std()) * math.sqrt(252), unit="pct_annualized")


def downside_deviation(returns: pd.Series, mar: float = 0.0) -> Value:
    """Downside deviation (RSK-DOWN-002): `sqrt(mean(min(Return-MAR,0)^2))
    * sqrt(252)`. MAR defaults to 0 daily (FORMULAS.md)."""
    valid = returns.dropna()
    if len(valid) < 2:
        return _null(NullState.MISSING, "pct_annualized", "DOWNSIDE_DEVIATION_INSUFFICIENT_HISTORY")
    shortfall = np.minimum(valid.to_numpy(dtype=float) - mar, 0.0)
    return _ok(float(np.sqrt(np.mean(shortfall**2))) * math.sqrt(252), unit="pct_annualized")


def market_beta(stock_returns: pd.Series, bench_returns: pd.Series) -> Value:
    """Market beta (RSK-BETA-003): `cov(Stock, Benchmark) /
    var(Benchmark)`. `beta` is on `PROHIBITED_IMPUTATION` -- never proxied
    from another security or a sector average (see module docstring);
    this function only ever consumes the security's own aligned return
    series."""
    n = min(len(stock_returns), len(bench_returns))
    s, b = stock_returns.iloc[-n:].to_numpy(dtype=float), bench_returns.iloc[-n:].to_numpy(dtype=float)
    mask = ~(np.isnan(s) | np.isnan(b))
    s, b = s[mask], b[mask]
    if len(s) < 30:
        return _null(NullState.MISSING, "ratio", "BETA_INSUFFICIENT_ALIGNED_HISTORY")
    var_b = float(np.var(b, ddof=1))
    if var_b == 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "BETA_ZERO_BENCHMARK_VARIANCE")
    cov = float(np.cov(s, b, ddof=1)[0, 1])
    return _ok(cov / var_b, unit="ratio")


def downside_beta(stock_returns: pd.Series, bench_returns: pd.Series) -> Value:
    """Downside beta (RSK-DBETA-004): beta computed only over sessions
    where the benchmark return is negative. `FORMULAS.md`: minimum 30
    down-market observations."""
    n = min(len(stock_returns), len(bench_returns))
    s, b = stock_returns.iloc[-n:].to_numpy(dtype=float), bench_returns.iloc[-n:].to_numpy(dtype=float)
    mask = ~(np.isnan(s) | np.isnan(b)) & (b < 0)
    s, b = s[mask], b[mask]
    if len(s) < 30:
        return _null(NullState.MISSING, "ratio", "DOWNSIDE_BETA_NEEDS_30_DOWN_OBSERVATIONS")
    var_b = float(np.var(b, ddof=1))
    if var_b == 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "DOWNSIDE_BETA_ZERO_VARIANCE")
    cov = float(np.cov(s, b, ddof=1)[0, 1])
    return _ok(cov / var_b, unit="ratio")


def market_correlation(stock_returns: pd.Series, bench_returns: pd.Series) -> Value:
    """Market correlation (RSK-CORR-005): `corr(Stock, Benchmark)`."""
    n = min(len(stock_returns), len(bench_returns))
    s, b = stock_returns.iloc[-n:].to_numpy(dtype=float), bench_returns.iloc[-n:].to_numpy(dtype=float)
    mask = ~(np.isnan(s) | np.isnan(b))
    s, b = s[mask], b[mask]
    if len(s) < 30:
        return _null(NullState.MISSING, "ratio", "CORRELATION_INSUFFICIENT_ALIGNED_HISTORY")
    if float(np.std(s)) == 0 or float(np.std(b)) == 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "CORRELATION_ZERO_VARIANCE")
    return _ok(float(np.corrcoef(s, b)[0, 1]), unit="ratio")


def max_drawdown(index_values: pd.Series) -> Value:
    """Maximum drawdown (RSK-MDD-006): `min(Value_t/RunningPeak_t - 1)`
    over a total-return index."""
    valid = index_values.dropna()
    if len(valid) < 2:
        return _null(NullState.MISSING, "pct", "MDD_INSUFFICIENT_HISTORY")
    running_peak = valid.cummax()
    drawdown = valid / running_peak - 1.0
    return _ok(float(drawdown.min()), unit="pct")


def drawdown_recovery_sessions(index_values: pd.Series) -> Value:
    """Drawdown recovery time (RSK-REC-007): sessions from the trough
    until the prior peak is regained; `NOT_APPLICABLE` (open) if
    unrecovered as of the latest session."""
    valid = index_values.dropna()
    if len(valid) < 2:
        return _null(NullState.MISSING, "sessions", "RECOVERY_INSUFFICIENT_HISTORY")
    running_peak = valid.cummax()
    drawdown = valid / running_peak - 1.0
    trough_pos = int(drawdown.to_numpy().argmin())
    peak_value_at_trough = running_peak.iloc[trough_pos]
    for j in range(trough_pos + 1, len(valid)):
        if valid.iloc[j] >= peak_value_at_trough:
            return _ok(float(j - trough_pos), unit="sessions")
    return _null(NullState.NOT_APPLICABLE, "sessions", "DRAWDOWN_STILL_OPEN_UNRECOVERED")


def historical_var(returns: pd.Series, confidence: float = 0.95, horizon_days: int = 1) -> Value:
    """Historical VaR (RSK-VAR-008): `-quantile(Return, 1-confidence)`,
    scaled to `horizon_days` by `sqrt(time)` (an approximation,
    FORMULAS.md: "10-day scaling by simulation preferred; sqrt-time only
    as approximation")."""
    valid = returns.dropna()
    if len(valid) < 30:
        return _null(NullState.MISSING, "pct", "VAR_INSUFFICIENT_HISTORY")
    q = float(np.quantile(valid.to_numpy(dtype=float), 1.0 - confidence))
    scale = math.sqrt(horizon_days)
    warnings = [] if len(valid) >= 500 else ["VAR_BELOW_500_OBSERVATIONS_PREFERRED"]
    return Value.of(-q * scale, unit="pct", evidence_class=EvidenceClass.C, warnings=warnings)


def expected_shortfall(returns: pd.Series, confidence: float = 0.95) -> Value:
    """Expected shortfall / CVaR (RSK-CVAR-009): `-mean(Return | Return <=
    VaR quantile)`. `FORMULAS.md`: minimum 500 observations preferred for
    tail stability (warning below that, not a hard refusal)."""
    valid = returns.dropna()
    if len(valid) < 30:
        return _null(NullState.MISSING, "pct", "CVAR_INSUFFICIENT_HISTORY")
    arr = valid.to_numpy(dtype=float)
    q = np.quantile(arr, 1.0 - confidence)
    tail = arr[arr <= q]
    if len(tail) == 0:
        return _null(NullState.MISSING, "pct", "CVAR_EMPTY_TAIL")
    warnings = [] if len(valid) >= 500 else ["CVAR_BELOW_500_OBSERVATIONS_PREFERRED"]
    return Value.of(-float(tail.mean()), unit="pct", evidence_class=EvidenceClass.C, warnings=warnings)


def worst_overnight_gap(opens: pd.Series, prior_closes: pd.Series) -> Value:
    """Worst overnight gap (RSK-GAP-010): `min(Open_t/PriorClose_t - 1)`."""
    n = min(len(opens), len(prior_closes))
    o, c = opens.iloc[-n:].to_numpy(dtype=float), prior_closes.iloc[-n:].to_numpy(dtype=float)
    mask = ~(np.isnan(o) | np.isnan(c)) & (c != 0)
    if mask.sum() == 0:
        return _null(NullState.MISSING, "pct", "GAP_INSUFFICIENT_HISTORY")
    ratios = o[mask] / c[mask] - 1.0
    return _ok(float(ratios.min()), unit="pct")


# ============================================================================
# RSK-ICOV-011..RSK-MAT-016: financing and liquidity
# ============================================================================


def interest_coverage(ebit: float, interest_expense: float) -> Value:
    """Interest coverage (RSK-ICOV-011): `Normalized EBIT / Cash interest
    expense`. Below 1.5x -> mandatory `SOLVENCY_WARNING`."""
    if interest_expense == 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "INTEREST_COVERAGE_ZERO_INTEREST_EXPENSE")
    coverage = ebit / interest_expense
    warnings = [SOLVENCY_WARNING] if coverage < 1.5 else []
    return Value.of(coverage, unit="ratio", evidence_class=EvidenceClass.C, warnings=warnings)


def fixed_charge_coverage(ebit: float, interest_expense: float, lease_charge: float) -> Value:
    """Fixed-charge coverage (RSK-FCC-012): `(EBIT + lease/rent charge) /
    (Interest + lease/rent charge)`."""
    denom = interest_expense + lease_charge
    if denom == 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "FCC_ZERO_DENOMINATOR")
    return _ok((ebit + lease_charge) / denom, unit="ratio")


def net_debt_to_ebitda(net_debt: float, ebitda: float) -> Value:
    """Net debt to EBITDA (RSK-ND-013). Not meaningful for negative EBITDA
    (RSK-T006)."""
    if ebitda <= 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "NET_DEBT_TO_EBITDA_NONPOSITIVE_EBITDA")
    return _ok(net_debt / ebitda, unit="ratio")


def net_debt_to_fcf(net_debt: float, fcf: float) -> Value:
    """Net debt to FCF (RSK-DFC-014). Not meaningful for FCF<=0."""
    if fcf <= 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "NET_DEBT_TO_FCF_NONPOSITIVE_FCF")
    return _ok(net_debt / fcf, unit="ratio")


def cash_runway_months(cash: float, committed_liquidity: float, monthly_burn: float) -> Value:
    """Cash runway (RSK-RUN-015): `(Cash + committed undrawn liquidity) /
    Average monthly cash burn`. Only meaningful for a burning company
    (`monthly_burn > 0`, FORMULAS.md: "Only for negative FCF/burn")."""
    if monthly_burn <= 0:
        return _null(NullState.NOT_APPLICABLE, "months", "CASH_RUNWAY_NOT_APPLICABLE_NOT_BURNING")
    return _ok((cash + committed_liquidity) / monthly_burn, unit="months")


def maturity_wall_coverage(cash: float, expected_fcf_before_maturity: float, committed_liquidity: float, debt_due: float) -> Value:
    """Maturity-wall coverage (RSK-MAT-016): `(Cash + expected FCF before
    maturity + committed liquidity) / Debt due within horizon`. `<1`
    implies refinancing need (FORMULAS.md)."""
    if debt_due <= 0:
        return _null(NullState.NOT_APPLICABLE, "ratio", "MATURITY_WALL_NO_DEBT_DUE")
    return _ok((cash + expected_fcf_before_maturity + committed_liquidity) / debt_due, unit="ratio")


# ============================================================================
# RSK-CUST-017..GEO-019: concentration
# ============================================================================


def customer_hhi(shares: list[float]) -> Value:
    """Customer HHI (RSK-CUST-017): `sum(share_i^2)`."""
    if not shares:
        return _null(NullState.MISSING, "ratio", "CUSTOMER_HHI_EMPTY_SHARES")
    return _ok(sum(s * s for s in shares), unit="ratio")


def product_hhi(shares: list[float]) -> Value:
    """Product HHI (RSK-PROD-018): `sum(product_share_i^2)`."""
    if not shares:
        return _null(NullState.MISSING, "ratio", "PRODUCT_HHI_EMPTY_SHARES")
    return _ok(sum(s * s for s in shares), unit="ratio")


def geographic_hhi(shares: list[float]) -> Value:
    """Geographic concentration HHI (RSK-GEO-019): `sum(geography_share_i^2)`."""
    if not shares:
        return _null(NullState.MISSING, "ratio", "GEO_HHI_EMPTY_SHARES")
    return _ok(sum(s * s for s in shares), unit="ratio")


# ============================================================================
# RSK-ACCR-020..RSK-PIO-031: execution / earnings quality / forensics
# ============================================================================


def accrual_ratio(net_income: float, ocf: float, avg_total_assets: float) -> Value:
    """Accrual ratio (RSK-ACCR-020): `(Net income - OCF) / Average total
    assets`. High positive accruals are a warning (FORMULAS.md)."""
    if avg_total_assets <= 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "ACCRUAL_RATIO_NONPOSITIVE_AVG_ASSETS")
    return _ok((net_income - ocf) / avg_total_assets, unit="ratio")


def beneish_dsri(receivables_t: float, revenue_t: float, receivables_t1: float, revenue_t1: float) -> Value:
    """Beneish DSRI (RSK-DSRI-021): `(Receivables_t/Revenue_t) /
    (Receivables_t-1/Revenue_t-1)`."""
    if revenue_t == 0 or revenue_t1 == 0 or receivables_t1 == 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "DSRI_ZERO_DENOMINATOR")
    return _ok((receivables_t / revenue_t) / (receivables_t1 / revenue_t1), unit="ratio")


def beneish_gmi(gross_margin_t: float, gross_margin_t1: float) -> Value:
    """Beneish GMI (RSK-GMI-022): `GrossMargin_t-1 / GrossMargin_t`."""
    if gross_margin_t == 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "GMI_ZERO_CURRENT_MARGIN")
    return _ok(gross_margin_t1 / gross_margin_t, unit="ratio")


def beneish_aqi(current_assets_t: float, ppe_t: float, assets_t: float, current_assets_t1: float, ppe_t1: float, assets_t1: float) -> Value:
    """Beneish AQI (RSK-AQI-023): `[1-(CurrentAssets+PPE)/Assets]_t / same_t-1`."""
    if assets_t == 0 or assets_t1 == 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "AQI_ZERO_ASSETS")
    aq_t = 1 - (current_assets_t + ppe_t) / assets_t
    aq_t1 = 1 - (current_assets_t1 + ppe_t1) / assets_t1
    if aq_t1 == 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "AQI_ZERO_PRIOR")
    return _ok(aq_t / aq_t1, unit="ratio")


def beneish_sgi(revenue_t: float, revenue_t1: float) -> Value:
    """Beneish SGI (RSK-SGI-024): `Revenue_t / Revenue_t-1`."""
    if revenue_t1 == 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "SGI_ZERO_PRIOR_REVENUE")
    return _ok(revenue_t / revenue_t1, unit="ratio")


def beneish_depi(dep_t: float, ppe_t: float, dep_t1: float, ppe_t1: float) -> Value:
    """Beneish DEPI (RSK-DEPI-025): `DepRate_t-1 / DepRate_t`,
    `DepRate = Depreciation/(Depreciation+NetPPE)`."""
    rate_t_denom = dep_t + ppe_t
    rate_t1_denom = dep_t1 + ppe_t1
    if rate_t_denom == 0 or rate_t1_denom == 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "DEPI_ZERO_DENOMINATOR")
    rate_t = dep_t / rate_t_denom
    rate_t1 = dep_t1 / rate_t1_denom
    if rate_t == 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "DEPI_ZERO_CURRENT_RATE")
    return _ok(rate_t1 / rate_t, unit="ratio")


def beneish_sgai(sga_t: float, revenue_t: float, sga_t1: float, revenue_t1: float) -> Value:
    """Beneish SGAI (RSK-SGAI-026): `(SG&A_t/Revenue_t) /
    (SG&A_t-1/Revenue_t-1)`."""
    if revenue_t == 0 or revenue_t1 == 0 or sga_t1 == 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "SGAI_ZERO_DENOMINATOR")
    return _ok((sga_t / revenue_t) / (sga_t1 / revenue_t1), unit="ratio")


def beneish_lvgi(debt_t: float, assets_t: float, debt_t1: float, assets_t1: float) -> Value:
    """Beneish LVGI (RSK-LVGI-027): `Leverage_t / Leverage_t-1`,
    `Leverage = Debt/Assets`."""
    if assets_t == 0 or assets_t1 == 0 or debt_t1 == 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "LVGI_ZERO_DENOMINATOR")
    lev_t = debt_t / assets_t
    lev_t1 = debt_t1 / assets_t1
    if lev_t1 == 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "LVGI_ZERO_PRIOR_LEVERAGE")
    return _ok(lev_t / lev_t1, unit="ratio")


def beneish_tata(operating_income: float, ocf: float, total_assets: float) -> Value:
    """Beneish TATA (RSK-TATA-028): `(Income from operations - OCF) /
    Total assets`."""
    if total_assets == 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "TATA_ZERO_ASSETS")
    return _ok((operating_income - ocf) / total_assets, unit="ratio")


def beneish_m_score(dsri: float, gmi: float, aqi: float, sgi: float, depi: float, sgai: float, tata: float, lvgi: float) -> Value:
    """Beneish M-score (RSK-MSCR-029), verbatim:
    `-4.84 + 0.920*DSRI + 0.528*GMI + 0.404*AQI + 0.892*SGI + 0.115*DEPI -
    0.172*SGAI + 4.679*TATA - 0.327*LVGI`. Screening only -- a flag above
    the classic -1.78 threshold is never proof of manipulation
    (`DECISION_RULES.md`'s forensic-screen rules)."""
    m = -4.84 + 0.920 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi + 0.115 * depi - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi
    warnings = ["BENEISH_M_SCORE_ABOVE_SCREEN_THRESHOLD"] if m > -1.78 else []
    return Value.of(m, unit="score", evidence_class=EvidenceClass.C, warnings=warnings)


def altman_z_double_prime(wc_ta: float, re_ta: float, ebit_ta: float, be_tl: float) -> Value:
    """Altman Z'' (RSK-ALT-030), verbatim: `6.56*WC/TA + 3.26*RE/TA +
    6.72*EBIT/TA + 1.05*BookEquity/TotalLiabilities`. Non-financial
    companies only (`FORMULAS.md`)."""
    return _ok(6.56 * wc_ta + 3.26 * re_ta + 6.72 * ebit_ta + 1.05 * be_tl, unit="score")


def piotroski_f_score(
    *, roa_t: float | None, roa_t1: float | None, ocf: float | None, ni: float | None,
    leverage_t: float | None, leverage_t1: float | None, current_ratio_t: float | None, current_ratio_t1: float | None,
    shares_t: float | None, shares_t1: float | None, gross_margin_t: float | None, gross_margin_t1: float | None,
    asset_turnover_t: float | None, asset_turnover_t1: float | None,
) -> Value:
    """Piotroski F-score (RSK-PIO-031): sum of 9 binary signals (0-9). Any
    missing pair of inputs simply fails to award that signal's point
    (documented, not a per-signal null) -- the composite score itself is
    `NOT_SCORABLE` only when every signal's inputs are entirely absent."""
    signals: list[bool | None] = [
        (roa_t > 0) if roa_t is not None else None,
        (ocf > 0) if ocf is not None else None,
        (roa_t > roa_t1) if roa_t is not None and roa_t1 is not None else None,
        (ocf > ni) if ocf is not None and ni is not None else None,
        (leverage_t < leverage_t1) if leverage_t is not None and leverage_t1 is not None else None,
        (current_ratio_t > current_ratio_t1) if current_ratio_t is not None and current_ratio_t1 is not None else None,
        (shares_t <= shares_t1) if shares_t is not None and shares_t1 is not None else None,
        (gross_margin_t > gross_margin_t1) if gross_margin_t is not None and gross_margin_t1 is not None else None,
        (asset_turnover_t > asset_turnover_t1) if asset_turnover_t is not None and asset_turnover_t1 is not None else None,
    ]
    known = [s for s in signals if s is not None]
    if not known:
        return _null(NullState.MISSING, "score_0_9", "PIOTROSKI_ALL_SIGNALS_UNAVAILABLE")
    warnings = [] if len(known) == 9 else [f"PIOTROSKI_{9 - len(known)}_SIGNALS_UNAVAILABLE"]
    return Value.of(float(sum(1 for s in known if s)), unit="score_0_9", evidence_class=EvidenceClass.C, warnings=warnings)


# ============================================================================
# RSK-DIL-032..RSK-THESIS-035
# ============================================================================


def diluted_share_cagr(shares_end: float, shares_begin: float, years: float) -> Value:
    """Diluted-share CAGR (RSK-DIL-032). Positive values are dilution
    risk (FORMULAS.md)."""
    return _cagr(shares_end, shares_begin, years)


def sbc_to_fcf(sbc: float, fcf: float, materiality_floor: float = 1.0) -> Value:
    """SBC to FCF (RSK-SBC-033): `SBC / max(abs(FCF), materiality_floor)`."""
    return _ok(sbc / max(abs(fcf), materiality_floor), unit="ratio")


def macro_sensitivity_beta(company_series: list[float], macro_series: list[float]) -> Value:
    """Macro sensitivity beta (RSK-CYC-034): OLS coefficient of company
    metric/excess-return changes on macro-factor changes."""
    if len(company_series) != len(macro_series) or len(company_series) < 6:
        return _null(NullState.MISSING, "ratio", "MACRO_SENSITIVITY_INSUFFICIENT_OBSERVATIONS")
    x = np.asarray(macro_series, dtype=float)
    y = np.asarray(company_series, dtype=float)
    if float(np.std(x)) == 0:
        return _null(NullState.NOT_MEANINGFUL, "ratio", "MACRO_SENSITIVITY_ZERO_FACTOR_VARIANCE")
    slope, _intercept = np.polyfit(x, y, 1)
    return _ok(float(slope), unit="ratio")


def thesis_killer_priority(probability: float, impact: float, detectability: float, time_urgency: float) -> Value:
    """Thesis-killer priority (RSK-THESIS-035): `Probability * Impact *
    (1-Detectability) * TimeUrgency`."""
    return _ok(probability * impact * (1 - detectability) * time_urgency, unit="score", evidence_class=EvidenceClass.A)


def profile_fit(risk_per_trade_pct: float | None) -> dict[str, Any]:
    """Per-trade-risk and horizon fit against `PROFILE`.

    Angel's rule sizes each trade to risk 3-5% of capital, so the fit checks the
    intended per-trade risk against that budget (not a position-size cap — see the
    PROFILE note). `within_position_cap` is kept as an alias so callers/serializers
    that read the old key still work.
    """
    lo, hi = PROFILE["risk_per_trade_pct"]
    within = (lo <= risk_per_trade_pct <= hi) if risk_per_trade_pct is not None else None
    return {
        "risk_per_trade_pct": risk_per_trade_pct,
        "risk_per_trade_pct_range": list(PROFILE["risk_per_trade_pct"]),
        "within_risk_budget": within,
        "within_position_cap": within,   # alias retrocompatible
        "horizon_years_range": list(PROFILE["horizon_years"]),
        "style": PROFILE["style"],
        "capital_usd": PROFILE["capital_usd"],
    }


# ============================================================================
# Scoring helper. (risk_analysis has no numeric dimension-level caps -- every
# SCORING.md "Gate / cap" entry here is a confidence caveat or the label-only
# <=4/15 Speculative override, not a point cap -- so no _apply_dimension_cap.)
# ============================================================================


def _score_from_anchor(v: Value, anchors: list[tuple[float, float]]) -> float | None:
    if v.is_null:
        return None
    return anchor_score(v.value, anchors)


# ============================================================================
# Verdict
# ============================================================================


def verdict(score10: float) -> str:
    """`DECISION_RULES.md` gives no explicit risk-verdict band table
    (unlike the other five specialists) -- only the `<=4/15` override
    (see `capped_verdict`). This module's own band construction, mirroring
    the direction rule (higher = more resilient): documented, not a value
    taken from a Cerebro table."""
    if score10 >= 8.0:
        return "Resilient"
    if score10 >= 6.0:
        return "Manageable risk"
    if score10 >= 4.0:
        return "Elevated risk"
    return "High risk"


def capped_verdict(score10: float, awarded_points: float) -> str:
    """`DECISION_RULES.md`'s risk override: "Category points <=4/15 cap
    the main-agent profile at Speculative regardless of total score." This
    caps the label only (never `category.awarded_points`, per
    `SpecialistOutput.verdict`'s docstring)."""
    if awarded_points <= 4.0:
        return "Speculative"
    return verdict(score10)


# ============================================================================
# Envelope assembly
# ============================================================================


class RiskOutput(SpecialistOutput):
    """`risk_analysis`'s `run()` result: the shared envelope plus
    OUTPUT_SCHEMA.md's risk-specific extension fields, plus `profile_fit`
    (task-18 brief addition, not in the OUTPUT_SCHEMA.md yaml stub but not
    precluded by it either)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    market_risk: dict[str, Any] = Field(default_factory=dict)
    liquidity_and_solvency: dict[str, Any] = Field(default_factory=dict)
    concentrations: dict[str, Any] = Field(default_factory=dict)
    earnings_quality_and_forensics: dict[str, Any] = Field(default_factory=dict)
    regulatory_legal_macro: list[dict[str, Any]] = Field(default_factory=list)
    valuation_compression: dict[str, Any] = Field(default_factory=dict)
    thesis_killers: list[dict[str, Any]] = Field(default_factory=list)
    mandatory_warnings: list[str] = Field(default_factory=list)
    profile_fit: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class _Row:
    metric_id: str
    value: Value
    score10: float | None
    confidence: float = 0.0


def _confidence_for(v: Value) -> float:
    if v.is_null:
        return 0.0
    base_by_class = {
        EvidenceClass.R: 95.0, EvidenceClass.C: 85.0, EvidenceClass.E: 60.0,
        EvidenceClass.A: 50.0, EvidenceClass.Q: 30.0,
    }
    base = base_by_class.get(v.evidence_class, 70.0) if v.evidence_class else 70.0
    base -= 15.0 * len(v.warnings)
    return max(0.0, min(100.0, base))


def _num(row: dict, key: str) -> float | None:
    v = row.get(key)
    return float(v) if isinstance(v, (int, float)) else None


def _annual_rows(packet: Packet) -> list[dict]:
    rows = packet.fundamentals.get("annual") or []
    return list(reversed(rows))  # ascending, per DATASET.md


def _to_returns(rows: list) -> pd.Series:
    ascending = list(reversed(rows))
    closes = pd.Series([r.adj_close for r in ascending])
    return np.log(closes / closes.shift(1))


def run(packet: Packet, overlay: dict[str, Any] | None = None) -> RiskOutput:
    """Run the risk specialist against `packet`, producing a `RiskOutput`
    (max 15 pts, higher = safer): the six weighted resilience dimensions,
    mandatory warnings, and OUTPUT_SCHEMA.md's extension fields.

    See the module docstring for `overlay`'s supported keys
    (`interest_expense`, `retained_earnings`, `ppe`/`ppe_prior`,
    `depreciation`/`depreciation_prior`, `sga`/`sga_prior`,
    `cash_burn` -> `{"committed_liquidity": float}`, `customer_shares`,
    `product_shares`, `geographic_shares`, `debt_due`/`expected_fcf_before_maturity`,
    `macro_series`/`company_series`, `margin_of_safety`,
    `position_size_pct`).
    """
    overlay = overlay or {}
    annual = _annual_rows(packet)
    assumptions: list[str] = []
    judgment_requests: list[JudgmentRequest] = []
    rows: list[_Row] = []

    def add(metric_id: str, v: Value, score10: float | None) -> None:
        rows.append(_Row(metric_id=metric_id, value=v, score10=score10, confidence=_confidence_for(v)))

    latest = annual[-1] if annual else {}
    prior = annual[-2] if len(annual) >= 2 else {}
    ebit = _num(latest, "ebit")
    revenue_t, revenue_t1 = _num(latest, "revenue"), _num(prior, "revenue")
    ni_t = _num(latest, "net_income")
    ocf_t = _num(latest, "operating_cash_flow")
    assets_t, assets_t1 = _num(latest, "total_assets"), _num(prior, "total_assets")
    liab_t = _num(latest, "total_liabilities")
    equity_t = _num(latest, "total_equity")
    debt_t, debt_t1 = _num(latest, "total_debt"), _num(prior, "total_debt")
    cash_t = _num(latest, "cash")
    ca_t, cl_t = _num(latest, "total_current_assets"), _num(latest, "total_current_liabilities")
    ca_t1, cl_t1 = _num(prior, "total_current_assets"), _num(prior, "total_current_liabilities")
    fcf_t = _num(latest, "fcf")
    recv_t, recv_t1 = _num(latest, "net_receivables"), _num(prior, "net_receivables")
    gp_t, gp_t1 = _num(latest, "gross_profit"), _num(prior, "gross_profit")
    sbc_t = _num(latest, "stock_based_compensation")

    # ---- Market/drawdown returns from packet.market_data.daily ----
    daily_rows = packet.market_data.daily
    stock_returns = _to_returns(daily_rows) if daily_rows else pd.Series(dtype=float)
    ascending_daily = list(reversed(daily_rows))
    index_values = pd.Series([r.adj_close for r in ascending_daily]) if ascending_daily else pd.Series(dtype=float)
    opens = pd.Series([r.open for r in ascending_daily]) if ascending_daily else pd.Series(dtype=float)
    prior_closes = index_values.shift(1)

    v_vol = annualized_volatility(stock_returns)
    add("RSK-VOL-001", v_vol, None)
    v_down = downside_deviation(stock_returns)
    add("RSK-DOWN-002", v_down, None)

    bench_rows = packet.market_data.benchmark
    if bench_rows and len(bench_rows) >= 30:
        bench_returns = _to_returns(bench_rows)
        v_beta = market_beta(stock_returns, bench_returns)
        v_dbeta = downside_beta(stock_returns, bench_returns)
        v_corr = market_correlation(stock_returns, bench_returns)
    else:
        v_beta = _null(NullState.MISSING, "ratio", "BETA_UNAVAILABLE_EMPTY_BENCHMARK_PROHIBITED_IMPUTATION")
        v_dbeta = _null(NullState.MISSING, "ratio", "DOWNSIDE_BETA_UNAVAILABLE_EMPTY_BENCHMARK")
        v_corr = _null(NullState.MISSING, "ratio", "CORRELATION_UNAVAILABLE_EMPTY_BENCHMARK")
    add("RSK-BETA-003", v_beta, None)
    add("RSK-DBETA-004", v_dbeta, _score_from_anchor(v_dbeta, [(2.0, 0), (1.5, 3), (0.8, 6), (0.3, 10)]))
    add("RSK-CORR-005", v_corr, None)

    v_mdd = max_drawdown(index_values)
    add("RSK-MDD-006", v_mdd, _score_from_anchor(v_mdd, [(-0.80, 0), (-0.60, 3), (-0.30, 6), (0.0, 10)]))
    v_rec = drawdown_recovery_sessions(index_values)
    add("RSK-REC-007", v_rec, None)
    v_var95 = historical_var(stock_returns, 0.95, 1)
    add("RSK-VAR-008", v_var95, _score_from_anchor(v_var95, [(0.0, 10), (0.02, 7), (0.05, 3), (0.10, 0)]))
    v_cvar95 = expected_shortfall(stock_returns, 0.95)
    add("RSK-CVAR-009", v_cvar95, None)
    v_gap = worst_overnight_gap(opens, prior_closes)
    add("RSK-GAP-010", v_gap, None)

    # ---- Financing (overlay-driven interest expense) ----
    interest_expense = overlay.get("interest_expense")
    if interest_expense is not None and ebit is not None:
        v_icov = interest_coverage(ebit, float(interest_expense))
    else:
        v_icov = _null(NullState.MISSING, "ratio", "INTEREST_EXPENSE_UNAVAILABLE")
        assumptions.append("RSK-ICOV-011 not computed: interest_expense is not part of Packet.fundamentals and no overlay['interest_expense'] was supplied.")
    add("RSK-ICOV-011", v_icov, _score_from_anchor(v_icov, [(1.5, 0), (3.0, 6), (5.0, 10)]))
    mandatory_warnings: list[str] = [SOLVENCY_WARNING] if SOLVENCY_WARNING in v_icov.warnings else []

    lease_charge = float(overlay.get("lease_charge", 0.0))
    if interest_expense is not None and ebit is not None:
        v_fcc = fixed_charge_coverage(ebit, float(interest_expense), lease_charge)
    else:
        v_fcc = _null(NullState.MISSING, "ratio", "FCC_INPUTS_UNAVAILABLE")
    add("RSK-FCC-012", v_fcc, None)

    add("RSK-ND-013", _null(NullState.MISSING, "ratio", "EBITDA_UNAVAILABLE_NO_DA_FIELD"), None)

    net_debt = (debt_t or 0.0) - (cash_t or 0.0) if debt_t is not None else None
    if net_debt is not None and fcf_t is not None:
        v_dfc = net_debt_to_fcf(net_debt, fcf_t)
    else:
        v_dfc = _null(NullState.MISSING, "ratio", "NET_DEBT_TO_FCF_INPUTS_UNAVAILABLE")
    add("RSK-DFC-014", v_dfc, _score_from_anchor(v_dfc, [(0.0, 10), (2.0, 7), (4.0, 4), (8.0, 0)]))

    cash_burn_overlay = overlay.get("cash_burn") or {}
    committed_liquidity = float(cash_burn_overlay.get("committed_liquidity", 0.0))
    monthly_burn = -(fcf_t or 0.0) / 12.0 if fcf_t is not None and fcf_t < 0 else 0.0
    if cash_t is not None:
        v_run = cash_runway_months(cash_t, committed_liquidity, monthly_burn)
    else:
        v_run = _null(NullState.MISSING, "months", "CASH_RUNWAY_INPUTS_UNAVAILABLE")
    add("RSK-RUN-015", v_run, _score_from_anchor(v_run, [(0.0, 0), (12.0, 3), (24.0, 6), (36.0, 10)]) if v_run.is_valid else None)

    debt_due = overlay.get("debt_due")
    expected_fcf_before_maturity = overlay.get("expected_fcf_before_maturity")
    if debt_due is not None and cash_t is not None:
        v_mat = maturity_wall_coverage(cash_t, float(expected_fcf_before_maturity or 0.0), committed_liquidity, float(debt_due))
    else:
        v_mat = _null(NullState.MISSING, "ratio", "MATURITY_WALL_UNAVAILABLE")
    add("RSK-MAT-016", v_mat, _score_from_anchor(v_mat, [(0.5, 0), (1.0, 5), (1.5, 8), (2.5, 10)]) if v_mat.is_valid else None)

    # ---- Concentration (overlay only) ----
    largest_customer_share = overlay.get("largest_customer_share")
    customer_shares = overlay.get("customer_shares")
    if customer_shares:
        v_cust = customer_hhi(customer_shares)
    else:
        v_cust = _null(NullState.MISSING, "ratio", "CUSTOMER_HHI_UNAVAILABLE")
    add("RSK-CUST-017", v_cust, None)
    # DECISION_RULES.md's anchor is stated on the *largest single customer
    # share*, not the HHI -- scored separately from the reported HHI row.
    v_cust_share = _ok(largest_customer_share, unit="pct") if largest_customer_share is not None else _null(NullState.MISSING, "pct", "CUSTOMER_CONCENTRATION_UNAVAILABLE_PROHIBITED_IMPUTATION")
    cust_score = _score_from_anchor(v_cust_share, [(0.50, 0), (0.30, 3), (0.10, 6), (0.0, 10)])

    product_shares = overlay.get("product_shares")
    v_prod = product_hhi(product_shares) if product_shares else _null(NullState.MISSING, "ratio", "PRODUCT_HHI_UNAVAILABLE")
    add("RSK-PROD-018", v_prod, _score_from_anchor(v_prod, [(1.0, 0), (0.5, 4), (0.25, 7), (0.0, 10)]))

    geo_shares = overlay.get("geographic_shares")
    v_geo = geographic_hhi(geo_shares) if geo_shares else _null(NullState.MISSING, "ratio", "GEO_HHI_UNAVAILABLE")
    add("RSK-GEO-019", v_geo, _score_from_anchor(v_geo, [(1.0, 0), (0.5, 4), (0.25, 7), (0.0, 10)]))

    # ---- Execution / earnings quality / forensics ----
    avg_assets = (assets_t + assets_t1) / 2 if assets_t is not None and assets_t1 is not None else None
    if ni_t is not None and ocf_t is not None and avg_assets is not None:
        v_accr = accrual_ratio(ni_t, ocf_t, avg_assets)
    else:
        v_accr = _null(NullState.MISSING, "ratio", "ACCRUAL_RATIO_INPUTS_UNAVAILABLE")
    add("RSK-ACCR-020", v_accr, _score_from_anchor(v_accr, [(0.15, 0), (0.05, 3), (0.0, 7), (-0.05, 10)]))

    if None not in (recv_t, revenue_t, recv_t1, revenue_t1) and revenue_t1 != 0 and recv_t1 != 0:
        v_dsri = beneish_dsri(recv_t, revenue_t, recv_t1, revenue_t1)
    else:
        v_dsri = _null(NullState.MISSING, "ratio", "DSRI_INPUTS_UNAVAILABLE")
    add("RSK-DSRI-021", v_dsri, None)

    gm_t = gp_t / revenue_t if gp_t is not None and revenue_t not in (None, 0) else None
    gm_t1 = gp_t1 / revenue_t1 if gp_t1 is not None and revenue_t1 not in (None, 0) else None
    v_gmi = beneish_gmi(gm_t, gm_t1) if gm_t is not None and gm_t1 is not None else _null(NullState.MISSING, "ratio", "GMI_INPUTS_UNAVAILABLE")
    add("RSK-GMI-022", v_gmi, None)

    ppe_t, ppe_t1 = overlay.get("ppe"), overlay.get("ppe_prior")
    if None not in (ca_t, ppe_t, assets_t, ca_t1, ppe_t1, assets_t1):
        v_aqi = beneish_aqi(ca_t, ppe_t, assets_t, ca_t1, ppe_t1, assets_t1)
    else:
        v_aqi = _null(NullState.MISSING, "ratio", "AQI_UNAVAILABLE_NO_PPE_FIELD")
    add("RSK-AQI-023", v_aqi, None)

    v_sgi = beneish_sgi(revenue_t, revenue_t1) if revenue_t is not None and revenue_t1 is not None else _null(NullState.MISSING, "ratio", "SGI_INPUTS_UNAVAILABLE")
    add("RSK-SGI-024", v_sgi, None)

    dep_t, dep_t1 = overlay.get("depreciation"), overlay.get("depreciation_prior")
    if None not in (dep_t, ppe_t, dep_t1, ppe_t1):
        v_depi = beneish_depi(dep_t, ppe_t, dep_t1, ppe_t1)
    else:
        v_depi = _null(NullState.MISSING, "ratio", "DEPI_UNAVAILABLE_NO_DA_FIELD")
    add("RSK-DEPI-025", v_depi, None)

    sga_t, sga_t1 = overlay.get("sga"), overlay.get("sga_prior")
    if None not in (sga_t, revenue_t, sga_t1, revenue_t1):
        v_sgai = beneish_sgai(sga_t, revenue_t, sga_t1, revenue_t1)
    else:
        v_sgai = _null(NullState.MISSING, "ratio", "SGAI_UNAVAILABLE_NO_SGA_FIELD")
    add("RSK-SGAI-026", v_sgai, None)

    if None not in (debt_t, assets_t, debt_t1, assets_t1):
        v_lvgi = beneish_lvgi(debt_t, assets_t, debt_t1, assets_t1)
    else:
        v_lvgi = _null(NullState.MISSING, "ratio", "LVGI_INPUTS_UNAVAILABLE")
    add("RSK-LVGI-027", v_lvgi, None)

    if ebit is not None and ocf_t is not None and assets_t is not None:
        v_tata = beneish_tata(ebit, ocf_t, assets_t)
    else:
        v_tata = _null(NullState.MISSING, "ratio", "TATA_INPUTS_UNAVAILABLE")
    add("RSK-TATA-028", v_tata, None)

    components = [v_dsri, v_gmi, v_aqi, v_sgi, v_depi, v_sgai, v_tata, v_lvgi]
    if all(c.is_valid for c in components):
        v_mscr = beneish_m_score(v_dsri.value, v_gmi.value, v_aqi.value, v_sgi.value, v_depi.value, v_sgai.value, v_tata.value, v_lvgi.value)
    else:
        v_mscr = _null(NullState.MISSING, "score", "MSCR_REQUIRES_ALL_8_COMPONENTS")
    add("RSK-MSCR-029", v_mscr, _score_from_anchor(v_mscr, [(0.0, 0), (-1.78, 5), (-2.5, 8), (-4.0, 10)]))

    retained_earnings = overlay.get("retained_earnings")
    if None not in (ca_t, cl_t, assets_t, retained_earnings, ebit, equity_t, liab_t) and liab_t != 0 and assets_t != 0:
        v_alt = altman_z_double_prime((ca_t - cl_t) / assets_t, retained_earnings / assets_t, ebit / assets_t, equity_t / liab_t)
    else:
        v_alt = _null(NullState.MISSING, "score", "ALTMAN_UNAVAILABLE_NO_RETAINED_EARNINGS_FIELD")
    add("RSK-ALT-030", v_alt, _score_from_anchor(v_alt, [(0.0, 0), (1.1, 3), (2.6, 6), (4.0, 10)]))

    roa_t = ni_t / assets_t if ni_t is not None and assets_t not in (None, 0) else None
    roa_t1 = _num(prior, "net_income") / assets_t1 if _num(prior, "net_income") is not None and assets_t1 not in (None, 0) else None
    leverage_t = debt_t / assets_t if debt_t is not None and assets_t not in (None, 0) else None
    leverage_t1 = debt_t1 / assets_t1 if debt_t1 is not None and assets_t1 not in (None, 0) else None
    cr_t = ca_t / cl_t if ca_t is not None and cl_t not in (None, 0) else None
    cr_t1 = ca_t1 / cl_t1 if ca_t1 is not None and cl_t1 not in (None, 0) else None
    shares_t, shares_t1 = _num(latest, "diluted_shares"), _num(prior, "diluted_shares")
    at_t = revenue_t / assets_t if revenue_t is not None and assets_t not in (None, 0) else None
    at_t1 = revenue_t1 / assets_t1 if revenue_t1 is not None and assets_t1 not in (None, 0) else None
    v_pio = piotroski_f_score(
        roa_t=roa_t, roa_t1=roa_t1, ocf=ocf_t, ni=ni_t, leverage_t=leverage_t, leverage_t1=leverage_t1,
        current_ratio_t=cr_t, current_ratio_t1=cr_t1, shares_t=shares_t, shares_t1=shares_t1,
        gross_margin_t=gm_t, gross_margin_t1=gm_t1, asset_turnover_t=at_t, asset_turnover_t1=at_t1,
    )
    add("RSK-PIO-031", v_pio, _score_from_anchor(v_pio, [(0.0, 0), (9.0, 10)]))

    valid_diluted = [x for x in (shares_t1, shares_t) if x is not None]
    if len(valid_diluted) == 2:
        v_dil = diluted_share_cagr(valid_diluted[1], valid_diluted[0], 1.0)
    else:
        v_dil = _null(NullState.MISSING, "pct", "DILUTED_SHARE_CAGR_INSUFFICIENT_HISTORY")
    add("RSK-DIL-032", v_dil, None)

    if sbc_t is not None and fcf_t is not None:
        v_sbc = sbc_to_fcf(sbc_t, fcf_t)
    else:
        v_sbc = _null(NullState.MISSING, "ratio", "SBC_TO_FCF_INPUTS_UNAVAILABLE")
    add("RSK-SBC-033", v_sbc, None)

    macro_series, company_series = overlay.get("macro_series"), overlay.get("company_series")
    v_cyc = macro_sensitivity_beta(company_series, macro_series) if macro_series and company_series else _null(NullState.MISSING, "ratio", "MACRO_SENSITIVITY_UNAVAILABLE")
    add("RSK-CYC-034", v_cyc, _score_from_anchor(v_cyc, [(2.0, 0), (1.0, 4), (0.5, 7), (0.0, 10)]) if v_cyc.is_valid else None)

    # ---- RSK-THESIS-035: thesis-killer priority (judgment-only) ----
    # Probability/Impact/Detectability/TimeUrgency are all explicit 0-1
    # assumptions (FORMULAS.md: "Custom prioritization... no false
    # precision"), so this metric is never scored mechanically -- it is a
    # NOT_SCORABLE row paired with the thesis_killers JudgmentRequest below,
    # the same judgment-only discipline as business.py's moat classification.
    # Registered as a row so all 35 RSK formulas surface in out.metrics.
    add("RSK-THESIS-035", _null(NullState.NOT_SCORABLE, "score", "THESIS_KILLER_PRIORITY_JUDGMENT_REQUIRED"), None)

    # ---- Thesis killers (judgment requests, per DECISION_RULES.md: always list >=3) ----
    thesis_killers_overlay = overlay.get("thesis_killers") or []
    thesis_killer_rows: list[dict[str, Any]] = []
    for i, tk in enumerate(thesis_killers_overlay):
        thesis_killer_rows.append(
            {
                "risk": tk.get("risk"), "probability_assumption": tk.get("probability_assumption"),
                "impact": tk.get("impact"), "early_warning_metric": tk.get("early_warning_metric"),
                "trigger_level": tk.get("trigger_level"), "time_horizon": tk.get("time_horizon"),
                "mitigant": tk.get("mitigant"),
            }
        )
    judgment_requests.append(
        JudgmentRequest(
            request_id="risk_analysis:thesis_killers",
            agent_id=AGENT_ID,
            metric_id="thesis_killers",
            question="List at least three risks that could invalidate the thesis, each with "
            "probability_assumption, impact (low|medium|high|catastrophic), early_warning_metric, "
            "trigger_level, time_horizon, and mitigant (DECISION_RULES.md, mandatory).",
            schema_hint="array of >=3 {risk, probability_assumption, impact, early_warning_metric, "
            "trigger_level, time_horizon, mitigant}",
        )
    )

    # ---- Valuation compression (cross-agent input, per SCORING.md: "use the valuation-agent packet") ----
    mos = overlay.get("margin_of_safety")
    v_mos = _ok(mos, unit="pct", evidence_class=EvidenceClass.E) if mos is not None else _null(NullState.MISSING, "pct", "MARGIN_OF_SAFETY_UNAVAILABLE_NO_VALUATION_OVERLAY")
    mos_score = _score_from_anchor(v_mos, [(-0.30, 0), (0.0, 4), (0.15, 7), (0.30, 10)])

    by_id = {r.metric_id: r for r in rows}

    if packet.analysis.industry_adapter != "default_nonfinancial":
        assumptions.append(
            f"industry_adapter={packet.analysis.industry_adapter!r}: forensic screens (Beneish/Altman) "
            "are excluded for financial companies per DECISION_RULES.md; results above should not be trusted."
        )

    metric_rows: list[MetricRow] = []
    for r in rows:
        score: float | str = r.score10 if r.score10 is not None else "NOT_SCORABLE"
        metric_rows.append(MetricRow.from_value(r.metric_id, r.value, formula_id=r.metric_id, formula_version=_VERSION, score=score, confidence=r.confidence))

    # ---- DIM_FINANCING (3 pts) ----
    financing_scores: list[tuple[float, Value]] = []
    for mid in ("RSK-ICOV-011", "RSK-RUN-015", "RSK-MAT-016", "RSK-DFC-014"):
        s = by_id[mid].score10
        financing_scores.append((0.25, Value.of(s, unit="score") if s is not None else Value.null(NullState.NOT_SCORABLE, unit="score")))
    financing_dim = Dimension(name=DIM_FINANCING, max_points=DIMENSION_MAX_POINTS[DIM_FINANCING], metric_scores=financing_scores)

    # ---- DIM_CONCENTRATION (3 pts) ----
    concentration_scores: list[tuple[float, Value]] = [
        (1 / 3, Value.of(cust_score, unit="score") if cust_score is not None else Value.null(NullState.NOT_SCORABLE, unit="score")),
        (1 / 3, Value.of(by_id["RSK-PROD-018"].score10, unit="score") if by_id["RSK-PROD-018"].score10 is not None else Value.null(NullState.NOT_SCORABLE, unit="score")),
        (1 / 3, Value.of(by_id["RSK-GEO-019"].score10, unit="score") if by_id["RSK-GEO-019"].score10 is not None else Value.null(NullState.NOT_SCORABLE, unit="score")),
    ]
    concentration_dim = Dimension(name=DIM_CONCENTRATION, max_points=DIMENSION_MAX_POINTS[DIM_CONCENTRATION], metric_scores=concentration_scores)

    # ---- DIM_EXECUTION_QUALITY (3 pts) ----
    execution_scores: list[tuple[float, Value]] = []
    for mid in ("RSK-ACCR-020", "RSK-MSCR-029", "RSK-ALT-030", "RSK-PIO-031"):
        s = by_id[mid].score10
        execution_scores.append((0.25, Value.of(s, unit="score") if s is not None else Value.null(NullState.NOT_SCORABLE, unit="score")))
    execution_dim = Dimension(name=DIM_EXECUTION_QUALITY, max_points=DIMENSION_MAX_POINTS[DIM_EXECUTION_QUALITY], metric_scores=execution_scores)

    # ---- DIM_REGULATORY_MACRO (2 pts): only RSK-CYC-034 is mechanically scorable ----
    cyc_score = by_id["RSK-CYC-034"].score10
    regulatory_scores: list[tuple[float, Value]] = [
        (1.0, Value.of(cyc_score, unit="score") if cyc_score is not None else Value.null(NullState.NOT_SCORABLE, unit="score")),
    ]
    regulatory_dim = Dimension(name=DIM_REGULATORY_MACRO, max_points=DIMENSION_MAX_POINTS[DIM_REGULATORY_MACRO], metric_scores=regulatory_scores)
    if overlay.get("regulatory_events"):
        judgment_requests.append(
            JudgmentRequest(
                request_id="risk_analysis:regulatory_legal_exposure",
                agent_id=AGENT_ID,
                metric_id="regulatory_legal_exposure",
                question="Assess material regulatory/legal exposure and disclose the probability "
                "assumption explicitly (SCORING.md: qualitative probability must be disclosed).",
                schema_hint="array of {event, impact, probability_assumption}",
            )
        )

    # ---- DIM_VALUATION_COMPRESSION (2 pts) ----
    valuation_scores: list[tuple[float, Value]] = [
        (1.0, Value.of(mos_score, unit="score") if mos_score is not None else Value.null(NullState.NOT_SCORABLE, unit="score")),
    ]
    valuation_dim = Dimension(name=DIM_VALUATION_COMPRESSION, max_points=DIMENSION_MAX_POINTS[DIM_VALUATION_COMPRESSION], metric_scores=valuation_scores)

    # ---- DIM_VOLATILITY_DRAWDOWN (2 pts) ----
    volatility_scores: list[tuple[float, Value]] = []
    for mid, anchors in (
        ("RSK-VOL-001", [(0.15, 10), (0.30, 6), (0.50, 3), (0.80, 0)]),
        ("RSK-MDD-006", None), ("RSK-DBETA-004", None), ("RSK-VAR-008", None),
    ):
        r = by_id[mid]
        s = anchor_score(r.value.value, anchors) if anchors and r.value.is_valid else r.score10
        volatility_scores.append((0.25, Value.of(s, unit="score") if s is not None else Value.null(NullState.NOT_SCORABLE, unit="score")))
    volatility_dim = Dimension(name=DIM_VOLATILITY_DRAWDOWN, max_points=DIMENSION_MAX_POINTS[DIM_VOLATILITY_DRAWDOWN], metric_scores=volatility_scores)

    dimensions = [financing_dim, concentration_dim, execution_dim, regulatory_dim, valuation_dim, volatility_dim]

    cat = Category(name=AGENT_ID, max_points=MAX_POINTS, dimensions=dimensions)
    awarded_points = cat.points()
    dim_score10 = cat.score10()
    coverage = cat.coverage()

    label = capped_verdict(dim_score10, awarded_points)

    category = CategoryStats(
        max_points=MAX_POINTS, awarded_points=awarded_points, score_10=dim_score10,
        confidence=_category_confidence(coverage, packet),
    )
    status = status_from_coverage(coverage)

    passed = 0
    failed = 0
    checks = [
        abs((category.awarded_points or 0.0) - Category(name=AGENT_ID, max_points=MAX_POINTS, dimensions=dimensions).points()) < 1e-6,
        all(row.formula_id for row in metric_rows),
        bool(packet.analysis.knowledge_timestamp),
        category.confidence is not None,
    ]
    for ok in checks:
        passed += 1 if ok else 0
        failed += 0 if ok else 1
    validation_tests = ValidationTestsSummary(passed=passed, failed=failed, warnings=0)

    # Angel dimensiona por riesgo-por-operación; se lee esa clave del overlay y se
    # cae a la vieja por compatibilidad.
    position_size_pct = overlay.get("risk_per_trade_pct", overlay.get("position_size_pct"))

    return RiskOutput(
        agent_id=AGENT_ID,
        status=status,
        security=SecurityRef(ticker=packet.security.ticker, exchange=packet.security.exchange, currency=packet.security.reporting_currency),
        knowledge_timestamp=packet.analysis.knowledge_timestamp,
        category=category,
        verdict=label,
        coverage=coverage,
        dimensions=dimensions,
        metrics=metric_rows,
        mandatory_flags=([SOLVENCY_WARNING] if mandatory_warnings else []) + (["RISK_OVERRIDE_SPECULATIVE_CAP"] if awarded_points <= 4.0 else []),
        assumptions=assumptions,
        judgment_requests=judgment_requests,
        source_lineage=["packet.fundamentals.annual", "packet.market_data.daily", "packet.market_data.benchmark"],
        validation_tests=validation_tests,
        market_risk={"volatility": v_vol.value if v_vol.is_valid else None, "max_drawdown": v_mdd.value if v_mdd.is_valid else None, "var_95_1d": v_var95.value if v_var95.is_valid else None},
        liquidity_and_solvency={"interest_coverage": v_icov.value if v_icov.is_valid else None, "cash_runway_months": v_run.value if v_run.is_valid else None},
        concentrations={"largest_customer_share": largest_customer_share},
        earnings_quality_and_forensics={"beneish_m_score": v_mscr.value if v_mscr.is_valid else None, "altman_z_double_prime": v_alt.value if v_alt.is_valid else None, "piotroski_f_score": v_pio.value if v_pio.is_valid else None},
        regulatory_legal_macro=[],
        valuation_compression={"margin_of_safety": mos},
        thesis_killers=thesis_killer_rows,
        mandatory_warnings=mandatory_warnings,
        profile_fit=profile_fit(position_size_pct),
    )


def _category_confidence(coverage: float, packet: Packet) -> float:
    coverage_component = max(0.0, min(1.0, coverage)) * 100.0
    source_quality = 85.0
    freshness = 100.0 if packet.staleness.get("quarterly_fundamentals", "FRESH") == "FRESH" else 50.0
    consistency = 75.0
    model_fit = 90.0 if packet.analysis.industry_adapter == "default_nonfinancial" else 40.0
    return _confidence_formula(
        coverage=coverage_component, source_quality=source_quality, freshness=freshness,
        consistency=consistency, model_fit=model_fit,
    )
