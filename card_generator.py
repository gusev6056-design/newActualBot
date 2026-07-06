from PIL import Image, ImageDraw, ImageFont, ImageFilter
import io
import os
import math

try:
    import numpy as np
    _NUMPY = True
except ImportError:
    _NUMPY = False

# ==================== PATHS ====================
_THIS_DIR   = os.path.dirname(os.path.abspath(__file__))
_ASSETS_DIR = _THIS_DIR

# Level frame PNG filenames
LEVEL_FRAME_FILES = {
    1:  "frame_level_1_1783334323790.png",
    2:  "frame_level_2_1783334323811.png",
    3:  "frame_level_3_1783334323828.png",
    4:  "frame_level_4_1783334323842.png",
    5:  "frame_level_5_1783334323852.png",
    6:  "frame_level_6_1783334323890.png",
    7:  "frame_level_7_1783334323865.png",
    8:  "frame_level_8_1783334323880.png",
    9:  "frame_level_9_1783334323901.png",
    10: "frame_level_10_1783334323914.png",
}

# Level banner palettes — matched to each frame's dominant colour
LEVEL_BANNER_CFG = {
    1:  {"main": (120, 120, 120), "accent": (190, 190, 190),
         "bg":   (18,  18,  18),  "label": "IRON"},
    2:  {"main": (170, 100,  35), "accent": (235, 160,  60),
         "bg":   (24,  12,   4),  "label": "BRONZE"},
    3:  {"main": (  0, 175, 175), "accent": ( 80, 235, 235),
         "bg":   ( 4,  20,  24),  "label": "SILVER"},
    4:  {"main": (  0, 185, 215), "accent": ( 70, 240, 255),
         "bg":   ( 2,  16,  28),  "label": "STEEL"},
    5:  {"main": ( 35, 195,  60), "accent": (100, 255, 120),
         "bg":   ( 4,  20,   6),  "label": "FOREST"},
    6:  {"main": (190,   0, 225), "accent": (230,  80, 255),
         "bg":   (18,   4,  26),  "label": "VOID"},
    7:  {"main": (210, 165,   0), "accent": (255, 220,  50),
         "bg":   (22,  16,   2),  "label": "GOLD"},
    8:  {"main": (210,  28,  12), "accent": (255, 110,  30),
         "bg":   (24,   4,   2),  "label": "FIRE"},
    9:  {"main": (150, 225, 255), "accent": (220, 248, 255),
         "bg":   ( 4,  14,  22),  "label": "ICE"},
    10: {"main": None,            "accent": (255, 255, 255),
         "bg":   ( 8,   4,  14),  "label": "PRISM"},   # rainbow — handled separately
}

# ==================== COLORS ====================
BG           = (18,  17,  12)
BG_GRID      = (24,  23,  16)
CARD_BG      = (28,  27,  18)
CARD_BG_GOLD = (42,  36,  10)
GOLD         = (232, 185,   0)
GOLD_DIM     = (165, 128,   0)
WHITE        = (255, 255, 255)
GRAY         = (140, 138, 120)
LGRAY        = (195, 192, 170)
GREEN        = ( 50, 200, 110)
RED          = (210,  55,  55)
TEAL         = (  0, 168, 170)
TEAL_DIM     = (  0, 110, 115)
CT_BLUE      = ( 60, 130, 210)
T_ORANGE     = (215, 120,  25)

BL_WHITE     = (245, 245, 250)
BL_BLACK     = ( 12,  10,  18)
BL_BLUE      = (  0,  80, 220)
BL_BLUE_LT   = ( 30, 130, 255)
BL_NAVY      = (  8,  18,  52)

LVL_COLORS = {
    1:  ( 90,  90,  90), 2:  (110, 110, 110), 3:  (  0, 160, 160),
    4:  (  0, 160, 160), 5:  ( 30, 130, 210), 6:  ( 30, 130, 210),
    7:  (200, 155,   0), 8:  (200, 155,   0), 9:  (215,  85,  10),
    10: (232, 185,   0),
}

# ==================== FONT LOADING ====================
_font_cache: dict = {}

def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    candidates_bold = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    ]
    candidates_reg = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    ]
    for path in (candidates_bold if bold else candidates_reg):
        if os.path.exists(path):
            try:
                f = ImageFont.truetype(path, size)
                _font_cache[key] = f
                return f
            except Exception:
                pass
    try:
        f = ImageFont.load_default(size=size)
    except TypeError:
        f = ImageFont.load_default()
    _font_cache[key] = f
    return f


# ==================== DRAW HELPERS ====================
def _tw(draw, text: str, font) -> int:
    try:
        return int(draw.textlength(text, font=font))
    except Exception:
        return int(font.getlength(text))

def _rr(draw, xy, r: int, fill=None, outline=None, width: int = 1):
    try:
        draw.rounded_rectangle(xy, radius=r, fill=fill, outline=outline, width=width)
    except AttributeError:
        draw.rectangle(xy, fill=fill, outline=outline, width=width)

def _text_r(draw, x, y, text, font, fill):
    draw.text((x - _tw(draw, text, font), y), text, font=font, fill=fill)

