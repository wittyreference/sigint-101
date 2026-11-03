# ABOUTME: Builds the SIGINT 101 desktop icon: a phosphor-green hammer and sickle over faux-Cyrillic
# ABOUTME: "ЅIGIИT 101", every glyph converted to SVG paths so the icon needs no installed font.
import sys

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

FONTS = sys.argv[1]          # sigint101/static/fonts
DEJAVU = sys.argv[2]         # DejaVuSans-Bold.ttf (carries U+262D)
OUT = sys.argv[3]

GREEN = "#2ee66a"            # the console's dark-theme phosphor
BG = "#040806"

latin = TTFont(f"{FONTS}/russoone-latin.woff2")
cyr = TTFont(f"{FONTS}/russoone-cyrillic.woff2")
dejavu = TTFont(DEJAVU)


def font_for(ch):
    for f in (cyr, latin) if ord(ch) > 0x3FF else (latin, cyr):
        if ord(ch) in f.getBestCmap():
            return f
    raise SystemExit(f"no glyph for {ch!r}")


def run_paths(text, size, x, baseline, letter_space=0.0):
    """SVG path data for `text` set at `size` px with its baseline at y=baseline, from x."""
    out, pen_x = [], x
    for ch in text:
        f = font_for(ch)
        upm = f["head"].unitsPerEm
        scale = size / upm
        gname = f.getBestCmap()[ord(ch)]
        gs = f.getGlyphSet()
        pen = SVGPathPen(gs)
        gs[gname].draw(TransformPen(pen, (scale, 0, 0, -scale, pen_x, baseline)))
        out.append(pen.getCommands())
        pen_x += gs[gname].width * scale + letter_space
    return " ".join(out), pen_x - x


def width_of(text, size, letter_space=0.0):
    return run_paths(text, size, 0, 0, letter_space)[1]


def centred(text, size, baseline, letter_space=0.0):
    w = width_of(text, size, letter_space)
    return run_paths(text, size, (256 - w) / 2, baseline, letter_space)[0]


# Hammer and sickle from DejaVu Sans Bold, scaled to sit in the top of the tile.
gs = dejavu.getGlyphSet()
gname = dejavu.getBestCmap()[0x262D]
upm = dejavu["head"].unitsPerEm
size = 128
scale = size / upm
glyph_w = gs[gname].width * scale
pen = SVGPathPen(gs)
gs[gname].draw(TransformPen(pen, (scale, 0, 0, -scale, (256 - glyph_w) / 2, 128)))
sickle = pen.getCommands()

word = centred("ЅIGIИT", 46, 186, letter_space=2)
num = centred("101", 40, 232, letter_space=4)

svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256">
  <title>SIGINT 101</title>
  <defs>
    <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="3.2" result="blur"/>
      <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>
  <rect x="4" y="4" width="248" height="248" rx="22" fill="{BG}"/>
  <rect x="10" y="10" width="236" height="236" rx="17" fill="none"
        stroke="{GREEN}" stroke-width="3"/>
  <rect x="16" y="16" width="224" height="224" rx="12" fill="none"
        stroke="{GREEN}" stroke-width="1.5" opacity="0.7"/>
  <g fill="{GREEN}" filter="url(#glow)">
    <path d="{sickle}"/>
    <path d="{word}"/>
    <path d="{num}"/>
  </g>
</svg>
"""
open(OUT, "w").write(svg)
print("wrote", OUT, len(svg), "bytes")
