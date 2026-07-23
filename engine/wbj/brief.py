"""Company brief — plain-Spanish read of the scorecard for the web app.

Four sections, assembled from data already in the packet plus two extra FMP
feeds (earnings calendar + insider trades). Pure functions, no network I/O —
`_build_packet` fetches the inputs, the webapp renders the output.

- `interpretation`: what each 0-10 category means in words + a research
  classification (favorece / neutral / evitar) — a classification with
  evidence, never a buy/sell order (CLAUDE.md).
- `probability`: volatility-based reachability toward each Bull/Base/Bear
  target. Lognormal terminal price at a 12-month horizon under a neutral
  (zero-drift, median = today) assumption: `P(S_T >= K) = Φ(ln(S0/K)/(σ√T))`.
  Separates statistical reachability from the fundamental target view.
- `where`: reuses `targets.narrative` verbatim.
- `watch`: price levels, next catalyst, insiders > $1M, and thesis-killers.

Honesty (Cerebro): every number carries a formula and declared assumptions;
any missing input makes its block NOT_SCORABLE, never imputed. The probability
is a labeled statistical model, not a promise of return.
"""

from __future__ import annotations

import math
from statistics import NormalDist, pstdev

_HORIZON_YEARS = 1.0
_INSIDER_THRESHOLD_USD = 1_000_000.0
_MIN_VOL_SESSIONS = 60


# ============================================================================
# Interpretation
# ============================================================================


def _category_meaning(score10: float | None) -> str:
    if score10 is None:
        return "sin datos suficientes para puntuar"
    if score10 >= 8.0:
        return "muy fuerte"
    if score10 >= 6.5:
        return "sólido"
    if score10 >= 5.0:
        return "mixto"
    if score10 >= 3.5:
        return "débil"
    return "problemático"


def _classification(overall_10: float | None) -> tuple[str | None, str]:
    if overall_10 is None:
        return None, "sin data suficiente para una conclusión de inversión"
    if overall_10 >= 7.0:
        return "favorece", "el análisis favorece invertir (clasificación de research)"
    if overall_10 >= 5.0:
        return "neutral", "señal mixta — ni claramente a favor ni en contra"
    return "evitar", "el análisis favorece evitar por ahora (clasificación de research)"


def _interpretation(packet: dict, scorecard: dict, next_earnings: dict | None) -> dict:
    overall = scorecard.get("overall_10")
    key, label = _classification(overall)
    revisit = None
    if key == "evitar":
        if next_earnings and next_earnings.get("date"):
            revisit = f"revisitar en el próximo earnings ({next_earnings['date']})"
        else:
            revisit = "revisitar con el próximo 10-K/10-Q o cambio material de estimados"
    categories = [
        {
            "key": r["key"],
            "label": r["label"],
            "score10": r.get("score10"),
            "meaning": _category_meaning(r.get("score10")),
        }
        for r in scorecard.get("categories", [])
    ]
    return {
        "overall_10": overall,
        "classification": key,
        "classification_label": label,
        "revisit": revisit,
        "categories": categories,
    }


# ============================================================================
# Probability (volatility-based lognormal reachability)
# ============================================================================


def _closes_chrono(ohlcv: list[dict] | None) -> list[float]:
    if not ohlcv:
        return []
    rows = sorted(ohlcv, key=lambda r: r.get("date", ""))
    out: list[float] = []
    for r in rows:
        c = r.get("adjClose", r.get("close"))
        if c is not None:
            out.append(float(c))
    return out


def annualized_vol(closes: list[float]) -> float | None:
    """Annualized volatility = stdev(daily log returns) * sqrt(252).

    Needs at least `_MIN_VOL_SESSIONS` sessions; None below that.
    """
    if len(closes) < _MIN_VOL_SESSIONS:
        return None
    rets = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0 and closes[i] > 0
    ]
    if len(rets) < _MIN_VOL_SESSIONS - 1:
        return None
    return pstdev(rets) * math.sqrt(252)