def _text_c(draw, cx, y, text, font, fill):
    draw.text((cx - _tw(draw, text, font) // 2, y), text, font=font, fill=fill)

def _draw_scalloped_badge(draw, cx, cy, size,
                           badge_color=(29, 155, 240), check_color=(255, 255, 255)):
    R_outer = size / 2.0
    R_inner = R_outer * 0.82
    n_scallops = 12
    n_pts = n_scallops * 16
    pts = []
    for i in range(n_pts):
        angle = 2 * math.pi * i / n_pts - math.pi / 2
        modulation = 0.5 + 0.5 * math.cos(n_scallops * angle)
        r = R_inner + (R_outer - R_inner) * modulation
        pts.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    draw.polygon(pts, fill=badge_color)
    s = size * 0.28
    p1 = (cx - s * 0.65, cy + s * 0.05)
    p2 = (cx - s * 0.05, cy + s * 0.60)
    p3 = (cx + s * 0.70, cy - s * 0.55)
    lw = max(2, int(size * 0.13))
    draw.line([p1, p2], fill=check_color, width=lw)
    draw.line([p2, p3], fill=check_color, width=lw)
    r_cap = max(1, lw // 2)
    for px, py in [p1, p2, p3]:
        draw.ellipse([(px - r_cap, py - r_cap), (px + r_cap, py + r_cap)], fill=check_color)

def format_league(league: str) -> str:
    return {"quals": "Quals", "default": "Default"}.get(league, league.capitalize())


# ==================== GLOW EFFECT ====================
def _apply_glow(img: Image.Image, xy, r: int, color, strength: int = 18, layers: int = 8) -> Image.Image:
    x1, y1, x2, y2 = xy
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd   = ImageDraw.Draw(glow)
    for i in range(layers, 0, -1):
        expand = int(strength * i / layers)
        alpha  = int(180 * (i / layers) ** 1.6)
        try:
            gd.rounded_rectangle(
                (x1 - expand, y1 - expand, x2 + expand, y2 + expand),
                radius=r + expand, fill=(*color, alpha),
            )
        except AttributeError:
            gd.rectangle(
                (x1 - expand, y1 - expand, x2 + expand, y2 + expand),
                fill=(*color, alpha),
            )
    glow = glow.filter(ImageFilter.GaussianBlur(radius=strength // 2))
    base = img.convert("RGBA")
    base = Image.alpha_composite(base, glow)
    return base.convert("RGB")


# ==================== LEVEL FRAME DRAWING ====================

def _draw_level_frame(img: Image.Image, ax: int, ay: int, asize: int, level: int):
    """Draw a decorative level frame around the avatar.
    First tries to load the PNG frame from attached_assets; falls back to
    programmatic drawing if the file is not found.
    Returns (img, draw).
    """
    # ── Try PNG frame first ────────────────────────────────────────────────────
    PAD = 20
    frame_file = LEVEL_FRAME_FILES.get(level)
    if frame_file:
        frame_path = os.path.join(_ASSETS_DIR, frame_file)
        if os.path.exists(frame_path):
            try:
                frame_png = Image.open(frame_path).convert("RGBA")
                frame_size = asize + PAD * 2          # e.g. 118+40 = 158
                frame_png  = frame_png.resize((frame_size, frame_size), Image.LANCZOS)
                fx = ax - PAD
                fy = ay - PAD
                # Clip to image canvas if frame extends outside (e.g. fy<0)
                src_x = max(0, -fx)
                src_y = max(0, -fy)
                dst_x = max(0,  fx)
                dst_y = max(0,  fy)
                crop  = frame_png.crop((src_x, src_y, frame_png.width, frame_png.height))
                base  = img.convert("RGBA")
                base.paste(crop, (dst_x, dst_y), crop)
                result = base.convert("RGB")
                return result, ImageDraw.Draw(result)
            except Exception as e:
                print(f"[_draw_level_frame] PNG load failed (level {level}): {e}")

    # ── Programmatic fallback ──────────────────────────────────────────────────
    cfg = LEVEL_BANNER_CFG.get(level, LEVEL_BANNER_CFG[1])

    RAINBOW = [
        (255, 50, 110), (255, 130, 20), (210, 195, 0),
        (30, 215, 55),  (0,  205, 235), (70, 70, 255), (195, 0, 255),
    ]

    fx   = ax - PAD
    fy   = ay - PAD
    fx2  = ax + asize + PAD
    fy2  = ay + asize + PAD

    draw = ImageDraw.Draw(img)

    # ── shared helpers ────────────────────────────────────────────────────────
    def _dot(cx, cy, r, col):
        draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=col)

    def _diamond(cx, cy, r, col, outline=None):
        pts = [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]
        draw.polygon(pts, fill=col, outline=outline)

    def _bracket(cx, cy, sz, thick, col, mx, my):
        """L-shaped corner bracket — all four corners drawn correctly."""
        sx, sy = (-1 if mx else 1), (-1 if my else 1)
        x1a, y1a = cx, cy
        x1b, y1b = cx + sx * sz, cy + sy * thick
        draw.rectangle([(min(x1a, x1b), min(y1a, y1b)),
                         (max(x1a, x1b), max(y1a, y1b))], fill=col)
        x2a, y2a = cx, cy
        x2b, y2b = cx + sx * thick, cy + sy * sz
        draw.rectangle([(min(x2a, x2b), min(y2a, y2b)),
                         (max(x2a, x2b), max(y2a, y2b))], fill=col)

    # ── background fill in the PAD strips (never touch avatar area) ───────────
    bg_col = cfg["bg"]
    ax2 = ax + asize
    ay2 = ay + asize
    draw.rectangle([(fx,  fy),  (fx2, ay)],  fill=bg_col)
    draw.rectangle([(fx,  ay2), (fx2, fy2)], fill=bg_col)
    draw.rectangle([(fx,  ay),  (ax,  ay2)], fill=bg_col)
    draw.rectangle([(ax2, ay),  (fx2, ay2)], fill=bg_col)

    # ── Level 1 — IRON : thick silver slab + large rivet bolts ───────────────
    if level == 1:
        draw.rectangle([(fx,      fy),      (fx2,      fy2)],      outline=(35, 35, 35),    width=3)
        draw.rectangle([(fx + 3,  fy + 3),  (fx2 - 3,  fy2 - 3)],  outline=(160, 160, 160), width=8)
        draw.rectangle([(fx + 11, fy + 11), (fx2 - 11, fy2 - 11)], outline=(75,  75,  75),  width=2)
        for mx, my in [(False, False), (True, False), (False, True), (True, True)]:
            cx2 = fx + 6  if not mx else fx2 - 6
            cy2 = fy + 6  if not my else fy2 - 6
            _dot(cx2, cy2, 7,  (195, 195, 195))
            _dot(cx2, cy2, 4,  (100, 100, 100))
            _dot(cx2, cy2, 2,  (215, 215, 215))
            _dot(cx2, cy2, 1,  (60,  60,  60))

    # ── Level 2 — BRONZE : thick copper border + circuit + large corner bolts ─
    elif level == 2:
        tl = (235, 160, 60)
        draw.rectangle([(fx,      fy),      (fx2,      fy2)],      outline=(65, 32, 4),     width=3)
        draw.rectangle([(fx + 3,  fy + 3),  (fx2 - 3,  fy2 - 3)],  outline=(200, 120, 38),  width=8)
        draw.rectangle([(fx + 11, fy + 11), (fx2 - 11, fy2 - 11)], outline=(125, 68, 14),   width=2)
        for bx2, bx2e in [(fx + 13, fx + 32), (fx2 - 32, fx2 - 13)]:
            draw.line([(bx2, fy + 5),  (bx2e, fy + 5)],   fill=tl, width=2)
            draw.line([(bx2, fy2 - 5), (bx2e, fy2 - 5)],  fill=tl, width=2)
        for by2, by2e in [(fy + 13, fy + 32), (fy2 - 32, fy2 - 13)]:
            draw.line([(fx + 5,  by2), (fx + 5,  by2e)],  fill=tl, width=2)
            draw.line([(fx2 - 5, by2), (fx2 - 5, by2e)],  fill=tl, width=2)
        for mx, my in [(False, False), (True, False), (False, True), (True, True)]:
            cx2 = fx + 3  if not mx else fx2 - 11
            cy2 = fy + 3  if not my else fy2 - 11
            draw.rectangle([(cx2, cy2), (cx2 + 8, cy2 + 8)],
                            fill=tl, outline=(140, 90, 18), width=1)
            _dot(cx2 + 4, cy2 + 4, 2, (255, 205, 110))

    # ── Level 3 — SILVER (Cyan) : teal border + large diamond gems ───────────
    elif level == 3:
        draw.rectangle([(fx,      fy),      (fx2,      fy2)],      outline=(0, 65, 65),     width=3)
        draw.rectangle([(fx + 3,  fy + 3),  (fx2 - 3,  fy2 - 3)],  outline=(0, 175, 175),   width=8)
        draw.rectangle([(fx + 11, fy + 11), (fx2 - 11, fy2 - 11)], outline=(80, 235, 235),  width=2)
        for mx, my in [(False, False), (True, False), (False, True), (True, True)]:
            cx2 = fx + 7  if not mx else fx2 - 7
            cy2 = fy + 7  if not my else fy2 - 7
            _diamond(cx2, cy2, 9, (80, 235, 235), outline=(0, 100, 100))
            _diamond(cx2, cy2, 5, (180, 255, 255))
            _diamond(cx2, cy2, 2, (80, 235, 235))
        mid_x2 = (fx + fx2) // 2
        mid_y2 = (fy + fy2) // 2
        for px2, py2 in [(mid_x2, fy + 5), (mid_x2, fy2 - 5),
                          (fx + 5, mid_y2),  (fx2 - 5, mid_y2)]:
            _dot(px2, py2, 4, (80, 235, 235))
            _dot(px2, py2, 2, (200, 255, 255))

    # ── Level 4 — STEEL (Blue) : dark panel + large cyan L-brackets ──────────
    elif level == 4:
        draw.rectangle([(fx,     fy),     (fx2,     fy2)],     outline=(0, 50, 85),     width=3)
        draw.rectangle([(fx + 3, fy + 3), (fx2 - 3, fy2 - 3)], outline=(0, 140, 180),   width=6)
        draw.rectangle([(fx + 9, fy + 9), (fx2 - 9, fy2 - 9)], outline=(70, 240, 255),  width=2)
        bsz = 22
        for mx, my in [(False, False), (True, False), (False, True), (True, True)]:
            bx2 = fx if not mx else fx2
            by2 = fy if not my else fy2
            _bracket(bx2, by2, bsz,     5, (70, 240, 255), mx, my)
            _bracket(bx2 + (2 if not mx else -2),
                     by2 + (2 if not my else -2),
                     bsz - 4, 3, (0, 160, 200), mx, my)
        mid_x2 = (fx + fx2) // 2
        mid_y2 = (fy + fy2) // 2
        for px2, py2, horiz in [(mid_x2, fy + 5, True), (mid_x2, fy2 - 5, True),
                                  (fx + 5, mid_y2, False), (fx2 - 5, mid_y2, False)]:
            if horiz:
                draw.rectangle([(px2 - 6, py2 - 3), (px2 + 6, py2 + 3)], fill=(70, 240, 255))
            else:
                draw.rectangle([(px2 - 3, py2 - 6), (px2 + 3, py2 + 6)], fill=(70, 240, 255))

    # ── Level 5 — FOREST (Green) : circuit bracket + large nodes ─────────────
    elif level == 5:
        draw.rectangle([(fx,     fy),     (fx2,     fy2)],     outline=(6, 70, 14),     width=3)
        draw.rectangle([(fx + 3, fy + 3), (fx2 - 3, fy2 - 3)], outline=(35, 195, 60),   width=7)
        draw.rectangle([(fx + 10, fy + 10), (fx2 - 10, fy2 - 10)], outline=(100, 255, 120), width=2)
        csz = 22
        for mx, my in [(False, False), (True, False), (False, True), (True, True)]:
            bx2 = fx if not mx else fx2
            by2 = fy if not my else fy2
            _bracket(bx2, by2, csz, 5, (100, 255, 120), mx, my)
            nx2 = bx2 + (11 if not mx else -11)
            ny2 = by2 + (11 if not my else -11)
            _dot(nx2, ny2, 5, (100, 255, 120))
            _dot(nx2, ny2, 3, (200, 255, 200))
            _dot(nx2, ny2, 1, (255, 255, 255))
            tx2 = bx2 + (csz if not mx else -csz)
            ty2 = by2 + (csz if not my else -csz)
            draw.line([(tx2, by2 + (5 if not my else -5)),
                        (tx2, by2 + (13 if not my else -13))], fill=(35, 195, 60), width=2)
            draw.line([(bx2 + (5 if not mx else -5), ty2),
                        (bx2 + (13 if not mx else -13), ty2)], fill=(35, 195, 60), width=2)

    # ── Level 6 — VOID (Purple) : thick border + gems + external ornaments ────
    elif level == 6:
        draw.rectangle([(fx,      fy),      (fx2,      fy2)],      outline=(70, 0, 90),     width=3)
        draw.rectangle([(fx + 3,  fy + 3),  (fx2 - 3,  fy2 - 3)],  outline=(190, 0, 225),   width=8)
        draw.rectangle([(fx + 11, fy + 11), (fx2 - 11, fy2 - 11)], outline=(230, 80, 255),  width=2)
        for mx, my in [(False, False), (True, False), (False, True), (True, True)]:
            cx2 = fx + 7  if not mx else fx2 - 7
            cy2 = fy + 7  if not my else fy2 - 7
            _diamond(cx2, cy2, 9,  (230, 80, 255), outline=(80, 0, 110))
            _diamond(cx2, cy2, 5,  (255, 160, 255))
            _diamond(cx2, cy2, 2,  (255, 230, 255))
            dx2 = 1 if not mx else -1
            dy2 = 1 if not my else -1
            draw.line([(cx2, cy2), (cx2 + dx2 * 16, cy2)],          fill=(190, 0, 225), width=2)
            draw.line([(cx2, cy2), (cx2,             cy2 + dy2*16)], fill=(190, 0, 225), width=2)
            # External ornament spikes beyond the frame border
            ox2 = fx - 1 if not mx else fx2 + 1
            oy2 = fy - 1 if not my else fy2 + 1
            sdx = -1 if not mx else 1
            sdy = -1 if not my else 1
            draw.line([(ox2, oy2), (ox2 + sdx * 9, oy2 + sdy * 9)], fill=(230, 80, 255), width=2)
            draw.line([(ox2, oy2), (ox2 + sdx * 12, oy2)],           fill=(190, 0, 225), width=1)
            draw.line([(ox2, oy2), (ox2, oy2 + sdy * 12)],           fill=(190, 0, 225), width=1)
            _dot(ox2 + sdx * 9, oy2 + sdy * 9, 2, (255, 180, 255))

    # ── Level 7 — GOLD : ornate thick gold + large triple-layer gems ──────────
    elif level == 7:
        draw.rectangle([(fx,      fy),      (fx2,      fy2)],      outline=(80, 55, 0),     width=3)
        draw.rectangle([(fx + 3,  fy + 3),  (fx2 - 3,  fy2 - 3)],  outline=(210, 165, 0),   width=8)
        draw.rectangle([(fx + 11, fy + 11), (fx2 - 11, fy2 - 11)], outline=(255, 220, 50),  width=3)
        mid_x2 = (fx + fx2) // 2
        mid_y2 = (fy + fy2) // 2
        for px2, py2 in [(mid_x2, fy + 6), (mid_x2, fy2 - 6),
                          (fx + 6, mid_y2),  (fx2 - 6, mid_y2)]:
            _diamond(px2, py2, 7,  (255, 220, 50), outline=(140, 95, 0))
            _diamond(px2, py2, 4,  (255, 255, 180))
            _diamond(px2, py2, 1,  (255, 220, 50))
        for mx, my in [(False, False), (True, False), (False, True), (True, True)]:
            cx2 = fx + 7  if not mx else fx2 - 7
            cy2 = fy + 7  if not my else fy2 - 7
            _diamond(cx2, cy2, 10, (255, 220, 50), outline=(140, 95, 0))
            _diamond(cx2, cy2, 6,  (255, 255, 180))
            _diamond(cx2, cy2, 3,  (255, 220, 50))
            _diamond(cx2, cy2, 1,  (255, 255, 255))

    # ── Level 8 — FIRE : thick red border + large flame spikes ───────────────
    elif level == 8:
        draw.rectangle([(fx,      fy),      (fx2,      fy2)],      outline=(90, 10, 2),     width=3)
        draw.rectangle([(fx + 3,  fy + 3),  (fx2 - 3,  fy2 - 3)],  outline=(210, 28, 12),   width=8)
        draw.rectangle([(fx + 11, fy + 11), (fx2 - 11, fy2 - 11)], outline=(255, 110, 30),  width=2)
        spk = 17
        for mx, my in [(False, False), (True, False), (False, True), (True, True)]:
            bx2 = fx if not mx else fx2
            by2 = fy if not my else fy2
            dx2 = 1 if not mx else -1
            dy2 = 1 if not my else -1
            pts_outer = [
                (bx2,              by2),
                (bx2 + dx2 * spk,  by2 + dy2 * 3),
                (bx2 + dx2 * 9,    by2 + dy2 * 9),
                (bx2 + dx2 * 3,    by2 + dy2 * spk),
            ]
            draw.polygon(pts_outer, fill=(255, 110, 30), outline=(180, 20, 5))
            pts_inner = [
                (bx2 + dx2 * 3,    by2 + dy2 * 3),
                (bx2 + dx2 * 11,   by2 + dy2 * 5),
                (bx2 + dx2 * 7,    by2 + dy2 * 7),
                (bx2 + dx2 * 5,    by2 + dy2 * 11),
            ]
            draw.polygon(pts_inner, fill=(255, 210, 60))

    # ── Level 9 — ICE : thick crystal border + large shard corners ───────────
    elif level == 9:
        draw.rectangle([(fx,      fy),      (fx2,      fy2)],      outline=(25, 70, 110),   width=3)
        draw.rectangle([(fx + 3,  fy + 3),  (fx2 - 3,  fy2 - 3)],  outline=(150, 225, 255), width=8)
        draw.rectangle([(fx + 11, fy + 11), (fx2 - 11, fy2 - 11)], outline=(220, 248, 255), width=2)
        for mx, my in [(False, False), (True, False), (False, True), (True, True)]:
            gx2 = fx if not mx else fx2
            gy2 = fy if not my else fy2
            dx2 = 1 if not mx else -1
            dy2 = 1 if not my else -1
            pts_outer = [
                (gx2,              gy2),
                (gx2 + dx2 * 15,   gy2 + dy2 * 3),
                (gx2 + dx2 * 9,    gy2 + dy2 * 9),
                (gx2 + dx2 * 3,    gy2 + dy2 * 15),
            ]
            draw.polygon(pts_outer, fill=(220, 248, 255), outline=(80, 160, 220))
            pts_inner = [
                (gx2 + dx2 * 3,    gy2 + dy2 * 3),
                (gx2 + dx2 * 10,   gy2 + dy2 * 5),
                (gx2 + dx2 * 6,    gy2 + dy2 * 6),
                (gx2 + dx2 * 5,    gy2 + dy2 * 10),
            ]
            draw.polygon(pts_inner, fill=(255, 255, 255))
        fw2 = fx2 - fx
        fh2 = fy2 - fy
        for t in range(1, 5):
            r2 = 3 if t % 2 == 0 else 2
            _dot(fx + int(fw2 * t / 5), fy + 5,   r2, (220, 248, 255))
            _dot(fx + int(fw2 * t / 5), fy2 - 5,  r2, (220, 248, 255))
            _dot(fx + 5,  fy + int(fh2 * t / 5),  r2, (220, 248, 255))
            _dot(fx2 - 5, fy + int(fh2 * t / 5),  r2, (220, 248, 255))

    # ── Level 10 — PRISM (Rainbow) : wide rainbow border + layered gems ───────
    else:
        fw2 = fx2 - fx
        fh2 = fy2 - fy
        seg = 24
        pts_top    = [(fx + int(i * fw2 / seg), fy)  for i in range(seg)]
        pts_right  = [(fx2, fy + int(i * fh2 / seg)) for i in range(seg)]
        pts_bottom = [(fx2 - int(i * fw2 / seg), fy2) for i in range(seg)]
        pts_left   = [(fx,  fy2 - int(i * fh2 / seg)) for i in range(seg)]
        perim = pts_top + pts_right + pts_bottom + pts_left
        n = len(perim)
        for i in range(n):
            col = RAINBOW[i % len(RAINBOW)]
            draw.line([perim[i], perim[(i + 1) % n]], fill=col, width=10)
        for i in range(n):
            col2 = RAINBOW[(i + 3) % len(RAINBOW)]
            draw.line([perim[i], perim[(i + 1) % n]], fill=col2, width=3)
        draw.rectangle([(fx + 10, fy + 10), (fx2 - 10, fy2 - 10)],
                        outline=(255, 255, 255), width=1)
        for mx, my in [(False, False), (True, False), (False, True), (True, True)]:
            cx2 = fx + 7  if not mx else fx2 - 7
            cy2 = fy + 7  if not my else fy2 - 7
            for ri, rc in enumerate(RAINBOW):
                if 8 - ri > 0:
                    _diamond(cx2, cy2, 8 - ri, rc)

    return img, draw


# ==================== BLUE LOCK DECORATIONS ====================

def _draw_bl_anime_eye(draw, cx: int, cy: int, w: int = 28, h: int = 14,
                        color=(0, 80, 220), highlight=(255, 255, 255)):
    pts = [
        (cx - w,      cy),
        (cx - w // 3, cy - h),
        (cx + w // 3, cy - h),
        (cx + w,      cy),
        (cx + w // 3, cy + h // 2),
        (cx - w // 3, cy + h // 2),
    ]
    draw.polygon(pts, fill=color)
    pr = int(h * 0.62)
    draw.ellipse([(cx - pr, cy - pr), (cx + pr, cy + pr)], fill=(6, 4, 12))
    ir = int(pr * 0.75)
    draw.ellipse([(cx - ir, cy - ir), (cx + ir, cy + ir)], fill=color)
    pc = int(ir * 0.45)
    draw.ellipse([(cx - pc, cy - pc), (cx + pc, cy + pc)], fill=(6, 4, 12))
    hs = max(3, int(h * 0.38))
    hx, hy = cx - ir // 2, cy - ir // 2
    draw.ellipse([(hx - hs, hy - hs), (hx + hs, hy + hs)], fill=highlight)
    draw.ellipse([(cx + ir // 3, cy - ir // 3 - 1),
                  (cx + ir // 3 + hs // 2, cy - ir // 3 + hs // 2 - 1)], fill=highlight)


def _draw_bl_corner_bracket(draw, x: int, y: int, size: int, color,
                              corner: str = "tl", width: int = 3):
    L = size
    if corner == "tl":
        draw.line([(x, y), (x + L, y)], fill=color, width=width)
        draw.line([(x, y), (x, y + L)], fill=color, width=width)
    elif corner == "tr":
        draw.line([(x - L, y), (x, y)], fill=color, width=width)
        draw.line([(x, y), (x, y + L)], fill=color, width=width)
    elif corner == "bl":
        draw.line([(x, y - L), (x, y)], fill=color, width=width)
        draw.line([(x, y), (x + L, y)], fill=color, width=width)
    elif corner == "br":
        draw.line([(x, y - L), (x, y)], fill=color, width=width)
        draw.line([(x - L, y), (x, y)], fill=color, width=width)


def _draw_bl_banner(draw, img: Image.Image, x1: int, y1: int, x2: int, y2: int):
    W = x2 - x1
    H = y2 - y1
    draw.rectangle([(x1, y1), (x2, y2)], fill=BL_WHITE)
    draw.rectangle([(x1, y1), (x1 + 148, y2)], fill=BL_NAVY)
    slash_pts = [
        (x1 + W // 2 - 40, y1),
        (x1 + W // 2 + 30, y1),
        (x1 + W // 2 - 30, y2),
        (x1 + W // 2 - 100, y2),
    ]
    draw.polygon(slash_pts, fill=(12, 10, 18))
    for offset in (0, 6, 12):
        pts = [
            (x1 + W // 2 - 40 + offset, y1),
            (x1 + W // 2 - 40 + offset + 4, y1),
            (x1 + W // 2 - 30 + offset + 4, y2),
            (x1 + W // 2 - 30 + offset, y2),
        ]
        draw.polygon(pts, fill=BL_BLUE_LT)
    draw.rectangle([(x1, y2 - 3), (x2, y2)], fill=BL_BLUE)
    bx, by = x2 - 48, y1 + 6
    draw.rectangle([(bx, by), (bx + 40, by + 18)], fill=BL_BLUE)
    draw.text((bx + 4, by + 2), "BL", font=_font(12, bold=True), fill=BL_WHITE)
    eye_y = y1 + H // 2
    _draw_bl_anime_eye(draw, x2 - 110, eye_y, w=22, h=10, color=BL_BLUE, highlight=BL_WHITE)
    _draw_bl_anime_eye(draw, x2 - 55,  eye_y, w=22, h=10, color=BL_BLUE, highlight=BL_WHITE)
    return draw


def _draw_bl_background(img: Image.Image) -> Image.Image:
    W, H = img.size
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    step = 48
    for i in range(-H, W + H, step):
        od.line([(i, 0), (i + H, H)], fill=(*BL_BLUE, 18), width=1)
    for i in range(-H, W + H, step * 3):
        od.line([(i, 0), (i + H, H)], fill=(*BL_BLUE, 35), width=2)
    tri_size = 60
    od.polygon([(W - tri_size, 0), (W, 0), (W, tri_size)], fill=(*BL_BLUE, 55))
    od.polygon([(0, H - tri_size), (tri_size, H), (0, H)],  fill=(*BL_BLUE, 55))
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=0))
    base = img.convert("RGBA")
    base = Image.alpha_composite(base, overlay)
    return base.convert("RGB")


# ==================== FRAME BORDER (SQUARE, NAMED COSMETIC) ====================

def _draw_frame_border(img: Image.Image, draw: ImageDraw.ImageDraw,
                       ax: int, ay: int, size: int, frame_name: str) -> tuple:
    fn = (frame_name or "").lower()

    if "gold" in fn or "золот" in fn:
        COLORS = [(232, 185, 0), (255, 215, 60), (200, 150, 0)]
        for i, col in enumerate(COLORS):
            bw = 4 - i
            off = i * 3
            draw.rectangle(
                [(ax - off - bw, ay - off - bw),
                 (ax + size + off + bw, ay + size + off + bw)],
                outline=col, width=bw,
            )
        for cx2, cy2 in [
            (ax, ay), (ax + size, ay),
            (ax, ay + size), (ax + size, ay + size)
        ]:
            s = 5
            draw.polygon(
                [(cx2, cy2 - s), (cx2 + s, cy2), (cx2, cy2 + s), (cx2 - s, cy2)],
                fill=(255, 215, 60),
            )

    elif "diamond" in fn or "алмаз" in fn:
        COLORS = [(0, 220, 230), (80, 240, 255), (0, 160, 180)]
        for i, col in enumerate(COLORS):
            bw = 4 - i
            off = i * 3
            draw.rectangle(
                [(ax - off - bw, ay - off - bw),
                 (ax + size + off + bw, ay + size + off + bw)],
                outline=col, width=bw,
            )
        bsz = 12
        for cx2, cy2, corner in [
            (ax - 6, ay - 6, "tl"), (ax + size + 6, ay - 6, "tr"),
            (ax - 6, ay + size + 6, "bl"), (ax + size + 6, ay + size + 6, "br"),
        ]:
            _draw_bl_corner_bracket(draw, cx2, cy2, bsz, (130, 245, 255),
                                    corner=corner, width=2)

    elif "elite" in fn or "элит" in fn:
        COLORS = [(148, 0, 211), (180, 60, 255), (100, 0, 160)]
        for i, col in enumerate(COLORS):
            bw = 4 - i
            off = i * 3
            draw.rectangle(
                [(ax - off - bw, ay - off - bw),
                 (ax + size + off + bw, ay + size + off + bw)],
                outline=col, width=bw,
            )
        edge_step = size // 4
        for k in range(1, 4):
            for px2, py2 in [
                (ax + k * edge_step, ay - 8),
                (ax + k * edge_step, ay + size + 8),
                (ax - 8, ay + k * edge_step),
                (ax + size + 8, ay + k * edge_step),
            ]:
                draw.ellipse([(px2 - 2, py2 - 2), (px2 + 2, py2 + 2)],
                             fill=(200, 100, 255))

    elif "blue lock" in fn or "блю лок" in fn or "bluelock" in fn or "bl" == fn:
        draw.rectangle(
            [(ax - 5, ay - 5), (ax + size + 5, ay + size + 5)],
            outline=BL_WHITE, width=4,
        )
        draw.rectangle(
            [(ax - 2, ay - 2), (ax + size + 2, ay + size + 2)],
            outline=BL_BLACK, width=2,
        )
        bsz = 18
        bracket_w = 3
        for cx2, cy2, corner in [
            (ax - 6, ay - 6, "tl"), (ax + size + 6, ay - 6, "tr"),
            (ax - 6, ay + size + 6, "bl"), (ax + size + 6, ay + size + 6, "br"),
        ]:
            _draw_bl_corner_bracket(draw, cx2, cy2, bsz, BL_BLUE_LT,
                                    corner=corner, width=bracket_w)
        _draw_bl_anime_eye(draw, ax + 12,       ay - 14, w=10, h=5,
                            color=BL_BLUE, highlight=BL_WHITE)
        _draw_bl_anime_eye(draw, ax + size - 12, ay - 14, w=10, h=5,
                            color=BL_BLUE, highlight=BL_WHITE)

    elif "уровень" in fn or "level_" in fn:
        # Named level-frame item (e.g. "Рамка Уровень 5" or "level_5").
        # Fall back to level 1 frame if no digit found in the name.
        import re as _re
        _m = _re.search(r"(\d+)", fn)
        _lvl_override = max(1, min(10, int(_m.group(1)))) if _m else 1
        return _draw_level_frame(img, ax, ay, size, _lvl_override)

    elif "premium" in fn:
        PUR_MID   = (148,  20, 220)
        PUR_LT    = (190,  90, 255)
        GOLD_BR   = (255, 210,  50)
        GOLD_MID  = (220, 170,   0)
        GOLD_DK   = (160, 110,   0)
        layers = [(GOLD_MID, 4, 0), (PUR_MID, 3, 5), (GOLD_BR, 2, 10)]
        for col, bw, off in layers:
            draw.rectangle(
                [(ax - off - bw, ay - off - bw),
                 (ax + size + off + bw, ay + size + off + bw)],
                outline=col, width=bw,
            )
        shimmer_len = 16
        for (cx2, cy2), dirs in [
            ((ax - 12, ay - 12), (+1, +1)),
            ((ax + size + 12, ay - 12), (-1, +1)),
            ((ax - 12, ay + size + 12), (+1, -1)),
            ((ax + size + 12, ay + size + 12), (-1, -1)),
        ]:
            dx, dy = dirs
            draw.line(
                [(cx2, cy2), (cx2 + dx * shimmer_len, cy2 + dy * shimmer_len)],
                fill=PUR_LT, width=2,
            )
        for cx2, cy2 in [
            (ax, ay), (ax + size, ay),
            (ax, ay + size), (ax + size, ay + size),
        ]:
            s = 6
            draw.polygon(
                [(cx2, cy2 - s), (cx2 + s, cy2), (cx2, cy2 + s), (cx2 - s, cy2)],
                fill=GOLD_BR,
            )
        crow_cx = ax + size // 2
        crow_y  = ay - 14
        cw, ch  = 14, 8
        draw.polygon([
            (crow_cx - cw, crow_y + ch),
            (crow_cx - cw, crow_y + 2),
            (crow_cx - cw // 2, crow_y + ch - 3),
            (crow_cx, crow_y),
            (crow_cx + cw // 2, crow_y + ch - 3),
            (crow_cx + cw, crow_y + 2),
            (crow_cx + cw, crow_y + ch),
        ], fill=GOLD_BR, outline=GOLD_DK)

    return img, draw


# ==================== AVATAR PASTE (SQUARE) ====================

def _paste_avatar(img: Image.Image, avatar_bytes: bytes, x: int, y: int, size: int,
                  border_color=None, border_width: int = 2,
                  square: bool = True) -> Image.Image:
    try:
        av_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
        av_img = av_img.resize((size, size), Image.LANCZOS)
        if square:
            output = Image.new("RGBA", img.size, (0, 0, 0, 0))
            output.paste(av_img, (x, y))
        else:
            mask = Image.new("L", (size, size), 0)
            md = ImageDraw.Draw(mask)
            md.ellipse([(0, 0), (size - 1, size - 1)], fill=255)
            output = Image.new("RGBA", img.size, (0, 0, 0, 0))
            output.paste(av_img, (x, y), mask)
        result = Image.alpha_composite(img.convert("RGBA"), output).convert("RGB")
        if border_color:
            d = ImageDraw.Draw(result)
            bw = border_width
            if square:
                d.rectangle(
                    [(x - bw, y - bw), (x + size + bw - 1, y + size + bw - 1)],
                    outline=border_color, width=bw,
                )
            else:
                d.ellipse(
                    [(x - bw, y - bw), (x + size + bw - 1, y + size + bw - 1)],
                    outline=border_color, width=bw,
                )
        return result
    except Exception as e:
        print(f"[_paste_avatar] {e}")
        return img


# ==================== LEVEL HELPER ====================
def get_level(elo: int) -> int:
    # Пороги совпадают с ELO_THRESHOLDS в боте:
    # Lvl1=0-199, Lvl2=200-399, Lvl3=400-599, Lvl4=600-899,
    # Lvl5=900-1099, Lvl6=1100-1399, Lvl7=1400-1599,
    # Lvl8=1600-1799, Lvl9=1800-1999, Lvl10=2000+
    thresholds = [0, 200, 400, 600, 900, 1100, 1400, 1600, 1800, 2000]
    for i in range(len(thresholds) - 1, -1, -1):
        if elo >= thresholds[i]:
            return i + 1
    return 1


# ==================== PROFILE CARD ====================
def generate_profile_card(
    username:          str,
    game_id:           str,
    user_id:           int,
    elo:               int,
    wins:              int,
    losses:            int,
    kills:             int,
    deaths:            int,
    assists:           int,
    is_premium:        bool,
    is_admin:          bool,
    global_rank:       int,
    league:            str,
    map_stats:         list,
    recent:            list,
    leaderboard:       list,
    quals_stats:       dict  = None,
    mvp_count:         int   = 0,
    is_verified:       bool  = False,
    duo_stats:         dict  = None,
    avatar_bytes:      bytes = None,
    active_frame:      str   = None,
    active_banner:     str   = None,
    active_background: str   = None,
) -> io.BytesIO:

    QUALS_H = 70 if quals_stats else 0
    DUO_H   = 70 if duo_stats   else 0
    W, H = 1055, 695 + QUALS_H + DUO_H

    _BG         = (14,  14,  14)
    _BG_GRID    = (20,  20,  20)
    _PANEL      = (24,  24,  24)
    _PANEL_GOLD = (34,  28,  10)
    _HEADER     = (18,  18,  18)
    _GOLD       = (232, 185,   0)
    _GOLD_DIM   = (165, 128,   0)
    _TEXT       = (235, 235, 235)
    _TEXT_MID   = (170, 170, 170)
    _TEXT_GRAY  = (110, 110, 110)
    _TEXT_LGRAY = (140, 140, 140)
    _GREEN      = ( 50, 198, 108)
    _RED        = (210,  52,  52)
    _TEAL       = (  0, 168, 200)
    _TEAL_DIM   = (  0, 100, 130)
    _WH         = (255, 255, 255)
    _DUO_COL    = ( 80, 170, 255)
    _DUO_DIM    = ( 40, 100, 180)

    img  = Image.new("RGB", (W, H), _BG)
    draw = ImageDraw.Draw(img)

    for x in range(0, W, 42):
        draw.line([(x, 0), (x, H)], fill=_BG_GRID, width=1)
    for y in range(0, H, 42):
        draw.line([(0, y), (W, y)], fill=_BG_GRID, width=1)

    lvl    = get_level(elo)
    games  = wins + losses
    wr     = round(wins / games * 100, 1) if games > 0 else 0.0
    kd     = round(kills / deaths, 2)     if deaths > 0 else float(kills)
    avg_k  = round(kills  / games, 1)     if games > 0 else 0.0
    kpr    = round(kills  / max(games * 8, 1), 2)
    impact = round((kills + assists) / games, 2) if games > 0 else 0.0
    rating = round(kd * (wr / 100) * 2, 2)       if games > 0 else 0.0

    # ===== BACKGROUND STYLE =====
    fn_bg = (active_background or "").lower()
    if "blue lock" in fn_bg or "блю лок" in fn_bg or "bluelock" in fn_bg:
        img = _draw_bl_background(img)
        draw = ImageDraw.Draw(img)

    # ===== BANNER STRIP =====
    fn_banner = (active_banner or "").lower()

    # ── Existing named banners ───────────────────────────────────────────────
    def _draw_gold_banner_hdr(d, x1, y1, x2, y2):
        d.rectangle([(x1, y1), (x2, y2)], fill=(54, 38, 4))
        for i, (col, h_frac) in enumerate([
            ((88, 60, 6),   0.0),
            ((110, 76, 8),  0.2),
            ((80, 55, 5),   0.55),
            ((62, 42, 4),   0.75),
        ]):
            band_y = int(y1 + (y2 - y1) * h_frac)
            band_h = int((y2 - y1) * 0.25)
            d.rectangle([(x1, band_y), (x2, band_y + band_h)], fill=col)
        step = 55
        for sx in range(x1 - (y2 - y1), x2, step):
            d.line([(sx, y1), (sx + (y2 - y1), y2)], fill=(232, 185, 0, 30), width=1)
        for sx in range(x1 - (y2 - y1), x2, step * 3):
            d.line([(sx, y1), (sx + (y2 - y1), y2)], fill=(255, 215, 60), width=2)
        d.rectangle([(x1, y1), (x2, y1 + 4)], fill=(232, 185, 0))
        d.rectangle([(x1, y2 - 4), (x2, y2)], fill=(232, 185, 0))
        for cx2, cy2 in [(x1 + 20, y1 + 20), (x2 - 20, y1 + 20),
                          (x1 + 20, y2 - 20), (x2 - 20, y2 - 20)]:
            s = 8
            d.polygon([(cx2, cy2 - s), (cx2 + s//3, cy2 - s//3),
                        (cx2 + s, cy2), (cx2 + s//3, cy2 + s//3),
                        (cx2, cy2 + s), (cx2 - s//3, cy2 + s//3),
                        (cx2 - s, cy2), (cx2 - s//3, cy2 - s//3)],
                       fill=(255, 220, 80))
        d.rectangle([(x1 + 5, y1 + 5), (x2 - 5, y2 - 5)], outline=(160, 120, 0), width=1)
        return d

    def _draw_diamond_banner_hdr(d, x1, y1, x2, y2):
        d.rectangle([(x1, y1), (x2, y2)], fill=(4, 30, 46))
        for col, h_frac in [
            ((6, 46, 68),  0.0),
            ((0, 62, 88),  0.3),
            ((4, 38, 58),  0.6),
            ((2, 24, 38),  0.8),
        ]:
            band_y = int(y1 + (y2 - y1) * h_frac)
            band_h = int((y2 - y1) * 0.3)
            d.rectangle([(x1, band_y), (x2, band_y + band_h)], fill=col)
        gstep = 40
        for gx in range(x1, x2 + gstep, gstep):
            for gy in range(y1, y2 + gstep, gstep):
                s = 8
                d.polygon([(gx, gy - s), (gx + s, gy),
                            (gx, gy + s), (gx - s, gy)],
                           outline=(0, 180, 210), width=1)
        H2 = y2 - y1
        for sx in range(x1 - H2, x2, 80):
            d.line([(sx, y1), (sx + H2, y2)], fill=(0, 210, 240), width=1)
        for sx in range(x1 - H2, x2, 240):
            d.line([(sx, y1), (sx + H2, y2)], fill=(80, 245, 255), width=2)
        d.rectangle([(x1, y1), (x2, y1 + 4)], fill=(0, 210, 230))
        d.rectangle([(x1, y2 - 4), (x2, y2)], fill=(0, 210, 230))
        for cx2, cy2 in [(x1 + 22, y1 + 22), (x2 - 22, y1 + 22),
                          (x1 + 22, y2 - 22), (x2 - 22, y2 - 22)]:
            s = 10
            d.polygon([(cx2, cy2 - s), (cx2 + s, cy2),
                        (cx2, cy2 + s), (cx2 - s, cy2)],
                       fill=(130, 245, 255))
        d.rectangle([(x1 + 5, y1 + 5), (x2 - 5, y2 - 5)], outline=(0, 160, 185), width=1)
        return d

    def _draw_elite_banner_hdr(d, x1, y1, x2, y2):
        d.rectangle([(x1, y1), (x2, y2)], fill=(22, 4, 42))
        for col, h_frac in [
            ((38, 8, 72),  0.0),
            ((48, 10, 88), 0.25),
            ((30, 6, 58),  0.55),
            ((18, 2, 36),  0.78),
        ]:
            band_y = int(y1 + (y2 - y1) * h_frac)
            band_h = int((y2 - y1) * 0.32)
            d.rectangle([(x1, band_y), (x2, band_y + band_h)], fill=col)
        H2 = y2 - y1
        for sx in range(x1 - H2, x2, 70):
            d.line([(sx, y1), (sx + H2, y2)], fill=(130, 40, 210), width=1)
        for sx in range(x1 - H2, x2, 210):
            d.line([(sx, y1), (sx + H2, y2)], fill=(180, 80, 255), width=2)
        for ox, oy, r2 in [(x1 + 60, (y1+y2)//2, 35),
                            (x2 - 60, (y1+y2)//2, 35)]:
            for layer in range(5, 0, -1):
                lr = r2 + layer * 6
                alpha_col = (100 + layer * 18, 20 + layer * 10, 200 + layer * 10)
                d.ellipse([(ox - lr, oy - lr), (ox + lr, oy + lr)], fill=alpha_col)
        d.rectangle([(x1, y1), (x2, y1 + 4)], fill=(160, 60, 230))
        d.rectangle([(x1, y2 - 4), (x2, y2)], fill=(160, 60, 230))
        for cx2, cy2 in [(x1 + 22, y1 + 22), (x2 - 22, y1 + 22),
                          (x1 + 22, y2 - 22), (x2 - 22, y2 - 22)]:
            s = 9
            d.polygon([(cx2, cy2 - s), (cx2 + s//3, cy2 - s//3),
                        (cx2 + s, cy2), (cx2 + s//3, cy2 + s//3),
                        (cx2, cy2 + s), (cx2 - s//3, cy2 + s//3),
                        (cx2 - s, cy2), (cx2 - s//3, cy2 - s//3)],
                       fill=(200, 100, 255))
        d.rectangle([(x1 + 5, y1 + 5), (x2 - 5, y2 - 5)], outline=(100, 30, 160), width=1)
        return d

    def _draw_premium_banner_hdr(d, x1, y1, x2, y2):
        W2 = x2 - x1
        H2 = y2 - y1
        d.rectangle([(x1, y1), (x2, y2)], fill=(18, 6, 36))
        for col, h_frac in [
            ((42, 10, 80),  0.0),
            ((58, 14, 106), 0.22),
            ((36,  8, 68),  0.50),
            ((20,  4, 42),  0.74),
        ]:
            band_y = int(y1 + H2 * h_frac)
            band_h = int(H2 * 0.32)
            d.rectangle([(x1, band_y), (x2, band_y + band_h)], fill=col)
        for sx in range(x1 - H2, x2, 90):
            d.line([(sx, y1), (sx + H2, y2)], fill=(100, 40, 180), width=1)
        for sx in range(x1 - H2, x2, 270):
            d.line([(sx, y1), (sx + H2, y2)], fill=(220, 170, 0), width=2)
        for sx in range(x1 - H2, x2, 45):
            d.line([(sx, y1), (sx + H2, y2)], fill=(80, 20, 140), width=1)
        d.rectangle([(x1, y1),         (x2, y1 + 5)], fill=(220, 170,   0))
        d.rectangle([(x1, y2 - 5),     (x2, y2)],     fill=(220, 170,   0))
        d.rectangle([(x1, y1 + 7),     (x2, y1 + 9)], fill=(160, 60, 230))
        d.rectangle([(x1, y2 - 9),     (x2, y2 - 7)], fill=(160, 60, 230))
        for ox, oy in [(x1 + W2 // 4, y1 + H2 // 2),
                        (x1 + 3 * W2 // 4, y1 + H2 // 2)]:
            for layer in range(6, 0, -1):
                lr = 20 + layer * 7
                r_ch = min(255, 60 + layer * 16)
                g_ch = min(255, layer * 12)
                b_ch = min(255, 180 + layer * 10)
                d.ellipse([(ox - lr, oy - lr), (ox + lr, oy + lr)],
                          fill=(r_ch, g_ch, b_ch))
        for cx2, cy2 in [(x1 + 24, y1 + 24), (x2 - 24, y1 + 24),
                          (x1 + 24, y2 - 24), (x2 - 24, y2 - 24)]:
            s = 10
            d.polygon([(cx2,       cy2 - s),
                        (cx2 + s//3, cy2 - s//3),
                        (cx2 + s,   cy2),
                        (cx2 + s//3, cy2 + s//3),
                        (cx2,       cy2 + s),
                        (cx2 - s//3, cy2 + s//3),
                        (cx2 - s,   cy2),
                        (cx2 - s//3, cy2 - s//3)],
                       fill=(255, 210, 50))
        ccx, ccy = x1 + W2 // 2, y1 + 14
        cw2, ch2 = 22, 14
        d.polygon([
            (ccx - cw2,        ccy + ch2),
            (ccx - cw2,        ccy + 3),
            (ccx - cw2 // 2,   ccy + ch2 - 4),
            (ccx,              ccy),
            (ccx + cw2 // 2,   ccy + ch2 - 4),
            (ccx + cw2,        ccy + 3),
            (ccx + cw2,        ccy + ch2),
        ], fill=(255, 210, 50), outline=(180, 130, 0))
        d.rectangle([(x1 + 6, y1 + 6), (x2 - 6, y2 - 6)],
                    outline=(140, 60, 200), width=1)
        return d

    # ── Level-based banner: low-poly crystal background + giant right-side number ──
    def _draw_level_banner_hdr(d, x1, y1, x2, y2, level):
        """Crystal polygon pattern matching the frame colour + large right-aligned number."""
        import random as _rnd
        cfg = LEVEL_BANNER_CFG.get(level, LEVEL_BANNER_CFG[1])
        mc  = cfg["main"]    # primary colour (None only for level 10)
        ac  = cfg["accent"]  # highlight / number colour
        bg  = cfg["bg"]

        BW2 = x2 - x1
        BH2 = y2 - y1

        # --- Dark base fill ---
        d.rectangle([(x1, y1), (x2, y2)], fill=bg)

        # --- Low-poly triangle grid (crystal pattern) ---
        rng  = _rnd.Random(level * 997)   # seeded → same pattern per level every time
        cols = 18
        rows = 5
        jx   = BW2 / cols * 0.42
        jy   = BH2 / rows * 0.42

        # Rainbow palette for level 10
        _rainbow = [
            (255, 60, 120), (255, 110, 20), (220, 190, 0),
            (40, 210, 60),  (0,  195, 220), (60, 80, 255),
            (180, 0, 255),
        ]

        # Build grid of perturbed points
        grid = {}
        for r in range(rows + 2):
            for c in range(cols + 2):
                px = x1 + c * BW2 / cols + rng.uniform(-jx, jx)
                py = y1 + r * BH2 / rows + rng.uniform(-jy, jy)
                # clamp to banner bounds
                px = max(x1, min(x2, px))
                py = max(y1, min(y2, py))
                grid[(r, c)] = (px, py)

        # Draw triangles
        for r in range(rows + 1):
            for c in range(cols + 1):
                p00 = grid[(r,   c)]
                p10 = grid[(r+1, c)]
                p01 = grid[(r,   c+1)]
                p11 = grid[(r+1, c+1)]
                for tri in ((p00, p10, p11), (p00, p01, p11)):
                    bright = rng.uniform(0.28, 0.82)
                    if level == 10:
                        col = _rainbow[rng.randint(0, len(_rainbow) - 1)]
                        r2 = min(255, int(col[0] * bright * 0.85 + bg[0] * 0.15))
                        g2 = min(255, int(col[1] * bright * 0.85 + bg[1] * 0.15))
                        b2 = min(255, int(col[2] * bright * 0.85 + bg[2] * 0.15))
                    else:
                        r2 = min(255, int(mc[0] * bright * 0.82 + bg[0] * 0.18))
                        g2 = min(255, int(mc[1] * bright * 0.82 + bg[1] * 0.18))
                        b2 = min(255, int(mc[2] * bright * 0.82 + bg[2] * 0.18))
                    d.polygon([p for pt in tri for p in pt], fill=(r2, g2, b2))

        # Triangle wireframe — thin dark lines give the facet look
        for r in range(rows + 1):
            for c in range(cols + 1):
                p00 = grid[(r,   c)]
                p10 = grid[(r+1, c)]
                p01 = grid[(r,   c+1)]
                p11 = grid[(r+1, c+1)]
                edge_col = (bg[0] + 8, bg[1] + 8, bg[2] + 8)
                d.line([p00, p10], fill=edge_col, width=1)
                d.line([p00, p01], fill=edge_col, width=1)
                d.line([p00, p11], fill=edge_col, width=1)

        # --- Giant level number on the RIGHT ---
        lvl_str  = str(level)
        # Try large font; fall back gracefully if metrics are tiny
        lvl_font = _font(118, bold=True)
        lw2      = _tw(d, lvl_str, lvl_font)
        lh2      = 118
        # Centre horizontally and vertically
        lx = x1 + (BW2 - lw2) // 2
        ly = y1 + (BH2 - lh2) // 2 - 4

        num_col = (255, 255, 255) if level == 10 else ac

        # Drop shadow for depth
        shadow = (max(0, bg[0] - 10), max(0, bg[1] - 10), max(0, bg[2] - 10))
        for off in (4, 2):
            d.text((lx + off, ly + off), lvl_str, font=lvl_font, fill=shadow)
        # Main number
        d.text((lx, ly), lvl_str, font=lvl_font, fill=num_col)

        return d

    # Apply banner
    if "blue lock" in fn_banner or "блю лок" in fn_banner or "bluelock" in fn_banner:
        pass  # drawn below after glow step
    elif "premium" in fn_banner:
        draw = _draw_premium_banner_hdr(draw, 8, 8, W - 8, 148)
    elif "gold" in fn_banner or "золот" in fn_banner:
        draw = _draw_gold_banner_hdr(draw, 8, 8, W - 8, 148)
    elif "diamond" in fn_banner or "алмаз" in fn_banner:
        draw = _draw_diamond_banner_hdr(draw, 8, 8, W - 8, 148)
    elif "elite" in fn_banner or "элит" in fn_banner:
        draw = _draw_elite_banner_hdr(draw, 8, 8, W - 8, 148)
    else:
        # No custom banner → draw level banner automatically
        draw = _draw_level_banner_hdr(draw, 8, 8, W - 8, 148, lvl)

    # ===== HEADER PANEL =====
    glow_color = _GOLD if is_premium else _GOLD_DIM

    if "blue lock" in fn_banner or "блю лок" in fn_banner or "bluelock" in fn_banner:
        draw = _draw_bl_banner(draw, img, 8, 8, W - 8, 148)
        _HDR_TEXT      = BL_BLACK
        _HDR_TEXT_GRAY = (60, 60, 100)
        _HDR_GOLD      = BL_BLUE
    elif fn_banner:
        if "premium" in fn_banner:
            _rr(draw, (8, 8, W-8, 148), 10, outline=(220, 170, 0),   width=2)
            _rr(draw, (11, 11, W-11, 145), 8, outline=(140, 40, 200), width=1)
        elif "gold" in fn_banner or "золот" in fn_banner:
            _rr(draw, (8, 8, W-8, 148), 10, outline=(160, 120, 0), width=1)
        elif "diamond" in fn_banner or "алмаз" in fn_banner:
            _rr(draw, (8, 8, W-8, 148), 10, outline=(0, 160, 185), width=1)
        elif "elite" in fn_banner or "элит" in fn_banner:
            _rr(draw, (8, 8, W-8, 148), 10, outline=(100, 30, 160), width=1)
        _HDR_TEXT      = _TEXT
        _HDR_TEXT_GRAY = _TEXT_GRAY
        _HDR_GOLD      = _GOLD
    else:
        # Level banner — add coloured outline matching level colour
        lc = LEVEL_BANNER_CFG.get(lvl, LEVEL_BANNER_CFG[1])
        _rr(draw, (8, 8, W-8, 148), 10, outline=lc["main"], width=2)
        _HDR_TEXT      = _TEXT
        _HDR_TEXT_GRAY = _TEXT_GRAY
        _HDR_GOLD      = _GOLD

    # ===== AVATAR (SQUARE) =====
    AX, AY, AS = 20, 20, 118

    fn_frame = (active_frame or "").lower()
    if "premium" in fn_frame:
        _av_glow = (200, 80, 255)
    elif "gold" in fn_frame or "золот" in fn_frame:
        _av_glow = (232, 185, 0)
    elif "diamond" in fn_frame or "алмаз" in fn_frame:
        _av_glow = (0, 210, 225)
    elif "elite" in fn_frame or "элит" in fn_frame:
        _av_glow = (160, 60, 230)
    elif "blue lock" in fn_frame or "блю лок" in fn_frame or "bluelock" in fn_frame:
        _av_glow = BL_BLUE_LT
    else:
        # Use level colour for glow when no custom frame
        _av_glow = LEVEL_BANNER_CFG.get(lvl, LEVEL_BANNER_CFG[1])["main"] or (130, 130, 130)

    img = _apply_glow(img, (AX, AY, AX+AS, AY+AS), r=4, color=_av_glow, strength=14, layers=8)
    draw = ImageDraw.Draw(img)

    if avatar_bytes:
        draw.rectangle([(AX, AY), (AX+AS, AY+AS)], fill=(22, 16, 44), outline=_av_glow, width=2)
        img = _paste_avatar(img, avatar_bytes, AX + 2, AY + 2, AS - 4,
                            border_color=_av_glow, border_width=2, square=True)
        draw = ImageDraw.Draw(img)
    else:
        draw.rectangle([(AX, AY), (AX+AS, AY+AS)], fill=(22, 16, 44), outline=_av_glow, width=2)
        initials = (username[:2]).upper() if username else "??"
        _text_c(draw, AX + AS//2, AY + AS//2 - 22, initials, _font(38, bold=True), _GOLD)

    # ===== FRAME BORDER OVER AVATAR =====
    if active_frame:
        # Named cosmetic frame (shop items)
        img, draw = _draw_frame_border(img, draw, AX, AY, AS, active_frame)
    else:
        # Auto-draw level frame programmatically
        img, draw = _draw_level_frame(img, AX, AY, AS, lvl)

    draw.text((165, 20), f"#{user_id}", font=_font(13), fill=_HDR_TEXT_GRAY)
    fname2 = _font(30, bold=True)
    draw.text((165, 36), username, font=fname2, fill=_HDR_TEXT)
    badge_x = 165 + _tw(draw, username, fname2) + 10
    if is_premium:
        draw.text((badge_x, 40), "★", font=_font(26, bold=True), fill=_HDR_GOLD)
        badge_x += _tw(draw, "★", _font(26, bold=True)) + 8
    if is_admin:
        _rr(draw, (badge_x, 40, badge_x + 40, 62), 4, fill=(170, 28, 28))
        draw.text((badge_x + 4, 42), "ADM", font=_font(12, bold=True), fill=_WH)
        badge_x += 48
    if is_verified:
        _draw_scalloped_badge(draw, badge_x + 11, 51, 22)

    draw.text((165, 78), f"ID: {game_id}", font=_font(14), fill=_HDR_TEXT_GRAY)
    draw.text((W - 200, 16), "ELO RATING", font=_font(11), fill=_HDR_TEXT_GRAY)
    _text_r(draw, W - 36, 28, str(elo), _font(46, bold=True), _HDR_GOLD)
    BX, BY, BS = W - 80, 88, 40

    # Level badge — use level colour instead of fixed purple
    lc2 = LEVEL_BANNER_CFG.get(lvl, LEVEL_BANNER_CFG[1])
    _rr(draw, (BX, BY, BX+BS, BY+BS), 6, fill=lc2["main"], outline=lc2["accent"], width=2)
    _text_c(draw, BX + BS//2, BY + 8, str(lvl), _font(20, bold=True), _WH)

    # ===== RANK BAR =====
    RY = 162
    _rr(draw, (8, RY, W-8, RY+33), 6, fill=(22, 16, 46), outline=(56, 42, 96), width=1)
    draw.ellipse([(18, RY+10), (30, RY+22)], outline=_TEXT, width=2)
    draw.text((36, RY+8), f"GLOBAL RANK:  #{global_rank}", font=_font(12, bold=True), fill=_TEXT)
    draw.line([(225, RY+5), (225, RY+28)], fill=(56, 42, 96), width=1)
    draw.ellipse([(233, RY+10), (243, RY+22)], fill=_GOLD)
    draw.text((250, RY+8), f"LEAGUE:  {format_league(league).upper()}", font=_font(12, bold=True), fill=_TEXT)

    # ===== STAT CARDS =====
    SY = 205
    LW = 588
    RX = 605

    def stat_card(x, y, w, h, label, value, highlight=False, sub=None):
        bg2 = _PANEL_GOLD if highlight else _PANEL
        ol  = _GOLD_DIM   if highlight else (44, 34, 82)
        _rr(draw, (x, y, x+w, y+h), 8, fill=bg2, outline=ol, width=1)
        draw.text((x+12, y+9),  label,      font=_font(10),            fill=_TEXT_GRAY)
        draw.text((x+12, y+28), str(value), font=_font(34, bold=True), fill=(_GOLD if highlight else _TEXT))
        if sub:
            draw.text((x+12, y+h-18), sub, font=_font(11), fill=_TEXT_MID)

    CW1, CW2, CH = 225, 350, 90

    stat_card(8,         SY,           CW1, CH, "MATCHES",   games)
    stat_card(8+CW1+8,   SY,           CW2, CH, "WIN RATE",  f"{wr}%",
              highlight=True, sub=f"{wins}W — {losses}L")
    stat_card(8,         SY+CH+8,      CW1, CH, "K/D RATIO", f"{kd:.2f}")
    stat_card(8+CW1+8,   SY+CH+8,      CW2, CH, "RATING",    f"{rating:.2f}")

    mini_labels = ["AVG KILLS", "KPR",        "IMPACT",          "MVP"]
    mini_values = [avg_k,       f"{kpr:.2f}", f"{impact:.2f}",  mvp_count]
    MW = (LW - 8 - 3*8) // 4
    for i, (lbl, val) in enumerate(zip(mini_labels, mini_values)):
        stat_card(8 + i*(MW+8), SY+(CH+8)*2, MW, CH, lbl, val)

    # ===== MAP STATS PANEL =====
    _rr(draw, (RX, SY, W-8, SY+CH*3+8*2), 8, fill=_PANEL, outline=(44, 34, 82), width=1)
    draw.text((RX+12, SY+8), "○  MAP STATS", font=_font(11), fill=_TEXT_GRAY)
    MROW = (CH*3+8*2-30) // max(len(map_stats[:5]), 1)
    for idx, ms in enumerate(map_stats[:5]):
        my   = SY + 30 + idx * MROW
        mw2  = ms.get("wins", 0)
        ml2  = ms.get("losses", 0)
        mg2  = mw2 + ml2
        mwr  = ms.get("wr", round(mw2 / mg2, 2) if mg2 > 0 else 0.0)
        draw.text((RX+12, my+2), ms["map"].upper(), font=_font(12, bold=True), fill=_TEXT_LGRAY)
        _text_r(draw, W-20, my+2,  f"{round(mwr*100)}% WR", _font(11), _TEXT_GRAY)
        _text_r(draw, W-20, my+17, f"{ms.get('kd', 0.0):.2f} K/D", _font(11), _TEXT_GRAY)
        if idx < len(map_stats[:5]) - 1:
            draw.line([(RX+10, my+MROW-2), (W-20, my+MROW-2)], fill=(44, 34, 82), width=1)

    # ===== RECENT PERFORMANCE =====
    RPY = SY + (CH+8)*3 + 10
    draw.text((14, RPY+2), "⚡  RECENT PERFORMANCE", font=_font(11, bold=True), fill=_TEXT_GRAY)
    SQ = 44
    for i, won in enumerate(recent[:5]):
        sx = 14 + i * (SQ+8)
        sy = RPY + 22
        _rr(draw, (sx, sy, sx+SQ, sy+SQ-4), 6, fill=(_GREEN if won else _RED))
        _text_c(draw, sx+SQ//2, sy+8, "W" if won else "L", _font(18, bold=True), _WH)

    # ===== MINI LEADERBOARD =====
    LBY = RPY + 76
    draw.line([(8, LBY), (W-8, LBY)], fill=(44, 34, 82), width=1)
    draw.text((14, LBY+7), "🏆  LEADERBOARD", font=_font(11, bold=True), fill=_TEXT_GRAY)

    for i, entry in enumerate(leaderboard[:2]):
        rank, name, p_elo = entry[0], entry[1], entry[2]
        is_p  = entry[3] if len(entry) > 3 else False
        is_ad = entry[4] if len(entry) > 4 else False
        is_vf = entry[5] if len(entry) > 5 else False
        ly = LBY + 30 + i * 42
        draw.text((14, ly+10), str(rank), font=_font(14, bold=True), fill=_TEXT_GRAY)
        _rr(draw, (38, ly, 72, ly+34), 5, fill=(26, 18, 52), outline=_GOLD_DIM, width=1)
        _text_c(draw, 55, ly+8, name[:2].upper(), _font(13, bold=True), _GOLD)
        nx2 = 82
        draw.text((nx2, ly+10), name.upper(), font=_font(14, bold=True), fill=_GOLD)
        nx2 += _tw(draw, name.upper(), _font(14, bold=True)) + 6
        if is_p:
            draw.text((nx2, ly+10), "★", font=_font(14, bold=True), fill=_GOLD)
            nx2 += _tw(draw, "★", _font(14, bold=True)) + 5
        if is_ad:
            _rr(draw, (nx2, ly+10, nx2+32, ly+26), 3, fill=(170, 28, 28))
            draw.text((nx2+3, ly+11), "ADM", font=_font(10, bold=True), fill=_WH)
            nx2 += 38
        if is_vf:
            _draw_scalloped_badge(draw, nx2 + 8, ly + 18, 16)
        _text_r(draw, W-20, ly+10, str(p_elo), _font(14, bold=True), _TEXT)

    # ===== QUALS STATS SECTION (optional) =====
    if quals_stats:
        QY = LBY + 30 + 2 * 42 + 8
        draw.line([(8, QY), (W-8, QY)], fill=_TEAL_DIM, width=1)
        _rr(draw, (8, QY+4, W-8, QY+QUALS_H-4), 8, fill=(18, 14, 40), outline=_TEAL_DIM, width=1)
        draw.text((20, QY+10), "⭐  QUALS STATS", font=_font(11, bold=True), fill=_TEAL)

        qw  = quals_stats.get("wins",   0)
        ql  = quals_stats.get("losses", 0)
        qk  = quals_stats.get("kills",  0)
        qd  = quals_stats.get("deaths", 0)
        qa  = quals_stats.get("assists",0)
        qelo= quals_stats.get("elo",    1000)
        qg  = qw + ql
        qwr = round(qw / qg * 100, 1) if qg > 0 else 0.0
        qkd = round(qk / qd, 2) if qd > 0 else float(qk)

        for label, value, px in [
            ("Q.ELO",     str(qelo),      20),
            ("Q.MATCHES", str(qg),        160),
            ("Q.WIN%",    f"{qwr}%",      300),
            ("Q.K/D",     f"{qkd:.2f}",  440),
            ("Q.KILLS",   str(qk),        580),
        ]:
            draw.text((px, QY+26), label, font=_font(10),           fill=_TEXT_GRAY)
            draw.text((px, QY+40), value, font=_font(16, bold=True), fill=_TEAL)

    # ===== DUO (2v2) STATS SECTION (optional) =====
    if duo_stats:
        base_y = LBY + 30 + 2 * 42 + 8 + QUALS_H
        DY = base_y
        draw.line([(8, DY), (W-8, DY)], fill=_DUO_DIM, width=1)
        _rr(draw, (8, DY+4, W-8, DY+DUO_H-4), 8, fill=(22, 22, 22), outline=_DUO_DIM, width=1)
        draw.text((20, DY+10), "👥  2v2 STATS", font=_font(11, bold=True), fill=_DUO_COL)

        dw  = duo_stats.get("wins",   0)
        dl  = duo_stats.get("losses", 0)
        dk  = duo_stats.get("kills",  0)
        dd  = duo_stats.get("deaths", 0)
        da  = duo_stats.get("assists",0)
        delo= duo_stats.get("elo",    1000)
        dg  = dw + dl
        dwr = round(dw / dg * 100, 1) if dg > 0 else 0.0
        dkd = round(dk / dd, 2) if dd > 0 else float(dk)

        for label, value, px in [
            ("2v2 ELO",     str(delo),      20),
            ("2v2 MATCHES", str(dg),        160),
            ("2v2 WIN%",    f"{dwr}%",      300),
            ("2v2 K/D",     f"{dkd:.2f}",  440),
            ("2v2 KILLS",   str(dk),        580),
        ]:
            draw.text((px, DY+26), label, font=_font(10),           fill=_TEXT_GRAY)
            draw.text((px, DY+40), value, font=_font(16, bold=True), fill=_DUO_COL)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# ==================== LEADERBOARD CARD ====================
def generate_leaderboard_card(players: list, title: str = "TOP ИГРОКОВ ПО ELO",
                               avatars: dict = None) -> io.BytesIO:
    n      = min(len(players), 10)
    ROW_H  = 74
    HEAD_H = 60
    H      = HEAD_H + ROW_H * n + 8
    W      = 970

    _BG    = (14,  14,  14)
    _ROW_A = (20,  20,  20)
    _ROW_B = (14,  14,  14)
    _SEP   = (38,  38,  38)
    _HDR   = (140, 140, 140)
    _TEXT  = (235, 235, 235)
    _GOLD  = (232, 185,   0)
    _GREEN = ( 48, 198, 108)
    _RED   = (210,  52,  52)
    _TD    = ( 55,  55,  80)
    _GRAY  = (110, 110, 110)
    _WH    = (255, 255, 255)

    _RANK_BG     = {1: (24, 20, 12), 2: (20, 20, 20), 3: (20, 20, 20)}
    _RANK_STRIPE = {1: (200, 155,  0), 2: (148, 148, 175), 3: (158, 105, 42)}

    img  = Image.new("RGB", (W, H), _BG)
    draw = ImageDraw.Draw(img)

    draw.rectangle([(0, 0), (W, HEAD_H - 1)], fill=(10, 10, 10))
    draw.line([(0, HEAD_H - 1), (W, HEAD_H - 1)], fill=_SEP, width=2)
    draw.ellipse([(18, 19), (30, 31)], fill=(180, 180, 180))
    draw.text((38, 17), "ACTUAL FACEIT", font=_font(13, bold=True), fill=_TEXT)

    badge_x = W - 12
    for badge_text, badge_col in [
        ("2V2",   ( 60,  60,  80)),
        ("QUALS", ( 50,  50,  70)),
        ("DEFAULT", ( 40,  40,  55)),
    ]:
        if badge_text in title.upper():
            bw = _tw(draw, badge_text, _font(11, bold=True)) + 16
            badge_x -= bw
            _rr(draw, (badge_x, 15, badge_x + bw, 37), 5, fill=badge_col)
            draw.text((badge_x + 8, 17), badge_text, font=_font(11, bold=True), fill=_WH)
            break

    for label, x in [("#", 22), ("PLAYER  ELO", 74), ("WINS", 430),
                      ("LOSSES", 514), ("W/L%", 600), ("K/D", 710)]:
        draw.text((x, HEAD_H - 22), label, font=_font(11), fill=_HDR)

    for i, p in enumerate(players[:n]):
        y    = HEAD_H + i * ROW_H
        rank = p.get("rank", i + 1)

        row_bg = _RANK_BG.get(rank, _ROW_A if i % 2 == 0 else _ROW_B)
        draw.rectangle([(0, y), (W, y + ROW_H)], fill=row_bg)

        if rank in _RANK_STRIPE:
            draw.rectangle([(0, y), (5, y + ROW_H)], fill=_RANK_STRIPE[rank])

        rcolor = (_GOLD if rank == 1 else (132, 132, 152) if rank == 2
                  else (148, 100, 36) if rank == 3 else (110, 106, 88))
        draw.text((14, y + ROW_H // 2 - 10), str(rank), font=_font(17, bold=True), fill=rcolor)

        elo = p.get("elo", 1000)
        lv  = p.get("level", get_level(elo))
        av  = LVL_COLORS.get(lv, (130, 125, 105))
        ax, ay, ar = 54, y + ROW_H // 2 - 20, 19
        uid_av   = p.get("uid")
        av_bytes = (avatars or {}).get(uid_av) if uid_av else None
        if av_bytes:
            draw.rectangle([(ax, ay), (ax + ar*2, ay + ar*2)], fill=av, outline=(190, 186, 172), width=2)
            img  = _paste_avatar(img, av_bytes, ax, ay, ar * 2, border_color=(190, 186, 172),
                                  border_width=2, square=True)
            draw = ImageDraw.Draw(img)
        else:
            draw.ellipse([(ax, ay), (ax + ar*2, ay + ar*2)], fill=av, outline=(190, 186, 172), width=2)
            _text_c(draw, ax + ar, ay + ar - 10, (p.get("name", "??")[:2]).upper(),
                    _font(12, bold=True), _WH)

        name = p.get("name", "Unknown")
        nx   = 100
        draw.text((nx, y + 10), name, font=_font(14, bold=True), fill=_TEXT)
        bx = nx + _tw(draw, name, _font(14, bold=True)) + 8

        if p.get("is_premium"):
            draw.text((bx, y + 10), "★", font=_font(14, bold=True), fill=_GOLD)
            bx += _tw(draw, "★", _font(14, bold=True)) + 5
        if p.get("is_admin"):
            _rr(draw, (bx, y + 10, bx + 32, y + 25), 3, fill=(154, 24, 24))
            draw.text((bx + 4, y + 11), "ADM", font=_font(10, bold=True), fill=_WH)
            bx += 40
        if p.get("is_verified"):
            _draw_scalloped_badge(draw, bx + 8, y + 18, 15)

        uid_val = p.get("uid")
        if uid_val:
            draw.text((nx, y + 29), f"#{str(uid_val)[-7:]}", font=_font(10), fill=_GRAY)

        # Level badge with level-specific colour
        lc3 = LEVEL_BANNER_CFG.get(lv, LEVEL_BANNER_CFG[1])
        by2 = y + ROW_H - 21
        _rr(draw, (nx, by2, nx + 20, by2 + 14), 3, fill=lc3["main"])
        _text_c(draw, nx + 10, by2 + 2, str(lv), _font(10, bold=True), _WH)
        draw.text((nx + 24, by2 + 2), str(elo), font=_font(12, bold=True), fill=_TEXT)

        wins   = p.get("wins",   0)
        losses = p.get("losses", 0)
        games  = wins + losses
        wr_pct = f"{round(wins / games * 100)}%" if games > 0 else "0%"
        cy = y + ROW_H // 2 - 10
        draw.text((430, cy), str(wins),                  font=_font(15, bold=True), fill=_GREEN)
        draw.text((514, cy), str(losses),                font=_font(15, bold=True), fill=_RED)
        draw.text((600, cy), wr_pct,                     font=_font(15, bold=True), fill=_TEXT)
        draw.text((710, cy), f"{p.get('kd', 0.0):.2f}", font=_font(15, bold=True), fill=_TEXT)

        if i < n - 1:
            draw.line([(0, y + ROW_H), (W, y + ROW_H)], fill=_SEP, width=1)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# ==================== MATCH RESULT CARD ====================
def generate_match_result_card(
    match_code: str,
    map_name:   str,
    score_ct:   int,
    score_t:    int,
    winner:     str,
    ct_stats:   list,
    t_stats:    list,
    league:     str = "default",
) -> io.BytesIO:
    ROW_H      = 54
    HEAD_H     = 112
    COL_HEAD_H = 30
    FOOT_H     = 28
    n          = max(len(ct_stats), len(t_stats), 1)
    W          = 990
    H          = HEAD_H + COL_HEAD_H + ROW_H * n + FOOT_H

    img  = Image.new("RGB", (W, H), (14, 13, 18))
    draw = ImageDraw.Draw(img)

    for x in range(0, W, 40):
        draw.line([(x, 0), (x, H)], fill=(20, 19, 26), width=1)
    for y in range(0, H, 40):
        draw.line([(0, y), (W, y)], fill=(20, 19, 26), width=1)

    glow_col = CT_BLUE if winner == "ct" else T_ORANGE
    img  = _apply_glow(img, (8, 8, W-8, HEAD_H - 8), r=10, color=glow_col, strength=22, layers=10)
    draw = ImageDraw.Draw(img)

    _rr(draw, (8, 8, W-8, HEAD_H - 8), 10, fill=(20, 20, 28), outline=(55, 52, 75), width=1)

    draw.text((22, 16), f"МАТЧ #{match_code}", font=_font(22, bold=True), fill=WHITE)

    lg_text = format_league(league).upper()
    lg_col  = TEAL_DIM if league == "default" else (130, 40, 170)
    lw = _tw(draw, lg_text, _font(12, bold=True)) + 14
    _rr(draw, (22, 46, 22 + lw, 66), 4, fill=lg_col)
    draw.text((29, 49), lg_text, font=_font(12, bold=True), fill=WHITE)

    draw.text((22 + lw + 12, 49), f"🗺  {map_name.upper()}", font=_font(13), fill=LGRAY)

    sc_text = f"{score_ct}  :  {score_t}"
    _text_c(draw, W//2, 14, sc_text, _font(40, bold=True), WHITE)
    _text_c(draw, W//2 - 65, 62, "CT",  _font(16, bold=True), CT_BLUE)
    _text_c(draw, W//2 + 65, 62, "T",   _font(16, bold=True), T_ORANGE)

    w_label = "💙 CT — ПОБЕДА" if winner == "ct" else "🧡 T — ПОБЕДА"
    w_col   = CT_BLUE if winner == "ct" else T_ORANGE
    _text_r(draw, W - 18, 20, w_label, _font(14, bold=True), w_col)

    HALF_W = (W - 20) // 2
    CT_X   = 8
    T_X    = CT_X + HALF_W + 4
    CH_Y   = HEAD_H

    draw.rectangle([(CT_X, CH_Y), (CT_X + HALF_W, CH_Y + COL_HEAD_H)], fill=(16, 28, 50))
    draw.text((CT_X + 10, CH_Y + 7), "💙 КОМАНДА CT", font=_font(12, bold=True), fill=CT_BLUE)
    _text_r(draw, CT_X + HALF_W - 8, CH_Y + 7, "K / A / D    ELO", _font(11), GRAY)

    draw.rectangle([(T_X, CH_Y), (T_X + HALF_W, CH_Y + COL_HEAD_H)], fill=(48, 26, 10))
    draw.text((T_X + 10, CH_Y + 7), "🧡 КОМАНДА T", font=_font(12, bold=True), fill=T_ORANGE)
    _text_r(draw, T_X + HALF_W - 8, CH_Y + 7, "K / A / D    ELO", _font(11), GRAY)

    ROW_Y0 = HEAD_H + COL_HEAD_H

    all_flat = ct_stats + t_stats
    if all_flat:
        mvp_name = max(all_flat, key=lambda s: s.get("kills", 0)).get("name", "")
    else:
        mvp_name = ""

    def draw_player_row(base_x, y, w, stat, team_col, is_winner_side):
        name    = stat.get("name",       "?")
        kills   = stat.get("kills",       0)
        deaths  = stat.get("deaths",      0)
        assists = stat.get("assists",     0)
        elo_ch  = stat.get("elo_change",  0)
        is_mvp  = (name == mvp_name)
        is_prem = stat.get("is_premium", False)
        is_adm  = stat.get("is_admin",   False)

        row_bg = (30, 24, 6) if is_mvp else ((16, 26, 44) if is_winner_side else (22, 18, 24))
        draw.rectangle([(base_x, y), (base_x + w, y + ROW_H - 2)], fill=row_bg)
        if is_mvp:
            draw.rectangle([(base_x, y), (base_x + w, y + ROW_H - 2)], outline=GOLD_DIM, width=1)
            draw.text((base_x + w - 44, y + 4), "MVP", font=_font(10, bold=True), fill=GOLD)

        AX2, AY2, AR2 = base_x + 8, y + 7, 18
        av_col = GOLD if is_mvp else team_col
        draw.rectangle([(AX2, AY2), (AX2+AR2*2, AY2+AR2*2)], fill=av_col, outline=(60, 55, 80), width=1)
        _text_c(draw, AX2+AR2, AY2+AR2-9, (name[:2]).upper(), _font(11, bold=True), (14, 13, 20))

        nx2 = base_x + 48
        nm_disp = name[:15] + ("…" if len(name) > 15 else "")
        draw.text((nx2, y + 9), nm_disp, font=_font(13, bold=True), fill=(GOLD if is_mvp else WHITE))
        bx2 = nx2 + _tw(draw, nm_disp, _font(13, bold=True)) + 5
        if is_prem:
            draw.text((bx2, y + 9), "★", font=_font(13, bold=True), fill=GOLD)
            bx2 += _tw(draw, "★", _font(13, bold=True)) + 4
        if is_adm:
            _rr(draw, (bx2, y+9, bx2+28, y+23), 3, fill=(170, 28, 28))
            draw.text((bx2+3, y+10), "ADM", font=_font(9, bold=True), fill=WHITE)

        kd_str = f"{kills} / {assists} / {deaths}"
        _text_r(draw, base_x + w - 60, y + 8, kd_str, _font(13, bold=True), WHITE)

        elo_col = GREEN if elo_ch >= 0 else RED
        elo_str = f"{'+' if elo_ch >= 0 else ''}{elo_ch}"
        _text_r(draw, base_x + w - 8, y + 8, elo_str, _font(13, bold=True), elo_col)

        draw.line([(base_x, y + ROW_H - 1), (base_x + w, y + ROW_H - 1)], fill=(35, 32, 45), width=1)

    for i in range(n):
        ry = ROW_Y0 + i * ROW_H
        if i < len(ct_stats):
            draw_player_row(CT_X, ry, HALF_W, ct_stats[i], CT_BLUE,   winner == "ct")
        if i < len(t_stats):
            draw_player_row(T_X,  ry, HALF_W, t_stats[i],  T_ORANGE,  winner == "t")

    div_x = CT_X + HALF_W + 2
    draw.line([(div_x, HEAD_H), (div_x, H - FOOT_H)], fill=(45, 42, 60), width=2)

    fy = H - FOOT_H
    draw.rectangle([(0, fy), (W, H)], fill=(10, 10, 16))
    draw.text((16, fy + 8), "ACTUAL FACEIT", font=_font(12, bold=True), fill=GOLD_DIM)
    _text_r(draw, W - 16, fy + 8, f"#{match_code} | {map_name}", _font(11), GRAY)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# ==================== DUO (2v2) LEADERBOARD CARD ====================
def generate_duo_leaderboard_card(players: list, title: str = "TOP 2v2 ПО ELO",
                                   avatars: dict = None) -> io.BytesIO:
    n      = min(len(players), 10)
    ROW_H  = 74
    HEAD_H = 60
    H      = HEAD_H + ROW_H * n + 8
    W      = 970

    _BG    = (14, 14, 14)
    _ROW_A = (20, 20, 20)
    _ROW_B = (14, 14, 14)
    _SEP   = (38, 38, 38)
    _HDR   = (140, 140, 140)
    _WH    = (235, 235, 235)
    _GOLD  = (232, 185, 0)
    _GREEN = (48, 198, 108)
    _RED   = (210, 52, 52)
    _PUR   = ( 60,  60,  80)
    _PURD  = ( 40,  40,  60)
    _GRAY  = (110, 110, 110)

    img  = Image.new("RGB", (W, H), _BG)
    draw = ImageDraw.Draw(img)

    draw.rectangle([(0, 0), (W, HEAD_H - 1)], fill=(10, 10, 10))
    draw.line([(0, HEAD_H - 1), (W, HEAD_H - 1)], fill=_SEP, width=1)
    draw.ellipse([(18, 19), (30, 31)], fill=(180, 180, 180))
    draw.text((38, 17), "ACTUAL FACEIT", font=_font(13, bold=True), fill=_WH)

    bw = _tw(draw, "2V2", _font(11, bold=True)) + 16
    bx = W - 12 - bw
    _rr(draw, (bx, 15, bx + bw, 37), 5, fill=_PURD)
    draw.text((bx + 8, 17), "2V2", font=_font(11, bold=True), fill=_WH)

    for label, x in [("#", 22), ("PLAYER  ELO", 74), ("WINS", 430),
                      ("LOSSES", 514), ("W/L%", 600), ("K/D", 710)]:
        draw.text((x, HEAD_H - 22), label, font=_font(11), fill=_HDR)

    for i, p in enumerate(players[:n]):
        y  = HEAD_H + i * ROW_H
        draw.rectangle([(0, y), (W, y + ROW_H)], fill=(_ROW_A if i % 2 == 0 else _ROW_B))

        rank   = p.get("rank", i + 1)
        rcolor = (_GOLD if rank == 1 else (208, 162, 0) if rank == 2
                  else (182, 126, 56) if rank == 3 else (112, 96, 148))
        draw.text((22, y + ROW_H // 2 - 10), str(rank), font=_font(17, bold=True), fill=rcolor)

        elo = p.get("elo", 1000)
        lv  = p.get("level", get_level(elo))
        ax, ay, ar = 54, y + ROW_H // 2 - 20, 19
        uid_av2   = p.get("uid")
        av_bytes2 = (avatars or {}).get(uid_av2) if uid_av2 else None
        if av_bytes2:
            draw.rectangle([(ax, ay), (ax + ar*2, ay + ar*2)], fill=_PUR, outline=(50, 50, 70), width=2)
            img  = _paste_avatar(img, av_bytes2, ax, ay, ar * 2, border_color=(50, 50, 70),
                                  border_width=2, square=True)
            draw = ImageDraw.Draw(img)
        else:
            draw.ellipse([(ax, ay), (ax + ar*2, ay + ar*2)], fill=_PUR, outline=(50, 50, 70), width=2)
            _text_c(draw, ax + ar, ay + ar - 10, (p.get("name", "??")[:2]).upper(),
                    _font(12, bold=True), (236, 234, 252))

        name = p.get("name", "Unknown")
        nx   = 100
        draw.text((nx, y + 10), name, font=_font(14, bold=True), fill=_WH)
        bx2 = nx + _tw(draw, name, _font(14, bold=True)) + 8

        if p.get("is_premium"):
            draw.text((bx2, y + 10), "★", font=_font(14, bold=True), fill=_GOLD)
            bx2 += _tw(draw, "★", _font(14, bold=True)) + 5
        if p.get("is_admin"):
            _rr(draw, (bx2, y + 10, bx2 + 32, y + 25), 3, fill=(154, 24, 24))
            draw.text((bx2 + 4, y + 11), "ADM", font=_font(10, bold=True), fill=_WH)
            bx2 += 40

        uid_val2 = p.get("uid")
        if uid_val2:
            draw.text((nx, y + 29), f"#{str(uid_val2)[-7:]}", font=_font(10), fill=_GRAY)

        # Level badge with level colour
        lc4 = LEVEL_BANNER_CFG.get(lv, LEVEL_BANNER_CFG[1])
        by2 = y + ROW_H - 21
        _rr(draw, (nx, by2, nx + 20, by2 + 14), 3, fill=lc4["main"])
        _text_c(draw, nx + 10, by2 + 2, str(lv), _font(10, bold=True), _WH)
        draw.text((nx + 24, by2 + 2), str(elo), font=_font(12, bold=True), fill=_WH)

        wins   = p.get("wins",   0)
        losses = p.get("losses", 0)
        kills  = p.get("kills",  0)
        deaths = p.get("deaths", 1)
        games  = wins + losses
        wr_pct = f"{round(wins / games * 100)}%" if games > 0 else "0%"
        kd_str = f"{round(kills / max(deaths, 1), 2):.2f}"
        cy     = y + ROW_H // 2 - 10
        draw.text((430, cy), str(wins),  font=_font(15, bold=True), fill=_GREEN)
        draw.text((514, cy), str(losses),font=_font(15, bold=True), fill=_RED)
        draw.text((600, cy), wr_pct,     font=_font(15, bold=True), fill=_WH)
        draw.text((710, cy), kd_str,     font=_font(15, bold=True), fill=_WH)

        if i < n - 1:
            draw.line([(0, y + ROW_H), (W, y + ROW_H)], fill=_SEP, width=1)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
