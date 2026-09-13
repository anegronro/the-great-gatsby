"""Briefs: el paquete de datos duros del que se redacta un post.

El contrato del proyecto ("sin evidencia, no hay número") aplica igual a un
tweet que a un reporte. Aquí se resuelve por construcción:

1. El brief precomputa TODOS los números que un post tiene permitido usar,
   incluidos los derivados obvios (upside %, distancia al target).
2. El redactor recibe solo esos números.
3. `generate.validate_numbers` rechaza el borrador si aparece cualquier
   cifra que no esté en la lista.

Así un número inventado no llega a la cola; no depende de que el modelo se
porte bien.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class Fact:
    """Un número con su fuente. Sin fuente no entra."""

    label: str
    value: float
    unit: str  # "usd" | "pct" | "ratio" | "score" | "count"
    source: str

    def rendered(self) -> str:
        if self.unit == "usd":
            return f"${self.value:,.2f}"
        if self.unit == "pct":
            return f"{self.value:+.0%}"
        if self.unit == "score":
            return f"{self.value:.1f}/10"
        if self.unit == "count":
            return f"{self.value:,.0f}"
        return f"{self.value:,.2f}"

    def line(self) -> str:
        return f"- {self.label}: {self.rendered()}  [fuente: {self.source}]"


@dataclass
class Brief:
    """Todo lo que el redactor puede saber sobre un post."""

    kind: str          # target_range | thesis_check | conviction
    ticker: str
    angle: str         # el ángulo editorial, en una frase
    facts: list[Fact] = field(default_factory=list)
    context: list[str] = field(default_factory=list)  # prosa, sin números nuevos
    as_of: str = ""

    @property
    def brief_id(self) -> str:
        return f"{self.ticker}-{self.kind}-{self.as_of}"

    def allowed_numbers(self) -> list[float]:
        """Los valores que el post puede citar. Todo lo demás es invento."""
        return [f.value for f in self.facts]

    def render(self) -> str:
        """El brief tal como lo ve el redactor."""
        parts = [
            f"TICKER: {self.ticker}",
            f"ÁNGULO: {self.angle}",
            "",
            "NÚMEROS PERMITIDOS (no puedes usar ningún otro):",
            *[f.line() for f in self.facts],
        ]
        if self.context:
            parts += ["", "CONTEXTO (cualitativo, no inventes cifras de aquí):",
                      *[f"- {c}" for c in self.context]]
        return "\n".join(parts)


def _pct_change(new: float, old: float) -> float | None:
    return (new / old - 1.0) if old else None


def from_prediction(pred: dict, price_now: float | None = None) -> Brief | None:
    """Brief desde un `prediccion.json` guardado por `wbj analyze`.

    Con `price_now` el brief se vuelve un chequeo de tesis (lo que dijimos
    vs lo que pasó), que es contenido mucho más fuerte que repetir el
    target: es verificable y admite estar equivocado.
    """
    needed = ("ticker", "date", "price", "bear", "base", "bull")
    if not all(k in pred for k in needed):
        return None

    t = pred["ticker"]
    p0 = float(pred["price"])
    facts = [
        Fact("precio cuando publicamos el análisis", p0, "usd",
             f"wbj analyze {pred['date']}"),
        Fact("escenario bajo (12m)", float(pred["bear"]), "usd",
             f"wbj targets {pred['date']}"),
        Fact("escenario base (12m)", float(pred["base"]), "usd",
             f"wbj targets {pred['date']}"),
        Fact("escenario alto (12m)", float(pred["bull"]), "usd",
             f"wbj targets {pred['date']}"),
    ]
    if pred.get("score10") is not None:
        facts.append(Fact("score del modelo", float(pred["score10"]), "score",
                          f"wbj scorecard {pred['date']}"))
    if pred.get("pe_now") is not None:
        facts.append(Fact("P/E al momento del análisis", float(pred["pe_now"]),
                          "ratio", f"wbj analyze {pred['date']}"))
    if pred.get("growth_base") is not None:
        facts.append(Fact("crecimiento asumido en el escenario base",
                          float(pred["growth_base"]), "pct",
                          "supuesto declarado del modelo"))

    # Derivados: se precomputan para que el redactor no tenga que calcular
    # (y para que el validador los reconozca como legítimos).
    up_base = _pct_change(float(pred["base"]), p0)
    if up_base is not None:
        facts.append(Fact("upside al escenario base desde el precio del análisis",
                          up_base, "pct", "calculado: base/precio - 1"))

    kind, angle = "target_range", (
        f"El rango de 12 meses del modelo para {t}, con los supuestos a la vista."
    )
    context = [
        "El rango sale de escenarios con supuestos declarados, no de un precio único.",
        "Es una clasificación de research, nunca una instrucción de compra o venta.",
    ]

    if price_now:
        facts.append(Fact("precio hoy", float(price_now), "usd", "FMP quote"))
        moved = _pct_change(float(price_now), p0)
        if moved is not None:
            facts.append(Fact("movimiento real desde el análisis", moved, "pct",
                              "calculado: precio hoy/precio análisis - 1"))
        dentro = float(pred["bear"]) <= float(price_now) <= float(pred["bull"])
        kind = "thesis_check"
        angle = (
            f"Qué dijo el modelo sobre {t} el {pred['date']} y qué pasó realmente. "
            f"El precio hoy está {'DENTRO' if dentro else 'FUERA'} del rango bear-bull."
        )
        context = [
            f"El precio de hoy está {'dentro' if dentro else 'fuera'} del rango proyectado.",
            "Publicar el marcador — acierto o error — es el punto: la mayoría"
            " de cuentas solo publica los aciertos.",
            "El horizonte del target es 12 meses; aún no está maduro si han"
            " pasado menos.",
        ]

    return Brief(
        kind=kind, ticker=t, angle=angle, facts=facts, context=context,
        as_of=date.today().isoformat(),
    )


def load_briefs(
    reports_dir: Path,
    price_fn=None,
    *,
    limit: int = 10,
) -> list[Brief]:
    """Un brief por cada predicción guardada, la más reciente por ticker.

    `price_fn(ticker) -> float | None` es inyectable: sin ella los briefs se
    arman igual, solo que sin el chequeo contra la realidad.
    """
    from wbj.memoria import load_predictions

    preds = load_predictions(reports_dir)
    latest: dict[str, dict] = {}
    for p in preds:  # load_predictions viene ordenado del más viejo al más nuevo
        latest[p["ticker"]] = p

    briefs: list[Brief] = []
    for pred in sorted(latest.values(), key=lambda p: p["date"], reverse=True)[:limit]:
        price = None
        if price_fn is not None:
            try:
                price = price_fn(pred["ticker"])
            except Exception:
                price = None  # sin precio el brief sigue sirviendo, solo sin marcador
        b = from_prediction(pred, price)
        if b:
            briefs.append(b)
    return briefs


def load_brief_file(path: Path) -> Brief:
    """Brief desde un JSON a mano — para contenido que no sale del motor."""
    d = json.loads(path.read_text())
    return Brief(
        kind=d.get("kind", "custom"),
        ticker=d.get("ticker", ""),
        angle=d["angle"],
        facts=[Fact(**f) for f in d.get("facts", [])],
        context=d.get("context", []),
        as_of=d.get("as_of", date.today().isoformat()),
    )