def prob_reach(s0: float, k: float, sigma: float, t: float = _HORIZON_YEARS) -> float:
    """P(price >= K at horizon t) under a lognormal, zero-drift (median = S0)
    model: `Φ(ln(S0/K) / (σ·√t))`. K == S0 -> 0.50."""
    return NormalDist().cdf(math.log(s0 / k) / (sigma * math.sqrt(t)))


def _probability(packet: dict, targets: dict) -> dict:
    if targets.get("status") != "ok":
        return {"status": "not_scorable",
                "reason": targets.get("reason", "sin targets de precio")}
    s0 = targets.get("price")
    closes = _closes_chrono((packet.get("market_data") or {}).get("ohlcv"))
    sigma = annualized_vol(closes)
    if not s0 or sigma is None or sigma == 0:
        return {"status": "not_scorable",
                "reason": "historial de precio insuficiente para estimar volatilidad"}

    rows = []
    for s in targets["scenarios"]:
        rows.append({
            "key": s["key"], "label": s["label"], "target": s["target"],
            "prob_reach": round(prob_reach(s0, s["target"], sigma), 3),
        })

    # Modal zone across the three sorted targets (partition of the terminal
    # distribution into below-low / low-mid / mid-high / above-high).
    ordered = sorted(targets["scenarios"], key=lambda s: s["target"])
    lo, mid, hi = (ordered[0], ordered[1], ordered[2])
    p_lo = prob_reach(s0, lo["target"], sigma)
    p_mid = prob_reach(s0, mid["target"], sigma)
    p_hi = prob_reach(s0, hi["target"], sigma)
    zones = [
        (1 - p_lo, f"por debajo de {lo['label']} (${lo['target']:,.0f})"),
        (p_lo - p_mid, f"entre {lo['label']} y {mid['label']}"),
        (p_mid - p_hi, f"entre {mid['label']} y {hi['label']}"),
        (p_hi, f"por encima de {hi['label']} (${hi['target']:,.0f})"),
    ]
    modal_prob, modal_zone = max(zones, key=lambda z: z[0])

    return {
        "status": "ok",
        "volatility": round(sigma, 4),
        "horizon": targets.get("horizon", "12 meses"),
        "assumptions": (
            f"volatilidad histórica {sigma:.0%} anual · horizonte 12 meses · "
            "sin sesgo direccional (mediana = precio actual)"
        ),
        "targets": rows,
        "modal_zone": modal_zone,
        "modal_prob": round(modal_prob, 3),
    }


# ============================================================================
# Watch-list
# ============================================================================


def _latest(series: list[dict]) -> float | None:
    return series[-1]["val"] if series else None


def _levels(targets: dict) -> dict:
    if targets.get("status") != "ok":
        return {"status": "not_scorable",
                "reason": targets.get("reason", "sin precio de mercado")}
    by = {s["key"]: s for s in targets["scenarios"]}
    return {
        "status": "ok",
        "entrada": targets["price"],
        "invalidacion": by["bear"]["target"] if "bear" in by else None,
        "salida_base": by["base"]["target"] if "base" in by else None,
        "salida_bull": by["bull"]["target"] if "bull" in by else None,
        "nota": "niveles de referencia (research), no una orden de compra/venta",
    }


def _next_earnings(earnings: list[dict] | None, as_of: str) -> dict | None:
    future = [e for e in (earnings or []) if e.get("date", "") > as_of]
    return min(future, key=lambda e: e["date"]) if future else None


def _catalysts(earnings: list[dict] | None, as_of: str) -> dict:
    row = _next_earnings(earnings, as_of)
    if not row:
        return {"next_earnings": None}
    return {"next_earnings": {
        "date": row.get("date"),
        "eps_est": row.get("epsEstimated"),
        "rev_est": row.get("revenueEstimated"),
    }}


