"""Redacta posts en inglés sobre un brief, y verifica que no inventó nada.

Dos piezas independientes:

- `validate_numbers` es determinista y no usa modelo: extrae las cifras del
  texto y las compara contra las permitidas del brief. Es la red de
  seguridad, y corre siempre — incluso sobre un texto escrito a mano.
- `draft_posts` le pide a Claude tres variantes. El modelo redacta; los
  números salen del brief.

Objetivo editorial: el Ads Revenue Sharing paga por impresiones de usuarios
verificados en la sección de REPLIES. Un post que informa y se acaba no
monetiza; uno que abre una discusión con datos duros, sí. Por eso el prompt
empuja a la conversación, no al cierre.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from wbj.x.brief import Brief

MODEL = "claude-opus-5"
MAX_CHARS = 280


# --- Validación numérica (determinista, sin modelo) -----------------------

# Captura el número con el contexto inmediato que decide si es una cifra
# financiera (estricta) o estructural (permitida).
_NUM = re.compile(r"(\$)?(\d[\d,]*(?:\.\d+)?)\s*(%|/10|x\b|B\b|M\b)?")


def _candidates(value: float) -> set[float]:
    """Formas en que un fact puede aparecer legítimamente en el texto."""
    out = {value, abs(value)}
    for k in (0, 1, 2):
        out.add(round(value, k))
        out.add(round(abs(value), k))
    # Un ratio 0.04 se escribe "4%".
    out.update({value * 100, abs(value) * 100, round(abs(value) * 100, 1),
                round(abs(value) * 100, 0)})
    return out


def validate_numbers(text: str, allowed: list[float]) -> list[str]:
    """Devuelve las cifras del texto que ningún fact justifica.

    Estrictas: las que llevan `$`, `%`, `/10`, `x`, `B` o `M`, y cualquier
    decimal. Esas son afirmaciones financieras y tienen que venir del brief.

    Permitidas sin fact: enteros sueltos <= 100 y años (2000-2100) — son
    estructura del texto ("3 reasons", "12-month", "in 2026"), no datos.
    """
    ok: set[float] = set()
    for v in allowed:
        ok |= _candidates(v)

    unjustified: list[str] = []
    for m in _NUM.finditer(text):
        dollar, raw, suffix = m.group(1), m.group(2), m.group(3)
        try:
            n = float(raw.replace(",", ""))
        except ValueError:
            continue
        financiera = bool(dollar or suffix) or "." in raw
        if not financiera and (n <= 100 or 2000 <= n <= 2100):
            continue
        if any(abs(n - c) < 0.011 for c in ok):
            continue
        unjustified.append(m.group(0).strip())
    return unjustified


# --- Redacción ------------------------------------------------------------

_SYSTEM = """You write for a finance/markets account on X. Your posts exist to \
start arguments that informed people want to join — not to inform and stop.

Non-negotiable rules:
1. NEVER use a number that is not in the brief's allowed list. Not one. If \
you want to make a point that needs a number you don't have, make a \
different point.
2. NEVER tell anyone to buy, sell, or hold. You publish research \
classifications and ranges with stated assumptions. "The model's range is X" \
is fine; "load up here" is not.
3. NEVER include a link or a URL. Links cut organic reach and cost 13x more \
to publish.
4. Always state the assumption behind a projection. A number without its \
assumption is a lie told with confidence.
5. Never present a projection as a single value when the brief gives a range.

How to earn replies (this is what gets paid):
- Take a position, then show the number that supports it. A post with no \
position gets no replies.
- End on something a knowledgeable person would want to answer or correct — \
an open question, a claim they can push back on, a scenario they'd price \
differently. Not "what do you think?", which reads as engagement bait.
- Publishing your own misses beats publishing only your hits. It is rarer \
and it draws better replies.
- Concrete beats clever. No hype adjectives, no emoji walls, no hashtags.

Voice: direct, numerate, unimpressed. Short sentences. Someone who has read \
the filing and is mildly annoyed that others haven't."""


class _Variant(BaseModel):
    approach: str = Field(description="En una frase: el ángulo de esta variante")
    text: str = Field(description="El post en inglés, <= 280 caracteres")
    thread: list[str] = Field(
        default_factory=list,
        description="Si es hilo, los posts siguientes (<=280 c/u). Vacío si no.",
    )
    reply_hook: str = Field(
        description="Por qué alguien informado respondería a esto"
    )


class _Variants(BaseModel):
    variants: list[_Variant]


# Cierres legítimos de un post. Un texto que no termina en ninguno de estos
# casi siempre viene cortado.
_CIERRES = ".!?\"')]}…"


def _parece_cortado(texto: str) -> bool:
    """True si el texto parece interrumpido a media frase."""
    t = texto.rstrip()
    return bool(t) and t[-1] not in _CIERRES


@dataclass
class Draft:
    """Un borrador listo para revisión, con su auditoría."""

    text: str
    approach: str
    reply_hook: str
    thread: list[str] = field(default_factory=list)
    brief_id: str = ""
    ticker: str = ""
    kind: str = ""
    model: str = MODEL
    numeros_no_justificados: list[str] = field(default_factory=list)
    exceso_caracteres: int = 0
    truncado: bool = False

    @property
    def valido(self) -> bool:
        return (
            not self.numeros_no_justificados
            and self.exceso_caracteres == 0
            and not self.truncado
        )

    @property
    def full_text(self) -> str:
        return "\n\n---\n\n".join([self.text, *self.thread])


