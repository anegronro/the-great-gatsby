#!/usr/bin/env python3
"""Genera el apple-touch-icon.png de The Great Gatsby.

iOS NO usa el favicon SVG al "Agregar a inicio": exige un PNG apple-touch-icon.
Este script dibuja el emblema Art Déco (mismo diseño que _emblem_svg en webapp.py)
a sangre completa sobre fondo tinta — iOS le redondea las esquinas solo, así que
el ícono se ve como una app de verdad. Se corre UNA vez; el PNG se commitea como
asset estático y la webapp lo sirve (sin rasterizador en runtime, ni en el VPS).

    python engine/scripts/gen_app_icon.py

Salida: engine/scripts/assets/apple-touch-icon.png (180x180, el tamaño de iPhone).
Se dibuja a 4x (720px) y se reduce con LANCZOS para bordes nítidos.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Paleta Art Déco (igual que webapp.py)
INK = (28, 23, 16)       # #1C1710  fondo
GOLD = (198, 161, 91)    # #C6A15B  aro / sol / chevrons
CREAM = (244, 236, 214)  # #F4ECD6  la G
GREEN = (30, 107, 84)    # #1E6B54  la luz

OUT = Path(__file__).resolve().parent / "assets" / "apple-touch-icon.png"
FONT_PATHS = [
    "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_PATHS:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def render(final: int = 180, scale: int = 4) -> Image.Image:
    S = final * scale
    img = Image.new("RGB", (S, S), INK)  # fondo a sangre completa (sin alpha)
    d = ImageDraw.Draw(img)
    cx = cy = S / 2
    R = S / 2

    def circle(r, width, color):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=max(1, int(width)))

    # Aros dorados (proporciones del SVG: 47/44/33 sobre 50 de radio)
    circle(R * 0.94, S * 0.009, GOLD)          # aro exterior
    circle(R * 0.66, S * 0.005, (150, 122, 68))  # aro interior tenue

    # Sol naciente: 48 rayos entre r1 y r2
    n = 48
    r1, r2 = R * 0.74, R * 0.86
    for i in range(n):
        a = i / n * 2 * math.pi
        x1, y1 = cx + r1 * math.cos(a), cy + r1 * math.sin(a)
        x2, y2 = cx + r2 * math.cos(a), cy + r2 * math.sin(a)
        # rayos alternos más marcados
        w = S * (0.010 if i % 2 else 0.006)
        d.line([x1, y1, x2, y2], fill=GOLD, width=max(1, int(w)))

    # La "G" centrada
    f = _font(int(S * 0.5))
    tb = d.textbbox((0, 0), "G", font=f)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    d.text((cx - tw / 2 - tb[0], cy - th / 2 - tb[1]), "G", font=f, fill=CREAM)

    # La luz verde arriba
    gr = S * 0.028
    gy = cy - R * 0.56
    d.ellipse([cx - gr, gy - gr, cx + gr, gy + gr], fill=GREEN)

    return img.resize((final, final), Image.LANCZOS)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img = render()
    img.save(OUT, "PNG")
    print(f"✅ {OUT}  ({img.size[0]}x{img.size[1]})")


if __name__ == "__main__":
    main()
