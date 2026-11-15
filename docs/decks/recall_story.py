"""Recall Watch for Canva, told as a story, in the style of the Cancer Care Atlas (story) deck.

Ten slides: a hook, the gap, the product, the receipt, the loop, the live proof, the real cases,
trust, the pilot. A light page with white cards, a near-black plate for the hook and the pilot,
Context.dev blue for the loop and the proof, the app's red for a page that still sells.
IBM Plex Sans and DM Mono, because Canva has both. Coordinates are pixels of a 1600 x 900 slide.
The screenshots in shots/ come from the running app on 15 November 2026. The Context.dev logo is
from Context.dev's own brand record (brand.dev), rendered from its SVG.

    uv run --with python-pptx --with pillow python recall_story.py
"""
import os

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Pt

HERE = os.path.dirname(os.path.abspath(__file__))
A = lambda n: os.path.join(HERE, "assets", n)
S = lambda n: os.path.join(HERE, "shots", n)
IMG = os.path.join(HERE, "img"); os.makedirs(IMG, exist_ok=True)
EVIDENCE = os.path.join(HERE, "..", "..", "..", "recall-watch", "evidence", "artifacts")
OUT = os.path.join(HERE, "Recall Watch (story).pptx")


def c(h): return RGBColor.from_string(h.lstrip("#"))


BLUE, BLUE_SOFT, RED, RED_SOFT, GREEN, GREEN_SOFT = c("#2563eb"), c("#e8effe"), c("#c0322a"), c("#fbecea"), c("#15803d"), c("#e7f5ec")
BG, SURF, INK, MUTED, LINE = c("#f4f5f7"), c("#ffffff"), c("#0f172a"), c("#5b6472"), c("#e2e5ea")
PLATE, PLATE2, PLATE_INK, PLATE_MUTED, GHOST = c("#0b1220"), c("#172033"), c("#f5f7fb"), c("#94a3b8"), c("#334155")
BODY, MONO = "IBM Plex Sans", "DM Mono"
PX = 914400 // 120
TOTAL = 10


def px(v): return Emu(int(v * PX))


p = Presentation(); p.slide_width, p.slide_height = px(1600), px(900)
N = [0]


def new(bg=BG):
    s = p.slides.add_slide(p.slide_layouts[6]); f = s.background.fill; f.solid(); f.fore_color.rgb = bg
    N[0] += 1
    return s


def text(s, x, y, w, h, t, size, color=INK, bold=False, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, font=BODY, gap=0.0, spacing=None):
    b = s.shapes.add_textbox(px(x), px(y), px(w), px(h)); tf = b.text_frame
    tf.word_wrap = True; tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    for i, line in enumerate(t if isinstance(t, list) else [t]):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.alignment = align; para.space_after = Pt(size * 0.75 * gap)
        if spacing: para.line_spacing = spacing
        r = para.add_run(); r.text = line
        r.font.size = Pt(size * 0.75); r.font.bold = bold; r.font.name = font; r.font.color.rgb = color
    return b


def rect(s, x, y, w, h, fill=SURF, line=LINE, lw=1, radius=12, shape=MSO_SHAPE.ROUNDED_RECTANGLE):
    r = s.shapes.add_shape(shape, px(x), px(y), px(w), px(h))
    if fill is None: r.fill.background()
    else: r.fill.solid(); r.fill.fore_color.rgb = fill
    if line is None: r.line.fill.background()
    else: r.line.color.rgb = line; r.line.width = Pt(lw * 0.75)
    if shape == MSO_SHAPE.ROUNDED_RECTANGLE: r.adjustments[0] = min(0.5, radius / min(w, h))
    r.shadow.inherit = False
    return r


def bar(s, x, y, w, h, color): return rect(s, x, y, w, h, fill=color, line=None, shape=MSO_SHAPE.RECTANGLE)
def dot(s, x, y, d, fill, line=None, lw=1): return rect(s, x - d / 2, y - d / 2, d, d, fill=fill, line=line, lw=lw, shape=MSO_SHAPE.OVAL)


