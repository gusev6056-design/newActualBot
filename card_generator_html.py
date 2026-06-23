"""
Actual Faceit — HTML-based profile card generator.
Uses Playwright (Chromium) to render pixel-perfect cards from HTML/CSS.
Drop-in replacement for the Pillow-based card_generator.py.
"""

from __future__ import annotations

import base64
import io
import math
import os
from typing import Optional

# Проверяем зависимости при импорте — если недоступны, бот переключится на PIL
try:
    import fitz as _fitz_test  # noqa: F401
    from weasyprint import HTML as _wp_test  # noqa: F401
    # Реальный тест рендера — импорт может пройти, но Cairo/Pango могут отсутствовать
    _pdf_test = _wp_test(string="<p>test</p>").write_pdf()
    _doc_test = _fitz_test.open(stream=_pdf_test, filetype="pdf")
    del _fitz_test, _wp_test, _pdf_test, _doc_test
except Exception as _dep_err:
    raise ImportError(f"weasyprint/PyMuPDF недоступны, используем PIL: {_dep_err}")


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_level(elo: int) -> int:
    thresholds = [0, 801, 951, 1101, 1251, 1401, 1551, 1701, 1851, 2001]
    for i in range(len(thresholds) - 1, -1, -1):
        if elo >= thresholds[i]:
            return i + 1
    return 1


def _elo_range(lvl: int) -> tuple[int, int]:
    thresholds = [0, 801, 951, 1101, 1251, 1401, 1551, 1701, 1851, 2001, 9999]
    lo = thresholds[max(0, lvl - 1)]
    hi = thresholds[min(lvl, len(thresholds) - 1)]
    return lo, hi


def _elo_pct(elo: int, lvl: int) -> float:
    lo, hi = _elo_range(lvl)
    if hi <= lo:
        return 1.0
    return max(0.0, min(1.0, (elo - lo) / (hi - lo)))


LVL_COLORS = {
    1:  "#5a5a5a", 2:  "#6e6e6e", 3:  "#00a0a0",
    4:  "#00a0a0", 5:  "#1e82d2", 6:  "#1e82d2",
    7:  "#c89b00", 8:  "#c89b00", 9:  "#d7550a",
    10: "#e8b900",
}

MAP_COLORS = {
    "rust":      "linear-gradient(135deg,#8b3a1a 0%,#c45c2a 100%)",
    "province":  "linear-gradient(135deg,#1a3a5c 0%,#2a6090 100%)",
    "sandstone": "linear-gradient(135deg,#7a5c1a 0%,#c49a2a 100%)",
    "sakura":    "linear-gradient(135deg,#6a1a4a 0%,#c040a0 100%)",
    "zone 9":    "linear-gradient(135deg,#1a5c2a 0%,#2a9050 100%)",
    "breeze":    "linear-gradient(135deg,#1a4a5c 0%,#2a80b0 100%)",
}

def _map_bg(name: str) -> str:
    return MAP_COLORS.get(name.lower(), "linear-gradient(135deg,#2a2a3a,#3a3a4a)")


def _avatar_b64(avatar_bytes: Optional[bytes]) -> str:
    if not avatar_bytes:
        return ""
    b = base64.b64encode(avatar_bytes).decode()
    return f"data:image/jpeg;base64,{b}"


def _result_color(r: str) -> str:
    return "#3ab76e" if r.upper() == "W" else "#d73a3a"


def _stat_label(val: float) -> tuple[str, str]:
    """Return (label, color) based on stat value."""
    if val >= 1.4:
        return "Excellent", "#3ab76e"
    if val >= 1.1:
        return "Strong", "#1e82d2"
    if val >= 0.8:
        return "Stable", "#c89b00"
    return "Low", "#d73a3a"


# ──────────────────────────────────────────────────────────────────────────────
# SVG donut helper
# ──────────────────────────────────────────────────────────────────────────────

def _donut_svg(pct: float, value: str, size: int = 90,
               stroke: int = 9, color: str = "#1e82d2",
               bg: str = "#d73a3a") -> str:
    r = (size - stroke) / 2
    cx = cy = size / 2
    circ = 2 * math.pi * r
    filled = circ * pct
    gap = circ - filled
    return f"""<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}">
  <circle cx="{cx}" cy="{cy}" r="{r}" fill="none"
    stroke="{bg}" stroke-width="{stroke}"/>
  <circle cx="{cx}" cy="{cy}" r="{r}" fill="none"
    stroke="{color}" stroke-width="{stroke}"
    stroke-dasharray="{filled:.2f} {gap:.2f}"
    stroke-linecap="round"
    transform="rotate(-90 {cx} {cy})"/>
  <text x="{cx}" y="{cy}" text-anchor="middle" dominant-baseline="central"
    font-family="Inter,Arial,sans-serif" font-size="{size*0.22:.0f}"
    font-weight="700" fill="#ffffff">{value}</text>
</svg>"""


