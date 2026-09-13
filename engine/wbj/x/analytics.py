"""Analizador de engagement de X: qué funciona en TU cuenta, con evidencia.

Fuente: el CSV que exportas de analytics.x.com (Posts -> export). No usa la
API, así que no cuesta nada y no depende de créditos.

La métrica que manda aquí NO es likes. El programa de Ads Revenue Sharing
paga por impresiones de usuarios verificados en la sección de REPLIES de tus
posts: un post que se lee y nadie responde no monetiza. Por eso el ranking
primario es `reply_rate = replies / impressions`, y los likes son contexto.

Honestidad estadística (la razón de ser de este módulo):
- Mediana, no media. Las distribuciones de engagement son long-tail; una
  sola publicación viral mueve la media y te hace creer un patrón falso.
- Todo hallazgo trae n de cada grupo y un p-value (Mann-Whitney U, no
  paramétrico: no asume normalidad, que aquí no se cumple).
- Con muestra chica el hallazgo se marca INSUFICIENTE y no se usa para
  decidir nada. Un lift de +40% con n=4 es ruido, no una lección.
"""

from __future__ import annotations

import csv
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Un post con puñado de impresiones produce ratios absurdos (1 reply / 3
# impresiones = 33%). Se excluyen del análisis de patrones.
MIN_IMPRESSIONS = 200

# Por debajo de esto, un grupo no sostiene una conclusión.
MIN_GROUP = 8

# Umbral de significancia. No es sagrado, pero hay que declararlo.
ALPHA = 0.05


# --- Lectura del CSV ------------------------------------------------------

def _norm(name: str) -> str:
    """'Post text ' -> 'posttext'. El export de X cambia mayúsculas y espacios."""
    return re.sub(r"[^a-z0-9]", "", name.strip().lower())


# Alias por columna: X ha renombrado estas cabeceras varias veces y el export
# nuevo no coincide con el viejo. Se prueba en orden.
_ALIASES: dict[str, tuple[str, ...]] = {
    "post_id": ("postid", "tweetid", "id"),
    "text": ("posttext", "tweettext", "text", "post"),
    "created": ("date", "time", "createdat", "datetime"),
    "impressions": ("impressions", "views", "impression"),
    "likes": ("likes", "like"),
    "replies": ("replies", "reply", "comments"),
    "reposts": ("reposts", "retweets", "repost", "retweet"),
    "bookmarks": ("bookmarks", "bookmark"),
    "engagements": ("engagements", "engagement"),
    "profile_visits": ("profilevisits", "userprofileclicks", "profileclicks"),
    "follows": ("follows", "newfollows", "newfollowers"),
    "url_clicks": ("urlclicks", "linkclicks"),
    "media_views": ("mediaviews",),
}

# Sin esto no hay análisis posible.
_REQUIRED = ("impressions", "replies")


class AnalyticsError(ValueError):
    """El CSV no sirve. Se lanza fuerte: un análisis a medias miente."""


def _resolve_columns(header: list[str]) -> dict[str, str]:
    """Mapea nombre canónico -> nombre real de la columna en el CSV."""
    present = {_norm(h): h for h in header}
    found: dict[str, str] = {}
    for canon, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in present:
                found[canon] = present[alias]
                break
    missing = [c for c in _REQUIRED if c not in found]
    if missing:
        raise AnalyticsError(
            f"El CSV no trae columna(s) {missing}. Cabeceras encontradas: "
            f"{sorted(present.values())}. ¿Es el export de posts de "
            f"analytics.x.com?"
        )
    return found


def _num(raw: str | None) -> float:
    """'1,234' / '12.3%' / '' -> float. Devuelve 0.0 si no es número."""
    if raw is None:
        return 0.0
    s = raw.strip().replace(",", "").replace("%", "").replace("$", "")
    if not s or s in {"-", "—", "N/A", "n/a"}:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


_DATE_FORMATS = (
    "%Y-%m-%d %H:%M %z", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d", "%m/%d/%Y %H:%M", "%m/%d/%Y",
)


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    s = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


@dataclass
class Post:
    """Un post con sus métricas y las features que se van a correlacionar."""

    post_id: str
    text: str
    created: datetime | None
    impressions: float
    likes: float
    replies: float
    reposts: float
    bookmarks: float
    profile_visits: float
    follows: float

    @property
    def reply_rate(self) -> float:
        """La métrica que paga: conversación por impresión."""
        return self.replies / self.impressions if self.impressions else 0.0

    @property
    def engagement_rate(self) -> float:
        total = self.likes + self.replies + self.reposts + self.bookmarks
        return total / self.impressions if self.impressions else 0.0

    @property
    def amplification_rate(self) -> float:
        """Reposts: lo que te lleva a audiencias nuevas."""
        return self.reposts / self.impressions if self.impressions else 0.0

    @property
    def follow_rate(self) -> float:
        return self.follows / self.impressions if self.impressions else 0.0


def load_posts(csv_path: Path) -> list[Post]:
    """Lee el export de X Analytics. Lanza AnalyticsError si el CSV no sirve."""
    if not csv_path.exists():
        raise AnalyticsError(f"No existe {csv_path}")
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise AnalyticsError(f"{csv_path} está vacío o no es un CSV.")
        cols = _resolve_columns(list(reader.fieldnames))
        posts: list[Post] = []
        for row in reader:
            def g(canon: str) -> str | None:
                col = cols.get(canon)
                return row.get(col) if col else None

            posts.append(
                Post(
                    post_id=(g("post_id") or "").strip(),
                    text=(g("text") or "").strip(),
                    created=_parse_date(g("created")),
                    impressions=_num(g("impressions")),
                    likes=_num(g("likes")),
                    replies=_num(g("replies")),
                    reposts=_num(g("reposts")),
                    bookmarks=_num(g("bookmarks")),
                    profile_visits=_num(g("profile_visits")),
                    follows=_num(g("follows")),
                )
            )
    if not posts:
        raise AnalyticsError(f"{csv_path} no tiene filas de datos.")
    return posts


# --- Features del texto ---------------------------------------------------

_URL = re.compile(r"https?://\S+|\bt\.co/\S+")
_CASHTAG = re.compile(r"\$[A-Z]{1,5}\b")
_NUMBER = re.compile(r"\d")
_THREAD = re.compile(r"(^|\s)(1/\d*|🧵|thread\b)", re.IGNORECASE)
# Marcadores de postura: el contenido que provoca replies casi siempre
# contradice algo. Lista deliberadamente corta y en inglés (la cuenta es en
# inglés); ampliarla sin medir sería inventar un patrón.
_STANCE = re.compile(
    r"\b(but|actually|wrong|nobody|everyone|myth|isn't|aren't|won't|"
    r"stop|forget|unpopular|contrarian|truth)\b",
    re.IGNORECASE,
)


def features(post: Post) -> dict[str, bool]:
    """Features booleanas de un post, para el análisis de lift."""
    t = post.text
    lines = [ln for ln in t.splitlines() if ln.strip()]
    return {
        "tiene_link": bool(_URL.search(t)),
        "tiene_pregunta": "?" in t,
        "tiene_cashtag": bool(_CASHTAG.search(t)),
        "tiene_numero": bool(_NUMBER.search(t)),
        "es_hilo": bool(_THREAD.search(t)),
        "postura_fuerte": bool(_STANCE.search(t)),
        "multilinea": len(lines) >= 3,
        "texto_largo": len(t) >= 200,
    }


def _hour_bucket(post: Post) -> str | None:
    if post.created is None:
        return None
    h = post.created.hour
    if 5 <= h < 9:
        return "05-09 (premarket)"
    if 9 <= h < 12:
        return "09-12 (apertura)"
    if 12 <= h < 16:
        return "12-16 (sesión)"
    if 16 <= h < 20:
        return "16-20 (cierre/tarde)"
    return "20-05 (noche)"


# --- Análisis de patrones -------------------------------------------------

@dataclass
class Finding:
    """Un patrón medido. `suficiente=False` significa: no concluyas nada."""

    feature: str
    n_con: int
    n_sin: int
    mediana_con: float
    mediana_sin: float
    lift: float | None          # cambio relativo de la mediana, con vs sin
    p_value: float | None
    suficiente: bool
    metric: str = "reply_rate"
    # Lo pone `_benjamini_hochberg` sobre el conjunto completo de findings:
    # un p-value aislado no basta cuando se corren 8 tests a la vez.
    pasa_correccion: bool = False

    @property
    def significativo(self) -> bool:
        return self.suficiente and self.pasa_correccion

    @property
    def veredicto(self) -> str:
        if not self.suficiente:
            return f"INSUFICIENTE (n={self.n_con}/{self.n_sin}, mínimo {MIN_GROUP})"
        if self.p_value is None:
            return "SIN TEST"
        if not self.pasa_correccion:
            if self.p_value < ALPHA:
                # El caso traicionero: p<0.05 aislado, pero con 8 tests
                # simultáneos un falso positivo así es lo ESPERADO.
                return (f"descartado por comparaciones múltiples "
                        f"(p={self.p_value:.3f}, no sobrevive Benjamini-Hochberg)")
            return f"no distinguible del ruido (p={self.p_value:.2f})"
        direccion = "SUBE" if (self.lift or 0) > 0 else "BAJA"
        return f"{direccion} {abs(self.lift or 0):.0%} (p={self.p_value:.3f})"


def _benjamini_hochberg(findings: list["Finding"], alpha: float = ALPHA) -> None:
    """Marca `pasa_correccion` controlando la tasa de falsos descubrimientos.

    Se prueban ~8 features a la vez. A alpha=0.05, uno de cada veinte tests
    da un "hallazgo" que es puro azar — con 8 tests, encontrar un falso
    positivo es lo esperado, no la excepción. Sin esta corrección el
    generador convertiría ese ruido en una regla de estilo y escribiría
    peor con cada análisis.

    Benjamini-Hochberg en vez de Bonferroni: con muestras chicas Bonferroni
    es tan conservador que borra también los efectos reales.
    """
    testeados = [f for f in findings if f.suficiente and f.p_value is not None]
    m = len(testeados)
    if m == 0:
        return
    ordenados = sorted(testeados, key=lambda f: f.p_value)
    corte = 0  # el mayor k cuyo p_k <= (k/m)*alpha
    for k, f in enumerate(ordenados, start=1):
        if f.p_value <= (k / m) * alpha:
            corte = k
    for k, f in enumerate(ordenados, start=1):
        f.pasa_correccion = k <= corte


def _mannwhitney(a: list[float], b: list[float]) -> float | None:
    """p-value de dos colas. None si scipy falta o la muestra es degenerada."""
    if len(a) < 3 or len(b) < 3:
        return None
    try:
        from scipy.stats import mannwhitneyu
    except ImportError:
        return None
    try:
        return float(mannwhitneyu(a, b, alternative="two-sided").pvalue)
    except ValueError:
        # Ocurre cuando todos los valores son idénticos.
        return None


def analyze_feature(posts: list[Post], name: str, metric: str = "reply_rate") -> Finding:
    """Compara la mediana de `metric` entre los posts con y sin la feature."""
    con = [getattr(p, metric) for p in posts if features(p)[name]]
    sin = [getattr(p, metric) for p in posts if not features(p)[name]]
    med_con = statistics.median(con) if con else 0.0
    med_sin = statistics.median(sin) if sin else 0.0
    lift = (med_con - med_sin) / med_sin if med_sin > 0 else None
    suficiente = len(con) >= MIN_GROUP and len(sin) >= MIN_GROUP
    return Finding(
        feature=name,
        n_con=len(con),
        n_sin=len(sin),
        mediana_con=med_con,
        mediana_sin=med_sin,
        lift=lift,
        p_value=_mannwhitney(con, sin) if suficiente else None,
        suficiente=suficiente,
        metric=metric,
    )


@dataclass
class Report:
    """Resultado del análisis. Lo consume el generador y el reporte en texto."""

    n_posts: int
    n_analizados: int
    periodo: tuple[str, str] | None
    total_impressions: float
    mediana_reply_rate: float
    mediana_engagement_rate: float
    findings: list[Finding] = field(default_factory=list)
    top_posts: list[Post] = field(default_factory=list)
    peores_posts: list[Post] = field(default_factory=list)
    por_horario: dict[str, tuple[int, float]] = field(default_factory=dict)

    @property
    def lecciones(self) -> list[Finding]:
        """Solo lo que sobrevive n mínimo Y significancia. Esto es lo que
        el generador tiene permitido usar como regla de estilo."""
        return [f for f in self.findings if f.significativo]

    @property
    def impresiones_faltantes(self) -> float:
        """Cuánto falta para el umbral de 5M del programa de monetización."""
        return max(0.0, 5_000_000 - self.total_impressions)


def analyze(posts: list[Post], *, metric: str = "reply_rate") -> Report:
    """Mide qué funciona. `metric` es la variable a explicar."""
    usables = [p for p in posts if p.impressions >= MIN_IMPRESSIONS]

    fechas = sorted(p.created for p in posts if p.created is not None)
    periodo = (
        (fechas[0].date().isoformat(), fechas[-1].date().isoformat())
        if fechas else None
    )

    por_horario: dict[str, list[float]] = {}
    for p in usables:
        b = _hour_bucket(p)
        if b:
            por_horario.setdefault(b, []).append(getattr(p, metric))

    findings: list[Finding] = []
    if usables:
        for name in features(usables[0]):
            findings.append(analyze_feature(usables, name, metric))
    _benjamini_hochberg(findings)
    # Primero lo accionable: significativo y de mayor efecto.
    findings.sort(
        key=lambda f: (f.significativo, abs(f.lift or 0)), reverse=True
    )

    ranked = sorted(usables, key=lambda p: getattr(p, metric), reverse=True)
    return Report(
        n_posts=len(posts),
        n_analizados=len(usables),
        periodo=periodo,
        total_impressions=sum(p.impressions for p in posts),
        mediana_reply_rate=(
            statistics.median([p.reply_rate for p in usables]) if usables else 0.0
        ),
        mediana_engagement_rate=(
            statistics.median([p.engagement_rate for p in usables]) if usables else 0.0
        ),
        findings=findings,
        top_posts=ranked[:5],
        peores_posts=ranked[-5:][::-1] if len(ranked) > 5 else [],
        por_horario={
            k: (len(v), statistics.median(v)) for k, v in sorted(por_horario.items())
        },
    )
