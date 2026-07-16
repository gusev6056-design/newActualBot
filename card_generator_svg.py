"""
card_generator_svg.py  —  cairosvg-based profile card for Vision Faceit.
Drop-in replacement for generate_profile_card() from card_generator.py.

Requirements:  pip install cairosvg
"""

import io
import math
import base64
from typing import Optional

try:
    import cairosvg as _cairosvg
    _HAS_CAIRO = True
except ImportError:
    _HAS_CAIRO = False


# ════════════════════════════════════════════════════════════════════════════
#  LEVEL HELPERS
# ════════════════════════════════════════════════════════════════════════════

_ELO_THRESHOLDS = [0, 200, 400, 600, 900, 1100, 1400, 1600, 1800, 2000]

_LEVEL_ELO_RANGE = {
    1: (0, 200),   2: (200, 400),  3: (400, 600),   4: (600, 900),
    5: (900, 1100),6: (1100, 1400),7: (1400, 1600),  8: (1600, 1800),
    9: (1800, 2000),10: (2000, 9999),
}

_LEVEL_COLOR = {
    1: "#6e6e6e", 2: "#8a7a6a", 3: "#00a8a8", 4: "#00b0d8",
    5: "#1e8ad2", 6: "#8a20e0", 7: "#c89b00", 8: "#d05508",
    9: "#aad8f8", 10: "#e8b900",
}

_LEVEL_LABEL = {
    1: "IRON", 2: "BRONZE", 3: "SILVER", 4: "STEEL",
    5: "FOREST", 6: "VOID", 7: "GOLD", 8: "FIRE",
    9: "ICE", 10: "PRISM",
}


def get_level(elo: int) -> int:
    for i in range(len(_ELO_THRESHOLDS) - 1, -1, -1):
        if elo >= _ELO_THRESHOLDS[i]:
            return i + 1
    return 1


def _format_league(league: str) -> str:
    return {"quals": "Quals", "default": "Default", "fpl": "FPL"}.get(
        (league or "").lower(), (league or "").capitalize()
    )


# ════════════════════════════════════════════════════════════════════════════
#  SVG HELPERS
# ════════════════════════════════════════════════════════════════════════════

_FONT = "DejaVu Sans, Liberation Sans, Arial, sans-serif"

# Palette
_BG       = "#0d0d14"
_GRID     = "#141420"
_PANEL    = "#1a1a28"
_PANEL2   = "#1e1e2e"
_PANEL3   = "#222236"
_BORDER   = "#2e2a50"
_GOLD     = "#e8b900"
_WHITE    = "#ebebeb"
_MID      = "#9090a8"
_GRAY     = "#606078"
_GREEN    = "#32c66c"
_RED      = "#d23444"
_BLUE     = "#3a7ef0"
_PINK     = "#e03a7a"
_TEAL     = "#00a8c8"