# ──────────────────────────────────────────────────────────────────────────────
# HTML template builder
# ──────────────────────────────────────────────────────────────────────────────

def _build_html(
    username: str,
    game_id: str,
    user_id: int,
    elo: int,
    wins: int,
    losses: int,
    kills: int,
    deaths: int,
    assists: int,
    is_premium: bool,
    is_admin: bool,
    global_rank: int,
    league: str,
    map_stats: list,
    recent: list,
    leaderboard: list,
    quals_stats: Optional[dict],
    mvp_count: int,
    is_verified: bool,
    duo_stats: Optional[dict],
    avatar_bytes: Optional[bytes],
    active_frame: str,
    active_banner: str,
    playtime_hours: int = 0,
    join_date: str = "",
    games: int = 0,
) -> str:
    lvl = _get_level(elo)
    total_games = wins + losses
    if games > 0:
        total_games = games
    wr = round(wins / total_games * 100, 1) if total_games > 0 else 0.0
    kd = round(kills / deaths, 2) if deaths > 0 else float(kills)
    avg_k = round(kills / total_games, 1) if total_games > 0 else 0.0
    kpr = round(kills / max(total_games * 8, 1), 2)
    impact = round((kills + assists) / total_games, 2) if total_games > 0 else 0.0
    rating = round(kd * (wr / 100) * 2, 2) if total_games > 0 else 0.0
    svr = round(assists / total_games, 2) if total_games > 0 else 0.0
    avg_assists = round(assists / total_games, 1) if total_games > 0 else 0.0

    lvl_color = LVL_COLORS.get(lvl, "#e8b900")
    lo, hi = _elo_range(lvl)
    elo_pct = _elo_pct(elo, lvl)

    kd_pct = min(kd / 2.0, 1.0)
    kd_donut = _donut_svg(kd_pct, f"{kd:.2f}", size=88, stroke=8,
                           color="#1e82d2", bg="#d73a3a")

    wr_pct = wr / 100.0
    wr_donut = _donut_svg(wr_pct, f"{wr:.0f}%", size=84, stroke=8,
                           color="#1e82d2", bg="#d73a3a")

    avatar_src = _avatar_b64(avatar_bytes)

    rating_label, rating_col = _stat_label(rating)
    avg_label, avg_col = _stat_label(avg_k / 20)
    imp_label, imp_col = _stat_label(impact / 15)
    kpr_label, kpr_col = _stat_label(kpr / 0.15)
    svr_label, svr_col = _stat_label(svr / 3.0)

    # ── leaderboard places ──
    # entry may be a dict {"username":..., "avatar_bytes":...}
    # or a tuple (rank, username, elo, is_premium, is_admin, is_verified)
    places_html = ""
    user_rank_html = ""
    for i, entry in enumerate(leaderboard[:3]):
        if isinstance(entry, dict):
            name = entry.get("username", "?")
            av = entry.get("avatar_bytes", None)
        else:
            name = entry[1] if len(entry) > 1 else "?"
            av = None
        av_src = _avatar_b64(av) if av else ""
        places_html += f"""
        <div class="place-row">
          <span class="place-num">#{i+1}</span>
          <div class="place-avatar">
            {"<img src='" + av_src + "'/>" if av_src else "<div class='av-ph'></div>"}
          </div>
          <span class="place-name">{name}</span>
        </div>"""

    if global_rank > 0:
        user_rank_html = f"""
        <div class="place-row user-rank">
          <span class="place-num">#{global_rank}</span>
          <div class="place-avatar">
            {"<img src='" + avatar_src + "'/>" if avatar_src else "<div class='av-ph'></div>"}
          </div>
          <span class="place-name">{username}</span>
        </div>"""

    # ── map stats ──
    map_cards_html = ""
    best_map = None
    best_map_wr = -1
    for m in map_stats:
        mn = m.get("map", "?")
        mw = m.get("wins", 0)
        ml = m.get("losses", 0)
        mwrv = round(mw / (mw + ml) * 100) if mw + ml > 0 else 0
        mkd = m.get("kd", 0.0)
        if mwrv > best_map_wr:
            best_map_wr = mwrv
            best_map = m
        img = _map_bg(mn)
        map_cards_html += f"""
        <div class="map-mini">
          <div class="map-mini-img" style="background:{img}">
            <span class="map-mini-name">{mn}</span>
          </div>
          <div class="map-mini-stats">
            <span>W = <b class="green">{mw}</b>  L = <b class="red">{ml}</b></span>
            <span>K/D = {mkd:.2f}</span>
            <span>W/R = {mwrv}%</span>
          </div>
        </div>"""

    # best map large card
    best_map_html = ""
    if best_map:
        mn = best_map.get("map", "?")
        mw = best_map.get("wins", 0)
        ml = best_map.get("losses", 0)
        mwrv = round(mw / (mw + ml) * 100) if mw + ml > 0 else 0
        mkd = best_map.get("kd", 0.0)
        img = _map_bg(mn)
        best_map_html = f"""
        <div class="best-map">
          <div class="best-map-img" style="background:{img}">
            <div class="best-map-label">BEST MAP</div>
          </div>
          <div class="best-map-info">
            <div class="best-map-name">{mn}</div>
            <div class="best-map-line">W = <b class="green">{mw}</b>  L = <b class="red">{ml}</b></div>
            <div class="best-map-line">K/D = {mkd:.2f}&nbsp;&nbsp;W/R = {mwrv}%</div>
          </div>
        </div>"""

    # ── recent matches ──
    recent_html = ""
    rows = [recent[i:i+7] for i in range(0, min(len(recent), 35), 7)]
    for row in rows[:5]:
        recent_html += "<div class='rec-row'>"
        for r in row:
            c = _result_color(r)
            recent_html += f"<div class='rec-cell' style='background:{c}'>{r.upper()}</div>"
        recent_html += "</div>"

    # ── quals section ──
    quals_html = ""
    if quals_stats:
        qw = quals_stats.get("wins", 0)
        ql = quals_stats.get("losses", 0)
        qkd = quals_stats.get("kd", 0.0)
        qelo = quals_stats.get("elo", 0)
        quals_html = f"""
        <div class="quals-row">
          <span class="quals-title">Quals</span>
          <span>ELO: <b>{qelo}</b></span>
          <span>W = <b class="green">{qw}</b>  L = <b class="red">{ql}</b></span>
          <span>K/D = <b>{qkd:.2f}</b></span>
        </div>"""

    league_display = {"quals": "Quals", "default": "Default"}.get(
        (league or "default").lower(), (league or "Default").capitalize()
    )

    premium_badge = ""
    if is_premium:
        premium_badge = "<span class='premium-badge'>👑 PREMIUM</span>"
    if is_admin:
        premium_badge += "<span class='admin-badge'>⚙️ ADMIN</span>"

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet"/>
<style>
*{{margin:0;padding:0;box-sizing:border-box;}}
body{{
  width:1055px;
  background:#0e0c18;
  font-family:'Inter',Arial,sans-serif;
  color:#e8e6f8;
}}

