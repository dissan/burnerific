"""Render Burnerific's brand images from SVG.

    python brand/make_brand.py

Needs ``resvg-py`` and ``Pillow``. Writes the PNG sizes of Home Assistant's brands
spec into ``custom_components/burnerific/brand/``. Home Assistant 2026.x serves
these straight from the integration folder (``/api/brands/integration/burnerific/icon.png``).
"""

from __future__ import annotations

import io
import shutil
from pathlib import Path

import resvg_py
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "custom_components" / "burnerific" / "brand"

DEFS = """
<defs>
  <linearGradient id="outer" x1="0" y1="1" x2="0" y2="0">
    <stop offset="0" stop-color="#D9310B"/>
    <stop offset="0.55" stop-color="#F25A0F"/>
    <stop offset="1" stop-color="#FF8F1F"/>
  </linearGradient>
  <linearGradient id="inner" x1="0" y1="1" x2="0" y2="0">
    <stop offset="0" stop-color="#FFB21E"/>
    <stop offset="1" stop-color="#FFE36B"/>
  </linearGradient>
  <linearGradient id="wood" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#E2B47A"/>
    <stop offset="0.5" stop-color="#C98F4E"/>
    <stop offset="1" stop-color="#9E6630"/>
  </linearGradient>
  <radialGradient id="cap" cx="0.45" cy="0.45" r="0.6">
    <stop offset="0" stop-color="#B07A40"/>
    <stop offset="1" stop-color="#7A4A1F"/>
  </radialGradient>
</defs>
"""

FLAME = """
<path fill="url(#outer)" d="M256 30
  C 306 112, 404 168, 394 292
  C 386 384, 322 424, 256 424
  C 190 424, 126 384, 118 292
  C 112 214, 166 172, 188 112
  C 204 166, 224 186, 240 188
  C 228 136, 234 80, 256 30 Z"/>
<path fill="url(#inner)" d="M262 168
  C 292 220, 338 252, 332 314
  C 328 368, 296 396, 256 396
  C 216 396, 184 368, 182 322
  C 180 282, 204 260, 220 234
  C 228 262, 240 272, 250 272
  C 244 238, 248 204, 262 168 Z"/>
"""


def pellet(cx: float, cy: float, angle: float, length: float = 156, d: float = 54) -> str:
    half, r = length / 2, d / 2
    lines = "".join(
        f'<line x1="{x}" y1="{-r + 9}" x2="{x + 6}" y2="{r - 9}" stroke="#8A5527" stroke-opacity="0.35" stroke-width="4" stroke-linecap="round"/>'
        for x in (-half + 34, -half + 70, -half + 104)
    )
    return f"""
<g transform="translate({cx} {cy}) rotate({angle})">
  <rect x="{-half}" y="{-r}" width="{length}" height="{d}" rx="{r}" fill="url(#wood)"/>
  {lines}
  <ellipse cx="{half - r + 8}" cy="0" rx="{r * 0.62}" ry="{r}" fill="url(#cap)"/>
  <ellipse cx="{half - r + 8}" cy="0" rx="{r * 0.62}" ry="{r}" fill="none" stroke="#E7BE86" stroke-opacity="0.55" stroke-width="3"/>
</g>"""


MARK = FLAME + pellet(168, 428, -14) + pellet(346, 430, 12) + pellet(250, 458, 0)


def icon_svg(size: int) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 512 512">{DEFS}{MARK}</svg>'


def logo_svg(height: int, text_color: str) -> str:
    # Mark on the left, wordmark to the right; 4:1 canvas trimmed afterwards.
    w = height * 4
    s = height / 512
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{height}" viewBox="0 0 {w} {height}">
  {DEFS}
  <g transform="scale({s})">{MARK}</g>
  <text x="{height * 0.80}" y="{height * 0.69}" font-family="Segoe UI, Arial, sans-serif" font-weight="900"
        font-size="{height * 0.5}" fill="{text_color}" letter-spacing="{-height * 0.01}">Burnerific</text>
</svg>"""


def render(svg: str) -> Image.Image:
    png = resvg_py.svg_to_bytes(svg_string=svg, skip_system_fonts=False)
    return Image.open(io.BytesIO(bytes(png))).convert("RGBA")


def trimmed(img: Image.Image) -> Image.Image:
    box = img.getbbox()
    return img.crop(box) if box else img


def square(img: Image.Image, size: int) -> Image.Image:
    """Trim, then centre on a square canvas the way the brands repo wants icons."""
    img = trimmed(img)
    side = max(img.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    return canvas.resize((size, size), Image.LANCZOS)


def save(img: Image.Image, name: str) -> None:
    SHIPPED.mkdir(parents=True, exist_ok=True)
    img.save(SHIPPED / name, optimize=True)


def main() -> None:
    big = render(icon_svg(1024))
    save(square(big, 256), "icon.png")
    save(square(big, 512), "icon@2x.png")
    for prefix, color in (("", "#2B2B2B"), ("dark_", "#F2F2F2")):
        save(trimmed(render(logo_svg(146, color))), f"{prefix}logo.png")
        save(trimmed(render(logo_svg(290, color))), f"{prefix}logo@2x.png")
    shutil.copy(SHIPPED / "icon.png", ROOT / "icon.png")  # for HACS / the README
    print("wrote", sorted(p.name for p in SHIPPED.iterdir()))


if __name__ == "__main__":
    main()
