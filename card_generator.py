from PIL import Image, ImageDraw, ImageFont, ImageFilter
import io
import os
import math

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
    """Draw a Twitter/Meta-style scalloped verified badge without background."""
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


# ==================== AVATAR PASTE ====================
def _paste_avatar(img: Image.Image, avatar_bytes: bytes, x: int, y: int, size: int,
                  border_color=None, border_width: int = 2) -> Image.Image:
    """Paste a circular avatar image at (x, y) with given size."""
    try:
        av_img = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
        av_img = av_img.resize((size, size), Image.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        md = ImageDraw.Draw(mask)
        md.ellipse([(0, 0), (size - 1, size - 1)], fill=255)
        output = Image.new("RGBA", img.size, (0, 0, 0, 0))
        output.paste(av_img, (x, y), mask)
        result = Image.alpha_composite(img.convert("RGBA"), output).convert("RGB")
        if border_color:
            d = ImageDraw.Draw(result)
            bw = border_width
            d.ellipse([(x - bw, y - bw), (x + size + bw - 1, y + size + bw - 1)],
                      outline=border_color, width=bw)
        return result
    except Exception as e:
        print(f"[_paste_avatar] {e}")
        return img


# ==================== LEVEL HELPER ====================
def get_level(elo: int) -> int:
    thresholds = [0, 801, 951, 1101, 1251, 1401, 1551, 1701, 1851, 2001]
    for i in range(len(thresholds) - 1, -1, -1):
        if elo >= thresholds[i]:
            return i + 1
    return 1


# ==================== PROFILE CARD ====================
def generate_profile_card(
    username:     str,
    game_id:      str,
    user_id:      int,
    elo:          int,
    wins:         int,
    losses:       int,
    kills:        int,
    deaths:       int,
    assists:      int,
    is_premium:   bool,
    is_admin:     bool,
    global_rank:  int,
    league:       str,
    map_stats:    list,
    recent:       list,
    leaderboard:  list,
    quals_stats:  dict  = None,
    mvp_count:    int   = 0,
    is_verified:  bool  = False,
    duo_stats:    dict  = None,
    avatar_bytes: bytes = None,
) -> io.BytesIO:

    QUALS_H = 70 if quals_stats else 0
    DUO_H   = 70 if duo_stats   else 0
    W, H = 1055, 695 + QUALS_H + DUO_H
    # ===== DEEP PURPLE THEME =====
    _BG         = (14,  12,  24)
    _BG_GRID    = (20,  16,  34)
    _PANEL      = (24,  18,  44)
    _PANEL_GOLD = (34,  22,  62)
    _HEADER     = (20,  15,  38)
    _GOLD       = (130,  96, 242)
    _GOLD_DIM   = ( 72,  48, 168)
    _TEXT       = (232, 230, 248)
    _TEXT_MID   = (172, 164, 210)
    _TEXT_GRAY  = (120, 112, 158)
    _TEXT_LGRAY = (142, 134, 174)
    _GREEN      = ( 50, 198, 108)
    _RED        = (210,  52,  52)
    _TEAL       = (110,  80, 232)
    _TEAL_DIM   = ( 64,  44, 152)
    _WH         = (255, 255, 255)
    _DUO_COL    = (110,  80, 232)
    _DUO_DIM    = ( 64,  44, 152)

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

    # ===== HEADER PANEL =====
    glow_color = _GOLD if is_premium else _GOLD_DIM
    img = _apply_glow(img, (8, 8, W-8, 148), r=10, color=glow_color, strength=12, layers=6)
    draw = ImageDraw.Draw(img)

    _rr(draw, (8, 8, W-8, 148), 10, fill=_HEADER, outline=_GOLD_DIM, width=1)

    AX, AY, AS = 20, 18, 118
    img = _apply_glow(img, (AX, AY, AX+AS, AY+AS), r=8, color=_GOLD, strength=10, layers=6)
    draw = ImageDraw.Draw(img)
    if avatar_bytes:
        _rr(draw, (AX, AY, AX+AS, AY+AS), 8, fill=(22, 16, 44), outline=_GOLD, width=2)
        img = _paste_avatar(img, avatar_bytes, AX + 3, AY + 3, AS - 6,
                            border_color=_GOLD, border_width=2)
        draw = ImageDraw.Draw(img)
    else:
        _rr(draw, (AX, AY, AX+AS, AY+AS), 8, fill=(22, 16, 44), outline=_GOLD, width=2)
        initials = (username[:2]).upper() if username else "??"
        _text_c(draw, AX + AS//2, AY + AS//2 - 22, initials, _font(38, bold=True), _GOLD)

    draw.text((152, 20), f"#{user_id}", font=_font(13), fill=_TEXT_GRAY)
    fname = _font(30, bold=True)
    draw.text((152, 36), username, font=fname, fill=_TEXT)
    badge_x = 152 + _tw(draw, username, fname) + 10
    if is_premium:
        draw.text((badge_x, 40), "★", font=_font(26, bold=True), fill=_GOLD)
        badge_x += _tw(draw, "★", _font(26, bold=True)) + 8
    if is_admin:
        _rr(draw, (badge_x, 40, badge_x + 40, 62), 4, fill=(170, 28, 28))
        draw.text((badge_x + 4, 42), "ADM", font=_font(12, bold=True), fill=_WH)
        badge_x += 48
    if is_verified:
        _draw_scalloped_badge(draw, badge_x + 11, 51, 22)

    draw.text((152, 78), f"ID: {game_id}", font=_font(14), fill=_TEXT_GRAY)
    draw.text((W - 200, 16), "ELO RATING", font=_font(11), fill=_TEXT_GRAY)
    _text_r(draw, W - 36, 28, str(elo), _font(46, bold=True), _GOLD)
    BX, BY, BS = W - 80, 88, 40
    _rr(draw, (BX, BY, BX+BS, BY+BS), 6, fill=_GOLD)
    _text_c(draw, BX + BS//2, BY + 8, str(lvl), _font(20, bold=True), (232, 228, 255))

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
        bg = _PANEL_GOLD if highlight else _PANEL
        ol = _GOLD_DIM   if highlight else (44, 34, 82)
        _rr(draw, (x, y, x+w, y+h), 8, fill=bg, outline=ol, width=1)
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

    mini_labels = ["AVG KILLS", "KPR",        "IMPACT",         "MVP"]
    mini_values = [avg_k,       f"{kpr:.2f}", f"{impact:.2f}", mvp_count]
    MW = (LW - 8 - 3*8) // 4
    for i, (lbl, val) in enumerate(zip(mini_labels, mini_values)):
        stat_card(8 + i*(MW+8), SY+(CH+8)*2, MW, CH, lbl, val)

    # ===== MAP STATS PANEL =====
    _rr(draw, (RX, SY, W-8, SY+CH*3+8*2), 8, fill=_PANEL, outline=(44, 34, 82), width=1)
    draw.text((RX+12, SY+8), "○  MAP STATS", font=_font(11), fill=_TEXT_GRAY)
    MROW = (CH*3+8*2-30) // max(len(map_stats[:5]), 1)
    for idx, ms in enumerate(map_stats[:5]):
        my = SY + 30 + idx * MROW
        draw.text((RX+12, my+2), ms["map"].upper(), font=_font(12, bold=True), fill=_TEXT_LGRAY)
        _text_r(draw, W-20, my+2,  f"{round(ms['wr']*100)}% WR", _font(11), _TEXT_GRAY)
        _text_r(draw, W-20, my+17, f"{ms['kd']:.2f} K/D",        _font(11), _TEXT_GRAY)
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
        _rr(draw, (8, DY+4, W-8, DY+DUO_H-4), 8, fill=(20, 14, 44), outline=_DUO_DIM, width=1)
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

    # ── deep purple theme (same as 2v2) ──
    _BG    = (13,  11,  22)
    _ROW_A = (20,  16,  34)
    _ROW_B = (13,  11,  22)
    _SEP   = (42,  32,  68)
    _HDR   = (110,  98, 145)
    _TEXT  = (232, 230, 248)
    _GOLD  = (232, 185,   0)
    _GREEN = ( 48, 198, 108)
    _RED   = (210,  52,  52)
    _TD    = ( 64,  44, 152)
    _GRAY  = (108,  98, 140)
    _WH    = (255, 255, 255)

    # top-1/2/3: subtle tints + gold/silver/bronze left stripe
    _RANK_BG     = {1: (26, 20, 44), 2: (20, 16, 40), 3: (22, 14, 38)}
    _RANK_STRIPE = {1: (200, 155,  0), 2: (148, 148, 175), 3: (158, 105, 42)}

    img  = Image.new("RGB", (W, H), _BG)
    draw = ImageDraw.Draw(img)

    # ── header ──
    draw.rectangle([(0, 0), (W, HEAD_H - 1)], fill=(9, 7, 18))
    draw.line([(0, HEAD_H - 1), (W, HEAD_H - 1)], fill=_SEP, width=2)
    draw.ellipse([(18, 19), (30, 31)], fill=(110, 80, 232))
    draw.text((38, 17), "ACTUAL FACEIT", font=_font(13, bold=True), fill=_TEXT)

    badge_x = W - 12
    for badge_text, badge_col in [
        ("2V2",   ( 88,  58, 208)),
        ("QUALS", ( 64,  44, 152)),
        ("DEFAULT", (52, 38, 112)),
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

    # ── rows ──
    for i, p in enumerate(players[:n]):
        y    = HEAD_H + i * ROW_H
        rank = p.get("rank", i + 1)

        # row background: gold/silver/bronze tint for top 3, else alternating
        row_bg = _RANK_BG.get(rank, _ROW_A if i % 2 == 0 else _ROW_B)
        draw.rectangle([(0, y), (W, y + ROW_H)], fill=row_bg)

        # coloured left stripe for top 3
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
            draw.ellipse([(ax, ay), (ax + ar*2, ay + ar*2)], fill=av, outline=(190, 186, 172), width=2)
            img  = _paste_avatar(img, av_bytes, ax, ay, ar * 2, border_color=(190, 186, 172), border_width=2)
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

        by2 = y + ROW_H - 21
        _rr(draw, (nx, by2, nx + 20, by2 + 14), 3, fill=_TD)
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
    """
    ct_stats / t_stats — list of dicts:
      {"name", "kills", "deaths", "assists", "elo_change", "is_premium", "is_admin"}
    winner — "ct" or "t"
    """
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

    # glow on header
    glow_col = CT_BLUE if winner == "ct" else T_ORANGE
    img  = _apply_glow(img, (8, 8, W-8, HEAD_H - 8), r=10, color=glow_col, strength=22, layers=10)
    draw = ImageDraw.Draw(img)

    # header panel
    _rr(draw, (8, 8, W-8, HEAD_H - 8), 10, fill=(20, 20, 28), outline=(55, 52, 75), width=1)

    # match code
    draw.text((22, 16), f"МАТЧ #{match_code}", font=_font(22, bold=True), fill=WHITE)

    # league badge
    lg_text = format_league(league).upper()
    lg_col  = TEAL_DIM if league == "default" else (130, 40, 170)
    lw = _tw(draw, lg_text, _font(12, bold=True)) + 14
    _rr(draw, (22, 46, 22 + lw, 66), 4, fill=lg_col)
    draw.text((29, 49), lg_text, font=_font(12, bold=True), fill=WHITE)

    # map
    draw.text((22 + lw + 12, 49), f"🗺  {map_name.upper()}", font=_font(13), fill=LGRAY)

    # Score (center)
    sc_text = f"{score_ct}  :  {score_t}"
    _text_c(draw, W//2, 14, sc_text, _font(40, bold=True), WHITE)
    _text_c(draw, W//2 - 65, 62, "CT",  _font(16, bold=True), CT_BLUE)
    _text_c(draw, W//2 + 65, 62, "T",   _font(16, bold=True), T_ORANGE)

    # Winner label (top right)
    w_label = "💙 CT — ПОБЕДА" if winner == "ct" else "🧡 T — ПОБЕДА"
    w_col   = CT_BLUE if winner == "ct" else T_ORANGE
    _text_r(draw, W - 18, 20, w_label, _font(14, bold=True), w_col)

    # ===== COLUMN HEADERS =====
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

    # MVP = player with most kills across both teams
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
        draw.ellipse([(AX2, AY2), (AX2+AR2*2, AY2+AR2*2)], fill=av_col, outline=(60, 55, 80), width=1)
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

    # divider
    div_x = CT_X + HALF_W + 2
    draw.line([(div_x, HEAD_H), (div_x, H - FOOT_H)], fill=(45, 42, 60), width=2)

    # footer
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

    _BG    = (13, 11, 22)
    _ROW_A = (20, 16, 34)
    _ROW_B = (13, 11, 22)
    _SEP   = (40, 30, 65)
    _HDR   = (108, 96, 140)
    _WH    = (232, 230, 248)
    _GOLD  = (232, 185, 0)
    _GREEN = (48, 198, 108)
    _RED   = (210, 52, 52)
    _PUR   = (108, 78, 228)
    _PURD  = (62, 42, 148)
    _GRAY  = (108, 96, 136)

    img  = Image.new("RGB", (W, H), _BG)
    draw = ImageDraw.Draw(img)

    # ── header ──
    draw.rectangle([(0, 0), (W, HEAD_H - 1)], fill=(8, 6, 18))
    draw.line([(0, HEAD_H - 1), (W, HEAD_H - 1)], fill=_SEP, width=1)
    draw.ellipse([(18, 19), (30, 31)], fill=_PUR)
    draw.text((38, 17), "ACTUAL FACEIT", font=_font(13, bold=True), fill=_WH)

    bw = _tw(draw, "2V2", _font(11, bold=True)) + 16
    bx = W - 12 - bw
    _rr(draw, (bx, 15, bx + bw, 37), 5, fill=_PURD)
    draw.text((bx + 8, 17), "2V2", font=_font(11, bold=True), fill=_WH)

    for label, x in [("#", 22), ("PLAYER  ELO", 74), ("WINS", 430),
                      ("LOSSES", 514), ("W/L%", 600), ("K/D", 710)]:
        draw.text((x, HEAD_H - 22), label, font=_font(11), fill=_HDR)

    # ── rows ──
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
            draw.ellipse([(ax, ay), (ax + ar*2, ay + ar*2)], fill=_PUR, outline=(52, 34, 80), width=2)
            img  = _paste_avatar(img, av_bytes2, ax, ay, ar * 2, border_color=(52, 34, 80), border_width=2)
            draw = ImageDraw.Draw(img)
        else:
            draw.ellipse([(ax, ay), (ax + ar*2, ay + ar*2)], fill=_PUR, outline=(52, 34, 80), width=2)
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
        uid_val = p.get("uid")
        if uid_val:
            draw.text((nx, y + 29), f"#{str(uid_val)[-7:]}", font=_font(10), fill=_GRAY)

        by2 = y + ROW_H - 21
        _rr(draw, (nx, by2, nx + 20, by2 + 14), 3, fill=_PURD)
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