def pic(s, path, x, y, w=None, h=None):
    iw, ih = Image.open(path).size
    if w and h:
        k = min(w / iw, h / ih); ww, hh = iw * k, ih * k; x += (w - ww) / 2; y += (h - hh) / 2; w, h = ww, hh
    elif w: h = ih * w / iw
    else: w = iw * h / ih
    s.shapes.add_picture(path, px(x), px(y), px(w), px(h)); return w, h


def shot(s, path, x, y, w=None, h=None):
    """A screenshot on a white card with a thin border, like a window on the page."""
    iw, ih = Image.open(path).size
    if w and h:
        k = min(w / iw, h / ih); w, h = iw * k, ih * k
    elif w: h = ih * w / iw
    else: w = iw * h / ih
    rect(s, x - 10, y - 10, w + 20, h + 20, fill=SURF, line=LINE, radius=14)
    pic(s, path, x, y, w, h)
    return w, h


def mark(s, x, y, d, dark=False):
    """The Recall Watch mark: a ghost circle (what the page still shows) behind a red one (the recall)."""
    dot(s, x + d * 0.62, y + d * 0.38, d, None, PLATE_MUTED if dark else c("#9aa3b2"), lw=max(1.5, d / 14))
    dot(s, x + d * 0.38, y + d * 0.62, d, RED)


def logo_pill(s, x, y, w=250, h=64):
    rect(s, x, y, w, h, fill=SURF, line=None, radius=14)
    pic(s, A("context-dev-black.png"), x + 18, y + 12, w - 36, h - 24)


def header(s, title, sub=None, dark=False):
    bar(s, 0, 0, 1600, 6, BLUE)
    mark(s, 80, 36, 22, dark)
    text(s, 116, 34, 400, 30, "RECALL WATCH", 14, PLATE_INK if dark else INK, True, anchor=MSO_ANCHOR.MIDDLE)
    text(s, 1320, 34, 200, 30, f"{N[0]} / {TOTAL}", 13, PLATE_MUTED if dark else MUTED, align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)
    text(s, 80, 108, 1440, 64, title, 44, PLATE_INK if dark else INK, True)
    if sub: text(s, 80, 172, 1440, 34, sub, 20, PLATE_MUTED if dark else MUTED)


def big(s, x, y, w, value, caption, color=INK, size=72, cap=MUTED):
    text(s, x, y, w, size * 1.2, value, size, color, True)
    text(s, x, y + size * 1.45, w, 70, caption, 19, cap)


def chip(s, x, y, label, fg=BLUE, bg=BLUE_SOFT, size=13):
    w = 9.8 * len(label) * size / 13 + 34
    rect(s, x, y, w, 28, fill=bg, line=None, radius=14)
    text(s, x, y, w, 28, label, size, fg, True, PP_ALIGN.CENTER, MSO_ANCHOR.MIDDLE, font=MONO)
    return w


lead = os.path.join(IMG, "lead.png")
Image.open(S("first-screen.png")).crop((220, 250, 2660, 1190)).save(lead)
chili = os.path.join(EVIDENCE, "case-c-02ec64b97b-0.png")
pudding = os.path.join(EVIDENCE, "case-c-b301543412-0.jpg")

# 1 Title ---------------------------------------------------------------------
s = new(PLATE)
bar(s, 0, 0, 1600, 6, BLUE)
# The motif, large: what the page still shows (ghost) and the official recall (red).
dot(s, 1180, 380, 520, None, GHOST, lw=3)
dot(s, 1080, 500, 520, c("#16203a"), None)
dot(s, 1080, 500, 360, RED)
mark(s, 90, 120, 52, dark=True)
text(s, 90, 220, 860, 240, ["Recall", "Watch"], 104, PLATE_INK, True, spacing=0.95)
text(s, 90, 560, 700, 100, "Where an official recall has not reached the public web yet.", 28, PLATE_INK)
logo_pill(s, 90, 700)
text(s, 90, 800, 700, 26, "Team Kindergarten Engineering · Junction 2026 · Context.dev challenge", 15, PLATE_MUTED)

