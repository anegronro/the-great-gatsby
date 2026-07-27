"""WBJ mini web app: search any US-listed company, analyze on demand.

Usage: .venv/bin/python scripts/webapp.py  ->  http://localhost:8765
Stdlib http.server only — no extra dependencies.
Card-dashboard UI (light, big numbers, plain-Spanish explanations) modeled
on the reference screenshots in Referencias/.
"""

from __future__ import annotations

import json
import math
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from wbj.cli import _build_packet, _compute
from wbj.config import load_settings
from wbj.providers.cache import Cache
from wbj.providers.edgar import (
    _EDGAR_HEADERS,
    _GLOBAL_CACHE_TICKER,
    _MAX_AGE_TICKERS,
    TICKERS_URL,
    EdgarProvider,
)
from wbj.screener import screen as run_screen
from wbj.brief import company_brief
from wbj.order import build_ticket
from wbj.providers.robinhood import OrderRefused, RobinhoodProvider
from wbj.targets import live_price, narrative, price_history, price_targets

# Bind address/port. Default localhost-only; `WBJ_HOST` (p.ej. la IP de Tailscale)
# lo abre al tailnet para el teléfono sin exponerlo al WiFi público. Ver
# scripts/serve_tailnet.sh.
HOST = os.environ.get("WBJ_HOST", "127.0.0.1")
PORT = int(os.environ.get("WBJ_PORT", "8765"))
# Modo solo-análisis: `WBJ_READONLY=1` apaga la colocación de órdenes. Se usa en
# el despliegue del VPS (24/7) — el análisis sí, pero el gatillo de dinero real
# NUNCA vive en un servidor de nube; las órdenes se colocan solo desde la Mac.
READONLY = bool(os.environ.get("WBJ_READONLY"))
_lock = threading.Lock()

# Capital del perfil (Perfil Inversionista/Angel Negron.md) — se usa para el
# PREVIEW del ticket cuando Robinhood aún no está configurado. En la ejecución
# real el tamaño se recalcula contra el poder de compra vivo de la cuenta.
PROFILE_CAPITAL = 1000.0

settings = load_settings()
edgar = EdgarProvider(settings, Cache(settings.cache_dir))
rh_provider = RobinhoodProvider(settings)


def _order_preview(ticker: str, brief: dict) -> dict | None:
    """Build a BUY ticket from the research levels (entrada=entry, invalidación=
    stop). Long-only, favorable-only. Returns None when the research doesn't
    favor a clean long. Preview only — never places."""
    interp = (brief or {}).get("interpretation") or {}
    # Only pre-fill an order when the research classification favors entering.
    if interp.get("classification") != "favorece":
        return None
    lv = ((brief or {}).get("watch") or {}).get("levels") or {}
    if lv.get("status") != "ok":
        return None

    # Reflect the live account when Robinhood is connected, so funding shows up
    # in the panel automatically; otherwise fall back to the profile capital.
    # In read-only mode (VPS) never touch Robinhood.
    buying_power = (rh_provider.buying_power()
                    if rh_provider.available and not READONLY else None)
    ticket = _size_for_account(ticker, lv.get("entrada"), lv.get("invalidacion"),
                               buying_power)
    out = ticket.as_dict()
    out["equity_basis"] = (
        f"cuenta margin (vivo): ${buying_power:,.2f}" if buying_power is not None
        else "perfil $1,000 (preview)"
    )
    return out


def _size_for_account(ticker: str, entry, stop, buying_power: float | None):
    """Size a BUY ticket by Angel's rule, shared by preview and placement.

    Risk is 3% of $1,000 (or of the live buying power if smaller) — never of a
    larger balance; buying power only caps the notional. `buying_power=None`
    (Robinhood not connected / read failed) falls back to the profile capital.
    """
    risk_base = min(PROFILE_CAPITAL, buying_power) if buying_power else PROFILE_CAPITAL
    cap_notional = buying_power or PROFILE_CAPITAL
    return build_ticket(ticker, "buy", entry=entry, stop=stop,
                        equity=risk_base, risk_frac=0.03, max_notional=cap_notional)


def ticker_map() -> list[dict]:
    payload = edgar.get_json(
        TICKERS_URL, {}, "tickers", _GLOBAL_CACHE_TICKER,
        max_age_days=_MAX_AGE_TICKERS, headers=_EDGAR_HEADERS,
    )
    if not isinstance(payload, dict):
        return []
    return [e for e in payload.values() if isinstance(e, dict)]


def search(q: str, limit: int = 8) -> list[dict]:
    q = q.strip().upper()
    if not q:
        return []
    exact, prefix, name = [], [], []
    for e in ticker_map():
        t = str(e.get("ticker", "")).upper()
        n = str(e.get("title", "")).upper()
        row = {"ticker": t, "name": e.get("title", "")}
        if t == q:
            exact.append(row)
        elif t.startswith(q):
            prefix.append(row)
        elif q in n:
            name.append(row)
    return (exact + prefix + name)[:limit]


def _history(packet: dict) -> list[dict]:
    """Yearly revenue + net margin for the charts."""
    rev = {r["end"]: r["val"] for r in packet["annual"]["revenue"]}
    ni = {r["end"]: r["val"] for r in packet["annual"]["net_income"]}
    rows = []
    for end in sorted(rev)[-6:]:
        rows.append({
            "year": end[:4],
            "revenue": rev[end],
            "margin": (ni[end] / rev[end]) if end in ni and rev[end] else None,
        })
    return rows


def analyze(ticker: str) -> dict:
    from datetime import date

    from wbj.memoria import save_prediction

    packet = _build_packet(ticker)
    result = _compute(packet)
    price = live_price(ticker, fmp_api_key=settings.fmp_api_key)
    targets = price_targets(packet, price)
    # Seed agent memory: every web analysis also records its prediction.
    save_prediction(settings.reports_dir, ticker, date.today(),
                    result["scorecard"], targets)
    result["targets"] = targets
    result["narrative"] = narrative(packet, result["scorecard"], targets)
    result["brief"] = company_brief(packet, result["scorecard"], targets)
    result["history"] = _history(packet)
    result["chart"] = price_history(ticker)
    result["order"] = _order_preview(ticker, result["brief"])
    result["rh_configured"] = rh_provider.available and not READONLY
    result["readonly"] = READONLY
    return result


def place_order(payload: dict) -> dict:
    """Place a confirmed BUY-with-stop on Robinhood — REAL MONEY.

    Re-sizes against live buying power (never trusts a client-sent qty) and
    re-validates via `build_ticket` before touching the broker. The human's
    confirm flag must be exactly True; anything else refuses. Returns a JSON-safe
    status dict, never raises out to the handler.
    """
    ticker = str(payload.get("ticker", "")).upper().strip()
    entry = payload.get("entry")
    stop = payload.get("stop")
    confirm = payload.get("confirm") is True

    if READONLY:
        return {"ok": False, "status": "readonly",
                "note": "Modo solo análisis (VPS). Las órdenes reales se colocan "
                        "desde la app en tu Mac. No se envió nada."}

    if not rh_provider.available:
        return {"ok": False, "status": "not_configured",
                "note": "Robinhood no está configurado (faltan RH_USERNAME/"
                        "RH_PASSWORD en API/.env). No se envió nada."}

    # Same sizing as the preview: risk 3% of $1,000 (or less if buying power is
    # smaller), never more; buying power caps the notional. Read live so funding
    # is reflected automatically.
    ticket = _size_for_account(ticker, entry, stop, rh_provider.buying_power())
    if not ticket.tradeable:
        return {"ok": False, "status": "not_tradeable", "note": ticket.reason}

    try:
        res = rh_provider.place_long_with_stop(
            ticker, ticket.qty, float(entry), float(stop), confirm=confirm)
    except OrderRefused as e:
        return {"ok": False, "status": "refused", "note": str(e)}
    except Exception as e:  # broker/transport — report, keep server alive
        return {"ok": False, "status": "error", "note": str(e)}

    res["ok"] = res.get("status") == "placed"
    res["qty"] = ticket.qty
    res["entry"] = ticket.entry
    res["stop"] = ticket.stop
    res["risk_dollars"] = ticket.risk_dollars
    return res


