# ABOUTME: Renders the SIGINT 101 wallpaper: an LED-matrix hammer and sickle over faux-Cyrillic
# ABOUTME: "Welcome Comrade", phosphor green on black with scanlines. Dev tool (Pillow, fonttools).
"""Usage: make_wallpaper.py FONTS_DIR DEJAVU_BOLD_TTF OUT_PNG [WIDTH HEIGHT]

FONTS_DIR is sigint101/static/fonts (Russo One woff2). DEJAVU_BOLD_TTF carries U+262D.
"""
import io
import sys

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONTS, DEJAVU, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
W = int(sys.argv[4]) if len(sys.argv) > 4 else 800
H = int(sys.argv[5]) if len(sys.argv) > 5 else 480

GREEN = (46, 230, 106)       # the console's dark-theme phosphor, #2ee66a
DIM = (7, 34, 17)            # an unlit LED cell: there, but dark
BG = (2, 4, 3)

# Faux-Cyrillic "WELCOME COMRADE": letters swapped for Cyrillic look-alikes, comically Soviet but
# still readable by an English speaker (W=Ш, E=Э, O=Ф, R=Я, A=Д).
GREETING = "ШЭLСФМЭ СФМЯДDЭ"


def woff2_to_font(path, size):
    """A Pillow font from a woff2 file, decompressed in memory (Pillow's FreeType may lack
    brotli)."""
    buf = io.BytesIO()
    font = TTFont(path)
    font.flavor = None
    font.save(buf)
    buf.seek(0)
    return ImageFont.truetype(buf, size)


def glyph_bitmap(ttf_path, char, cells):
    """The glyph rasterised onto a cells x cells grid: True where an LED is lit."""
    scale = 8
    big = cells * scale
    # Render large, then crop to the inked pixels: the ☭ sits small inside its em square, so
    # sizing by the font's own box leaves the emblem lost in its panel.
    raw = Image.new("L", (1400, 1400), 0)
    ImageDraw.Draw(raw).text((200, 100), char, font=ImageFont.truetype(ttf_path, 1000), fill=255)
    ink = raw.crop(raw.getbbox())
    fit = int(big * 0.92)
    k = fit / max(ink.size)
    ink = ink.resize((max(1, int(ink.width * k)), max(1, int(ink.height * k))), Image.LANCZOS)
    img = Image.new("L", (big, big), 0)
    img.paste(ink, ((big - ink.width) // 2, (big - ink.height) // 2))
    # Thicken the strokes by about a third of a cell before sampling, as an LED sign's font is
    # drawn: thin strokes otherwise fall between cells and break up.
    img = img.filter(ImageFilter.MaxFilter(scale // 2 + 1))
    small = img.resize((cells, cells), Image.BOX)
    return [[small.getpixel((x, y)) > 100 for x in range(cells)] for y in range(cells)]


def draw_glow(base, layer, radius, strength=1.0):
    """Composite a green layer onto base with a soft phosphor bloom underneath."""
    bloom = layer.filter(ImageFilter.GaussianBlur(radius))
    if strength != 1.0:
        bloom = Image.eval(bloom, lambda v: min(255, int(v * strength)))
    base.paste(Image.new("RGB", base.size, GREEN), (0, 0), bloom.convert("L"))
    base.paste(Image.new("RGB", base.size, GREEN), (0, 0), layer.convert("L"))


canvas = Image.new("RGB", (W, H), BG)

# The emblem: a big LED-matrix hammer and sickle, centred right of the desktop icon column.
cells = 44
pitch = max(6, int(H * 0.62) // cells)
led = pitch - 2
grid = glyph_bitmap(DEJAVU, "☭", cells)
ex = int(W * 0.63) - cells * pitch // 2
ey = int(H * 0.13)
unlit = ImageDraw.Draw(canvas)
lit = Image.new("L", (W, H), 0)
ld = ImageDraw.Draw(lit)
for y, row in enumerate(grid):
    for x, on in enumerate(row):
        x0, y0 = ex + x * pitch, ey + y * pitch
        if on:
            ld.rectangle([x0, y0, x0 + led - 1, y0 + led - 1], fill=255)
        else:
            unlit.rectangle([x0, y0, x0 + led - 1, y0 + led - 1], fill=DIM)
draw_glow(canvas, lit, radius=6, strength=1.4)

# The greeting, in Russo One with its Cyrillic, under the emblem.
size = int(H * 0.085)
font_lat = woff2_to_font(f"{FONTS}/russoone-latin.woff2", size)
font_cyr = woff2_to_font(f"{FONTS}/russoone-cyrillic.woff2", size)
text = Image.new("L", (W, H), 0)
td = ImageDraw.Draw(text)


def advance(ch):
    f = font_cyr if ord(ch) > 0x3FF else font_lat
    return f, td.textlength(ch, font=f)


track = int(size * 0.06)
total = sum(advance(c)[1] + track for c in GREETING) - track
tx = int(W * 0.63 - total / 2)
ty = ey + cells * pitch + int(H * 0.05)
for ch in GREETING:
    f, w = advance(ch)
    td.text((tx, ty), ch, font=f, fill=255)
    tx += w + track
draw_glow(canvas, text, radius=5, strength=1.2)

# CRT finish: every third row dimmed for scanlines, then a gentle vignette.
px = canvas.load()
for y in range(0, H, 3):
    for x in range(W):
        r, g, b = px[x, y]
        px[x, y] = (r * 6 // 10, g * 6 // 10, b * 6 // 10)
vignette = Image.new("L", (W, H), 0)
vd = ImageDraw.Draw(vignette)
vd.ellipse([-W * 0.25, -H * 0.35, W * 1.25, H * 1.35], fill=255)
vignette = vignette.filter(ImageFilter.GaussianBlur(H // 5))
canvas = Image.composite(canvas, Image.new("RGB", (W, H), (0, 0, 0)), vignette)

canvas.save(OUT, optimize=True)
print("wrote", OUT, canvas.size)