def _insider_highlights(trades: list[dict] | None,
                        threshold: float = _INSIDER_THRESHOLD_USD) -> list[dict]:
    """Form-4 rows whose transaction value exceeds `threshold`, newest first.

    Value = shares * price (gifts/awards carry price 0 -> value 0 -> dropped).
    Side from `transactionType`: "P…" purchase (compra), "S…" sale (venta).
    """
    out = []
    for t in (trades or []):
        shares = t.get("securitiesTransacted") or 0
        price = t.get("price") or 0
        value = shares * price
        if value <= threshold:
            continue
        ttype = (t.get("transactionType") or "").upper()
        side = "compra" if ttype.startswith("P") else "venta" if ttype.startswith("S") else "otro"
        if side == "otro":
            continue
        out.append({
            "name": t.get("reportingName"),
            "side": side,
            "value": round(value, 2),
            "shares": shares,
            "price": price,
            "date": t.get("transactionDate") or t.get("filingDate"),
        })
    out.sort(key=lambda r: r.get("date") or "", reverse=True)
    return out[:6]


def _notable_holders(rows: list[dict] | None, limit: int = 8) -> list[dict]:
    """Recognized 13F investors holding the name, largest position first.

    Passthrough of `EdgarProvider.notable_holders` output, trimmed to the top
    `limit` and shaped for the report card. Empty list is a valid answer (no
    recognized investor from the registry holds the name) — never fabricated.
    """
    out = []
    for r in (rows or []):
        if (r.get("value") or 0) <= 0:
            continue
        out.append({
            "investor": r.get("investor"),
            "value": r.get("value"),
            "shares": r.get("shares"),
            "options": r.get("options") or 0,
            "as_of": r.get("as_of"),
        })
    return out[:limit]


def _risks(packet: dict, scorecard: dict) -> list[str]:
    a = packet["annual"]
    rev, ni = a["revenue"], a["net_income"]
    ocf, capex = a["operating_cash_flow"], a["capex"]
    debt, eq = a["long_term_debt"], a["equity"]
    out: list[str] = []

    debt_l, eq_l = _latest(debt), _latest(eq)
    if debt_l is not None and eq_l:
        de = debt_l / eq_l
        if de >= 2.0:
            out.append(f"Deuda alta: debe ${de:,.1f} por cada $1 de capital propio.")

    ocf_l, capex_l = _latest(ocf), _latest(capex)
    if ocf_l is not None and capex_l is not None and (ocf_l - capex_l) < 0:
        out.append("Quema efectivo: flujo de caja libre negativo — vigilar financiamiento.")

    if len(ni) >= 2 and rev and len(rev) >= 2 and rev[-1]["val"] and rev[-2]["val"]:
        m_now = ni[-1]["val"] / rev[-1]["val"]
        m_prev = ni[-2]["val"] / rev[-2]["val"]
        if m_now < m_prev - 0.02:
            out.append(f"Márgenes cayendo: margen neto bajó de {m_prev:.0%} a {m_now:.0%}.")

    if rev and len(rev) >= 2 and rev[-2]["val"]:
        g = rev[-1]["val"] / rev[-2]["val"] - 1
        if g < 0:
            out.append(f"Ventas cayendo: ingresos {g:+.0%} en el último año fiscal.")

    risk_row = next((r for r in scorecard.get("categories", []) if r["key"] == "risk"), None)
    if risk_row and risk_row.get("score10") is not None and risk_row["score10"] < 5.0:
        out.append(f"Score de riesgo bajo ({risk_row['score10']}/10): resiliencia frágil.")

    return out[:4]


# ============================================================================
# Public entry point
# ============================================================================


def company_brief(packet: dict, scorecard: dict, targets: dict) -> dict:
    """Assemble the four-section plain-Spanish brief from already-computed
    scorecard + targets and the packet's market_data feeds."""
    from wbj.targets import narrative

    md = packet.get("market_data") or {}
    as_of = packet.get("as_of", "")
    next_earn = _next_earnings(md.get("earnings"), as_of)

    return {
        "interpretation": _interpretation(packet, scorecard, next_earn),
        "probability": _probability(packet, targets),
        "where": narrative(packet, scorecard, targets),
        "watch": {
            "levels": _levels(targets),
            "catalysts": _catalysts(md.get("earnings"), as_of),
            "insiders": _insider_highlights(md.get("insiders")),
            "notable_holders": _notable_holders(md.get("notable_holders")),
            "risks": _risks(packet, scorecard),
        },
    }