def _emblem_svg(attrs: str = "") -> str:
    """Art Déco 'G' emblem: ink coin, gold sunburst ring, chevrons, green light.

    Rays are baked (no JS) so the SAME markup serves as inline header logo and
    as the /favicon.svg. `attrs` lets callers add class/size."""
    cx = cy = 50
    rays = ""
    n, r1, r2 = 48, 37, 43
    for i in range(n):
        a = i / n * 2 * math.pi
        x1, y1 = cx + r1 * math.cos(a), cy + r1 * math.sin(a)
        x2, y2 = cx + r2 * math.cos(a), cy + r2 * math.sin(a)
        op = 0.9 if i % 2 else 0.5
        rays += (f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                 f'stroke="#C6A15B" stroke-width="0.7" opacity="{op}"/>')
    chev = ""
    for s in (1, -1):
        y = cy - s * 22
        chev += (f'<path d="M{cx-9} {y} l4 {-3*s} l5 0 l0 {-4*s} l0 {4*s} l5 0 '
                 f'l4 {3*s}" fill="none" stroke="#C6A15B" stroke-width="1.1"/>')
    return (
        f'<svg {attrs} viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="The Great Gatsby">'
        f'<circle cx="50" cy="50" r="47" fill="#1C1710"/>'
        f'<circle cx="50" cy="50" r="44" fill="none" stroke="#C6A15B" stroke-width="0.9"/>'
        f'<circle cx="50" cy="50" r="33" fill="none" stroke="#C6A15B" stroke-width="0.5" opacity="0.55"/>'
        f'{rays}{chev}'
        f'<text x="50" y="63" text-anchor="middle" '
        f"font-family=\"Georgia,'Times New Roman',serif\" font-size=\"42\" fill=\"#F4ECD6\">G</text>"
        f'<circle cx="50" cy="28" r="1.7" fill="#1E6B54"/>'
        f'</svg>'
    )


FAVICON_SVG = _emblem_svg()
EMBLEM_INLINE = _emblem_svg('class="emblem"')


