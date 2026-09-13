"""Tests del content engine de X."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from wbj.x.analytics import (
    AnalyticsError, Post, analyze, analyze_feature, features, load_posts,
)
from wbj.x.brief import Brief, Fact, from_prediction
from wbj.x.generate import Draft, validate_numbers
from wbj.x.store import Queue


# --- helpers --------------------------------------------------------------

def _post(text: str, impressions: float = 5000, replies: float = 25, **kw) -> Post:
    return Post(
        post_id=kw.get("post_id", "1"), text=text,
        created=kw.get("created", datetime(2026, 6, 1, 10, 0)),
        impressions=impressions, likes=kw.get("likes", 50), replies=replies,
        reposts=kw.get("reposts", 10), bookmarks=kw.get("bookmarks", 5),
        profile_visits=kw.get("profile_visits", 20), follows=kw.get("follows", 2),
    )


def _write_csv(path, rows, header=None):
    header = header or list(rows[0])
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        w.writerows(rows)
    return path


# --- lectura del CSV ------------------------------------------------------

def test_csv_sin_columnas_clave_falla_ruidosamente(tmp_path):
    """Un CSV sin impresiones no puede analizarse a medias: tiene que gritar."""
    p = _write_csv(tmp_path / "malo.csv", [{"Post text": "hola", "Likes": "3"}])
    with pytest.raises(AnalyticsError, match="impressions"):
        load_posts(p)


def test_csv_acepta_alias_de_columnas(tmp_path):
    """X ha renombrado 'Impressions' a 'Views'; ambos deben funcionar."""
    p = _write_csv(tmp_path / "v.csv", [
        {"Post id": "1", "Post text": "hi", "Date": "2026-06-01 10:00",
         "Views": "1,200", "Replies": "12"},
    ])
    posts = load_posts(p)
    assert posts[0].impressions == 1200
    assert posts[0].replies == 12


def test_numeros_con_formato_no_rompen(tmp_path):
    p = _write_csv(tmp_path / "f.csv", [
        {"Post text": "x", "Impressions": "12,345", "Replies": "-"},
    ])
    posts = load_posts(p)
    assert posts[0].impressions == 12345
    assert posts[0].replies == 0.0


# --- métricas -------------------------------------------------------------

def test_reply_rate_es_la_metrica_que_paga():
    p = _post("x", impressions=1000, replies=30)
    assert p.reply_rate == pytest.approx(0.03)


def test_impresiones_cero_no_divide_por_cero():
    assert _post("x", impressions=0, replies=5).reply_rate == 0.0


# --- features -------------------------------------------------------------

@pytest.mark.parametrize("texto,feature,esperado", [
    ("look https://t.co/abc", "tiene_link", True),
    ("no link here", "tiene_link", False),
    ("$NVDA is rich", "tiene_cashtag", True),
    ("NVDA is rich", "tiene_cashtag", False),
    ("what breaks first?", "tiene_pregunta", True),
    ("Everyone is wrong about this", "postura_fuerte", True),
    ("Revenue grew last year", "postura_fuerte", False),
    ("1/ here we go", "es_hilo", True),
])
def test_features_del_texto(texto, feature, esperado):
    assert features(_post(texto))[feature] is esperado


# --- honestidad estadística (el corazón del módulo) -----------------------

def test_muestra_chica_no_concluye():
    """Tres posts no sostienen una lección, por grande que sea el efecto."""
    posts = [_post("a?", replies=500) for _ in range(3)]
    posts += [_post("b", replies=1) for _ in range(3)]
    f = analyze_feature(posts, "tiene_pregunta")
    assert not f.suficiente
    assert not f.significativo
    assert "INSUFICIENTE" in f.veredicto


def test_detecta_un_patron_real():
    posts = [_post(f"q{i}?", impressions=5000, replies=150) for i in range(20)]
    posts += [_post(f"p{i}", impressions=5000, replies=10) for i in range(20)]
    rep = analyze(posts)
    hallazgo = next(f for f in rep.findings if f.feature == "tiene_pregunta")
    assert hallazgo.significativo
    assert hallazgo.lift > 1.0


def test_ruido_puro_no_produce_lecciones():
    """Sin señal real, `lecciones` tiene que quedar vacío."""
    import random

    random.seed(7)
    posts = [
        _post(f"{'q?' if i % 2 else 'p'} {i}", impressions=5000,
              replies=random.randint(10, 60))
        for i in range(60)
    ]
    assert analyze(posts).lecciones == []


def test_correccion_por_comparaciones_multiples():
    """Un p<0.05 aislado no basta cuando se corren 8 tests a la vez.

    Regresión de un falso positivo real: en una prueba con features
    aleatorias, `tiene_cashtag` dio p=0.041 sobre puro ruido. Sin
    Benjamini-Hochberg eso se habría convertido en una regla de estilo.
    """
    from wbj.x.analytics import ALPHA, Finding, _benjamini_hochberg

    fs = [
        Finding("real_a", 20, 20, 0.05, 0.01, 4.0, 0.0001, True),
        Finding("real_b", 20, 20, 0.04, 0.01, 3.0, 0.0002, True),
        Finding("borderline", 20, 20, 0.02, 0.015, 0.3, 0.041, True),
        Finding("ruido_1", 20, 20, 0.01, 0.01, 0.0, 0.32, True),
        Finding("ruido_2", 20, 20, 0.01, 0.01, 0.0, 0.55, True),
        Finding("ruido_3", 20, 20, 0.01, 0.01, 0.0, 0.75, True),
        Finding("ruido_4", 20, 20, 0.01, 0.01, 0.0, 0.81, True),
        Finding("ruido_5", 20, 20, 0.01, 0.01, 0.0, 0.93, True),
    ]
    _benjamini_hochberg(fs)
    por_nombre = {f.feature: f for f in fs}
    assert por_nombre["real_a"].significativo
    assert por_nombre["real_b"].significativo
    # p=0.041 < ALPHA aislado, pero no sobrevive la corrección.
    assert por_nombre["borderline"].p_value < ALPHA
    assert not por_nombre["borderline"].significativo
    assert "comparaciones múltiples" in por_nombre["borderline"].veredicto


# --- brief ----------------------------------------------------------------

_PRED = {
    "ticker": "NVDA", "date": "2026-07-27", "price": 196.53,
    "bear": 194.56, "base": 275.14, "bull": 316.41,
    "growth_base": 0.40, "pe_now": 40.10, "score10": 7.8,
}


def test_brief_desde_prediccion():
    b = from_prediction(_PRED)
    assert b.ticker == "NVDA"
    assert b.kind == "target_range"
    assert 275.14 in b.allowed_numbers()


def test_brief_con_precio_vivo_es_chequeo_de_tesis():
    b = from_prediction(_PRED, price_now=178.50)
    assert b.kind == "thesis_check"
    assert 178.50 in b.allowed_numbers()
    assert "FUERA" in b.angle  # 178.50 está por debajo del bear de 194.56


def test_prediccion_incompleta_no_produce_brief():
    assert from_prediction({"ticker": "X"}) is None


def test_brief_precomputa_derivados():
    """El post no debería tener que calcular: el upside ya viene en el brief."""
    b = from_prediction(_PRED)
    upside = (275.14 / 196.53) - 1
    assert any(abs(v - upside) < 1e-6 for v in b.allowed_numbers())


# --- validación numérica --------------------------------------------------

_ALLOWED = [196.53, 194.56, 275.14, 316.41, 7.8, 40.10, 0.40, 178.50, -0.09]


def test_acepta_cifras_del_brief():
    t = ("Range for $NVDA: bear $194.56, base $275.14, bull $316.41. "
         "Now $178.50.")
    assert validate_numbers(t, _ALLOWED) == []


@pytest.mark.parametrize("texto,esperado", [
    ("Fair value is $412.00 on my math.", "$412.00"),
    ("Revenue grew 18% last quarter.", "18%"),
    ("Trading at a 62.4 P/E.", "62.4"),
])
def test_rechaza_cifras_inventadas(texto, esperado):
    assert esperado in validate_numbers(texto, _ALLOWED)


@pytest.mark.parametrize("texto", [
    "3 reasons this is generous:",   # conteo estructural
    "Our 2026 call still stands.",   # año
    "A 12-month horizon.",           # plazo
])
def test_permite_numeros_estructurales(texto):
    assert validate_numbers(texto, _ALLOWED) == []


def test_ratio_escrito_como_porcentaje_es_valido():
    """El brief guarda 0.40; el post escribe '+40%'. Es el mismo dato."""
    assert validate_numbers("assumes +40% growth", _ALLOWED) == []


# --- cola -----------------------------------------------------------------

def _draft(text="hola", **kw):
    return Draft(text=text, approach="a", reply_hook="r", **kw)


def test_cola_persiste_y_no_borra(tmp_path):
    q = Queue(tmp_path / "cola.jsonl")
    a = q.add(_draft("A"))
    b = q.add(_draft("B"))
    q.rechazar(b.id, motivo="cifra sin fuente")
    assert len(Queue(tmp_path / "cola.jsonl").load()) == 2
    assert q.get(b.id).motivo_rechazo == "cifra sin fuente"
    assert [i.id for i in q.pendientes()] == [a.id]


def test_edicion_gana_sobre_original(tmp_path):
    q = Queue(tmp_path / "cola.jsonl")
    i = q.add(_draft("original"))
    q.aprobar(i.id, texto_editado="editado por Angel")
    assert q.get(i.id).texto_final == "editado por Angel"


def test_tasa_de_aprobacion_es_el_criterio_de_autonomia(tmp_path):
    q = Queue(tmp_path / "cola.jsonl")
    for i in range(8):
        item = q.add(_draft(f"p{i}"))
        q.aprobar(item.id) if i < 6 else q.rechazar(item.id)
    pct, n = q.tasa_aprobacion()
    assert (pct, n) == (0.75, 8)


def test_cola_vacia_no_reporta_tasa(tmp_path):
    assert Queue(tmp_path / "cola.jsonl").tasa_aprobacion() == (None, 0)


def test_linea_corrupta_no_tumba_la_cola(tmp_path):
    p = tmp_path / "cola.jsonl"
    q = Queue(p)
    q.add(_draft("bueno"))
    with p.open("a") as fh:
        fh.write("{esto no es json}\n")
    assert len(q.load()) == 1


# --- draft ----------------------------------------------------------------

def test_draft_con_cifra_sin_fuente_no_es_valido():
    assert not _draft(numeros_no_justificados=["$412"]).valido


def test_draft_que_excede_no_es_valido():
    assert not _draft(exceso_caracteres=12).valido


def test_draft_limpio_es_valido():
    assert _draft().valido


# --- truncamiento ---------------------------------------------------------

@pytest.mark.parametrize("texto,cortado", [
    ("A complete thought.", False),
    ("Is this the question?", False),
    ("Loud claim!", False),
    ('He said "no more"', False),                 # cierre con comilla
    ("the bear, what a 45.10 P/E costs you", True),   # el caso real que pasó
    ("Base still needs another", True),
])
def test_detecta_frase_cortada(texto, cortado):
    from wbj.x.generate import _parece_cortado

    assert _parece_cortado(texto) is cortado


def test_draft_truncado_no_es_valido():
    assert not _draft("sin cierre", truncado=True).valido


def test_cola_persiste_el_flag_de_truncado(tmp_path):
    q = Queue(tmp_path / "cola.jsonl")
    i = q.add(_draft("cortado a medias", truncado=True))
    assert q.get(i.id).truncado is True


# --- hilos automáticos ----------------------------------------------------

def test_hilo_parte_por_frases_completas():
    from wbj.x.generate import MAX_CHARS, _to_thread

    texto = " ".join(f"Sentence number {i} says something." for i in range(30))
    piezas = _to_thread(texto)
    assert len(piezas) > 1
    assert all(len(p) <= MAX_CHARS for p in piezas)
    # Nada se pierde y nada se parte a media palabra.
    assert " ".join(piezas) == texto


def test_hilo_no_parte_dentro_de_un_decimal():
    from wbj.x.generate import _frases

    assert _frases("Trading at a 45.10 P/E today.") == ["Trading at a 45.10 P/E today."]


def test_frase_unica_gigante_no_se_mutila():
    from wbj.x.generate import _to_thread

    larga = "x" * 400
    assert _to_thread(larga) == [larga]  # el llamador la marca para recorte


def test_umbral_decide_hilo_vs_recorte():
    """Pasarse por poco se recorta; pasarse por mucho va a hilo."""
    from wbj.x.generate import MAX_CHARS, UMBRAL_HILO

    assert (290 - MAX_CHARS) <= UMBRAL_HILO      # 10 de exceso -> recorte
    assert (400 - MAX_CHARS) > UMBRAL_HILO       # 120 de exceso -> hilo


# --- Telegram -------------------------------------------------------------

class FakeBot:
    """Bot de mentira que registra lo que se le pide."""

    def __init__(self, updates=None):
        self.enviados, self.callbacks, self.editados = [], [], []
        self._updates = updates or []
        self._next_id = 100

    def send(self, texto, *, botones=None, force_reply=False):
        self._next_id += 1
        self.enviados.append({"texto": texto, "botones": botones,
                              "force_reply": force_reply, "id": self._next_id})
        return {"message_id": self._next_id}

    def answer_callback(self, callback_id, texto=""):
        self.callbacks.append((callback_id, texto))

    def quitar_botones(self, message_id, nuevo_texto=None):
        self.editados.append((message_id, nuevo_texto))

    def updates(self, offset=None):
        return [u for u in self._updates
                if offset is None or u["update_id"] >= offset]


def _cb(item_id, accion, update_id=1, message_id=101):
    return {"update_id": update_id,
            "callback_query": {"id": f"c{update_id}", "data": f"{accion}:{item_id}",
                               "message": {"message_id": message_id}}}


def _estado(tmp_path):
    from wbj.x.telegram import Estado

    return Estado(tmp_path / "estado.json")


def test_enviar_pendientes_no_reenvia(tmp_path):
    q = Queue(tmp_path / "cola.jsonl")
    q.add(_draft("uno"))
    q.add(_draft("dos"))
    bot = FakeBot()
    from wbj.x.telegram import enviar_pendientes

    assert enviar_pendientes(q, bot) == 2
    assert enviar_pendientes(q, bot) == 0   # ya están en el teléfono
    assert len(bot.enviados) == 2
    assert bot.enviados[0]["botones"]       # llevan los tres botones


def test_boton_aprobar(tmp_path):
    from wbj.x.telegram import procesar_updates

    q = Queue(tmp_path / "cola.jsonl")
    i = q.add(_draft("texto"))
    bot = FakeBot([_cb(i.id, "ok")])
    procesar_updates(q, bot, _estado(tmp_path))
    assert q.get(i.id).estado == "aprobado"
    assert bot.callbacks  # sin answerCallbackQuery el botón se queda girando


def test_boton_descartar_no_borra(tmp_path):
    from wbj.x.telegram import procesar_updates

    q = Queue(tmp_path / "cola.jsonl")
    i = q.add(_draft("texto"))
    procesar_updates(q, FakeBot([_cb(i.id, "no")]), _estado(tmp_path))
    item = q.get(i.id)
    assert item.estado == "rechazado"
    assert item.motivo_rechazo
    assert len(q.load()) == 1


def test_editar_pide_texto_y_luego_aprueba(tmp_path):
    from wbj.x.telegram import procesar_updates

    q = Queue(tmp_path / "cola.jsonl")
    i = q.add(_draft("original"))
    est = _estado(tmp_path)

    bot = FakeBot([_cb(i.id, "ed")])
    procesar_updates(q, bot, est)
    assert bot.enviados[-1]["force_reply"]
    assert q.get(i.id).estado == "pendiente"      # todavía no se decide
    pedido_id = bot.enviados[-1]["id"]

    bot2 = FakeBot([{
        "update_id": 2,
        "message": {"message_id": 500, "text": "versión corregida",
                    "reply_to_message": {"message_id": pedido_id}},
    }])
    procesar_updates(q, bot2, est)
    item = q.get(i.id)
    assert item.estado == "aprobado"
    assert item.texto_final == "versión corregida"


def test_offset_evita_reprocesar(tmp_path):
    from wbj.x.telegram import procesar_updates

    q = Queue(tmp_path / "cola.jsonl")
    i = q.add(_draft("texto"))
    est = _estado(tmp_path)
    ups = [_cb(i.id, "ok", update_id=7)]
    assert procesar_updates(q, FakeBot(ups), est) != []
    # Segunda pasada con los MISMOS updates: el offset ya avanzó.
    assert procesar_updates(q, FakeBot(ups), est) == []


def test_callback_de_item_inexistente_no_revienta(tmp_path):
    from wbj.x.telegram import procesar_updates

    q = Queue(tmp_path / "cola.jsonl")
    bot = FakeBot([_cb("noexiste", "ok")])
    assert procesar_updates(q, bot, _estado(tmp_path)) == []
    assert bot.callbacks


def test_sin_token_falla_sin_caer_en_el_bot_de_forex(tmp_path):
    """El fallback silencioso mandaría posts de acciones al chat de forex."""
    from wbj.x.telegram import TelegramError, bot_desde

    s = SimpleNamespace(telegram_x_bot_token=None, telegram_x_chat_id="123")
    with pytest.raises(TelegramError, match="TELEGRAM_X_BOT_TOKEN"):
        bot_desde(s)


def test_sin_chat_id_falla_claro():
    from wbj.x.telegram import TelegramError, bot_desde

    s = SimpleNamespace(telegram_x_bot_token="t", telegram_x_chat_id=None)
    with pytest.raises(TelegramError, match="TELEGRAM_X_CHAT_ID"):
        bot_desde(s)


def test_mensaje_avisa_de_cifras_sin_fuente(tmp_path):
    from wbj.x.telegram import _texto_item

    q = Queue(tmp_path / "cola.jsonl")
    i = q.add(_draft("texto", numeros_no_justificados=["$412"]))
    assert "CIFRAS SIN FUENTE" in _texto_item(q.get(i.id))


def test_mensaje_numera_el_hilo(tmp_path):
    from wbj.x.telegram import _texto_item

    q = Queue(tmp_path / "cola.jsonl")
    i = q.add(_draft("primero", thread=["segundo"]))
    texto = _texto_item(q.get(i.id))
    assert "1/ primero" in texto and "2/ segundo" in texto
    assert "hilo de 2" in texto