# 2 The hook ------------------------------------------------------------------
s = new(PLATE)
bar(s, 0, 0, 1600, 6, BLUE)
text(s, 1440, 34, 80, 30, f"{N[0]} / {TOTAL}", 13, PLATE_MUTED, align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)
text(s, 80, 200, 420, 360, "18", 300, RED, True, spacing=0.9)
text(s, 530, 500, 220, 70, "days", 60, RED, True)
text(s, 760, 260, 520, 220, "after the official recall, SOK's own shop still offered the exact product for €1.19.", 34, PLATE_INK, True, spacing=1.1)
text(s, 760, 520, 520, 80, "Coop Chopped Chili 50 g. Cadmium above the limit. Recalled by the Finnish Food Authority on 27 October 2026. Read on 14 November.", 16, PLATE_MUTED)
rect(s, 1310, 230, 210, 330, fill=SURF, line=None, radius=14)
pic(s, chili, 1320, 240, 190, 310)
chip(s, 760, 640, "GTIN 7340191134195 · exact match", RED, c("#3a1416"))

# 3 The gap -------------------------------------------------------------------
s = new(); header(s, "Official in minutes. Public pages: nobody checks.",
                  "A recall is published once. Every shop and brand page that sells the product must catch up by hand.")
# A track from the notice to the day we read the page: the SLA is a sliver, the gap is the rest.
rect(s, 80, 300, 1440, 230, fill=SURF)
bar(s, 120, 400, 1360, 18, RED_SOFT)
bar(s, 120, 400, 18, 18, GREEN)
bar(s, 410, 400, 1070, 18, RED)
for x in (120, 150, 1478):
    rect(s, x, 372, 3, 74, fill=INK, line=None, shape=MSO_SHAPE.RECTANGLE)
text(s, 120, 324, 400, 30, "27 Oct · notice published", 20, GREEN, True)
text(s, 150, 456, 300, 30, "4 h target", 16, INK, True)
text(s, 1080, 324, 400, 30, "14 Nov · page still for sale", 20, RED, True, PP_ALIGN.RIGHT)
text(s, 520, 456, 900, 40, "18 days: the public page and the recall disagree", 20, RED, True)
for k, (h, t) in enumerate((("The authority", "publishes the notice and the GTIN."),
                            ("The shops", "update their own pages by hand."),
                            ("Nobody", "is shown which pages still look normal."))):
    x = 80 + k * 488
    rect(s, x, 590, 464, 150)
    text(s, x + 28, 614, 400, 40, h, 26, RED if k == 2 else INK, True)
    text(s, x + 28, 660, 400, 60, t, 19, MUTED)

# 4 The product ---------------------------------------------------------------
s = new(); header(s, "Recall Watch shows the gap, page by page.")
shot(s, lead, 90, 210, w=1000)
items = [("Official recall", "Notice, date, GTIN, lots"), ("Exact GTIN match", "Computed by code, not the model"),
         ("Public page now", "What the page visibly says"), ("Evidence receipt", "One click, every claim backed")]
for k, (h, t) in enumerate(items):
    y = 230 + k * 140
    rect(s, 1150, y, 44, 44, fill=BLUE_SOFT, line=None, radius=10)
    text(s, 1150, y, 44, 44, str(k + 1), 18, BLUE, True, PP_ALIGN.CENTER, MSO_ANCHOR.MIDDLE)
    text(s, 1214, y - 2, 320, 34, h, 24, INK, True)
    text(s, 1214, y + 34, 320, 30, t, 17, MUTED)
text(s, 80, 830, 1440, 24, "The running app, 15 November 2026. Every row is a Context.dev read of a real public page.", 13, MUTED)

# 5 The receipt ---------------------------------------------------------------
s = new(); header(s, "Every claim has a receipt.", "Not a generated compliance dashboard. A judge can check it in ten seconds.")
rows = [("Context.dev request ID", "f5f83e17-26b1-44c8-…"), ("Read at", "15 Nov 2026, 04:05:30 · cache miss"),
        ("Verbatim page text", "“n. Noin 1,19 €” · found word for word"), ("Screenshot", "taken in the same call"),
        ("Product identity", "Exact GTIN match")]
