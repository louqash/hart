"""Build the face's bitmap assets from the hart web UI's fonts and colours.

Connect IQ cannot draw the site's web fonts, so each size the face uses is baked
into a BMFont .fnt/.png pair under resources/fonts/, and the launcher icon is
drawn into resources/drawables/. Run after changing a size, glyph set or colour:

    uv run --with pillow --with fonttools --with brotli python tools/make_assets.py
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
WEB_FONTS = ROOT.parent / "src/hart/server/web/static/fonts"
OUT = ROOT / "resources/fonts"
DRAWABLES = ROOT / "resources/drawables"

BG = (10, 20, 15)
CREAM = (242, 232, 213)
GREEN = (110, 184, 134)
SOFT = (26, 47, 35)

DIGITS = "0123456789"
UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
ASCII = "".join(chr(c) for c in range(0x20, 0x7F)) + "·"


@dataclass(frozen=True)
class Spec:
    name: str
    source: str
    weight: int
    size: int
    chars: str
    tracking: int = 0  # extra advance per glyph, like CSS letter-spacing


SPECS = [
    Spec("time", "playfair-display-latin-wght-normal.woff2", 500, 132, DIGITS + ":"),
    Spec("label", "inter-latin-wght-normal.woff2", 400, 24, UPPER + DIGITS + " ", tracking=3),
    Spec("body", "inter-latin-wght-normal.woff2", 400, 26, ASCII),
    Spec("small", "inter-latin-wght-normal.woff2", 400, 20, ASCII),
    Spec("arc", "inter-latin-wght-normal.woff2", 500, 22, DIGITS + "-"),
    Spec("serif_italic", "playfair-display-latin-wght-italic.woff2", 400, 26, ASCII),
]


def static_ttf(source: str, weight: int) -> bytes:
    """Fix the variable font at one weight and map digits to lining, tabular figures."""
    font = TTFont(WEB_FONTS / source)
    if "fvar" in font:
        font = instantiateVariableFont(font, {"wght": weight})
    substitutions = _single_substitutions(font, ("lnum", "tnum"))
    for table in font["cmap"].tables:
        for code in map(ord, DIGITS):
            glyph = table.cmap.get(code)
            if glyph is None:
                continue
            for feature in ("lnum", "tnum"):
                glyph = substitutions[feature].get(glyph, glyph)
            table.cmap[code] = glyph
    buf = io.BytesIO()
    font.flavor = None
    font.save(buf)
    return buf.getvalue()


def _single_substitutions(font: TTFont, tags: tuple[str, ...]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {tag: {} for tag in tags}
    if "GSUB" not in font:
        return result
    gsub = font["GSUB"].table
    for record in gsub.FeatureList.FeatureRecord:
        if record.FeatureTag not in result:
            continue
        for index in record.Feature.LookupListIndex:
            lookup = gsub.LookupList.Lookup[index]
            for sub in lookup.SubTable:
                if lookup.LookupType == 7:  # extension
                    sub = sub.ExtSubTable
                mapping = getattr(sub, "mapping", None)
                if mapping:
                    result[record.FeatureTag].update(mapping)
    return result


def render(spec: Spec) -> None:
    font = ImageFont.truetype(io.BytesIO(static_ttf(spec.source, spec.weight)), spec.size)
    ascent, descent = font.getmetrics()
    line_height = ascent + descent

    glyphs = []
    for ch in spec.chars:
        advance = round(font.getlength(ch)) + spec.tracking
        pad = spec.size // 2
        canvas = Image.new("L", (int(font.getlength(ch)) + 2 * pad, line_height + 2 * pad), 0)
        ImageDraw.Draw(canvas).text((pad, pad), ch, font=font, fill=255)
        box = canvas.getbbox()
        if box is None:  # space
            glyphs.append((ch, None, 0, 0, advance))
            continue
        crop = canvas.crop(box)
        glyphs.append((ch, crop, box[0] - pad, box[1] - pad, advance))

    # Shelf-pack into a sheet no wider than 512px
    width, x, y, shelf = 512, 0, 0, 0
    placed = []
    for ch, img, xo, yo, adv in glyphs:
        w, h = img.size if img else (0, 0)
        if x + w > width:
            x, y, shelf = 0, y + shelf + 1, 0
        placed.append((ch, img, x, y, w, h, xo, yo, adv))
        x += w + 1
        shelf = max(shelf, h)
    height = y + shelf + 1

    sheet = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    lines = [
        f'info face="{spec.name}" size={spec.size} bold=0 italic=0 charset="" unicode=1 '
        f"stretchH=100 smooth=1 aa=1 padding=0,0,0,0 spacing=1,1",
        f"common lineHeight={line_height} base={ascent} scaleW={width} scaleH={height} pages=1 packed=0",
        f'page id=0 file="{spec.name}.png"',
        f"chars count={len(placed)}",
    ]
    for ch, img, px, py, w, h, xo, yo, adv in placed:
        if img:
            white = Image.new("RGBA", img.size, (255, 255, 255, 255))
            white.putalpha(img)
            sheet.paste(white, (px, py))
        lines.append(
            f"char id={ord(ch)} x={px} y={py} width={w} height={h} "
            f"xoffset={xo} yoffset={yo} xadvance={adv} page=0 chnl=15"
        )

    OUT.mkdir(parents=True, exist_ok=True)
    sheet.save(OUT / f"{spec.name}.png")
    (OUT / f"{spec.name}.fnt").write_text("\n".join(lines) + "\n")
    print(f"{spec.name}: {len(placed)} glyphs, {width}x{height}")


def make_icon(size: int = 65) -> None:
    """Launcher icon: a forest-dark disc, a green arc and a cream italic h."""
    scale = 4
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((0, 0, big - 1, big - 1), fill=BG)
    inset, width = 5 * scale, 3 * scale
    box = (inset, inset, big - inset, big - inset)
    draw.arc(box, 0, 360, fill=SOFT, width=width)
    draw.arc(box, 140, 330, fill=GREEN, width=width)
    italic = "playfair-display-latin-wght-italic.woff2"
    font = ImageFont.truetype(io.BytesIO(static_ttf(italic, 500)), 44 * scale)
    draw.text((big / 2, big / 2), "h", font=font, fill=CREAM, anchor="mm")
    DRAWABLES.mkdir(parents=True, exist_ok=True)
    img.resize((size, size), Image.LANCZOS).save(DRAWABLES / "launcher_icon.png")
    print("launcher_icon.png")


if __name__ == "__main__":
    for spec in SPECS:
        render(spec)
    make_icon()