def _e(s: str) -> str:
    """Escape XML special chars in text content."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _rrect(x, y, w, h, rx=8, fill=_PANEL, stroke=None, sw=1) -> str:
    s = f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" ry="{rx}" fill="{fill}"'
    if stroke:
        s += f' stroke="{stroke}" stroke-width="{sw}"'
    return s + "/>"


def _text(x, y, content, size=14, fill=_WHITE, weight="normal",
          anchor="start", opacity=1.0) -> str:
    op = f' opacity="{opacity}"' if opacity != 1.0 else ""
    return (f'<text x="{x}" y="{y}" font-family="{_FONT}" font-size="{size}" '
            f'fill="{fill}" font-weight="{weight}" text-anchor="{anchor}"{op}>'
            f'{_e(str(content))}</text>')


def _ring(cx, cy, r, sw, bg, pct, color, color2=None, gid="") -> str:
    """Progress ring. pct in [0,1]. Optional gradient from color→color2."""
    circ = 2 * math.pi * r
    dash = min(pct, 0.9999) * circ
    parts = []
    parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" '
                 f'stroke="{bg}" stroke-width="{sw}"/>')
    if pct > 0.001:
        if color2:
            gid2 = f"rg{gid}"
            parts.append(f'<defs><linearGradient id="{gid2}" x1="0%" y1="0%" x2="100%" y2="100%">'
                         f'<stop offset="0%" stop-color="{color}"/>'
                         f'<stop offset="100%" stop-color="{color2}"/>'
                         f'</linearGradient></defs>')
            stroke_val = f"url(#{gid2})"
        else:
            stroke_val = color
        parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" '
            f'stroke="{stroke_val}" stroke-width="{sw}" '
            f'stroke-dasharray="{dash:.2f} {circ:.2f}" '
            f'stroke-linecap="round" '
            f'transform="rotate(-90 {cx} {cy})"/>'
        )
    return "\n".join(parts)


def _stat_grade(value: float, key: str):
    """Return (label, color) for a stat."""
    rules = {
        "kd":      [(1.3, "Strong", _BLUE),  (0.9, "Stable", "#f0a030"), (0, "Low", _PINK)],
        "rating":  [(1.0, "Strong", _BLUE),  (0.7, "Stable", "#f0a030"), (0, "Low", _PINK)],
        "avg":     [(14,  "Strong", _BLUE),  (8,   "Stable", "#f0a030"), (0, "Low", _PINK)],
        "impact":  [(1.2, "Strong", _BLUE),  (0.9, "Stable", "#f0a030"), (0, "Low", _PINK)],
        "kpr":     [(0.7, "Strong", _BLUE),  (0.5, "Stable", "#f0a030"), (0, "Low", _PINK)],
        "assists": [(2.5, "Strong", _BLUE),  (1.3, "Stable", "#f0a030"), (0, "Low", _PINK)],
        "svr":     [(0.55,"Strong", _BLUE),  (0.4, "Stable", "#f0a030"), (0, "Low", _PINK)],
    }
    for threshold, label, color in rules.get(key, [(1, "Strong", _BLUE), (0, "Low", _PINK)]):
        if value >= threshold:
            return label, color
    return "Low", _PINK


# ════════════════════════════════════════════════════════════════════════════
#  MAIN GENERATOR
# ════════════════════════════════════════════════════════════════════════════

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

    # ── Derived stats ──────────────────────────────────────────────────────
    lvl     = get_level(elo)
    lv_col  = _LEVEL_COLOR.get(lvl, "#888")
    lv_lo, lv_hi = _LEVEL_ELO_RANGE.get(lvl, (0, 1000))
    elo_pct = min(1.0, max(0.0, (elo - lv_lo) / max(lv_hi - lv_lo, 1)))

    games   = wins + losses
    wr      = round(wins / games * 100, 1) if games > 0 else 0.0
    kd      = round(kills / deaths, 2)     if deaths > 0 else float(kills)
    avg_k   = round(kills / games, 1)      if games > 0 else 0.0
    kpr_val = round(kills / max(games * 8, 1), 2)
    impact  = round((kills + assists) / games, 2) if games > 0 else 0.0
    rating  = round(kd * (wr / 100) * 2, 2)       if games > 0 else 0.0
    assists_pg = round(assists / max(games, 1), 1)
    svr     = round(1 - deaths / max(games * 8, 1), 2)

    kd_pct  = kills / max(kills + deaths, 1)

    # Best map
    best_map = None
    if map_stats:
        best_map = max(
            map_stats,
            key=lambda m: m.get("wins", 0) / max(m.get("wins", 0) + m.get("losses", 0), 1)
        )

    map_total_w = sum(m.get("wins", 0) for m in map_stats) if map_stats else wins
    map_total_l = sum(m.get("losses", 0) for m in map_stats) if map_stats else losses
    map_total_g = map_total_w + map_total_l
    map_wr_pct  = map_total_w / max(map_total_g, 1)

    # Avatar
    av_data_uri = None
    if avatar_bytes:
        b64 = base64.b64encode(avatar_bytes).decode()
        av_data_uri = f"data:image/jpeg;base64,{b64}"

    # Canvas
    W, H = 1055, 820

    # ── Section layout ─────────────────────────────────────────────────────
    HDR_H   = 155        # header height
    RB_Y    = HDR_H + 8  # rank bar y
    RB_H    = 34
    CONT_Y  = RB_Y + RB_H + 8   # content area start
    CONT_H  = H - CONT_Y - 8

    LW      = 655        # left column width
    SB_X    = LW + 16   # sidebar x
    SB_W    = W - SB_X - 8

    # Left column split
    STAT_H  = 272
    MAP_Y   = CONT_Y + STAT_H + 8
    MAP_H   = CONT_H - STAT_H - 8

    # ── Accumulate SVG ─────────────────────────────────────────────────────
    out = []
    def p(s): out.append(s)

    # ── SVG open + defs ────────────────────────────────────────────────────
    p(f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
      f'xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">')

    p('<defs>')
    p(f'<pattern id="grid" width="42" height="42" patternUnits="userSpaceOnUse">'
      f'<path d="M42 0L0 0 0 42" fill="none" stroke="{_GRID}" stroke-width="1"/></pattern>')
    p('<clipPath id="avClip"><rect x="16" y="16" width="116" height="116" rx="6"/></clipPath>')
    p(f'<linearGradient id="lvGrad" x1="0%" y1="0%" x2="100%" y2="0%">'
      f'<stop offset="0%" stop-color="{lv_col}"/>'
      f'<stop offset="100%" stop-color="{lv_col}" stop-opacity="0.35"/></linearGradient>')
    p(f'<linearGradient id="hdrGrad" x1="0%" y1="0%" x2="0%" y2="100%">'
      f'<stop offset="0%" stop-color="#1e1e30"/>'
      f'<stop offset="100%" stop-color="#14141e"/></linearGradient>')
    p(f'<filter id="glow" x="-30%" y="-30%" width="160%" height="160%">'
      f'<feGaussianBlur stdDeviation="5" result="b"/>'
      f'<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>')
    p(f'<filter id="glowsm" x="-20%" y="-20%" width="140%" height="140%">'
      f'<feGaussianBlur stdDeviation="2.5" result="b"/>'
      f'<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>')
    p('</defs>')

    # ── Background ─────────────────────────────────────────────────────────
    p(f'<rect width="{W}" height="{H}" fill="{_BG}"/>')
    p(f'<rect width="{W}" height="{H}" fill="url(#grid)"/>')

    # ══════════════════════════════════════════════════════════════════════
    #  HEADER
    # ══════════════════════════════════════════════════════════════════════
    p(_rrect(8, 8, W-16, HDR_H, rx=10, fill="url(#hdrGrad)", stroke=_BORDER, sw=1))
    # Level color accent strip on top
    p(f'<rect x="8" y="8" width="{W-16}" height="3" rx="2" fill="{lv_col}" filter="url(#glowsm)"/>')

    # Avatar box
    AX, AY, AS = 16, 16, 116
    p(f'<rect x="{AX-3}" y="{AY-3}" width="{AS+6}" height="{AS+6}" rx="9" '
      f'fill="{lv_col}" opacity="0.25" filter="url(#glow)"/>')
    p(_rrect(AX, AY, AS, AS, rx=6, fill="#181830", stroke=lv_col, sw=2))

    if av_data_uri:
        p(f'<image clip-path="url(#avClip)" href="{av_data_uri}" '
          f'x="{AX}" y="{AY}" width="{AS}" height="{AS}" '
          f'preserveAspectRatio="xMidYMid slice"/>')
    else:
        initials = (username[:2]).upper() if username else "??"
        p(_text(AX + AS//2, AY + AS//2 + 14, initials, 36, _GOLD, "bold", anchor="middle"))

    # Player info
    IX = AX + AS + 18
    p(_text(IX, 34, f"#: {user_id}", 12, _GRAY))
    # Username
    uname_display = username
    if is_premium:
        uname_display += " ★"
    p(_text(IX, 64, uname_display, 30, _WHITE, "bold"))
    if is_admin:
        adm_x = IX + min(len(uname_display) * 17 + 10, W - 250 - IX)
        p(_rrect(adm_x, 44, 42, 22, rx=4, fill="#aa1818"))
        p(_text(adm_x + 4, 59, "ADM", 11, _WHITE, "bold"))
    p(_text(IX, 88, f"ID: {game_id}", 13, _GRAY))

    # ELO (right)
    p(_text(W - 220, 34, "ELO RATING", 11, _GRAY))
    p(_text(W - 18, 80, str(elo), 50, _GOLD, "bold", anchor="end"))

    # Level badge
    BX, BY, BS = W - 72, 92, 44
    p(_rrect(BX, BY, BS, BS, rx=8, fill=lv_col, stroke=f"{lv_col}88", sw=2))
    p(_text(BX + BS//2, BY + 28, str(lvl), 22, _WHITE, "bold", anchor="middle"))

    # Banner art area (decorative lines, right side)
    for i, (lx, opacity) in enumerate([(W-320, 0.06), (W-280, 0.08), (W-240, 0.10), (W-200, 0.08)]):
        p(f'<line x1="{lx}" y1="8" x2="{lx + 200}" y2="{8+HDR_H}" '
          f'stroke="{lv_col}" stroke-width="1" opacity="{opacity}"/>')

    # ══════════════════════════════════════════════════════════════════════
    #  RANK BAR
    # ══════════════════════════════════════════════════════════════════════
    p(_rrect(8, RB_Y, W-16, RB_H, rx=6, fill="#14142a", stroke=_BORDER, sw=1))
    p(f'<circle cx="28" cy="{RB_Y+17}" r="6" fill="none" stroke="{_WHITE}" stroke-width="1.5"/>')
    p(_text(40, RB_Y + 22, f"GLOBAL RANK:  #{global_rank}", 12, _WHITE, "bold"))
    p(f'<line x1="228" y1="{RB_Y+5}" x2="228" y2="{RB_Y+29}" stroke="{_BORDER}" stroke-width="1"/>')
    p(f'<circle cx="240" cy="{RB_Y+17}" r="5" fill="{_GOLD}"/>')
    p(_text(252, RB_Y + 22, f"LEAGUE:  {_format_league(league).upper()}", 12, _WHITE, "bold"))

    # Level label right side
    lv_lbl = _LEVEL_LABEL.get(lvl, "")
    p(_text(W - 20, RB_Y + 22, lv_lbl, 12, lv_col, "bold", anchor="end"))

    # ══════════════════════════════════════════════════════════════════════
    #  STATISTIC SECTION (left column, top)
    # ══════════════════════════════════════════════════════════════════════
    p(_rrect(8, CONT_Y, LW, STAT_H, rx=8, fill=_PANEL, stroke=_BORDER, sw=1))

    # Title row
    # Bar chart icon
    for bx, bh in [(16, 8), (22, 12), (28, 16)]:
        p(f'<rect x="{bx}" y="{CONT_Y + 24 - bh}" width="4" height="{bh}" fill="{_GRAY}"/>')
    p(_text(36, CONT_Y + 22, "Statistic", 13, _GRAY))

    # ── KD Ring ────────────────────────────────────────────────────────────
    KD_CX, KD_CY, KD_R = 80, CONT_Y + 122, 52
    p(_ring(KD_CX, KD_CY, KD_R, 10, _PANEL3, kd_pct, _BLUE, _PINK, "kd"))
    p(_text(KD_CX, KD_CY - 7, f"{kd:.2f}", 22, _WHITE, "bold", anchor="middle"))
    p(_text(KD_CX, KD_CY + 13, "K/D", 10, _GRAY, anchor="middle"))

    # Kill / Death text
    KDT_X = KD_CX + KD_R + 18
    p(_text(KDT_X, CONT_Y + 60, "Kill/Deaths", 12, _GRAY))
    p(_text(KDT_X, CONT_Y + 82, f"K = {kills}", 14, _BLUE, "bold"))
    p(_text(KDT_X + 90, CONT_Y + 82, f"D = {deaths}", 14, _PINK, "bold"))

    # ── Level / ELO section ────────────────────────────────────────────────
    LP_X = 345
    LP_Y = CONT_Y + 36

    p(_text(LP_X, LP_Y + 14, "Level", 12, _GRAY))

    # Level badge (gold diamond-ish)
    LB_CX = LP_X + 265
    p(f'<rect x="{LB_CX - 20}" y="{LP_Y - 2}" width="40" height="40" rx="8" '
      f'fill="{_GOLD}" stroke="{_GOLD}66" stroke-width="2"/>')
    p(_text(LB_CX, LP_Y + 23, str(lvl), 22, "#0a0800", "bold", anchor="middle"))

    # ELO bar
    p(_text(LP_X, LP_Y + 36, str(elo), 13, lv_col, "bold"))
    p(_text(LP_X + 275, LP_Y + 36, str(lv_hi), 11, _GRAY, anchor="end"))

    ELO_BAR_Y = LP_Y + 44
    ELO_BAR_W = 278
    p(f'<rect x="{LP_X}" y="{ELO_BAR_Y}" width="{ELO_BAR_W}" height="8" rx="4" fill="{_PANEL3}"/>')
    filled_w = max(4, int(ELO_BAR_W * elo_pct))
    p(f'<rect x="{LP_X}" y="{ELO_BAR_Y}" width="{filled_w}" height="8" rx="4" '
      f'fill="url(#lvGrad)"/>')

    # ── 6 Mini stat cards (2 rows × 3) ────────────────────────────────────
    MINI_STATS = [
        ("Rating",  f"{rating:.2f}",     rating,     "rating"),
        ("AVG",     f"{avg_k:.1f}",      avg_k,      "avg"),
        ("Impact",  f"{impact:.2f}",     impact,     "impact"),
        ("KPR",     f"{kpr_val:.2f}",    kpr_val,    "kpr"),
        ("Assists", f"{assists_pg:.1f}", assists_pg, "assists"),
        ("SVR",     f"{svr:.2f}",        svr,        "svr"),
    ]

    N_COLS  = 3
    CARD_W  = (LW - 16 - (N_COLS - 1) * 6) // N_COLS
    CARD_H  = 60
    ROW_Y0  = CONT_Y + STAT_H - 2 * CARD_H - 6 - 8

    for i, (label, val_str, val_f, key) in enumerate(MINI_STATS):
        col = i % N_COLS
        row = i // N_COLS
        cx  = 8 + col * (CARD_W + 6)
        cy  = ROW_Y0 + row * (CARD_H + 6)

        grade_lbl, grade_col = _stat_grade(val_f, key)
        bar_w = int((CARD_W - 16) * 0.65)

        p(_rrect(cx, cy, CARD_W, CARD_H, rx=6, fill=_PANEL2, stroke=_BORDER, sw=1))
        p(_text(cx + 10, cy + 16, label, 10, _GRAY))
        p(_text(cx + CARD_W - 10, cy + CARD_H - 14, val_str, 26, _WHITE, "bold", anchor="end"))
        # Grade bar
        BY2 = cy + CARD_H - 20
        p(f'<rect x="{cx+8}" y="{BY2}" width="{CARD_W-16}" height="3" rx="1" fill="{_PANEL3}"/>')
        p(f'<rect x="{cx+8}" y="{BY2}" width="{bar_w}" height="3" rx="1" fill="{grade_col}"/>')
        p(_text(cx + 8, cy + CARD_H - 4, grade_lbl, 9, grade_col))

    # ══════════════════════════════════════════════════════════════════════
    #  MAP STATISTIC SECTION (left column, bottom)
    # ══════════════════════════════════════════════════════════════════════
    p(_rrect(8, MAP_Y, LW, MAP_H, rx=8, fill=_PANEL, stroke=_BORDER, sw=1))

    # Title icon (controller-ish square)
    p(f'<rect x="16" y="{MAP_Y+12}" width="16" height="14" rx="3" fill="none" '
      f'stroke="{_GRAY}" stroke-width="1.5"/>')
    p(f'<line x1="20" y1="{MAP_Y+19}" x2="28" y2="{MAP_Y+19}" stroke="{_GRAY}" stroke-width="1.5"/>')
    p(_text(38, MAP_Y + 22, "Map Statistic", 13, _GRAY))

    # ── Win Rate Ring ──────────────────────────────────────────────────────
    WR_CX, WR_CY, WR_R = 80, MAP_Y + 98, 54
    p(_ring(WR_CX, WR_CY, WR_R, 10, _PANEL3, map_wr_pct, _BLUE, _PINK, "wr"))
    wr_label = f"{round(map_wr_pct * 100)}%"
    p(_text(WR_CX, WR_CY - 8, wr_label, 22, _WHITE, "bold", anchor="middle"))
    p(_text(WR_CX, WR_CY + 13, "Win Rate", 10, _GRAY, anchor="middle"))

    # W/L breakdown
    WR_TX = WR_CX + WR_R + 18
    p(_text(WR_TX, MAP_Y + 62, "Win Rate", 12, _GRAY))
    p(_text(WR_TX, MAP_Y + 82, f"W = {map_total_w}", 14, _BLUE, "bold"))
    p(_text(WR_TX + 92, MAP_Y + 82, f"L = {map_total_l}", 14, _PINK, "bold"))

    # ── Best Map panel ─────────────────────────────────────────────────────
    if best_map:
        BM = best_map
        bm_name = BM.get("map", "Unknown").title()
        bm_w = BM.get("wins", 0)
        bm_l = BM.get("losses", 0)
        bm_g = bm_w + bm_l
        bm_wr = round(bm_w / max(bm_g, 1) * 100)
        bm_kd = BM.get("kd", 0.0)

        BPX, BPY = 320, MAP_Y + 36
        BPW, BPH = 215, 120
        p(_rrect(BPX, BPY, BPW, BPH, rx=8, fill=_PANEL2, stroke=_BORDER, sw=1))

        # "BEST MAP" rotated label
        p(f'<text x="{BPX + BPW - 10}" y="{BPY + BPH//2}" '
          f'font-family="{_FONT}" font-size="9" fill="{_GRAY}" '
          f'font-weight="bold" text-anchor="middle" '
          f'transform="rotate(90 {BPX+BPW-10} {BPY+BPH//2})">BEST MAP</text>')

        # Map color thumbnail
        thumb_x, thumb_y, thumb_s = BPX + 8, BPY + 8, 78
        p(_rrect(thumb_x, thumb_y, thumb_s, thumb_s, rx=4, fill="#1e304a", stroke=_BLUE, sw=1))
        p(_text(thumb_x + thumb_s//2, thumb_y + thumb_s//2 + 6,
                bm_name[:3].upper(), 14, _WHITE, "bold", anchor="middle"))
        # Small map icon
        p(f'<rect x="{thumb_x+6}" y="{thumb_y+60}" width="{thumb_s-12}" height="12" '
          f'rx="2" fill="#2a4060" opacity="0.7"/>')

        # Stats
        TX2 = BPX + thumb_s + 18
        p(_text(TX2, BPY + 26, bm_name, 14, _WHITE, "bold"))
        p(_text(TX2, BPY + 44, f"W = {bm_w}", 12, _BLUE, "bold"))
        p(_text(TX2 + 60, BPY + 44, f"L = {bm_l}", 12, _PINK, "bold"))
        p(_text(BPX + 10, BPY + BPH - 8, f"K/D = {bm_kd:.2f}", 11, _GRAY))
        p(_text(BPX + 100, BPY + BPH - 8, f"W/R = {bm_wr}%", 11, _GRAY))

    # ── Map grid ───────────────────────────────────────────────────────────
    other_maps = [m for m in (map_stats or []) if m is not best_map][:6]
    GRID_Y  = MAP_Y + MAP_H - 2 * 66 - 10
    MCOL_W  = (LW - 16 - 2 * 6) // 3
    MCOL_H  = 60

    for mi, ms in enumerate(other_maps):
        col2 = mi % 3
        row2 = mi // 3
        mx   = 8 + col2 * (MCOL_W + 6)
        my   = GRID_Y + row2 * (MCOL_H + 6)
        mw   = ms.get("wins", 0)
        ml   = ms.get("losses", 0)
        mg   = mw + ml
        mwr  = round(mw / max(mg, 1) * 100)
        mkd  = ms.get("kd", 0.0)
        mname = ms.get("map", "").title()

        p(_rrect(mx, my, MCOL_W, MCOL_H, rx=6, fill=_PANEL2, stroke=_BORDER, sw=1))
        # Map thumb
        TS = MCOL_H - 12
        p(_rrect(mx+6, my+6, TS, TS, rx=4, fill="#1e304a"))
        p(_text(mx+6+TS//2, my+6+TS//2+5, mname[:3].upper(), 8, _WHITE, "bold", anchor="middle"))
        # Text
        TX3 = mx + TS + 12
        p(_text(TX3, my + 20, mname[:12], 11, _WHITE, "bold"))
        p(_text(TX3, my + 36, f"W={mw}  L={ml}", 10, _GRAY))
        p(_text(TX3, my + 50, f"K/D={mkd:.2f}  {mwr}%WR", 10, _GRAY))

    # ══════════════════════════════════════════════════════════════════════
    #  RIGHT SIDEBAR
    # ══════════════════════════════════════════════════════════════════════
    SB_Y = CONT_Y

    # ── Cosmetic slots row ─────────────────────────────────────────────────
    SLOT_N  = 5
    SLOT_W  = (SB_W - (SLOT_N - 1) * 4) // SLOT_N
    SLOT_H  = 36
    for si in range(SLOT_N):
        sx = SB_X + si * (SLOT_W + 4)
        p(_rrect(sx, SB_Y, SLOT_W, SLOT_H, rx=4, fill=_PANEL2, stroke=_BORDER, sw=1))
        # small icon placeholder
        ic = SLOT_H // 2 - 4
        p(f'<rect x="{sx+SLOT_W//2-ic//2}" y="{SB_Y+SLOT_H//2-ic//2}" '
          f'width="{ic}" height="{ic}" rx="2" fill="{_PANEL3}"/>')

    # ── Info panel (playtime, join date, game, mvp) ────────────────────────
    INF_Y  = SB_Y + SLOT_H + 8
    INF_H  = 106
    p(_rrect(SB_X, INF_Y, SB_W, INF_H, rx=8, fill=_PANEL, stroke=_BORDER, sw=1))

    half = SB_W // 2

    # Row 1: Playtime | Join Date
    def _info_cell(lx, ly, icon_char, label, value, col=_WHITE):
        p(f'<text x="{lx+6}" y="{ly+16}" font-family="{_FONT}" font-size="13" fill="{_GRAY}">{_e(icon_char)}</text>')
        p(_text(lx + 24, ly + 16, label, 10, _GRAY))
        p(_text(lx + 6, ly + 36, value, 18, col, "bold"))

    _info_cell(SB_X + 6,         INF_Y + 6,  "⏱", "Playtime",  f"{max(games, 0)}h")
    _info_cell(SB_X + half + 4,  INF_Y + 6,  "📅", "Join Date", "--.--.----")

    p(f'<line x1="{SB_X+10}" y1="{INF_Y+50}" x2="{SB_X+SB_W-10}" y2="{INF_Y+50}" '
      f'stroke="{_BORDER}" stroke-width="1"/>')

    _info_cell(SB_X + 6,         INF_Y + 54, "🎮", "Game",  str(games))
    _info_cell(SB_X + half + 4,  INF_Y + 54, "⭐", "MVP",   str(mvp_count), _GOLD)

    # ── League section ─────────────────────────────────────────────────────
    LG_Y = INF_Y + INF_H + 8
    LG_H = 72
    p(_rrect(SB_X, LG_Y, SB_W, LG_H, rx=8, fill=_PANEL, stroke=_BORDER, sw=1))
    p(_text(SB_X + 12, LG_Y + 16, "League", 10, _GRAY))
    p(_text(SB_X + 12, LG_Y + 44, _format_league(league), 18, _WHITE, "bold"))

    # Hexagon badge
    HCX = SB_X + SB_W - 34
    HCY = LG_Y + LG_H // 2
    HR  = 26
    hex_pts = " ".join(
        f"{HCX + HR * math.cos(math.radians(60*i - 30)):.1f},"
        f"{HCY + HR * math.sin(math.radians(60*i - 30)):.1f}"
        for i in range(6)
    )
    p(f'<polygon points="{hex_pts}" fill="{_PANEL3}" stroke="{_MID}" stroke-width="1.5"/>')
    # Eye logo (small) inside badge — Vision theme
    ECX, ECY = HCX, HCY
    p(f'<ellipse cx="{ECX}" cy="{ECY}" rx="10" ry="7" fill="none" '
      f'stroke="#ff6600" stroke-width="1.5"/>')
    p(f'<circle cx="{ECX}" cy="{ECY}" r="4" fill="#ff6600"/>')
    p(f'<circle cx="{ECX-1}" cy="{ECY-1}" r="1.5" fill="rgba(255,255,255,0.35)"/>')

    # ── Leaderboard / Places ────────────────────────────────────────────────
    LBD_Y = LG_Y + LG_H + 8
    # Reserve bottom for recent matches
    REC_H = 106
    LBD_H = H - LBD_Y - REC_H - 16
    p(_rrect(SB_X, LBD_Y, SB_W, LBD_H, rx=8, fill=_PANEL, stroke=_BORDER, sw=1))
    p(_text(SB_X + 12, LBD_Y + 16, "Places", 10, _GRAY))

    RANK_COLORS = [_GOLD, _MID, "#c88020"]

    # Top 3 leaderboard entries
    lb_top3 = (leaderboard or [])[:3]
    for li, entry in enumerate(lb_top3):
        rank_n  = entry[0] if len(entry) > 0 else li+1
        lname   = entry[1] if len(entry) > 1 else "?"
        lelo    = entry[2] if len(entry) > 2 else 0
        rc      = RANK_COLORS[li] if li < 3 else _GRAY
        ey      = LBD_Y + 26 + li * 36

        p(f'<text x="{SB_X+10}" y="{ey+14}" font-family="{_FONT}" '
          f'font-size="13" fill="{rc}" font-weight="bold">#{rank_n}</text>')
        p(f'<circle cx="{SB_X+44}" cy="{ey+10}" r="12" fill="{_PANEL3}" '
          f'stroke="{rc}" stroke-width="1"/>')
        p(_text(SB_X+44, ey+15, (lname[:2]).upper(), 9, rc, "bold", anchor="middle"))
        p(_text(SB_X+60, ey+15, _e(lname), 12, _WHITE))
        p(_text(SB_X+SB_W-8, ey+15, str(lelo), 12, _WHITE, anchor="end"))

        if li < len(lb_top3) - 1:
            p(f'<line x1="{SB_X+10}" y1="{ey+28}" x2="{SB_X+SB_W-10}" y2="{ey+28}" '
              f'stroke="{_BORDER}" stroke-width="1"/>')

    # Self entry (current user rank)
    self_y = LBD_Y + 26 + len(lb_top3) * 36 + 4
    if self_y + 28 < LBD_Y + LBD_H:
        p(f'<line x1="{SB_X+10}" y1="{self_y-4}" x2="{SB_X+SB_W-10}" y2="{self_y-4}" '
          f'stroke="{_BORDER}" stroke-width="1"/>')
        p(f'<text x="{SB_X+10}" y="{self_y+14}" font-family="{_FONT}" '
          f'font-size="13" fill="{_GRAY}" font-weight="bold">#{global_rank}</text>')
        p(f'<circle cx="{SB_X+44}" cy="{self_y+10}" r="12" fill="{_PANEL3}" '
          f'stroke="{_GRAY}" stroke-width="1"/>')
        p(_text(SB_X+44, self_y+15, (username[:2]).upper(), 9, _GRAY, "bold", anchor="middle"))
        p(_text(SB_X+60, self_y+15, _e(username), 12, _GRAY))

    # ── Recent Matches ──────────────────────────────────────────────────────
    REC_Y = H - REC_H - 8
    p(_rrect(SB_X, REC_Y, SB_W, REC_H, rx=8, fill=_PANEL, stroke=_BORDER, sw=1))
    p(_text(SB_X + 12, REC_Y + 16, "Recent Matches", 11, _GRAY, "bold"))

    SQ = 30
    SQ_GAP = 4
    PER_ROW = 7
    recent_list = list(recent or [])[:21]

    for ri, won in enumerate(recent_list):
        rc2  = ri % PER_ROW
        rr2  = ri // PER_ROW
        rx2  = SB_X + 6 + rc2 * (SQ + SQ_GAP)
        ry2  = REC_Y + 22 + rr2 * (SQ + SQ_GAP)
        sq_c = _GREEN if won else _RED
        p(f'<rect x="{rx2}" y="{ry2}" width="{SQ}" height="{SQ}" rx="4" '
          f'fill="{sq_c}22" stroke="{sq_c}" stroke-width="1.5"/>')
        p(_text(rx2 + SQ//2, ry2 + SQ//2 + 5, "W" if won else "L", 12,
                sq_c, "bold", anchor="middle"))

    for ri in range(len(recent_list), 21):
        rc2  = ri % PER_ROW
        rr2  = ri // PER_ROW
        rx2  = SB_X + 6 + rc2 * (SQ + SQ_GAP)
        ry2  = REC_Y + 22 + rr2 * (SQ + SQ_GAP)
        p(f'<rect x="{rx2}" y="{ry2}" width="{SQ}" height="{SQ}" rx="4" '
          f'fill="{_PANEL3}" stroke="{_BORDER}" stroke-width="1"/>')

    # ══════════════════════════════════════════════════════════════════════
    #  FOOTER BRANDING  —  VISION FACEIT
    # ══════════════════════════════════════════════════════════════════════
    # Bottom accent line
    p(f'<rect x="8" y="{H-4}" width="{W-16}" height="3" rx="2" fill="{lv_col}" opacity="0.4"/>')

    # Eye logo
    EY_X, EY_Y = W - 174, H - 22
    p(f'<ellipse cx="{EY_X}" cy="{EY_Y}" rx="14" ry="9" fill="none" '
      f'stroke="#ff5500" stroke-width="1.8" filter="url(#glowsm)"/>')
    p(f'<circle cx="{EY_X}" cy="{EY_Y}" r="5" fill="#ff5500"/>')
    p(f'<circle cx="{EY_X-1}" cy="{EY_Y-1}" r="2" fill="rgba(255,255,255,0.3)"/>')

    # "VISION FACEIT" text
    p(f'<text x="{W - 152}" y="{H - 15}" font-family="{_FONT}" '
      f'font-size="13" fill="{_GRAY}" font-weight="bold" text-anchor="start">'
      f'VISION FACEIT</text>')

    p('</svg>')

    # ── Render ─────────────────────────────────────────────────────────────
    svg_str = "\n".join(out)
    buf = io.BytesIO()

    if _HAS_CAIRO:
        _cairosvg.svg2png(bytestring=svg_str.encode("utf-8"), write_to=buf)
    else:
        # Fallback: return SVG as-is (won't display in Telegram but won't crash)
        buf.write(svg_str.encode("utf-8"))

    buf.seek(0)
    return buf