.card{{
  width:1055px;
  min-height:695px;
  background:#0e0c18;
  position:relative;
  overflow:hidden;
}}

/* grid lines */
.card::before{{
  content:'';
  position:absolute;inset:0;
  background-image:
    linear-gradient(rgba(130,96,242,.07) 1px,transparent 1px),
    linear-gradient(90deg,rgba(130,96,242,.07) 1px,transparent 1px);
  background-size:42px 42px;
  pointer-events:none;
}}

/* ── HEADER ── */
.header{{
  position:relative;
  height:150px;
  margin:8px 8px 0;
  border-radius:10px 10px 8px 8px;
  overflow:hidden;
  border:1px solid rgba(130,96,242,.35);
  background:#14112a;
  box-shadow:0 0 28px rgba(130,96,242,.25);
}}
.header-bg{{
  position:absolute;inset:0;
  background:linear-gradient(120deg,#0e0c18 0%,#1a1230 40%,#241545 100%);
  opacity:.85;
}}
.header-skull{{
  position:absolute;right:0;top:0;bottom:0;width:350px;
  background:linear-gradient(90deg,transparent,#1a0f2e 30%,#2d1a4a 100%);
  display:flex;align-items:center;justify-content:flex-end;
}}
.header-skull svg{{opacity:.15;}}
.header-content{{
  position:relative;z-index:2;
  display:flex;align-items:center;
  height:100%;padding:14px 20px;gap:18px;
}}
.avatar-wrap{{
  width:110px;height:110px;flex-shrink:0;
  border-radius:8px;
  border:2px solid rgba(130,96,242,.6);
  background:#1a1230;
  overflow:hidden;
  box-shadow:0 0 18px rgba(130,96,242,.4);
  display:flex;align-items:center;justify-content:center;
}}
.avatar-wrap img{{width:100%;height:100%;object-fit:cover;}}
.avatar-ph{{font-size:52px;color:rgba(130,96,242,.4);}}
.header-info{{flex:1;}}
.header-id{{font-size:13px;color:rgba(170,160,220,.7);font-weight:500;margin-bottom:4px;}}
.header-name{{font-size:32px;font-weight:800;color:#fff;letter-spacing:-.3px;line-height:1;}}
.header-gameid{{font-size:13px;color:rgba(170,160,220,.6);margin-top:5px;}}
.header-badges{{display:flex;gap:8px;margin-top:8px;}}
.premium-badge{{
  background:linear-gradient(135deg,#c89b00,#e8b900);
  color:#000;font-size:11px;font-weight:700;
  padding:2px 8px;border-radius:4px;
}}
.admin-badge{{
  background:linear-gradient(135deg,#1e82d2,#3ab0f5);
  color:#fff;font-size:11px;font-weight:700;
  padding:2px 8px;border-radius:4px;
}}

/* ── BODY LAYOUT ── */
.body{{display:flex;gap:8px;padding:8px;}}
.col-left{{flex:1;display:flex;flex-direction:column;gap:8px;}}
.col-right{{width:290px;display:flex;flex-direction:column;gap:8px;}}

/* ── PANELS ── */
.panel{{
  background:#18152e;
  border:1px solid rgba(130,96,242,.2);
  border-radius:8px;
  padding:14px;
}}
.panel-title{{
  font-size:13px;font-weight:600;
  color:rgba(200,190,240,.6);
  display:flex;align-items:center;gap:6px;
  margin-bottom:12px;
}}
.panel-title svg{{opacity:.5;}}

/* ── STAT SECTION ── */
.stat-top{{display:flex;gap:10px;margin-bottom:10px;}}
.kd-block{{
  background:#1e1a38;border-radius:8px;
  padding:14px 16px;
  display:flex;align-items:center;gap:14px;
  flex-shrink:0;
}}
.kd-info{{}}
.kd-title{{font-size:12px;color:rgba(200,190,240,.5);margin-bottom:4px;}}
.kd-kills{{font-size:13px;}}
.kd-kills b.k{{color:#1e82d2;}}
.kd-kills b.d{{color:#d73a3a;}}

.level-block{{
  background:#1e1a38;border-radius:8px;
  padding:14px 16px;flex:1;
  position:relative;
}}
.level-title{{font-size:12px;color:rgba(200,190,240,.5);margin-bottom:8px;}}
.level-badge{{
  position:absolute;top:12px;right:12px;
  width:38px;height:38px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  font-size:16px;font-weight:800;
  border:2px solid currentColor;
}}
.elo-line{{display:flex;justify-content:space-between;font-size:12px;color:rgba(200,190,240,.6);margin-bottom:6px;}}
.elo-bar-wrap{{height:6px;background:#2a2450;border-radius:3px;overflow:hidden;}}
.elo-bar-fill{{height:100%;border-radius:3px;background:linear-gradient(90deg,#d73a3a,#e8b900 60%,#3ab76e);}}

.stat-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;}}
.stat-cell{{
  background:#1e1a38;border-radius:8px;
  padding:10px 12px;
}}
.stat-cell-label{{font-size:11px;color:rgba(200,190,240,.45);margin-bottom:2px;}}
.stat-cell-val{{font-size:22px;font-weight:700;color:#fff;line-height:1;}}
.stat-cell-sub{{font-size:10px;margin-top:4px;}}
.stat-cell-bar{{height:3px;border-radius:2px;margin-top:6px;}}

/* ── MAP SECTION ── */
.map-top{{display:flex;gap:10px;margin-bottom:10px;align-items:stretch;}}
.winrate-block{{
  background:#1e1a38;border-radius:8px;
  padding:12px 14px;
  display:flex;align-items:center;gap:12px;
  flex-shrink:0;
}}
.wr-info .wr-title{{font-size:11px;color:rgba(200,190,240,.5);margin-bottom:4px;}}
.wr-wl{{font-size:13px;}}
.wr-wl b.w{{color:#3ab76e;}}
.wr-wl b.l{{color:#d73a3a;}}

.best-map{{
  flex:1;background:#1e1a38;border-radius:8px;
  overflow:hidden;display:flex;flex-direction:column;
}}
.best-map-img{{
  height:70px;
  background-size:cover;background-position:center;
  position:relative;
}}
.best-map-label{{
  position:absolute;right:0;top:0;bottom:0;
  writing-mode:vertical-rl;
  font-size:9px;font-weight:700;letter-spacing:2px;
  color:rgba(255,255,255,.7);
  background:rgba(0,0,0,.5);
  padding:4px 3px;display:flex;align-items:center;
}}
.best-map-info{{padding:8px 10px;}}
.best-map-name{{font-size:13px;font-weight:700;margin-bottom:3px;}}
.best-map-line{{font-size:11px;color:rgba(200,190,240,.6);}}
.green{{color:#3ab76e;}}
.red{{color:#d73a3a;}}

.map-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;}}
.map-mini{{background:#1e1a38;border-radius:8px;overflow:hidden;}}
.map-mini-img{{
  height:52px;background-size:cover;background-position:center;
  position:relative;display:flex;align-items:flex-end;
}}
.map-mini-name{{
  font-size:10px;font-weight:600;
  background:rgba(0,0,0,.65);
  padding:2px 6px;width:100%;
}}
.map-mini-stats{{padding:6px 8px;display:flex;flex-direction:column;gap:1px;}}
.map-mini-stats span{{font-size:10px;color:rgba(200,190,240,.65);}}
.map-mini-stats b{{font-weight:600;}}

/* ── RIGHT COLUMN ── */
.info-grid{{
  display:grid;grid-template-columns:1fr 1fr;gap:8px;
  margin-bottom:0;
}}
.info-cell{{
  background:#1e1a38;border-radius:8px;
  padding:10px 12px;
}}
.info-cell-icon{{font-size:14px;margin-bottom:3px;color:rgba(200,190,240,.5);}}
.info-cell-label{{font-size:10px;color:rgba(200,190,240,.45);margin-bottom:1px;}}
.info-cell-val{{font-size:16px;font-weight:700;}}

.league-panel{{
  background:#1e1a38;border-radius:8px;padding:12px 14px;
  display:flex;align-items:center;justify-content:space-between;
}}
.league-left .league-label{{font-size:10px;color:rgba(200,190,240,.45);margin-bottom:2px;}}
.league-left .league-name{{font-size:18px;font-weight:800;}}
.league-logo{{
  width:44px;height:44px;
  border-radius:50%;
  background:linear-gradient(135deg,#2d2050,#4a3080);
  border:2px solid rgba(130,96,242,.5);
  display:flex;align-items:center;justify-content:center;
  font-size:18px;
}}

.places-panel{{background:#1e1a38;border-radius:8px;padding:10px 12px;}}
.places-title{{font-size:10px;color:rgba(200,190,240,.45);margin-bottom:8px;font-weight:600;letter-spacing:.5px;}}
.place-row{{
  display:flex;align-items:center;gap:8px;
  padding:4px 0;
  border-bottom:1px solid rgba(130,96,242,.08);
}}
.place-row:last-child{{border-bottom:none;}}
.user-rank{{
  margin-top:4px;
  background:rgba(130,96,242,.1);border-radius:4px;
  padding:4px 6px;border-bottom:none;
}}
.place-num{{font-size:11px;font-weight:700;color:rgba(200,190,240,.5);width:26px;}}
.place-avatar{{width:22px;height:22px;border-radius:50%;overflow:hidden;flex-shrink:0;background:#2a2450;}}
.place-avatar img{{width:100%;height:100%;object-fit:cover;}}
.av-ph{{width:100%;height:100%;background:#2a2450;}}
.place-name{{font-size:12px;font-weight:500;}}

.recent-panel{{background:#1e1a38;border-radius:8px;padding:10px 12px;}}
.recent-title{{font-size:10px;color:rgba(200,190,240,.45);margin-bottom:8px;font-weight:600;letter-spacing:.5px;}}
.rec-row{{display:flex;gap:4px;margin-bottom:4px;}}
.rec-cell{{
  width:28px;height:22px;border-radius:4px;
  display:flex;align-items:center;justify-content:center;
  font-size:10px;font-weight:700;color:#fff;
}}

/* ── QUALS ── */
.quals-row{{
  background:#1e1a38;border-radius:8px;
  padding:10px 14px;
  display:flex;align-items:center;gap:20px;
  font-size:12px;
}}
.quals-title{{font-size:12px;font-weight:700;color:#c89b00;}}

/* ── WATERMARK ── */
.watermark{{
  position:absolute;bottom:10px;right:14px;
  display:flex;align-items:center;gap:6px;
  opacity:.45;
}}
.watermark-logo{{
  width:20px;height:20px;
  background:linear-gradient(135deg,#6244e8,#9b72f8);
  border-radius:3px;
  display:flex;align-items:center;justify-content:center;
}}
.watermark-logo svg{{fill:#fff;}}
.watermark-text{{
  font-size:12px;font-weight:800;letter-spacing:.5px;
  color:rgba(200,190,240,.6);
}}
</style>
</head>
<body>
<div class="card">

  <!-- HEADER -->
  <div class="header">
    <div class="header-bg"></div>
    <div class="header-skull">
      <!-- decorative skull shape via css gradient -->
    </div>
    <div class="header-content">
      <div class="avatar-wrap">
        {"<img src='" + avatar_src + "' alt='avatar'/>" if avatar_src else "<div class='avatar-ph'>?</div>"}
      </div>
      <div class="header-info">
        <div class="header-id">#: {user_id}</div>
        <div class="header-name">{username}</div>
        <div class="header-gameid">ID: {game_id}</div>
        <div class="header-badges">{premium_badge}</div>
      </div>
    </div>
  </div>

  <!-- BODY -->
  <div class="body">

    <!-- LEFT COLUMN -->
    <div class="col-left">

      <!-- STATISTIC PANEL -->
      <div class="panel">
        <div class="panel-title">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M18 20V10M12 20V4M6 20v-6"/>
          </svg>
          Statistic
        </div>

        <div class="stat-top">
          <!-- KD donut -->
          <div class="kd-block">
            {kd_donut}
            <div class="kd-info">
              <div class="kd-title">Kill/Deaths</div>
              <div class="kd-kills">K = <b class="k">{kills}</b>  D = <b class="d">{deaths}</b></div>
            </div>
          </div>

          <!-- Level block -->
          <div class="level-block">
            <div class="level-title">Level</div>
            <div class="level-badge" style="color:{lvl_color};border-color:{lvl_color};">{lvl}</div>
            <div class="elo-line">
              <span>{lo}</span><span style="font-weight:700;color:#fff;">{elo}</span><span>{hi}</span>
            </div>
            <div class="elo-bar-wrap">
              <div class="elo-bar-fill" style="width:{elo_pct*100:.1f}%;"></div>
            </div>
          </div>
        </div>

        <!-- Stat grid row 1 -->
        <div class="stat-grid" style="margin-bottom:8px;">
          <div class="stat-cell">
            <div class="stat-cell-label">Rating</div>
            <div class="stat-cell-val">{rating:.2f}</div>
            <div class="stat-cell-bar" style="background:{rating_col};width:{min(rating/2*100,100):.0f}%;"></div>
            <div class="stat-cell-sub" style="color:{rating_col}">{rating_label}</div>
          </div>
          <div class="stat-cell">
            <div class="stat-cell-label">AVG</div>
            <div class="stat-cell-val">{avg_k}</div>
            <div class="stat-cell-bar" style="background:{avg_col};width:{min(avg_k/20*100,100):.0f}%;"></div>
            <div class="stat-cell-sub" style="color:{avg_col}">Stable</div>
          </div>
          <div class="stat-cell">
            <div class="stat-cell-label">Impact</div>
            <div class="stat-cell-val">{impact:.2f}</div>
            <div class="stat-cell-bar" style="background:{imp_col};width:{min(impact/15*100,100):.0f}%;"></div>
            <div class="stat-cell-sub" style="color:{imp_col}">Stable</div>
          </div>
        </div>

        <!-- Stat grid row 2 -->
        <div class="stat-grid">
          <div class="stat-cell">
            <div class="stat-cell-label">KPR</div>
            <div class="stat-cell-val">{kpr:.2f}</div>
            <div class="stat-cell-bar" style="background:{kpr_col};width:{min(kpr/.15*100,100):.0f}%;"></div>
            <div class="stat-cell-sub" style="color:{kpr_col}">Stable</div>
          </div>
          <div class="stat-cell">
            <div class="stat-cell-label">Assists</div>
            <div class="stat-cell-val">{avg_assists}</div>
            <div class="stat-cell-bar" style="background:#c89b00;width:{min(avg_assists/5*100,100):.0f}%;"></div>
            <div class="stat-cell-sub" style="color:#c89b00">Stable</div>
          </div>
          <div class="stat-cell">
            <div class="stat-cell-label">SVR</div>
            <div class="stat-cell-val">{svr:.2f}</div>
            <div class="stat-cell-bar" style="background:{svr_col};width:{min(svr/3*100,100):.0f}%;"></div>
            <div class="stat-cell-sub" style="color:{svr_col}">{'Low' if svr < .8 else 'Stable'}</div>
          </div>
        </div>
      </div>

      <!-- MAP STATISTIC PANEL -->
      <div class="panel">
        <div class="panel-title">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polygon points="1 6 1 22 8 18 16 22 23 18 23 2 16 6 8 2 1 6"/>
            <line x1="8" y1="2" x2="8" y2="18"/>
            <line x1="16" y1="6" x2="16" y2="22"/>
          </svg>
          Map Statistic
        </div>

        <div class="map-top">
          <div class="winrate-block">
            {wr_donut}
            <div class="wr-info">
              <div class="wr-title">Win Rate</div>
              <div class="wr-wl">W = <b class="w">{wins}</b>  L = <b class="l">{losses}</b></div>
            </div>
          </div>
          {best_map_html}
        </div>

        <div class="map-grid">
          {map_cards_html}
        </div>
      </div>

      <!-- QUALS (optional) -->
      {quals_html}

    </div>

    <!-- RIGHT COLUMN -->
    <div class="col-right">

      <!-- INFO GRID -->
      <div class="panel">
        <div class="info-grid">
          <div class="info-cell">
            <div class="info-cell-icon">🕐</div>
            <div class="info-cell-label">Playtime</div>
            <div class="info-cell-val">{playtime_hours}h</div>
          </div>
          <div class="info-cell">
            <div class="info-cell-icon">📅</div>
            <div class="info-cell-label">Join Date</div>
            <div class="info-cell-val" style="font-size:12px;">{join_date or "—"}</div>
          </div>
          <div class="info-cell">
            <div class="info-cell-icon">🎮</div>
            <div class="info-cell-label">Game</div>
            <div class="info-cell-val">{total_games}</div>
          </div>
          <div class="info-cell">
            <div class="info-cell-icon">⭐</div>
            <div class="info-cell-label">MVP</div>
            <div class="info-cell-val">{mvp_count}</div>
          </div>
        </div>
      </div>

      <!-- LEAGUE -->
      <div class="league-panel" style="border:1px solid rgba(130,96,242,.2);">
        <div class="league-left">
          <div class="league-label">League</div>
          <div class="league-name">{league_display}</div>
          <div style="font-size:10px;color:rgba(200,190,240,.4);margin-top:2px;">Places</div>
        </div>
        <div class="league-logo">⚔️</div>
      </div>

      <!-- LEADERBOARD PLACES -->
      <div class="places-panel" style="border:1px solid rgba(130,96,242,.2);">
        <div class="places-title">TOP PLAYERS</div>
        {places_html}
        {user_rank_html}
      </div>

      <!-- RECENT MATCHES -->
      <div class="recent-panel" style="border:1px solid rgba(130,96,242,.2);">
        <div class="recent-title">Recent Matches</div>
        {recent_html}
      </div>

    </div>
  </div>

  <!-- WATERMARK -->
  <div class="watermark">
    <div class="watermark-logo">
      <svg width="12" height="12" viewBox="0 0 24 24">
        <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>
      </svg>
    </div>
    <div class="watermark-text">ACTUAL FACEIT</div>
  </div>

</div>
</body>
</html>"""


# ──────────────────────────────────────────────────────────────────────────────
# Public API — same signature as the Pillow version
# ──────────────────────────────────────────────────────────────────────────────

def _html_to_png(html: str, width: int = 1055) -> bytes:
    """
    Convert HTML string → PNG bytes.
    Pipeline: WeasyPrint (HTML→PDF) → PyMuPDF (PDF→PNG at 2× DPI).
    No system Chrome / wkhtmltoimage needed.
    """
    import fitz  # PyMuPDF

    # WeasyPrint needs a base URL so relative resources resolve correctly
    from weasyprint import HTML as WP_HTML, CSS

    # Override page size to match card width exactly; height = auto
    css_override = CSS(string=f"""
        @page {{
            margin: 0;
            size: {width}px 800px;
        }}
        body {{
            margin: 0;
            padding: 0;
            width: {width}px;
        }}
    """)

    pdf_bytes = WP_HTML(string=html).write_pdf(stylesheets=[css_override])

    # Render first page at 2× resolution for crisp output
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    # 2× zoom → 144 dpi (looks sharp in Telegram)
    mat = fitz.Matrix(2, 2)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    png_bytes = pix.tobytes("png")
    doc.close()
    return png_bytes


def generate_profile_card(
    username:      str,
    game_id:       str,
    user_id:       int,
    elo:           int,
    wins:          int,
    losses:        int,
    kills:         int,
    deaths:        int,
    assists:       int,
    is_premium:    bool        = False,
    is_admin:      bool        = False,
    global_rank:   int         = 0,
    league:        str         = "default",
    map_stats:     list        = None,
    recent:        list        = None,
    leaderboard:   list        = None,
    quals_stats:   dict        = None,
    mvp_count:     int         = 0,
    is_verified:   bool        = False,
    duo_stats:     dict        = None,
    avatar_bytes:  bytes       = None,
    active_frame:  str         = None,
    active_banner: str         = None,
    playtime_hours: int        = 0,
    join_date:     str         = "",
    games:         int         = 0,
) -> io.BytesIO:
    """Render a profile card and return it as a PNG BytesIO object."""
    html = _build_html(
        username=username, game_id=game_id, user_id=user_id,
        elo=elo, wins=wins, losses=losses, kills=kills,
        deaths=deaths, assists=assists, is_premium=is_premium,
        is_admin=is_admin, global_rank=global_rank, league=league,
        map_stats=map_stats or [], recent=recent or [],
        leaderboard=leaderboard or [], quals_stats=quals_stats,
        mvp_count=mvp_count, is_verified=is_verified,
        duo_stats=duo_stats, avatar_bytes=avatar_bytes,
        active_frame=active_frame, active_banner=active_banner,
        playtime_hours=playtime_hours, join_date=join_date, games=games,
    )

    try:
        png_bytes = _html_to_png(html, width=1055)
        buf = io.BytesIO(png_bytes)
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"⚠️ weasyprint/PyMuPDF недоступен, фоллбэк на Pillow: {e}")
        from card_generator import generate_profile_card as _pil_card
        return _pil_card(
            username=username, game_id=game_id, user_id=user_id,
            elo=elo, wins=wins, losses=losses, kills=kills,
            deaths=deaths, assists=assists, is_premium=is_premium,
            is_admin=is_admin, global_rank=global_rank, league=league,
            map_stats=map_stats or [], recent=recent or [],
            leaderboard=leaderboard or [], quals_stats=quals_stats,
            mvp_count=mvp_count, is_verified=is_verified,
            duo_stats=duo_stats, avatar_bytes=avatar_bytes,
            active_frame=active_frame, active_banner=active_banner,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Quick test — run: python card_generator_html.py
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    buf = generate_profile_card(
        username="yakoshi",
        game_id="yakoshi",
        user_id=6565814594,
        elo=894,
        wins=47,
        losses=49,
        kills=1325,
        deaths=1174,
        assists=312,
        is_premium=False,
        is_admin=False,
        global_rank=46,
        league="default",
        map_stats=[
            {"map": "Rust",      "wins": 23, "losses": 15, "kd": 1.17},
            {"map": "Sandstone", "wins": 14, "losses":  9, "kd": 1.37},
            {"map": "Province",  "wins":  7, "losses": 12, "kd": 0.98},
            {"map": "Breeze",    "wins":  3, "losses":  8, "kd": 0.90},
            {"map": "Zone 9",    "wins":  0, "losses":  2, "kd": 1.39},
            {"map": "Sakura",    "wins":  0, "losses":  3, "kd": 0.93},
        ],
        recent=["W","L","L","W","L","L","W",
                "W","W","L","L","L","L","W",
                "L","L","W","W","W","L","L",
                "L","W","L","W","L","L","W",
                "L","W","L","W","W","L"],
        leaderboard=[
            {"username": "sosvart",   "avatar_bytes": None},
            {"username": "Jambo",     "avatar_bytes": None},
            {"username": "44Fauswq",  "avatar_bytes": None},
        ],
        mvp_count=12,
        playtime_hours=45,
        join_date="26.02.2026",
        games=96,
    )
    with open("/tmp/test_card.png", "wb") as f:
        f.write(buf.read())
    print("✅ Saved to /tmp/test_card.png")