def _style_rules(lecciones: list[Any]) -> str:
    """Traduce los hallazgos medidos del analizador en reglas de estilo.

    Solo entran findings significativos: si no hay evidencia, el redactor no
    recibe ninguna regla inventada sobre "lo que funciona".
    """
    if not lecciones:
        return (
            "No hay todavía datos de engagement propios suficientes para "
            "derivar reglas de estilo medidas. Usa solo los principios "
            "generales de arriba."
        )
    mapa = {
        "tiene_pregunta": "terminar en una pregunta abierta",
        "tiene_cashtag": "incluir el cashtag ($TICKER)",
        "tiene_numero": "incluir cifras concretas",
        "es_hilo": "publicarlo como hilo",
        "postura_fuerte": "tomar una postura que contradiga el consenso",
        "multilinea": "usar varias líneas cortas en vez de un párrafo",
        "texto_largo": "escribir largo (200+ caracteres)",
        "tiene_link": "incluir un link",
    }
    out = ["Medido en ESTA cuenta (no en general):"]
    for f in lecciones:
        desc = mapa.get(f.feature, f.feature)
        verbo = "SUBE" if (f.lift or 0) > 0 else "BAJA"
        out.append(
            f"- {desc}: {verbo} la tasa de replies {abs(f.lift or 0):.0%} "
            f"(n={f.n_con}/{f.n_sin}, p={f.p_value:.3f})"
        )
    return "\n".join(out)


_SHRINK = """This post is {exceso} characters over the {limite}-character limit \
({actual} total). Rewrite it under the limit.

Hard constraints:
- Keep every number exactly as written. Do not drop, round, or reword a figure.
- Keep the position and the closing hook. Cut hedging, filler and connectives \
before you cut substance.
- Return only the rewritten post, nothing else.

POST:
{texto}"""


def _shrink(client, texto: str, model: str) -> str:
    """Pide una versión bajo el límite. Devuelve el original si no mejora.

    Un modelo no puede contar caracteres de forma fiable — es un artefacto de
    la tokenización, no algo que el prompt arregle. Así que el límite se
    verifica en código y el recorte se pide solo cuando hace falta; si la
    respuesta tampoco cabe, se devuelve el original y el borrador queda
    marcado para que lo edite un humano.
    """
    try:
        r = client.messages.create(
            model=model, max_tokens=1000,
            messages=[{"role": "user", "content": _SHRINK.format(
                exceso=len(texto) - MAX_CHARS, limite=MAX_CHARS,
                actual=len(texto), texto=texto)}],
        )
    except Exception:
        return texto
    nuevo = "".join(b.text for b in r.content if b.type == "text").strip()
    return nuevo if 0 < len(nuevo) <= MAX_CHARS else texto


def draft_posts(
    brief: Brief,
    settings,
    *,
    lecciones: list[Any] | None = None,
    n: int = 3,
    client: Any = None,
    model: str | None = None,
) -> list[Draft]:
    """Pide `n` variantes y valida cada una. `client` es inyectable en tests.

    Devuelve [] si no hay API key o SDK — nunca revienta el pipeline. Las
    variantes con números inventados se devuelven marcadas, no descartadas:
    ver qué inventó el modelo es información útil para el revisor.
    """
    if client is None:
        from wbj.config import anthropic_client

        client = anthropic_client(settings)
        if client is None:
            return []

    user = (
        f"{brief.render()}\n\n"
        f"REGLAS DE ESTILO DERIVADAS DE DATOS:\n{_style_rules(lecciones or [])}\n\n"
        f"Escribe {n} variantes distintas entre sí (distinto ángulo, no la "
        f"misma idea reformulada). Máximo {MAX_CHARS} caracteres por post. "
        f"Inglés. Sin links. Sin hashtags."
    )

    resp = client.messages.parse(
        model=model or MODEL,
        # En Opus 5 el thinking adaptativo está ON por defecto y sus tokens
        # cuentan contra este techo: con 8000 la última variante salía cortada
        # a media frase. No bajar sin volver a medir.
        max_tokens=16000,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_format=_Variants,
    )
    parsed = resp.parsed_output
    if parsed is None:
        return []
    # Si la respuesta chocó con el techo, lo último que venga está cortado.
    respuesta_truncada = getattr(resp, "stop_reason", None) == "max_tokens"

    allowed = brief.allowed_numbers()
    drafts: list[Draft] = []
    for v in parsed.variants:
        piezas = [v.text, *v.thread]
        # El límite se hace cumplir aquí, no en el prompt.
        piezas = [
            _shrink(client, p, model or MODEL) if len(p) > MAX_CHARS else p
            for p in piezas
        ]
        v.text, v.thread = piezas[0], piezas[1:]
        exceso = sum(max(0, len(p) - MAX_CHARS) for p in piezas)
        drafts.append(
            Draft(
                text=v.text,
                approach=v.approach,
                reply_hook=v.reply_hook,
                thread=list(v.thread),
                brief_id=brief.brief_id,
                ticker=brief.ticker,
                kind=brief.kind,
                model=model or MODEL,
                numeros_no_justificados=validate_numbers("\n".join(piezas), allowed),
                exceso_caracteres=exceso,
                truncado=respuesta_truncada or _parece_cortado(v.text),
            )
        )
    return drafts