for k, (h, t) in enumerate(rows):
    y = 250 + k * 108
    rect(s, 80, y, 600, 92)
    bar(s, 80, y + 1, 5, 90, GREEN)
    text(s, 108, y + 16, 540, 28, h, 20, INK, True)
    text(s, 108, y + 50, 560, 26, t, 15, MUTED, font=MONO)
shot(s, S("receipt.png"), 760, 240, w=720, h=560)

# 6 The loop ------------------------------------------------------------------
s = new(PLATE); header(s, "Context.dev runs the loop.", "From an official notice to a decision, on live web data.", dark=True)
steps = [("Monitor", "the authority's list", "Monitors"), ("Read", "products, GTIN, lots", "Scrape · JSON"),
         ("Find", "pages on approved shops", "Search"), ("Watch", "each page, every 10 min", "Monitors · changes"),
         ("Re-read", "fresh, with screenshot", "Scrape · maxAge 0")]
bar(s, 170, 381, 1260, 3, BLUE)
for k, (h, t, api) in enumerate(steps):
    x = 170 + k * 315
    dot(s, x, 382, 64, BLUE if k else BLUE, None)
    text(s, x - 32, 350, 64, 64, f"{k + 1}", 22, SURF, True, PP_ALIGN.CENTER, MSO_ANCHOR.MIDDLE)
    text(s, x - 140, 440, 280, 44, h, 30, PLATE_INK, True, PP_ALIGN.CENTER)
    text(s, x - 140, 488, 280, 30, t, 17, PLATE_MUTED, False, PP_ALIGN.CENTER)
    w = 9.8 * len(api) + 34
    rect(s, x - w / 2, 540, w, 28, fill=PLATE2, line=None, radius=14)
    text(s, x - w / 2, 540, w, 28, api, 13, c("#93b4ff"), True, PP_ALIGN.CENTER, MSO_ANCHOR.MIDDLE, font=MONO)
rect(s, 80, 650, 1440, 120, fill=PLATE2, line=None)
text(s, 120, 650, 1000, 120, "A change re-opens a resolved page. A person decides what happens next.", 26, PLATE_INK, True, anchor=MSO_ANCHOR.MIDDLE)
logo_pill(s, 1240, 678)

# 7 The live proof ------------------------------------------------------------
s = new(); header(s, "Live: we change a page. Context.dev catches it.",
                  "A public test page the team owns, edited on stage. Kept apart from the retailer evidence.")
rect(s, 80, 250, 1440, 230, fill=SURF)
bar(s, 140, 360, 1320, 14, LINE)
bar(s, 140, 360, 1320, 14, BLUE)
for t, h, col in ((2, "Page is public", MUTED), (32, "Change detected", BLUE), (40, "New receipt", GREEN)):
    x = 140 + 1320 * t / 40
    dot(s, x, 367, 34, col)
    al = PP_ALIGN.RIGHT if t == 40 else PP_ALIGN.CENTER if t == 32 else PP_ALIGN.LEFT
    xx = x - 260 if t == 40 else x - 130 if t == 32 else x - 20
    text(s, xx, 290, 260, 40, f"+{t} s", 30, col, True, al)
    text(s, xx, 400, 260, 30, h, 18, INK, True, al)
text(s, 140, 432, 600, 26, "0 s: the team edits the GitHub gist", 15, MUTED)
for k, (v, t, col) in enumerate((("2 s", "the edited page is public", MUTED), ("32 s", "Context.dev's monitor reports the change", BLUE),
                                 ("40 s", "fresh scrape, receipt, page re-opened", GREEN))):
    big(s, 80 + k * 488, 560, 440, v, t, col, 72)
chip(s, 80, 800, "ran live on 15 Nov 2026 · run_bd1983579c…", BLUE, BLUE_SOFT)

