"""Tests del universo de tickers para el análisis en lote."""

from __future__ import annotations

import pytest

from wbj.universo import NO_ANALIZABLES, SECTORES, universo


def test_universo_sin_duplicados():
    u = universo()
    assert len(u) == len(set(u))


def test_excluye_etfs_e_indices():
    """EDGAR no tiene companyfacts de un ETF: pedirlo solo ensucia el log."""
    u = set(universo())
    assert not (u & NO_ANALIZABLES)


def test_filtra_por_sector():
    semis = universo(["semis"])
    assert "NVDA" not in semis      # NVDA vive en mega_tech
    assert "AMD" in semis
    assert len(semis) < len(universo())


def test_sector_inexistente_devuelve_vacio():
    assert universo(["cripto_lunar"]) == []


def test_orden_estable():
    """El orden importa: un lote interrumpido se reanuda por posición."""
    assert universo() == universo()


@pytest.mark.parametrize("ticker", ["AAPL", "NVDA", "JPM", "XOM", "GME"])
def test_cubre_los_nombres_que_mueven_fintwit(ticker):
    assert ticker in universo()


def test_todo_ticker_tiene_forma_de_ticker():
    for t in universo():
        assert t.isupper() and 1 <= len(t) <= 5 and t.isalpha()


def test_cada_sector_tiene_contenido():
    for nombre, tickers in SECTORES.items():
        assert tickers, f"el sector {nombre} está vacío"
