"""Universo de tickers a analizar en lote.

No es "todas las acciones": de las ~6.000 cotizadas en EE.UU., la enorme
mayoría no se menciona nunca en la conversación de mercados, y analizarlas
sería quemar cuota de API para producir material que nadie va a usar.

Este universo es el que de verdad aparece en FinTwit: mega caps, el grueso
del Nasdaq 100, los sectores que mueven titulares y los nombres de moda. La
lista se edita a mano a propósito — es una decisión editorial, no un
screener, y conviene que se vea quién entra y por qué.
"""

from __future__ import annotations

# Agrupado por sector para que añadir o quitar sea una decisión consciente.
SECTORES: dict[str, list[str]] = {
    "mega_tech": ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AVGO"],
    "semis": ["AMD", "INTC", "MU", "TSM", "ARM", "QCOM", "TXN", "ADI", "LRCX",
              "AMAT", "KLAC", "ASML", "SMCI", "MRVL", "ON", "NXPI", "MCHP"],
    "software": ["CRM", "ORCL", "ADBE", "NOW", "SNOW", "PLTR", "DDOG", "NET",
                 "CRWD", "ZS", "PANW", "MDB", "TEAM", "SHOP", "INTU", "WDAY",
                 "SQ", "TWLO", "OKTA", "SNAP"],
    "ia_y_datos": ["AI", "PATH", "SOUN", "BBAI", "IONQ", "RGTI", "QBTS",
                   "VRT", "DELL", "ANET", "CIEN", "COHR"],
    "finanzas": ["JPM", "BAC", "WFC", "GS", "MS", "C", "SCHW", "BLK", "V",
                 "MA", "AXP", "PYPL", "COIN", "HOOD", "SOFI", "AFRM", "UPST"],
    "salud": ["UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "TMO", "ABT", "DHR",
              "AMGN", "GILD", "BMY", "CVS", "MRNA", "ISRG", "VRTX", "REGN"],
    "consumo": ["WMT", "COST", "HD", "PG", "KO", "PEP", "MCD", "NKE", "SBUX",
                "TGT", "LOW", "DIS", "NFLX", "ABNB", "UBER", "LYFT", "DASH",
                "CMG", "LULU", "RCL", "CCL", "MAR"],
    "energia": ["XOM", "CVX", "COP", "SLB", "EOG", "OXY", "PSX", "VLO", "MPC",
                "KMI", "WMB", "FSLR", "ENPH", "NEE"],
    "industria": ["BA", "CAT", "DE", "GE", "HON", "UNP", "LMT", "RTX", "NOC",
                  "UPS", "FDX", "MMM", "EMR", "ETN", "PH"],
    "telecom_media": ["T", "VZ", "TMUS", "CMCSA", "WBD", "PARA", "ROKU", "SPOT"],
    "autos": ["F", "GM", "RIVN", "LCID", "NIO", "XPEV", "LI"],
    "cripto_mineras": ["MSTR", "MARA", "RIOT", "CLSK", "IREN", "HUT", "CIFR"],
    "meme_y_retail": ["GME", "AMC", "BBAI", "DJT", "RDDT"],
    "aerolineas": ["DAL", "UAL", "AAL", "LUV"],
    "reits": ["PLD", "AMT", "SPG", "O", "EQIX", "DLR"],
}

# Los ETF y los índices no tienen `companyfacts` en EDGAR: el motor no puede
# analizarlos y pedírselo solo produce ruido en el log.
NO_ANALIZABLES = {"SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "ARKK", "XLF",
                  "XLE", "XLK", "SMH", "SOXL", "TQQQ", "UVXY", "VIX"}


def universo(sectores: list[str] | None = None) -> list[str]:
    """Tickers únicos del universo, en orden estable."""
    grupos = sectores or list(SECTORES)
    vistos: list[str] = []
    for g in grupos:
        for t in SECTORES.get(g, []):
            if t not in vistos and t not in NO_ANALIZABLES:
                vistos.append(t)
    return vistos