# 8 Real cases ----------------------------------------------------------------
s = new(); header(s, "3 real recalls. 2 pages still look for sale.", "The Afterimage: what each public page still showed after the notice.")
rect(s, 80, 230, 1440, 230, fill=SURF)
pic(s, S("timeline.png"), 104, 250, w=1392)
cases = [(pudding, "K-Ruoka", "€1.45 · “Add to cart”", RED, "Protein pudding, 26 Oct"),
         (chili, "S-kaupat", "€1.19 · exact GTIN", RED, "Frozen chili, 27 Oct"),
         (None, "List check", "case opened on its own", BLUE, "First spoons, 11 Nov")]
for k, (img, h, t, col, sub) in enumerate(cases):
    x = 80 + k * 488
    rect(s, x, 500, 464, 220)
    bar(s, x, 501, 5, 218, col)
    if img: pic(s, img, x + 340, 520, 100, 140)
    text(s, x + 28, 524, 300, 26, sub.upper(), 13, MUTED, True, font=MONO)
    text(s, x + 28, 560, 300, 44, h, 32, INK, True)
    text(s, x + 28, 612, 300, 60, t, 20, col, True)
text(s, 80, 760, 1440, 30, "Every bar is built from stored Context.dev observations. Watching began 18 days after the notices.", 16, MUTED)

# 9 Trust ---------------------------------------------------------------------
s = new(); header(s, "Careful with claims. Firm on evidence.")
items = [("Identity by code", "The official GTIN on the page, or the product name. Never the model."),
         ("Fresh reads only", "Cache miss in every receipt. Excerpt checked against the stored text."),
         ("No accusation", "When a recall names lots, the next step asks which lot the page sells."),
         ("A person decides", "Recall Watch drafts the message. It sends nothing on its own.")]
for k, (h, t) in enumerate(items):
    x = 80 + k * 362
    rect(s, x, 260, 346, 400)
    rect(s, x + 28, 290, 48, 48, fill=GREEN_SOFT, line=None, radius=10)
    text(s, x + 28, 290, 48, 48, "✓", 22, GREEN, True, PP_ALIGN.CENTER, MSO_ANCHOR.MIDDLE)
    text(s, x + 28, 372, 300, 80, h, 28, INK, True)
    text(s, x + 28, 470, 290, 160, t, 18, MUTED)
text(s, 80, 720, 1440, 40, "Approved domains only, an hourly credit budget, and an operator key for every change.", 20, INK, True)

# 10 Next ---------------------------------------------------------------------
s = new(PLATE)
bar(s, 0, 0, 1600, 6, BLUE)
text(s, 1440, 34, 80, 30, f"{N[0]} / {TOTAL}", 13, PLATE_MUTED, align=PP_ALIGN.RIGHT, anchor=MSO_ANCHOR.MIDDLE)
text(s, 80, 100, 1440, 70, "Next: a 4-week pilot with one retailer.", 48, PLATE_INK, True)
for k, (d, h) in enumerate((("Week 1", "10 pages approved"), ("Weeks 2–3", "Watch from the first hour"), ("Week 4", "Report the real lag"))):
    x = 80 + k * 488
    rect(s, x, 230, 464, 220, fill=PLATE2 if k < 2 else BLUE, line=None)
    text(s, x + 32, 260, 400, 24, d.upper(), 14, c("#93b4ff") if k < 2 else SURF, True, font=MONO)
    text(s, x + 32, 300, 400, 120, h, 34, PLATE_INK, True)
for k, (v, t) in enumerate((("10", "approved domains"), ("10 min", "notice to open case"), ("4 h", "propagation SLA"))):
    big(s, 80 + k * 488, 520, 440, v, t, PLATE_INK, 64, PLATE_MUTED)
logo_pill(s, 1270, 760)
text(s, 80, 790, 1100, 30, "Recall Watch · Team Kindergarten Engineering · Junction 2026", 15, PLATE_MUTED)

p.save(OUT)
print(OUT, N[0], "slides")