PAGE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The Great Gatsby — Scorecard</title>
<style>
  :root { color-scheme: light;
    /* Paleta Art Déco "The Great Gatsby": crema, tinta cálida, oro y el verde
       de la luz. --purple es el acento primario (botones/enlaces) = ORO. */
    --page:#F2E8CF; --card:#FBF6E8; --ink:#1C1710; --ink2:#6E6046; --muted:#A9997C;
    --grid:#E9DEC2; --green:#1E6B54; --green-bg:#E4EDE1; --purple:#A9822A;
    --purple-bg:#F1E6C6; --orange:#BC841F; --orange-bg:#F3E8CB; --red:#A33A34;
    --red-bg:#F1DDD6; --blue:#37766B; --blue-bg:#E1EBE6; --gold:#C6A15B; }
  * { margin:0; box-sizing:border-box; }
  body { background:var(--page); color:var(--ink);
    font-family:system-ui,-apple-system,"Segoe UI",sans-serif; padding:36px 20px 60px; }
  .wrap { max-width:1040px; margin:0 auto; }
  .kicker { font-size:12px; letter-spacing:.14em; text-transform:uppercase;
    color:var(--muted); font-weight:600; margin-bottom:6px; }
  h1 { font-size:26px; margin-bottom:16px; letter-spacing:-.02em; }
  .topbar { display:flex; gap:12px; align-items:stretch; max-width:760px; }
  .searchbox { position:relative; flex:1; max-width:560px; }
  .discover { font:inherit; font-size:14.5px; font-weight:700; padding:0 22px;
    border:0; border-radius:14px; background:var(--purple); color:#fff; cursor:pointer;
    box-shadow:0 1px 3px rgba(20,22,30,.15); white-space:nowrap; }
  .discover:hover { filter:brightness(1.08); }
  table.scr { width:100%; border-collapse:collapse; margin-top:14px; font-size:13.5px; }
  table.scr th { text-align:left; font-size:11.5px; text-transform:uppercase;
    letter-spacing:.08em; color:var(--muted); padding:0 10px 10px 0; font-weight:600; }
  table.scr td { padding:11px 10px 11px 0; border-top:1px solid var(--grid);
    font-variant-numeric:tabular-nums; }
  table.scr td.nm { max-width:230px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  table.scr .tk { font-weight:800; cursor:pointer; color:var(--purple); }
  table.scr .num { text-align:right; }
  .scorepill { display:inline-flex; align-items:center; gap:8px; }
  .scorepill .mini { width:64px; height:7px; background:var(--grid); border-radius:5px; overflow:hidden; }
  .scorepill .mini i { display:block; height:100%; background:var(--green); border-radius:5px; }
  input { width:100%; font:inherit; font-size:16px; padding:14px 18px;
    border-radius:14px; border:1px solid transparent; background:var(--card);
    color:var(--ink); outline:none; box-shadow:0 1px 3px rgba(20,22,30,.07); }
  input:focus { border-color:var(--purple); }
  .sugg { position:absolute; top:calc(100% + 8px); left:0; right:0; z-index:5;
    background:var(--card); border-radius:14px; overflow:hidden;
    box-shadow:0 12px 32px rgba(20,22,30,.16); display:none; }
  .sugg button { display:flex; gap:12px; width:100%; text-align:left; font:inherit;
    font-size:14px; padding:12px 18px; border:0; background:none; color:var(--ink);
    cursor:pointer; border-bottom:1px solid var(--grid); }
  .sugg button:last-child { border-bottom:none; }
  .sugg button:hover { background:var(--grid); }
  .sugg b { min-width:56px; } .sugg span { color:var(--ink2); }
  #status { margin:20px 2px; color:var(--ink2); font-size:14px; }
  .grid { display:grid; grid-template-columns:repeat(12, 1fr); gap:18px;
    margin-top:22px; display:none; }
  .card { background:var(--card); border-radius:18px; padding:24px;
    box-shadow:0 1px 3px rgba(20,22,30,.06); }
  .c-hero { grid-column:span 7; } .c-words { grid-column:span 5; }
  .c-chart { grid-column:span 12; background:#1A160D; color:#EFE7D2; }
  .c-score { grid-column:span 5; } .c-target { grid-column:span 7; }
  .c-brief { grid-column:span 12; }
  @media (max-width:860px) { .c-hero,.c-words,.c-chart,.c-score,.c-target { grid-column:span 12; } }
  /* --- company brief panel --- */
  .brief-grid { display:grid; grid-template-columns:repeat(12,1fr); gap:18px; margin-top:6px; }
  .brief-col { grid-column:span 6; } .brief-col.full { grid-column:span 12; }
  @media (max-width:860px) { .brief-col { grid-column:span 12; } }
  .bh { font-size:13px; font-weight:800; text-transform:uppercase; letter-spacing:.04em;
    color:var(--ink2); margin:2px 0 12px; }
  .classpill { display:inline-flex; align-items:center; gap:8px; padding:8px 14px;
    border-radius:999px; font-weight:800; font-size:15px; margin-bottom:6px; }
  .classpill.favorece { background:var(--green-bg); color:var(--green); }
  .classpill.neutral { background:var(--orange-bg); color:var(--orange); }
  .classpill.evitar { background:var(--red-bg); color:var(--red); }
  .revisit { font-size:13px; color:var(--ink2); margin:4px 0 14px; }
  .cat-row { display:grid; grid-template-columns:1fr auto; gap:10px; padding:7px 0;
    border-top:1px solid var(--grid); font-size:14px; }
  .cat-row .mn { font-weight:700; }
  .cat-row .mn.ns { color:var(--muted); font-weight:600; }
  .probbar { display:flex; height:26px; border-radius:8px; overflow:hidden; margin:4px 0 12px;
    border:1px solid var(--grid); }
  .probbar i { display:block; }
  .prow { display:grid; grid-template-columns:70px 1fr 54px; gap:10px; align-items:center;
    font-size:14px; padding:5px 0; }
  .prow .tag { font-weight:800; } .prow .tag.bull { color:var(--green); }
  .prow .tag.base { color:var(--blue); } .prow .tag.bear { color:var(--red); }
  .prow .pv { text-align:right; font-weight:800; }
  .prow .pt { color:var(--ink2); }
  .modal { font-size:14px; margin:8px 0 4px; }
  .modal b { color:var(--ink); }
  .levels { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin:6px 0 10px; }
  .lvl { background:var(--page); border-radius:12px; padding:12px 14px; min-width:0; }
  .lvl.inval { background:var(--red-bg); }
  .lvl.salida { background:var(--green-bg); }
  .lvl .k { font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--ink2); }
  .lvl .v { font-size:20px; font-weight:800; margin-top:2px; }
  .lvl.entrada .v { color:var(--ink); } .lvl.inval .v { color:var(--red); }
  .lvl.salida .v { color:#12543C; }
  .wl { list-style:none; margin:2px 0 10px; padding:0; }
  .wl li { padding:7px 0; border-top:1px solid var(--grid); font-size:14px; display:flex;
    justify-content:space-between; gap:10px; }
  .ins-buy { color:var(--green); font-weight:800; } .ins-sell { color:var(--red); font-weight:800; }
  .risk li::before { content:"⚠ "; color:var(--orange); }
  .brief-note { font-size:12px; color:var(--muted); margin-top:6px; }
  /* --- Order entry (Robinhood, dinero real) --- */
  .rh-banner { background:var(--red-bg); border:1px solid #f4c7c9; color:#a3282c;
    border-radius:12px; padding:12px 14px; font-size:13px; font-weight:700;
    display:flex; gap:8px; align-items:flex-start; margin:4px 0 16px; }
  .ticket { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:8px 0 16px; }
  @media (max-width:720px){ .ticket { grid-template-columns:repeat(2,1fr); } }
  .tk { background:var(--grid); border-radius:12px; padding:12px 14px; }
  .tk .k { font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--ink2); font-weight:700; }
  .tk .v { font-size:22px; font-weight:800; letter-spacing:-.01em; margin-top:2px; }
  .tk.buy .v { color:var(--green); } .tk.stop .v { color:var(--red); }
  .rh-confirm { display:flex; gap:10px; align-items:flex-start; font-size:13.5px;
    color:var(--ink); margin:2px 0 14px; cursor:pointer; }
  .rh-confirm input { width:20px; height:20px; margin-top:1px; accent-color:var(--red); }
  .rh-send { font:inherit; font-size:15px; font-weight:800; padding:13px 24px; border:0;
    border-radius:13px; background:#c1c6cf; color:#fff; cursor:not-allowed; width:100%; }
  .rh-send.armed { background:var(--red); cursor:pointer; }
  .rh-result { margin-top:14px; font-size:14px; padding:12px 14px; border-radius:12px; }
  .rh-result.ok { background:var(--green-bg); color:#1a7a52; }
  .rh-result.warn { background:var(--orange-bg); color:#9a6a12; }
  .rh-result.err { background:var(--red-bg); color:#a3282c; }
  .rh-note { font-size:12px; color:var(--muted); margin-top:8px; }
  .c-chart h2 { color:#F4ECD6; } .c-chart .sub { color:#B4A483; }
  .chart-head { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; margin:8px 0 4px; }
  .chart-head .px { font-size:34px; font-weight:800; letter-spacing:-.02em; }
  .chart-head .chg { font-size:14px; font-weight:700; }
  .chart-head .chg.up { color:#26d07c; } .chart-head .chg.down { color:#ff5a5f; }
  .chart-head .rng { color:#B4A483; font-size:13px; }
  #tvchart { height:340px; margin-top:10px; }
  .periods { display:flex; gap:8px; margin-top:12px; }
  .periods button { font:inherit; font-size:13px; font-weight:700; padding:6px 16px;
    border:0; border-radius:99px; background:transparent; color:#B4A483; cursor:pointer; }
  .periods button.on { background:#1d2b22; color:#26d07c; }
  .tglegend { display:flex; gap:18px; margin-top:10px; font-size:12.5px; flex-wrap:wrap; }
  .tglegend span { display:inline-flex; align-items:center; gap:7px; color:#aab1bb; }
  .tglegend i { width:18px; border-top:2px dashed; display:inline-block; }
  .card h2 { font-size:16px; font-weight:700; }
  .card .sub { color:var(--muted); font-size:12.5px; margin-top:3px; }
  .hero-num { display:flex; align-items:baseline; gap:12px; margin:16px 0 4px; }
  .hero-num .n { font-size:38px; font-weight:800; letter-spacing:-.03em; }
  .hero-num .u { font-size:13px; color:var(--ink2); }
  .chip { font-size:12.5px; font-weight:700; padding:3px 9px; border-radius:99px; }
  .chip.up { color:var(--green); background:var(--green-bg); }
  .chip.down { color:var(--red); background:var(--red-bg); }
  .bars { display:flex; align-items:flex-end; gap:14px; height:130px; margin-top:18px; }
  .bar { flex:1; display:flex; flex-direction:column; justify-content:flex-end;
    align-items:center; gap:6px; height:100%; }
  .bar .stick { width:100%; max-width:38px; border-radius:8px 8px 4px 4px;
    background:var(--purple); transition:height .5s ease; }
  .bar.last .stick { background:var(--green); }
  .bar .y { font-size:11.5px; color:var(--muted); }
  .bar .v { font-size:11.5px; color:var(--ink2); font-weight:600; }
  .legend { display:flex; gap:16px; margin-top:12px; font-size:12.5px; color:var(--ink2); }
  .legend i { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; }
  ul.words { list-style:none; margin-top:14px; }
  ul.words li { display:flex; gap:10px; padding:9px 0; font-size:14px; line-height:1.5;
    border-bottom:1px solid var(--grid); }
  ul.words li:last-child { border-bottom:none; }
  ul.words .dot { flex:none; width:8px; height:8px; border-radius:50%; margin-top:7px; }
  .gaugebox { display:flex; align-items:center; gap:6px; flex-direction:column; margin:6px 0 12px; }
  .gauge { width:190px; height:110px; }
  .gauge .track { stroke:var(--grid); } .gauge .arc { stroke:var(--green); }
  .gauge-num { font-size:34px; font-weight:800; margin-top:-58px; }
  .gauge-of { font-size:12px; color:var(--muted); margin-bottom:16px; }
  .srow { display:grid; grid-template-columns:150px 1fr 52px; gap:10px;
    align-items:center; margin-bottom:10px; font-size:13px; }
  .srow .nm { color:var(--ink2); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .srow .v { text-align:right; font-weight:700; font-variant-numeric:tabular-nums; }
  .track2 { height:9px; background:var(--grid); border-radius:6px; overflow:hidden; }
  .fill2 { height:100%; border-radius:6px; transition:width .5s ease; }
  .srow.ns .nm,.srow.ns .v { color:var(--muted); font-weight:500; }
  .price-now { display:flex; align-items:baseline; gap:10px; margin:14px 0 4px; }
  .price-now .n { font-size:30px; font-weight:800; }
  .price-now .u { font-size:12.5px; color:var(--muted); }
  .trow { display:grid; grid-template-columns:86px 1fr 96px 90px; gap:12px;
    align-items:center; padding:13px 0; border-bottom:1px solid var(--grid); }
  .trow:last-of-type { border-bottom:none; }
  .tag { font-size:12.5px; font-weight:700; padding:4px 0; border-radius:10px; text-align:center; }
  .tag.bull { color:var(--green); background:var(--green-bg); }
  .tag.base { color:var(--blue); background:var(--blue-bg); }
  .tag.bear { color:var(--red); background:var(--red-bg); }
  .trow .as { font-size:12px; color:var(--muted); line-height:1.45; }
  .trow .tp { font-size:19px; font-weight:800; text-align:right; font-variant-numeric:tabular-nums; }
  .trow .up { text-align:right; }
  /* Móvil: la cuadrícula de 4 columnas fijas no cabe; los supuestos se
     aplastaban. Reordenar a números arriba y supuestos a lo ancho debajo. */
  @media (max-width:600px) {
    .trow { grid-template-columns:auto 1fr auto;
      grid-template-areas:"tag price up" "as as as"; gap:6px 10px; }
    .trow .tag { grid-area:tag; align-self:center; padding:4px 14px; }
    .trow .tp { grid-area:price; align-self:center; }
    .trow .up { grid-area:up; align-self:center; }
    .trow .as { grid-area:as; }
    /* Niveles de precio: caben 3 en fila sin desbordar (números más chicos). */
    .levels { gap:8px; }
    .lvl { padding:10px 10px; }
    .lvl .k { font-size:10px; }
    .lvl .v { font-size:15.5px; }
    /* Encabezado de resultados: la marca en una fila, el buscador debajo a lo
       ancho — antes empujaba la página a lo ancho (scroll horizontal). */
    .app.results .hero { flex-wrap:wrap; gap:12px; }
    .app.results .topbar { flex-basis:100%; }
  }
  .range { position:relative; height:54px; margin:18px 6px 2px; }
  .range .line { position:absolute; top:24px; left:0; right:0; height:8px;
    border-radius:6px; background:linear-gradient(90deg, var(--red-bg), var(--grid), var(--green-bg)); }
  .range .pt { position:absolute; top:20px; width:16px; height:16px; border-radius:50%;
    border:3px solid #fff; box-shadow:0 1px 4px rgba(0,0,0,.25); transform:translateX(-50%); }
  .range .lb { position:absolute; top:0; font-size:11px; font-weight:700; transform:translateX(-50%); white-space:nowrap; }
  .range .lb.below { top:auto; bottom:0; font-weight:600; color:var(--ink2); }
  .foot { margin-top:22px; color:var(--muted); font-size:12px; line-height:1.6; max-width:760px; }
  .spin { display:inline-block; width:14px; height:14px; border:2px solid var(--grid);
    border-top-color:var(--purple); border-radius:50%; animation:r .7s linear infinite;
    vertical-align:-2px; margin-right:7px; }
  @keyframes r { to { transform:rotate(360deg); } }
  /* --- Estados de la app: landing (bienvenida centrada) vs results --- */
  .hero { transition: all .38s cubic-bezier(.4,0,.2,1); }
  .subtitle { font-size:17px; color:var(--ink2); margin:2px 0 0; letter-spacing:-.01em; }
  /* --- Logo Art Déco: emblema + wordmark (apilado en landing, en línea en results) --- */
  #brand { cursor:default; display:flex; align-items:center; gap:16px; }
  .brand .emblem { flex:none; display:block; }
  .wm-the, .wm-big, .wm-line { font-family:Georgia,'Times New Roman',serif;
    color:var(--ink); font-weight:400; display:block; }
  .wm-the { letter-spacing:.44em; color:#3a3327; }
  .wm-big { letter-spacing:.06em; line-height:1.0; }
  .wm-line { letter-spacing:.20em; }
  /* Landing: bloque de bienvenida centrado en la ventana */
  .app.landing { min-height:calc(100vh - 96px); display:flex; flex-direction:column;
    justify-content:center; }
  .app.landing .hero { text-align:center; }
  .app.landing .kicker { text-align:center; margin-bottom:10px; }
  .app.landing #brand { flex-direction:column; gap:14px; margin-bottom:6px; }
  .app.landing .emblem { width:78px; height:78px; }
  .app.landing .wm-line { display:none; }
  .app.landing .wm-the { font-size:19px; padding-left:.44em; margin-bottom:2px; }
  .app.landing .wm-big { font-size:52px; }
  .app.landing .subtitle { font-size:19px; margin-bottom:8px; }
  .app.landing .topbar { margin:26px auto 0; justify-content:center; }
  .app.landing .foot { display:none; }
  /* Results: encabezado colapsado a una barra superior compacta */
  .app.results .kicker, .app.results .subtitle { display:none; }
  .app.results .hero { display:flex; align-items:center; gap:20px; margin-bottom:6px; }
  .app.results #brand { gap:12px; margin-bottom:0; white-space:nowrap; cursor:pointer; }
  .app.results .emblem { width:40px; height:40px; }
  .app.results .wm-stack { display:none; }
  .app.results .wm-line { font-size:21px; }
  .app.results #brand:hover { filter:brightness(1.06); }
  .app.results .topbar { flex:1; margin:0; }
  /* --- Tarjeta de loading: checklist de etapas del análisis --- */
  .loadcard h2 { font-size:18px; }
  ul.stages { list-style:none; margin:18px 0 6px; }
  ul.stages li { display:flex; align-items:center; gap:12px; padding:9px 0;
    font-size:14.5px; color:var(--muted); transition:color .3s; }
  ul.stages li .ic { flex:none; width:20px; height:20px; border-radius:50%;
    border:2px solid var(--grid); display:inline-flex; align-items:center;
    justify-content:center; transition:background .3s,border-color .3s; }
  ul.stages li.active { color:var(--ink); font-weight:600; }
  ul.stages li.active .ic { border-color:var(--purple); border-top-color:transparent;
    animation:r .7s linear infinite; }
  ul.stages li.done { color:var(--ink2); }
  ul.stages li.done .ic { background:var(--green); border-color:var(--green); }
  ul.stages li.done .ic::after { content:'✓'; color:#fff; font-size:12px; font-weight:800; }
  .loadbar { height:10px; background:var(--grid); border-radius:6px; overflow:hidden; margin-top:14px; }
  .loadbar i { display:block; height:100%; width:0%; border-radius:6px;
    background:linear-gradient(90deg,var(--purple),var(--green)); transition:width .6s ease; }
  .loadpct { text-align:right; font-size:12.5px; color:var(--muted); margin-top:6px;
    font-variant-numeric:tabular-nums; font-weight:600; }
</style></head><body><div class="app landing" id="app"><div class="wrap">
  <header class="hero">
    <div id="brand" class="brand" title="Volver al inicio">
      __EMBLEM__
      <div class="wm">
        <div class="wm-stack">
          <span class="wm-the">THE</span>
          <span class="wm-big">GREAT</span>
          <span class="wm-big">GATSBY</span>
        </div>
        <div class="wm-line">THE GREAT GATSBY</div>
      </div>
    </div>
    <div class="kicker">Motor de Análisis · SEC EDGAR en vivo</div>
    <p class="subtitle">Tu Especialista Financiero</p>
    <div class="topbar">
      <div class="searchbox">
        <input id="q" placeholder="Escribe un ticker o nombre — ej. NFLX, Disney, Coca-Cola…"
          autocomplete="off" autofocus>
        <div class="sugg" id="sugg"></div>
      </div>
      <button class="discover" id="discoverBtn">✨ Descubrir empresas</button>
    </div>
  </header>
  <div id="status"></div>
  <div class="card" id="screenCard" style="display:none;margin-top:22px"></div>
  <div class="card loadcard" id="loadCard" style="display:none;margin-top:22px"></div>
  <div class="grid" id="grid">
    <div class="card c-hero" id="heroCard"></div>
    <div class="card c-words" id="wordsCard"></div>
    <div class="card c-chart" id="chartCard"></div>
    <div class="card c-score" id="scoreCard"></div>
    <div class="card c-target" id="targetCard"></div>
    <div class="card c-brief" id="briefCard"></div>
    <div class="card c-order" id="orderCard" style="display:none"></div>
  </div>
  <div class="foot" id="foot"><b>Nota:</b> Puntaje rápido con datos oficiales de la SEC (EDGAR).
  Sin evidencia no hay número: las categorías pendientes se muestran como N/S, nunca se inventan.
  Los targets son rangos de referencia con supuestos declarados — clasificación de research,
  no es asesoría de inversión.</div>
</div></div>
<script>
const q = document.getElementById('q'), sugg = document.getElementById('sugg'),
      status = document.getElementById('status'), grid = document.getElementById('grid'),
      app = document.getElementById('app'), brand = document.getElementById('brand');
let timer = null;

function setMode(mode) {
  const landing = mode === 'landing';
  app.classList.toggle('landing', landing);
  app.classList.toggle('results', !landing);
}

// Clic en el título (en modo resultados) regresa a la bienvenida centrada.
brand.addEventListener('click', () => {
  if (!app.classList.contains('results')) return;
  grid.style.display = 'none'; screenCard.style.display = 'none';
  status.textContent = ''; q.value = ''; sugg.style.display = 'none';
  setMode('landing'); q.focus();
});

// --- Loading: checklist de etapas mientras corre /api/analyze -------------
const LOAD_STAGES = [
  'Leyendo reportes SEC EDGAR',
  'Analizando negocio y finanzas',
  'Evaluando riesgo y valuación',
  'Calculando precio objetivo',
  'Sintetizando puntaje',
];
const LOAD_PCTS = [16, 38, 58, 78, 92];  // el último se sostiene hasta que llega la respuesta
let loadTimer = null;

function startLoading(t) {
  const el = document.getElementById('loadCard');
  el.style.display = 'block';
  el.innerHTML = `<h2>Analizando ${t}…</h2>
    <div class="sub">Leyendo datos oficiales de la SEC y calculando el puntaje</div>
    <ul class="stages">${LOAD_STAGES.map((s, i) =>
      `<li id="st${i}" class="pending"><span class="ic"></span><span>${s}</span></li>`).join('')}</ul>
    <div class="loadbar"><i id="loadfill"></i></div>
    <div class="loadpct" id="loadpct">0%</div>`;
  const fill = document.getElementById('loadfill'), pct = document.getElementById('loadpct');
  function setStage(n) {
    for (let i = 0; i < LOAD_STAGES.length; i++) {
      document.getElementById('st' + i).className = i < n ? 'done' : (i === n ? 'active' : 'pending');
    }
    fill.style.width = LOAD_PCTS[n] + '%'; pct.textContent = LOAD_PCTS[n] + '%';
  }
  let cur = 0; setStage(0);
  clearInterval(loadTimer);
  loadTimer = setInterval(() => {
    if (cur < LOAD_STAGES.length - 1) setStage(++cur);
    else clearInterval(loadTimer);  // se queda en la última etapa (~92%) hasta finishLoading
  }, 850);
}

function finishLoading(ok, cb) {
  clearInterval(loadTimer);
  const el = document.getElementById('loadCard');
  if (!ok) { el.style.display = 'none'; if (cb) cb(); return; }
  for (let i = 0; i < LOAD_STAGES.length; i++) {
    const li = document.getElementById('st' + i); if (li) li.className = 'done';
  }
  const fill = document.getElementById('loadfill'), pct = document.getElementById('loadpct');
  if (fill) fill.style.width = '100%'; if (pct) pct.textContent = '100%';
  setTimeout(() => { el.style.display = 'none'; if (cb) cb(); }, 320);
}

q.addEventListener('input', () => {
  clearTimeout(timer);
  timer = setTimeout(async () => {
    const v = q.value.trim();
    if (!v) { sugg.style.display = 'none'; return; }
    const r = await fetch('/api/search?q=' + encodeURIComponent(v));
    const items = await r.json();
    sugg.innerHTML = items.map(i =>
      `<button data-t="${i.ticker}"><b>${i.ticker}</b><span>${i.name}</span></button>`).join('');
    sugg.style.display = items.length ? 'block' : 'none';
  }, 180);
});
q.addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    const first = sugg.querySelector('button');
    run(first ? first.dataset.t : q.value.trim().toUpperCase());
  }
});
sugg.addEventListener('click', e => {
  const b = e.target.closest('button');
  if (b) run(b.dataset.t);
});

const fmtB = x => '$' + (x / 1e9).toFixed(1) + 'B';
const CAT_COLORS = { business:'var(--purple)', financial:'var(--green)',
  risk:'var(--blue)', market:'var(--orange)', technical:'var(--orange)', valuation:'var(--orange)' };
const WORD_COLORS = ['var(--purple)','var(--green)','var(--blue)','var(--orange)','var(--red)','var(--purple)'];

function heroHtml(d) {
  const h = d.history, last = h[h.length - 1];
  const maxRev = Math.max(...h.map(r => r.revenue));
  const yoy = d.metrics.revenue_yoy;
  const chip = typeof yoy === 'number'
    ? `<span class="chip ${yoy >= 0 ? 'up' : 'down'}">${yoy >= 0 ? '▲' : '▼'} ${(Math.abs(yoy) * 100).toFixed(1)}% vs año anterior</span>` : '';
  const bars = h.map((r, i) => `
    <div class="bar ${i === h.length - 1 ? 'last' : ''}">
      <span class="v">${(r.revenue / 1e9).toFixed(0)}</span>
      <div class="stick" data-h="${(r.revenue / maxRev * 100).toFixed(0)}" style="height:4%"></div>
      <span class="y">${r.year}</span>
    </div>`).join('');
  return `<h2>${d.entity} · ${d.ticker}</h2>
    <div class="sub">Año fiscal terminado ${d.fiscal_year_end} · Form 10-K · SEC EDGAR</div>
    <div class="hero-num"><span class="n">${fmtB(last.revenue)}</span>
      <span class="u">ventas anuales</span>${chip}</div>
    <div class="bars">${bars}</div>
    <div class="legend"><span><i style="background:var(--purple)"></i>Ventas por año (miles de millones $)</span>
      <span><i style="background:var(--green)"></i>Último año</span></div>`;
}

function wordsHtml(d) {
  const lis = d.narrative.map((s, i) =>
    `<li><span class="dot" style="background:${WORD_COLORS[i % WORD_COLORS.length]}"></span><span>${s}</span></li>`).join('');
  return `<h2>En palabras simples</h2>
    <div class="sub">Qué dicen los números, sin jerga</div><ul class="words">${lis}</ul>`;
}

function gaugeSvg(score) {
  const frac = Math.max(0, Math.min(1, score / 10));
  const R = 80, C = Math.PI * R;
  return `<svg class="gauge" viewBox="0 0 200 110">
    <path class="track" d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke-width="14" stroke-linecap="round"/>
    <path class="arc" d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke-width="14"
      stroke-linecap="round" stroke-dasharray="${(frac * C).toFixed(1)} ${C.toFixed(1)}"/>
  </svg>`;
}

function scoreHtml(d) {
  const sc = d.scorecard;
  const rows = sc.categories.map(r => {
    if (r.status === 'scored') {
      return `<div class="srow"><span class="nm">${r.label}</span>
        <div class="track2"><div class="fill2" data-w="${r.score10 * 10}"
          style="width:0%;background:${CAT_COLORS[r.key]}"></div></div>
        <span class="v">${r.score10.toFixed(1)}/10</span></div>`;
    }
    return `<div class="srow ns"><span class="nm" title="${r.reason}">${r.label}</span>
      <div class="track2"></div><span class="v">N/S</span></div>`;
  }).join('');
  const overall = sc.overall_10 === null ? '—' : sc.overall_10.toFixed(1);
  return `<h2>Puntaje de los agentes</h2>
    <div class="sub">1–10 por categoría · ${sc.evidence_points_covered}/100 puntos de evidencia</div>
    <div class="gaugebox">${gaugeSvg(sc.overall_10 ?? 0)}
      <div class="gauge-num">${overall}</div><div class="gauge-of">de 10 (rápido)</div></div>${rows}`;
}

function targetHtml(d) {
  const t = d.targets;
  if (t.status !== 'ok') {
    return `<h2>Precio objetivo — 12 meses</h2>
      <div class="sub">Bull / Medio / Bear</div>
      <p style="margin-top:16px;color:var(--ink2);font-size:14px">
      No se puede calcular: ${t.reason}.</p>`;
  }
  const order = ['bull', 'base', 'bear'];
  const by = {}; t.scenarios.forEach(s => by[s.key] = s);
  const rows = order.map(k => {
    const s = by[k];
    const cls = k === 'base' ? 'base' : k;
    const chip = `<span class="chip ${s.upside >= 0 ? 'up' : 'down'}">${s.upside >= 0 ? '+' : ''}${(s.upside * 100).toFixed(0)}%</span>`;
    return `<div class="trow"><span class="tag ${cls}">${s.label}</span>
      <span class="as">${s.assumptions}</span>
      <span class="tp">$${s.target.toLocaleString('en-US', {minimumFractionDigits: 2})}</span>
      <span class="up">${chip}</span></div>`;
  }).join('');
  const pts = [ {v: t.price, lb: 'Hoy', c: 'var(--ink)', below: true},
    {v: by.bear.target, lb: 'Bear', c: 'var(--red)'},
    {v: by.base.target, lb: 'Medio', c: 'var(--blue)'},
    {v: by.bull.target, lb: 'Bull', c: 'var(--green)'} ];
  const lo = Math.min(...pts.map(p => p.v)) * 0.97, hi = Math.max(...pts.map(p => p.v)) * 1.03;
  const pos = v => ((v - lo) / (hi - lo) * 100).toFixed(1) + '%';
  const marks = pts.map(p => `
    <span class="lb ${p.below ? 'below' : ''}" style="left:${pos(p.v)};color:${p.c}">${p.lb} $${p.v.toFixed(0)}</span>
    <span class="pt" style="left:${pos(p.v)};background:${p.c}"></span>`).join('');
  return `<h2>Precio objetivo — ${t.horizon}</h2>
    <div class="sub">Escenarios con supuestos declarados (nunca una sola línea)</div>
    <div class="price-now"><span class="n">$${t.price.toLocaleString('en-US', {minimumFractionDigits: 2})}</span>
      <span class="u">precio actual · EPS $${t.eps} · P/E ${t.pe_now}x</span></div>
    <div class="range"><div class="line"></div>${marks}</div>${rows}
    <div class="sub" style="margin-top:12px">${t.disclaimer}</div>`;
}

function money(x, dec) {
  return '$' + Number(x).toLocaleString('en-US', {minimumFractionDigits: dec || 0, maximumFractionDigits: dec || 0});
}

function briefHtml(d) {
  const b = d.brief;
  if (!b) return '';
  const it = b.interpretation, pr = b.probability, w = b.watch;

  // 1) Qué significa el score
  const cls = it.classification || 'neutral';
  const revisit = it.revisit ? `<div class="revisit">↻ ${it.revisit}</div>` : '';
  const cats = (it.categories || []).map(c => {
    const ns = c.score10 == null;
    const val = ns ? 'N/S' : `${c.score10}/10 · ${c.meaning}`;
    return `<div class="cat-row"><span class="mn ${ns ? 'ns' : ''}">${c.label}</span><span>${val}</span></div>`;
  }).join('');
  const interpCol = `<div class="brief-col">
    <div class="bh">Qué significa el puntaje</div>
    <span class="classpill ${cls}">${(cls || 'sin dato').toUpperCase()}</span>
    <div class="revisit">${it.classification_label}</div>${revisit}${cats}</div>`;

  // 2) Probabilidad hacia el precio
  let probCol;
  if (pr.status === 'ok') {
    const by = {}; pr.targets.forEach(t => by[t.key] = t);
    const order = ['bull', 'base', 'bear'];
    const col = {bull: 'var(--green)', base: 'var(--blue)', bear: 'var(--red)'};
    // partition bar: below-bear / bear-base / base-bull / above-bull
    const pb = by.bear.prob_reach, pm = by.base.prob_reach, pu = by.bull.prob_reach;
    const seg = [
      [(1 - pb) * 100, '#c9ced8', 'por debajo de Bear'],
      [(pb - pm) * 100, 'var(--red)', 'Bear→Medio'],
      [(pm - pu) * 100, 'var(--blue)', 'Medio→Bull'],
      [pu * 100, 'var(--green)', 'por encima de Bull'],
    ].map(s => `<i style="width:${Math.max(0, s[0]).toFixed(1)}%;background:${s[1]}" title="${s[2]}: ${s[0].toFixed(0)}%"></i>`).join('');
    const rows = order.map(k => {
      const t = by[k];
      return `<div class="prow"><span class="tag ${k}">${t.label}</span>
        <span class="pt">llegar a ${money(t.target, 0)}</span>
        <span class="pv">${(t.prob_reach * 100).toFixed(0)}%</span></div>`;
    }).join('');
    probCol = `<div class="brief-col">
      <div class="bh">Probabilidad hacia el precio (12 meses)</div>
      <div class="probbar">${seg}</div>
      <div class="modal">Resultado más probable: <b>${pr.modal_zone}</b> (${(pr.modal_prob * 100).toFixed(0)}%)</div>
      ${rows}
      <div class="brief-note">${pr.assumptions}</div></div>`;
  } else {
    probCol = `<div class="brief-col"><div class="bh">Probabilidad hacia el precio</div>
      <p style="color:var(--ink2);font-size:14px">No calculable: ${pr.reason}.</p></div>`;
  }

  // 3) Puntos clave a vigilar
  const lv = w.levels;
  const levelsBlock = lv.status === 'ok' ? `<div class="levels">
      <div class="lvl entrada"><div class="k">Entrada aprox.</div><div class="v">${money(lv.entrada, 2)}</div></div>
      <div class="lvl inval"><div class="k">Invalidación</div><div class="v">${lv.invalidacion != null ? money(lv.invalidacion, 2) : '—'}</div></div>
      <div class="lvl salida"><div class="k">Salida (Medio)</div><div class="v">${lv.salida_base != null ? money(lv.salida_base, 2) : '—'}</div></div>
    </div><div class="brief-note">${lv.nota}</div>` :
    `<p style="color:var(--ink2);font-size:14px">Niveles no disponibles: ${lv.reason}.</p>`;

  const ne = w.catalysts && w.catalysts.next_earnings;
  const catBlock = ne ? `<ul class="wl"><li><span>Próximo earnings</span><b>${ne.date}</b></li>
      ${ne.eps_est != null ? `<li><span>EPS estimado</span><b>$${ne.eps_est}</b></li>` : ''}</ul>` :
    `<div class="brief-note">Sin fecha de earnings próxima disponible.</div>`;

  const ins = w.insiders || [];
  const insBlock = ins.length ? `<ul class="wl">${ins.map(i =>
      `<li><span>${i.name || 'Insider'} · ${i.date || ''}</span>
        <span class="${i.side === 'compra' ? 'ins-buy' : 'ins-sell'}">${i.side === 'compra' ? 'COMPRA' : 'VENTA'} ${money(i.value, 0)}</span></li>`).join('')}</ul>` :
    `<div class="brief-note">Sin transacciones de insiders sobre $1M.</div>`;

  const nh = w.notable_holders || [];
  const nhBlock = nh.length ? `<ul class="wl">${nh.map(h =>
      `<li><span>${h.investor} · 13F ${h.as_of || ''}</span>
        <span class="ins-buy">${money(h.value, 0)}${h.options ? ' +opc' : ''}</span></li>`).join('')}</ul>` :
    `<div class="brief-note">Ningún inversionista reconocido del registro reporta posición (13F).</div>`;

  const risks = w.risks || [];
  const riskBlock = risks.length ? `<ul class="wl risk">${risks.map(r => `<li><span>${r}</span></li>`).join('')}</ul>` :
    `<div class="brief-note">Sin banderas de riesgo materiales en los datos.</div>`;

  const watchCol = `<div class="brief-col full">
    <div class="bh">Puntos clave a vigilar</div>
    <div class="brief-grid">
      <div class="brief-col"><div class="bh" style="font-size:12px">Niveles de precio</div>${levelsBlock}</div>
      <div class="brief-col"><div class="bh" style="font-size:12px">Próximos catalizadores</div>${catBlock}</div>
      <div class="brief-col"><div class="bh" style="font-size:12px">Insiders &gt; $1M (Forms 4)</div>${insBlock}</div>
      <div class="brief-col"><div class="bh" style="font-size:12px">Inversionistas importantes (13F)</div>${nhBlock}</div>
      <div class="brief-col"><div class="bh" style="font-size:12px">Riesgos / rompe-tesis</div>${riskBlock}</div>
    </div></div>`;

  return `<h2>Lectura para el inversionista</h2>
    <div class="sub">Qué significa el puntaje, probabilidad hacia el precio y qué observar</div>
    <div class="brief-grid" style="margin-top:16px">${interpCol}${probCol}${watchCol}</div>`;
}

// --- Entrar orden a Robinhood (dinero real, tu clic manda) ---------------
let ORDER = null;  // el ticket vigente, para el submit

function orderHtml(d) {
  const o = d.order;
  if (!o) return { show:false, html:'' };
  ORDER = { ticker:d.ticker, entry:o.entry, stop:o.stop };
  if (!o.tradeable) {
    const basis = o.equity_basis ? `<div class="brief-note" style="margin-top:6px">Base de cuenta: ${o.equity_basis}. Fondea la cuenta y el tamaño aparecerá automáticamente.</div>` : '';
    return { show:true, html:`<h2>Entrar orden</h2>
      <div class="sub">Orden derivada del research — no ejecutable</div>
      <div class="brief-note" style="margin-top:12px">No hay orden por ahora: ${o.reason || 'niveles insuficientes.'}</div>${basis}` };
  }
  const cfg = d.rh_configured;
  const ro = d.readonly;
  const capBasis = o.equity_basis || '';
  const banner = ro
    ? `<div class="rh-banner" style="background:var(--purple-bg);border-color:#e6d29a;color:#7a5f1e">📊 Modo solo análisis — la orden se envía desde la app en tu Mac, no desde aquí.</div>`
    : `<div class="rh-banner">⚠ DINERO REAL — esta orden se manda a Robinhood cuando TÚ la confirmas. El agente no la dispara solo.</div>`;
  const send = ro
    ? `<button class="rh-send" disabled>Enviar solo desde la Mac</button>`
    : (cfg
      ? `<button class="rh-send" id="rhSend" disabled onclick="submitOrder()">Enviar a Robinhood</button>`
      : `<button class="rh-send" disabled>Conecta Robinhood en API/.env para habilitar</button>`);
  const confirm = (ro || !cfg) ? '' :
    `<label class="rh-confirm"><input type="checkbox" id="rhChk" onchange="document.getElementById('rhSend').classList.toggle('armed', this.checked); document.getElementById('rhSend').disabled=!this.checked;">
        Entiendo que esto envía una <b>orden real</b> de compra con dinero real a mi cuenta de Robinhood, con su stop de protección adjunto.</label>`;
  return { show:true, html:`<h2>Entrar orden</h2>
    <div class="sub">Compra dimensionada al 3–5% de riesgo · stop adjunto · tu clic la envía</div>
    ${banner}
    <div class="ticket">
      <div class="tk"><div class="k">Ticker</div><div class="v">${o.ticker}</div></div>
      <div class="tk buy"><div class="k">Comprar</div><div class="v">${o.qty} <span style="font-size:13px;color:var(--ink2)">acc.</span></div></div>
      <div class="tk"><div class="k">Entrada (límite)</div><div class="v">${money(o.entry,2)}</div></div>
      <div class="tk stop"><div class="k">Stop</div><div class="v">${money(o.stop,2)}</div></div>
    </div>
    <div class="brief-note">Riesgo si toca el stop: <b>${money(o.risk_dollars,2)}</b> (${(o.risk_frac*100).toFixed(0)}% del capital) · Notional ${money(o.notional,2)} · Base: ${capBasis}. El tamaño real se recalcula contra tu poder de compra vivo al enviar.</div>
    ${confirm}
    ${send}
    <div class="rh-result" id="rhResult" style="display:none"></div>` };
}

async function submitOrder() {
  if (!ORDER) return;
  const btn = document.getElementById('rhSend');
  const out = document.getElementById('rhResult');
  btn.disabled = true; btn.textContent = 'Enviando…';
  try {
    const r = await fetch('/api/order/place', { method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ ticker:ORDER.ticker, entry:ORDER.entry, stop:ORDER.stop, confirm:true }) });
    const j = await r.json();
    out.style.display = 'block';
    if (j.ok) {
      out.className = 'rh-result ok';
      out.innerHTML = `✅ Orden colocada: comprar ${j.qty} ${ORDER.ticker} a ${money(j.entry,2)} con stop ${money(j.stop,2)}. Riesgo ${money(j.risk_dollars,2)}.`;
    } else if (j.status === 'unhedged') {
      out.className = 'rh-result warn';
      out.innerHTML = `⚠ ${j.note}`;
    } else {
      out.className = 'rh-result err';
      out.innerHTML = `⛔ No se colocó (${j.status}): ${j.note || 'error'}`;
      btn.disabled = false; btn.textContent = 'Enviar a Robinhood';
    }
  } catch (e) {
    out.style.display = 'block'; out.className = 'rh-result err';
    out.innerHTML = `⛔ Error de red: ${e.message}`;
    btn.disabled = false; btn.textContent = 'Enviar a Robinhood';
  }
}

// --- Gráfica SVG propia (cero dependencias externas) ---------------------
let tvData = [], tvTargets = null;
const PERIODS = { '1M': 21, '3M': 63, '6M': 126, '1A': 9999 };

function renderChart(d) {
  const el = document.getElementById('chartCard');
  tvData = d.chart || [];
  tvTargets = d.targets && d.targets.status === 'ok' ? d.targets : null;
  if (!tvData.length) {
    el.innerHTML = `<h2>Gráfica y targets</h2>
      <div class="sub">Sin historial de precio disponible para ${d.ticker}.</div>`;
    return;
  }
  el.innerHTML = `<h2>${d.ticker} — precio y targets</h2>
    <div class="sub">Último año · targets a 12 meses con supuestos declarados</div>
    <div class="chart-head" id="chartHead"></div>
    <div id="tvchart"></div>
    <div class="periods" id="periods">
      ${Object.keys(PERIODS).map(p =>
        `<button data-p="${p}" class="${p === '1A' ? 'on' : ''}">${p}</button>`).join('')}
    </div>`;
  document.getElementById('periods').addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    document.querySelectorAll('.periods button').forEach(x => x.classList.toggle('on', x === b));
    setPeriod(b.dataset.p);
  });
  setPeriod('1A');
}

function setPeriod(p) {
  const slice = tvData.slice(-PERIODS[p]);
  const W = 1000, H = 340, PADL = 14, PADR = 150, PADT = 18, PADB = 30;
  const vals = slice.map(r => r.value);
  const tg = tvTargets ? tvTargets.scenarios.map(s => s.target) : [];
  let lo = Math.min(...vals, ...(tg.length ? tg : vals));
  let hi = Math.max(...vals, ...(tg.length ? tg : vals));
  const pad = (hi - lo) * 0.07 || 1; lo -= pad; hi += pad;
  const X = i => PADL + i / (slice.length - 1) * (W - PADL - PADR);
  const Y = v => PADT + (1 - (v - lo) / (hi - lo)) * (H - PADT - PADB);

  const pts = slice.map((r, i) => `${X(i).toFixed(1)},${Y(r.value).toFixed(1)}`).join(' ');
  const area = `M ${X(0).toFixed(1)},${(H - PADB)} L ${pts.replaceAll(' ', ' L ')} L ${X(slice.length - 1).toFixed(1)},${H - PADB} Z`;

  // grid + eje de fechas
  const grid = [0.25, 0.5, 0.75].map(f => {
    const y = PADT + f * (H - PADT - PADB);
    return `<line x1="${PADL}" x2="${W - PADR + 60}" y1="${y}" y2="${y}"
      stroke="rgba(139,146,156,.14)" stroke-width="1"/>`;
  }).join('');
  const dates = [0, Math.floor(slice.length / 2), slice.length - 1].map(i =>
    `<text x="${X(i)}" y="${H - 8}" fill="#B4A483" font-size="11.5"
      text-anchor="${i === 0 ? 'start' : i === slice.length - 1 ? 'end' : 'middle'}">${slice[i].time}</text>`).join('');

  // líneas de target punteadas con etiqueta tipo píldora (como la referencia)
  let tlines = '';
  if (tvTargets) {
    const by = {}; tvTargets.scenarios.forEach(s => by[s.key] = s);
    const defs = [
      ['bull', '#26d07c', 'rgba(38,208,124,.14)', 'Bull'],
      ['base', '#f5a623', 'rgba(245,166,35,.14)', 'Medio'],
      ['bear', '#ff5a5f', 'rgba(255,90,95,.14)', 'Bear'],
    ];
    tlines = defs.map(([k, c, bg, lb]) => {
      const s = by[k], y = Y(s.target);
      const label = `${lb} $${s.target.toFixed(0)} (${s.upside >= 0 ? '+' : ''}${(s.upside * 100).toFixed(0)}%)`;
      return `<line x1="${PADL}" x2="${W - PADR + 4}" y1="${y}" y2="${y}"
          stroke="${c}" stroke-width="1.3" stroke-dasharray="7 6" opacity=".9"/>
        <rect x="${W - PADR + 8}" y="${y - 12}" width="${PADR - 16}" height="24" rx="7"
          fill="${bg}" stroke="${c}" stroke-width="1"/>
        <text x="${W - PADR / 2}" y="${y + 4}" fill="${c}" font-size="12.5"
          font-weight="700" text-anchor="middle">${label}</text>`;
    }).join('');
  }

  document.getElementById('tvchart').innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block"
        font-family="system-ui,-apple-system,sans-serif">
      <defs><linearGradient id="ga" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="rgba(38,208,124,.28)"/>
        <stop offset="1" stop-color="rgba(38,208,124,.02)"/>
      </linearGradient></defs>
      ${grid}
      <path d="${area}" fill="url(#ga)"/>
      <polyline points="${pts}" fill="none" stroke="#26d07c" stroke-width="2.2"
        stroke-linejoin="round" stroke-linecap="round"/>
      ${tlines}${dates}
    </svg>`;

  const first = slice[0].value, last = slice[slice.length - 1].value;
  const chg = last - first, pct = chg / first * 100;
  document.getElementById('chartHead').innerHTML = `
    <span class="px">$${last.toLocaleString('en-US', {minimumFractionDigits: 2})}</span>
    <span class="chg ${chg >= 0 ? 'up' : 'down'}">${chg >= 0 ? '+' : ''}$${Math.abs(chg).toFixed(2)}
      (${chg >= 0 ? '+' : ''}${pct.toFixed(2)}%)</span>
    <span class="rng">${p}</span>`;
}

const screenCard = document.getElementById('screenCard');
document.getElementById('discoverBtn').addEventListener('click', async () => {
  setMode('results');
  grid.style.display = 'none'; screenCard.style.display = 'none';
  status.innerHTML = `<span class="spin"></span>Escaneando todo el universo de la SEC:
    empresas medianas ($0.8B–$30B), rentables y creciendo… la primera vez tarda 1–2 min.`;
  try {
    const r = await fetch('/api/screen');
    if (!r.ok) throw new Error((await r.json()).error || r.status);
    const rows = await r.json();
    const trs = rows.map((x, i) => `<tr>
      <td>${i + 1}</td>
      <td class="tk" data-t="${x.ticker}">${x.ticker}</td>
      <td class="nm">${x.name}</td>
      <td class="num">$${(x.revenue / 1e9).toFixed(1)}B</td>
      <td class="num">${(x.growth * 100).toFixed(0)}%</td>
      <td class="num">${(x.margin * 100).toFixed(0)}%</td>
      <td class="num">${x.target_base ? '$' + x.target_base.toFixed(0) +
        ' <span class="chip ' + (x.upside_base >= 0 ? 'up' : 'down') + '">' +
        (x.upside_base >= 0 ? '+' : '') + (x.upside_base * 100).toFixed(0) + '%</span>' : '—'}</td>
      <td><span class="scorepill"><span class="mini"><i style="width:${x.score10 * 10}%"></i></span>
        <b>${x.score10.toFixed(1)}</b></span></td>
    </tr>`).join('');
    screenCard.innerHTML = `<h2>Empresas descubiertas — top ${rows.length} por puntaje</h2>
      <div class="sub">Prefiltro: ventas $0.8B–$30B · margen neto > 8% · crecimiento > 5% ·
      datos SEC EDGAR · clasificación de research, no asesoría. Toca un ticker para el análisis completo.</div>
      <table class="scr"><thead><tr><th>#</th><th>Ticker</th><th>Empresa</th>
        <th style="text-align:right">Ventas</th><th style="text-align:right">Crec.</th>
        <th style="text-align:right">Margen</th><th style="text-align:right">Target medio</th>
        <th>Puntaje</th></tr></thead><tbody>${trs}</tbody></table>`;
    screenCard.style.display = 'block';
    status.textContent = '';
    screenCard.querySelectorAll('.tk').forEach(el =>
      el.addEventListener('click', () => { screenCard.style.display = 'none'; run(el.dataset.t); }));
  } catch (err) {
    status.innerHTML = `No pude completar el descubrimiento: ${err.message}`;
  }
});

async function run(t) {
  if (!t) return;
  setMode('results');
  sugg.style.display = 'none'; grid.style.display = 'none';
  screenCard.style.display = 'none'; status.textContent = '';
  q.value = t;
  startLoading(t);
  try {
    const r = await fetch('/api/analyze?ticker=' + encodeURIComponent(t));
    if (!r.ok) throw new Error((await r.json()).error || r.status);
    const d = await r.json();
    document.getElementById('heroCard').innerHTML = heroHtml(d);
    document.getElementById('wordsCard').innerHTML = wordsHtml(d);
    document.getElementById('scoreCard').innerHTML = scoreHtml(d);
    document.getElementById('targetCard').innerHTML = targetHtml(d);
    document.getElementById('briefCard').innerHTML = briefHtml(d);
    const oc = document.getElementById('orderCard');
    const ord = orderHtml(d);
    oc.innerHTML = ord.html; oc.style.display = ord.show ? 'block' : 'none';
    finishLoading(true, () => {
      grid.style.display = 'grid';
      renderChart(d);
      requestAnimationFrame(() => {
        document.querySelectorAll('.stick[data-h]').forEach(b => b.style.height = b.dataset.h + '%');
        document.querySelectorAll('.fill2[data-w]').forEach(f => f.style.width = f.dataset.w + '%');
      });
    });
  } catch (err) {
    finishLoading(false);
    status.innerHTML = `No pude analizar <b>${t}</b>: ${err.message}`;
  }
}
</script></body></html>"""

# Bake the inline emblem into the page (kept out of the f-string-hostile CSS/JS).
PAGE = PAGE.replace("__EMBLEM__", EMBLEM_INLINE)


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj: dict | list, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        if url.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/favicon.svg":
            body = FAVICON_SVG.encode()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/api/search":
            self._json(search(qs.get("q", [""])[0]))
        elif url.path == "/api/screen":
            try:
                with _lock:
                    self._json(run_screen(limit=15))
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif url.path == "/api/analyze":
            ticker = qs.get("ticker", [""])[0].strip().upper()
            if not ticker:
                self._json({"error": "missing ticker"}, 400)
                return
            try:
                # One analysis at a time: providers share one httpx client/cache.
                with _lock:
                    result = analyze(ticker)
                self._json(result)
            except Exception as e:  # surface as JSON, keep server alive
                self._json({"error": str(e)}, 500)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 (stdlib API)
        url = urlparse(self.path)
        if url.path != "/api/order/place":
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json({"ok": False, "status": "bad_request",
                        "note": "cuerpo JSON inválido"}, 400)
            return
        try:
            with _lock:
                self._json(place_order(payload))
        except Exception as e:  # never crash the server on an order path
            self._json({"ok": False, "status": "error", "note": str(e)}, 500)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[wbj] {self.address_string()} {fmt % args}")


if __name__ == "__main__":
    shown = "localhost" if HOST in ("127.0.0.1", "0.0.0.0") else HOST
    print(f"The Great Gatsby -> http://{shown}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
