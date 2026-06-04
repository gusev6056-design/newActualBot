import os
import re
import telebot
from telebot import types
from telebot.handler_backends import CancelUpdate
import psycopg2
import psycopg2.extras
from flask import Flask
import threading
import random
import time
import datetime
import json

# ==================== FLASK ====================
app = Flask(__name__)

@app.route("/")
def health():
    return "Bot is running"

def run_flask():
    port = int(os.environ.get("PORT", 8099))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask, daemon=True).start()

# ==================== КОНФИГ ====================
TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID_RAW = os.environ.get("ADMIN_CHAT_ID", "0")
try:
    ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW)
except Exception:
    ADMIN_CHAT_ID = 0

DATABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("DATABASE_URL")

ACCEPT_TIMEOUT = 60
MAPS = ["Zone 9", "Rust", "Province", "Sakura", "Sandstone"]

_raw_ids = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS_LIST: list = [int(x.strip()) for x in _raw_ids.split(",") if x.strip().isdigit()]
ADMIN_ID = ADMIN_IDS_LIST[0] if ADMIN_IDS_LIST else 0

telebot.apihelper.ENABLE_MIDDLEWARE = True
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# ==================== ГЛОБАЛЬНЫЕ СОСТОЯНИЯ ====================

active_lobbies        = {}
running_matches       = {}
user_lobby            = {}
lobby_player_messages = {}
ban_status_messages   = {}
ban_turn_messages     = {}
accept_status_messages= {}
match_found_messages  = {}
user_flow             = {}
awaiting_screenshot   = {}
rename_flow           = {}
parties               = {}
user_party            = {}
admin_action          = {}
match_registration    = {}
awaiting_party_invite = {}
change_flow           = {}
editstat_flow         = {}
promo_flow            = {}
promo_admin_flow      = {}

# ==================== ТОВАРЫ МАГАЗИНА ====================
SHOP_ITEMS_DEFAULT = [
    ("AK-47 | Fuel Injector",  "Легендарный скин на АК-47", "skins", 500,  "skin"),
    ("AK-47 | Bloodsport",     "Агрессивный дизайн",        "skins", 350,  "skin"),
    ("M4A4 | Howl",            "Редкий скин M4A4",           "skins", 800,  "skin"),
    ("M4A4 | Neo-Noir",        "Элегантный скин M4A4",       "skins", 400,  "skin"),
    ("AWP | Dragon Lore",      "Легендарный AWP",            "skins", 1200, "skin"),
    ("AWP | Asiimov",          "Футуристичный AWP",          "skins", 600,  "skin"),
    ("Нож | Butterfly Blue",   "Красивый нож-бабочка",       "skins", 1500, "skin"),
    ("Нож | Karambit Fade",    "Редкий карамбит",            "skins", 2000, "skin"),
    ("Рамка Gold",             "Золотая рамка профиля",      "decor", 300,  "frame"),
    ("Рамка Diamond",          "Алмазная рамка профиля",     "decor", 600,  "frame"),
    ("Рамка Elite",            "Элитная рамка профиля",      "decor", 150,  "frame"),
    ("Стикер 🔥",              "Огненный стикер",            "decor", 50,   "sticker"),
    ("Стикер 💀",              "Стикер черепа",              "decor", 50,   "sticker"),
    ("Стикер ⚡",              "Стикер молнии",              "decor", 50,   "sticker"),
    ("Анимация Победа",        "Анимация при победе",        "decor", 400,  "animation"),
    ("Анимация Убийство",      "Анимация при убийстве",      "decor", 400,  "animation"),
    ("Premium статус",         "30 дней Premium: x1.5 монет, значок 👑", "goods", 1000, "premium"),
    ("x2 монеты",              "Удвоение монет за 7 дней",   "goods", 300,  "x2coins"),
    ("Снятие варна",           "Снять 1 предупреждение",     "goods", 150,  "unwarn"),
    ("Смена ника",             "Изменить ник в боте",        "goods", 10,   "rename"),
    ("Quals доступ",           "Постоянный доступ к QUALS",  "goods", 1500, "quals"),
]

CATEGORY_NAMES = {"skins": "🎨 Скины", "decor": "🖼 Декор", "goods": "📦 Товары"}
CATEGORY_ICONS = {"skins": "🎨", "decor": "🖼", "goods": "📦"}

COIN_PACKAGES = [
    ("Стартовый",   200,   40,   "40 ⭐"),
    ("Оптимальный", 600,   100,  "100 ⭐"),
    ("Выгодный",    2000,  300,  "300 ⭐"),
    ("Мега",        5000,  750,  "750 ⭐"),
    ("Элита",       70000, 1200, "1200 ⭐"),
]


NUMBER_EMOJI = ["①","②","③","④","⑤","⑥","⑦","⑧","⑨","⑩"]

def get_faceit_level(elo: int) -> int:
    if   elo < 801:  return 1
    elif elo < 951:  return 2
    elif elo < 1101: return 3
    elif elo < 1251: return 4
    elif elo < 1401: return 5
    elif elo < 1551: return 6
    elif elo < 1701: return 7
    elif elo < 1851: return 8
    elif elo < 2001: return 9
    else:            return 10

def elo_bar(elo: int, lvl: int) -> str:
    thresholds = [0, 801, 951, 1101, 1251, 1401, 1551, 1701, 1851, 2001, 3000]
    lo = thresholds[max(0, lvl - 1)]
    hi = thresholds[min(lvl, len(thresholds) - 1)]
    pct = (elo - lo) / (hi - lo) if hi > lo else 1.0
    pct = max(0.0, min(1.0, pct))
    filled = round(pct * 12)
    bar = "█" * filled + "░" * (12 - filled)
    return f"[{bar}] {round(pct * 100)}%"


# ==================== ПОДКЛЮЧЕНИЕ К БД ====================
def _db():
    return psycopg2.connect(DATABASE_URL)


def _add_column_if_missing(cur, table, col, definition):
    try:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
    except psycopg2.errors.DuplicateColumn:
        cur.connection.rollback()
    except Exception:
        cur.connection.rollback()


def init_db():
    conn = _db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS players (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            game_id TEXT,
            device TEXT,
            elo INTEGER DEFAULT 1000,
            coins INTEGER DEFAULT 100,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            kills INTEGER DEFAULT 0,
            deaths INTEGER DEFAULT 0,
            assists INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            registered INTEGER DEFAULT 0,
            is_bot INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            warns INTEGER DEFAULT 0,
            quals_access INTEGER DEFAULT 0,
            is_game_reg INTEGER DEFAULT 0,
            is_muted INTEGER DEFAULT 0,
            mute_until BIGINT DEFAULT 0,
            is_on_check INTEGER DEFAULT 0,
            check_admin_id BIGINT DEFAULT 0,
            tg_username TEXT DEFAULT ''
        )
    """)
    conn.commit()

    for col, definition in [
        ("is_banned",      "INTEGER DEFAULT 0"),
        ("warns",          "INTEGER DEFAULT 0"),
        ("quals_access",   "INTEGER DEFAULT 0"),
        ("is_game_reg",    "INTEGER DEFAULT 0"),
        ("is_muted",       "INTEGER DEFAULT 0"),
        ("mute_until",     "BIGINT DEFAULT 0"),
        ("is_on_check",    "INTEGER DEFAULT 0"),
        ("check_admin_id", "BIGINT DEFAULT 0"),
        ("tg_username",    "TEXT DEFAULT ''"),
    ]:
        _add_column_if_missing(cur, "players", col, definition)
    conn.commit()

    for admin_uid in ADMIN_IDS_LIST:
        cur.execute(
            "INSERT INTO players (user_id, username, registered, is_admin) VALUES (%s, 'Admin', 1, 1) ON CONFLICT (user_id) DO NOTHING",
            (admin_uid,),
        )
        cur.execute("UPDATE players SET is_admin=1 WHERE user_id=%s", (admin_uid,))
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM players WHERE is_bot=1")
    if cur.fetchone()[0] == 0:
        for i in range(1, 21):
            bot_id = 1000000000 + i
            cur.execute(
                "INSERT INTO players (user_id, username, game_id, device, registered, is_bot, elo) VALUES (%s, %s, %s, %s, 1, 1, 1000) ON CONFLICT (user_id) DO NOTHING",
                (bot_id, f"Bot_{i}", str(500000000 + i), "PC" if i % 2 == 0 else "MOBILE"),
            )
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS shop_items (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            category TEXT NOT NULL,
            price INTEGER NOT NULL,
            item_type TEXT NOT NULL,
            is_active INTEGER DEFAULT 1
        )
    """)
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM shop_items")
    if cur.fetchone()[0] == 0:
        for row in SHOP_ITEMS_DEFAULT:
            cur.execute(
                "INSERT INTO shop_items (name, description, category, price, item_type) VALUES (%s, %s, %s, %s, %s)",
                row,
            )
    else:
        price_updates = [
            (1000, "premium"),
            (300,  "x2coins"),
            (150,  "unwarn"),
            (10,   "rename"),
            (1500, "quals"),
        ]
        for price, item_type in price_updates:
            cur.execute("UPDATE shop_items SET price=%s WHERE item_type=%s", (price, item_type))
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            item_id INTEGER NOT NULL,
            purchased_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
            is_activated INTEGER DEFAULT 0,
            activated_at BIGINT DEFAULT NULL,
            FOREIGN KEY (user_id) REFERENCES players(user_id),
            FOREIGN KEY (item_id) REFERENCES shop_items(id)
        )
    """)
    conn.commit()

    for col, definition in [
        ("is_activated", "INTEGER DEFAULT 0"),
        ("activated_at", "BIGINT DEFAULT NULL"),
    ]:
        _add_column_if_missing(cur, "inventory", col, definition)
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS match_counter (
            id INTEGER PRIMARY KEY,
            value INTEGER DEFAULT 0
        )
    """)
    cur.execute("INSERT INTO match_counter (id, value) VALUES (1, 0) ON CONFLICT (id) DO NOTHING")
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id SERIAL PRIMARY KEY,
            match_id INTEGER NOT NULL,
            league TEXT,
            device TEXT,
            map_name TEXT,
            winner TEXT,
            score_w INTEGER,
            score_l INTEGER,
            finished_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT,
            players_json TEXT
        )
    """)
    conn.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS promo_codes (
            id SERIAL PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            reward_type TEXT NOT NULL,
            reward_value INTEGER DEFAULT 0,
            max_uses INTEGER DEFAULT 1,
            uses INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at BIGINT DEFAULT EXTRACT(EPOCH FROM NOW())::BIGINT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS promo_uses (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            code TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    print("✅ БД инициализирована (PostgreSQL / Supabase).")


# ==================== БД ХЕЛПЕРЫ ====================
def get_player(user_id):
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM players WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def is_registered(uid):
    p = get_player(uid)
    return p is not None and p[12] == 1

def is_admin(uid):
    p = get_player(uid)
    return p is not None and p[11] == 1

def is_game_reg_check(uid):
    p = get_player(uid)
    return p is not None and (p[11] == 1 or (len(p) > 17 and p[17] == 1))

def is_bot_player(uid):
    p = get_player(uid)
    return p is not None and p[13] == 1

def is_banned_check(uid):
    p = get_player(uid)
    return p is not None and len(p) > 14 and p[14] == 1

def is_muted_check(uid):
    p = get_player(uid)
    if p is None:
        return False
    if len(p) > 19 and p[18] == 1:
        mute_until = p[19] or 0
        if mute_until > time.time():
            return True
        conn = _db()
        cur = conn.cursor()
        cur.execute("UPDATE players SET is_muted=0, mute_until=0 WHERE user_id=%s", (uid,))
        conn.commit()
        conn.close()
    return False

def get_mute_remaining(uid):
    p = get_player(uid)
    if p is None or len(p) <= 19:
        return 0
    return max(0, int((p[19] or 0) - time.time()))

def is_on_check_db(uid):
    p = get_player(uid)
    return p is not None and len(p) > 20 and p[20] == 1

def get_check_admin(uid):
    p = get_player(uid)
    if p is None or len(p) <= 21:
        return None
    return p[21]

def has_quals_access(uid):
    if is_admin(uid):
        return True
    p = get_player(uid)
    return p is not None and len(p) > 16 and p[16] == 1

def has_active_premium(uid):
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM inventory i
        JOIN shop_items s ON i.item_id=s.id
        WHERE i.user_id=%s AND s.item_type='premium' AND i.is_activated=1
    """, (uid,))
    count = cur.fetchone()[0]
    conn.close()
    return count > 0

def register_user(uid, username, game_id, device, tg_username=""):
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO players (user_id, username, game_id, device, registered, coins, elo, tg_username)
           VALUES (%s, %s, %s, %s, 1, 100, 1000, %s)
           ON CONFLICT (user_id) DO UPDATE SET
               username=EXCLUDED.username,
               game_id=EXCLUDED.game_id,
               device=EXCLUDED.device,
               registered=1,
               tg_username=EXCLUDED.tg_username""",
        (uid, username, game_id, device, tg_username),
    )
    conn.commit()
    conn.close()

def update_tg_username(uid, tg_username):
    conn = _db()
    cur = conn.cursor()
    cur.execute("UPDATE players SET tg_username=%s WHERE user_id=%s", (tg_username or "", uid))
    conn.commit()
    conn.close()

def nick_taken(nick, exclude_uid=None):
    conn = _db()
    cur = conn.cursor()
    if exclude_uid:
        cur.execute("SELECT COUNT(*) FROM players WHERE username=%s AND user_id!=%s AND is_bot=0", (nick, exclude_uid))
    else:
        cur.execute("SELECT COUNT(*) FROM players WHERE username=%s AND is_bot=0", (nick,))
    count = cur.fetchone()[0]
    conn.close()
    return count > 0

def game_id_taken(game_id, exclude_uid=None):
    conn = _db()
    cur = conn.cursor()
    if exclude_uid:
        cur.execute("SELECT COUNT(*) FROM players WHERE game_id=%s AND user_id!=%s AND is_bot=0", (game_id, exclude_uid))
    else:
        cur.execute("SELECT COUNT(*) FROM players WHERE game_id=%s AND is_bot=0", (game_id,))
    count = cur.fetchone()[0]
    conn.close()
    return count > 0

def get_bots():
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, username FROM players WHERE is_bot=1")
    bots = cur.fetchall()
    conn.close()
    return bots

def get_all_players():
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
        SELECT user_id, username, elo, wins, losses, kills, deaths, coins, is_banned, warns
        FROM players WHERE is_bot=0 AND registered=1 ORDER BY elo DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return rows

def get_player_by_game_id(game_id):
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM players WHERE game_id=%s AND is_bot=0", (game_id,))
    row = cur.fetchone()
    conn.close()
    return row

def add_coins_to_player(uid, amount):
    conn = _db()
    cur = conn.cursor()
    cur.execute("UPDATE players SET coins=coins+%s WHERE user_id=%s", (amount, uid))
    conn.commit()
    conn.close()

def apply_mute(uid, hours=2):
    until = int(time.time()) + hours * 3600
    conn = _db()
    cur = conn.cursor()
    cur.execute("UPDATE players SET is_muted=1, mute_until=%s WHERE user_id=%s", (until, uid))
    conn.commit()
    conn.close()
    return until

def add_warn_to_player(uid):
    conn = _db()
    cur = conn.cursor()
    cur.execute("UPDATE players SET warns=warns+1 WHERE user_id=%s", (uid,))
    cur.execute("SELECT warns FROM players WHERE user_id=%s", (uid,))
    row = cur.fetchone()
    conn.commit()
    conn.close()
    return row[0] if row else 1

def get_next_match_id():
    conn = _db()
    cur = conn.cursor()
    cur.execute("UPDATE match_counter SET value=value+1 WHERE id=1 RETURNING value")
    val = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return val

def save_match_to_history(lobby, data, all_stats):
    players_info = []
    for uid, s in all_stats.items():
        p = get_player(uid)
        players_info.append({
            "user_id": uid,
            "name": p[1] if p else str(uid),
            "kills": s["kills"],
            "deaths": s["deaths"],
            "assists": s["assists"],
            "won": s["won"],
        })
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO matches (match_id, league, device, map_name, winner, score_w, score_l, players_json)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            lobby.get("match_id", 0),
            lobby.get("league", ""),
            lobby.get("device", ""),
            lobby.get("map_name", ""),
            data.get("winner", ""),
            data.get("score_w", 0),
            data.get("score_l", 0),
            json.dumps(players_info, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()

def get_match_history(limit=10):
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "SELECT match_id, league, device, map_name, winner, score_w, score_l, finished_at FROM matches ORDER BY finished_at DESC LIMIT %s",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ==================== ПРОМОКОДЫ ====================
def create_promo_code(code, reward_type, reward_value, max_uses):
    conn = _db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO promo_codes (code, reward_type, reward_value, max_uses) VALUES (%s, %s, %s, %s)",
            (code.upper(), reward_type, reward_value, max_uses),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        return False
    finally:
        conn.close()

def use_promo_code(uid, code):
    conn = _db()
    cur = conn.cursor()
    code_upper = code.upper()
    cur.execute(
        "SELECT id, reward_type, reward_value, max_uses, uses, is_active FROM promo_codes WHERE code=%s",
        (code_upper,),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return False, "❌ Промокод не найден"
    pid, reward_type, reward_value, max_uses, uses, is_active = row
    if not is_active:
        conn.close()
        return False, "❌ Промокод недействителен"
    if max_uses > 0 and uses >= max_uses:
        conn.close()
        return False, "❌ Промокод исчерпан"
    cur.execute("SELECT COUNT(*) FROM promo_uses WHERE user_id=%s AND code=%s", (uid, code_upper))
    if cur.fetchone()[0] > 0:
        conn.close()
        return False, "❌ Вы уже использовали этот промокод"
    if reward_type == "coins":
        cur.execute("UPDATE players SET coins=coins+%s WHERE user_id=%s", (reward_value, uid))
        msg = f"💰 Начислено <b>{reward_value} AC</b>!"
    elif reward_type == "premium":
        cur.execute("SELECT id FROM shop_items WHERE item_type='premium' LIMIT 1")
        item = cur.fetchone()
        if item:
            cur.execute(
                "INSERT INTO inventory (user_id, item_id, is_activated) VALUES (%s, %s, 1)",
                (uid, item[0]),
            )
        msg = "👑 <b>Premium</b> активирован!"
    elif reward_type == "quals":
        cur.execute("UPDATE players SET quals_access=1 WHERE user_id=%s", (uid,))
        msg = "⭐ Доступ к <b>QUALS</b> открыт!"
    else:
        conn.close()
        return False, "❌ Неизвестный тип награды"
    cur.execute("INSERT INTO promo_uses (user_id, code) VALUES (%s, %s)", (uid, code_upper))
    cur.execute("UPDATE promo_codes SET uses=uses+1 WHERE id=%s", (pid,))
    conn.commit()
    conn.close()
    return True, msg

def get_all_promo_codes():
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "SELECT code, reward_type, reward_value, max_uses, uses, is_active FROM promo_codes ORDER BY id DESC"
    )
    rows = cur.fetchall()
    conn.close()
    return rows

def deactivate_promo_code(code):
    conn = _db()
    cur = conn.cursor()
    cur.execute("UPDATE promo_codes SET is_active=0 WHERE code=%s", (code.upper(),))
    conn.commit()
    conn.close()


# ==================== МАГАЗИН ХЕЛПЕРЫ ====================
def get_shop_item(item_id):
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, description, category, price, item_type FROM shop_items WHERE id=%s",
        (item_id,),
    )
    item = cur.fetchone()
    conn.close()
    return item

def get_shop_items_by_category(category):
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, description, price, item_type FROM shop_items WHERE category=%s AND is_active=1",
        (category,),
    )
    items = cur.fetchall()
    conn.close()
    return items

def has_item_in_inventory(uid, item_id):
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM inventory WHERE user_id=%s AND item_id=%s", (uid, item_id))
    count = cur.fetchone()[0]
    conn.close()
    return count > 0

def buy_item(uid, item_id):
    item = get_shop_item(item_id)
    if not item:
        return False, "❌ Товар не найден"
    price = item[4]
    p = get_player(uid)
    if not p:
        return False, "❌ Игрок не найден"
    if p[5] < price:
        return False, f"❌ Недостаточно ActualCoin!\nНужно: {price} AC\nУ вас: {p[5]} AC"
    stackable = {"sticker", "unwarn", "x2coins", "rename"}
    if item[5] not in stackable and has_item_in_inventory(uid, item_id):
        return False, "❌ Этот предмет уже есть в вашем инвентаре!"
    conn = _db()
    cur = conn.cursor()
    cur.execute("UPDATE players SET coins=coins-%s WHERE user_id=%s", (price, uid))
    cur.execute("INSERT INTO inventory (user_id, item_id) VALUES (%s, %s)", (uid, item_id))
    conn.commit()
    conn.close()
    return True, f"✅ Куплено: <b>{item[1]}</b>\nСписано: {price} AC\n\n💡 Активируйте предмет в 🎒 Инвентаре"

def get_inventory(uid):
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """SELECT i.id, s.name, s.category, s.item_type, i.purchased_at, i.is_activated, s.id
           FROM inventory i JOIN shop_items s ON i.item_id=s.id
           WHERE i.user_id=%s ORDER BY i.purchased_at DESC""",
        (uid,),
    )
    items = cur.fetchall()
    conn.close()
    return items

def activate_inventory_item(inv_id, uid, item_type, item_name):
    conn = _db()
    cur = conn.cursor()
    if item_type == "unwarn":
        cur.execute("SELECT warns FROM players WHERE user_id=%s", (uid,))
        row = cur.fetchone()
        if row and row[0] > 0:
            cur.execute("UPDATE players SET warns=warns-1 WHERE user_id=%s", (uid,))
        else:
            conn.close()
            return False, "❌ У вас нет варнов для снятия"
    elif item_type == "rename":
        conn.close()
        return "rename", "✏️ Введите новый никнейм (2-20 символов):"
    elif item_type == "quals":
        cur.execute("UPDATE players SET quals_access=1 WHERE user_id=%s", (uid,))
    cur.execute(
        "UPDATE inventory SET is_activated=1, activated_at=%s WHERE id=%s",
        (int(time.time()), inv_id),
    )
    conn.commit()
    conn.close()
    return True, f"✅ Предмет <b>{item_name}</b> активирован!"


# ==================== ПАТИ ====================
def get_party_of(uid):
    pid = user_party.get(uid)
    return parties.get(pid) if pid else None

def get_party_max_size(party):
    for m in party["members"]:
        if has_active_premium(m):
            return 3
    return 2


# ==================== КАПИТАН (лидер пати 60%, не лидер пати — не может, premium +7%) ====================
def pick_captain(team):
    if not team:
        return None
    party_leaders_in_team = []
    party_members_non_leader = set()
    for u in team:
        party = get_party_of(u)
        if party and len(party["members"]) > 1:
            team_members_in_party = [m for m in party["members"] if m in team]
            if len(team_members_in_party) > 1:
                if u == party["leader"]:
                    party_leaders_in_team.append(u)
                else:
                    party_members_non_leader.add(u)
    eligible = [u for u in team if u not in party_members_non_leader]
    if not eligible:
        eligible = team
    if party_leaders_in_team:
        leader = party_leaders_in_team[0]
        if random.random() < 0.60:
            return leader
        rest = [u for u in eligible if u != leader]
        if not rest:
            return leader
        weights = [1.07 if has_active_premium(u) else 1.0 for u in rest]
        return random.choices(rest, weights=weights, k=1)[0]
    weights = [1.07 if has_active_premium(u) else 1.0 for u in eligible]
    return random.choices(eligible, weights=weights, k=1)[0]



# ==================== ПРОВЕРКА БЛОКИРОВОК ====================
def check_blocked(uid):
    if is_banned_check(uid):
        return "🚫 Вы заблокированы в боте."
    if is_on_check_db(uid):
        admin_uid = get_check_admin(uid)
        admin_name = "администратора"
        if admin_uid:
            ap = get_player(admin_uid)
            if ap:
                tg_u = ap[22] if len(ap) > 22 else ""
                admin_name = f"@{tg_u}" if tg_u else f"администратора (id:{admin_uid})"
        return (
            f"⚠️ <b>Вас вызвал на проверку {admin_name}</b>\n\n"
            "Доступ к боту ограничен до прохождения проверки.\nОбратитесь к администратору."
        )
    return None


# ==================== ГЛАВНОЕ МЕНЮ ====================
def main_menu(uid):
    kb = types.InlineKeyboardMarkup(row_width=2)
    if uid in user_lobby:
        kb.add(
            types.InlineKeyboardButton("👤 Профиль", callback_data="profile"),
            types.InlineKeyboardButton("🔄 Вернуться в лобби", callback_data="rejoin_lobby"),
        )
    else:
        kb.add(
            types.InlineKeyboardButton("👤 Профиль", callback_data="profile"),
            types.InlineKeyboardButton("🎮 Найти матч", callback_data="find"),
        )
    kb.add(
        types.InlineKeyboardButton("🏆 Топ", callback_data="top"),
        types.InlineKeyboardButton("🛒 Магазин", callback_data="shop"),
        types.InlineKeyboardButton("🎒 Инвентарь", callback_data="inv"),
        types.InlineKeyboardButton("💳 Купить монеты", callback_data="buy_coins"),
        types.InlineKeyboardButton("🎁 Промокод", callback_data="promo"),
    )
    in_party = uid in user_party
    kb.add(types.InlineKeyboardButton(
        "👥 Моя пати" if in_party else "➕ Создать пати", callback_data="party_menu"
    ))
    if is_admin(uid):
        kb.add(
            types.InlineKeyboardButton("🤖 Добавить ботов", callback_data="add_bots_admin"),
            types.InlineKeyboardButton("⚙️ Админ панель", callback_data="admin_panel"),
        )
    elif is_game_reg_check(uid):
        kb.add(types.InlineKeyboardButton("📋 Регистрация матчей", callback_data="game_reg_panel"))
    return kb


@bot.message_handler(commands=["start"])
def cmd_start(msg):
    uid = msg.from_user.id
    if msg.from_user.username:
        update_tg_username(uid, msg.from_user.username)
    err = check_blocked(uid)
    if err:
        bot.send_message(uid, err)
        return
    if is_registered(uid):
        bot.send_message(uid, "⚡ ACTUAL FACEIT", reply_markup=main_menu(uid))
        return
    user_flow[uid] = {"state": "nick"}
    bot.send_message(uid, "👋 Добро пожаловать!\n\n<b>Шаг 1:</b> Введи свой никнейм (2-20 символов):")


@bot.callback_query_handler(func=lambda c: c.data == "rejoin_lobby")
def cb_rejoin_lobby(c):
    uid = c.from_user.id
    lobby_id = user_lobby.get(uid)
    if not lobby_id:
        bot.answer_callback_query(c.id, "❌ Вы не в лобби")
        bot.edit_message_text("⚡ ACTUAL FACEIT", c.message.chat.id, c.message.message_id, reply_markup=main_menu(uid))
        return
    lobby = active_lobbies.get(lobby_id)
    if not lobby or lobby.get("status") != "waiting":
        bot.answer_callback_query(c.id, "❌ Лобби недоступно")
        bot.edit_message_text("⚡ ACTUAL FACEIT", c.message.chat.id, c.message.message_id, reply_markup=main_menu(uid))
        return
    text = build_lobby_text(lobby_id)
    kb = build_lobby_kb(lobby_id, uid)
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    if lobby_player_messages.get(lobby_id) is None:
        lobby_player_messages[lobby_id] = {}
    lobby_player_messages[lobby_id][uid] = (c.message.chat.id, c.message.message_id)
    bot.answer_callback_query(c.id)


# ==================== ПРОМОКОД (пользователь) ====================
@bot.callback_query_handler(func=lambda c: c.data == "promo")
def cb_promo(c):
    uid = c.from_user.id
    err = check_blocked(uid)
    if err:
        bot.answer_callback_query(c.id, "⚠️ Доступ ограничен", show_alert=True)
        return
    if not is_registered(uid):
        bot.answer_callback_query(c.id, "❌ Сначала зарегистрируйтесь /start")
        return
    promo_flow[uid] = True
    bot.answer_callback_query(c.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data="promo_cancel"))
    bot.send_message(uid, "🎁 Введите промокод:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "promo_cancel")
def cb_promo_cancel(c):
    uid = c.from_user.id
    promo_flow.pop(uid, None)
    bot.answer_callback_query(c.id)
    try:
        bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception:
        pass

@bot.message_handler(func=lambda m: m.from_user.id in promo_flow and m.text is not None)
def handle_promo_input(msg):
    uid = msg.from_user.id
    promo_flow.pop(uid, None)
    code = msg.text.strip()
    ok, result_msg = use_promo_code(uid, code)
    p = get_player(uid)
    balance = f"\n💰 Баланс: <b>{p[5]} AC</b>" if p and ok else ""
    bot.send_message(uid, f"{result_msg}{balance}")


# ==================== РЕГИСТРАЦИЯ ====================
@bot.message_handler(func=lambda m: user_flow.get(m.from_user.id, {}).get("state") == "nick")
def reg_nick(msg):
    uid = msg.from_user.id
    nick = msg.text.strip()
    if not (2 <= len(nick) <= 20):
        bot.send_message(uid, "❌ Никнейм 2-20 символов")
        return
    if nick_taken(nick):
        bot.send_message(uid, "❌ <b>Этот никнейм уже занят!</b>\n\nЕсли это ваш никнейм — обратитесь к администратору.\nВведите другой никнейм:")
        return
    user_flow[uid] = {"state": "id", "nick": nick}
    bot.send_message(uid, "<b>Шаг 2:</b> Введи игровой ID\n\nМожно: русские и английские буквы, цифры, <code>_</code> и <code>-</code>")

@bot.message_handler(func=lambda m: user_flow.get(m.from_user.id, {}).get("state") == "id")
def reg_id(msg):
    uid = msg.from_user.id
    game_id = msg.text.strip()
    if not re.match(r'^[a-zA-ZА-Яа-яёЁ0-9_-]+$', game_id):
        bot.send_message(uid, "❌ Недопустимые символы! Только буквы, цифры, <code>_</code>, <code>-</code>")
        return
    if game_id_taken(game_id):
        bot.send_message(uid, "❌ <b>Этот Game ID уже занят!</b>\n\nВведите другой Game ID:")
        return
    user_flow[uid]["game_id"] = game_id
    user_flow[uid]["state"] = "device"
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row("MOBILE", "PC")
    bot.send_message(uid, "<b>Шаг 3:</b> Выбери устройство:", reply_markup=kb)

@bot.message_handler(func=lambda m: user_flow.get(m.from_user.id, {}).get("state") == "device")
def reg_device(msg):
    uid = msg.from_user.id
    device = msg.text.strip()
    if device not in ("MOBILE", "PC"):
        bot.send_message(uid, "❌ Выбери MOBILE или PC")
        return
    data = user_flow.pop(uid)
    tg_u = msg.from_user.username or ""
    register_user(uid, data["nick"], data["game_id"], device, tg_u)
    bot.send_message(
        uid,
        f"✅ Регистрация завершена!\n\nНик: <b>{data['nick']}</b>\nGame ID: <code>{data['game_id']}</code>\nDevice: {device}",
        reply_markup=types.ReplyKeyboardRemove(),
    )
    bot.send_message(uid, "⚡ ACTUAL FACEIT", reply_markup=main_menu(uid))


# ==================== СМЕНА ДАННЫХ ====================
@bot.callback_query_handler(func=lambda c: c.data in ("change_nick", "change_game_id"))
def cb_change_own(c):
    uid = c.from_user.id
    field = "nick" if c.data == "change_nick" else "game_id"
    change_flow[uid] = {"field": field}
    bot.answer_callback_query(c.id)
    if field == "nick":
        bot.send_message(uid, "✏️ Введите новый никнейм (2-20 символов):")
    else:
        bot.send_message(uid, "🎮 Введите новый Game ID:\n\nТолько буквы, цифры, <code>_</code> и <code>-</code>")

@bot.message_handler(func=lambda m: m.from_user.id in change_flow and "field" in change_flow.get(m.from_user.id, {}) and m.text is not None)
def handle_change_flow(msg):
    uid = msg.from_user.id
    if uid not in change_flow:
        return
    data = change_flow.pop(uid)
    field = data["field"]
    text = msg.text.strip()
    conn = _db()
    cur = conn.cursor()
    if field == "nick":
        if not (2 <= len(text) <= 20):
            bot.send_message(uid, "❌ Никнейм 2-20 символов.")
            conn.close()
            return
        if nick_taken(text, exclude_uid=uid):
            bot.send_message(uid, "❌ <b>Этот никнейм уже занят!</b>")
            conn.close()
            return
        cur.execute("UPDATE players SET username=%s WHERE user_id=%s", (text, uid))
        conn.commit()
        conn.close()
        bot.send_message(uid, f"✅ Никнейм изменён на <b>{text}</b>!")
    elif field == "game_id":
        if not re.match(r'^[a-zA-ZА-Яа-яёЁ0-9_-]+$', text):
            bot.send_message(uid, "❌ Только буквы, цифры, <code>_</code> и <code>-</code>")
            conn.close()
            return
        if game_id_taken(text, exclude_uid=uid):
            bot.send_message(uid, "❌ <b>Этот Game ID уже занят!</b>")
            conn.close()
            return
        cur.execute("UPDATE players SET game_id=%s WHERE user_id=%s", (text, uid))
        conn.commit()
        conn.close()
        bot.send_message(uid, f"✅ Game ID изменён на <code>{text}</code>!")
    elif field == "admin_nick":
        target_id = data.get("target_id")
        conn.close()
        if not target_id:
            return
        if not (2 <= len(text) <= 20):
            bot.send_message(uid, "❌ Никнейм 2-20 символов.")
            return
        if nick_taken(text, exclude_uid=target_id):
            bot.send_message(uid, f"❌ Никнейм <b>{text}</b> уже занят!")
            return
        c2 = _db(); c2cur = c2.cursor()
        c2cur.execute("UPDATE players SET username=%s WHERE user_id=%s", (text, target_id))
        c2.commit(); c2.close()
        bot.send_message(uid, f"✅ Никнейм игрока изменён на <b>{text}</b>!")
        try:
            bot.send_message(target_id, f"✏️ Администратор изменил ваш никнейм на <b>{text}</b>!")
        except Exception:
            pass
    elif field == "admin_id":
        target_id = data.get("target_id")
        conn.close()
        if not target_id:
            return
        if not re.match(r'^[a-zA-ZА-Яа-яёЁ0-9_-]+$', text):
            bot.send_message(uid, "❌ Только буквы, цифры, <code>_</code> и <code>-</code>")
            return
        if game_id_taken(text, exclude_uid=target_id):
            bot.send_message(uid, f"❌ Game ID <code>{text}</code> уже занят!")
            return
        c2 = _db(); c2cur = c2.cursor()
        c2cur.execute("UPDATE players SET game_id=%s WHERE user_id=%s", (text, target_id))
        c2.commit(); c2.close()
        bot.send_message(uid, f"✅ Game ID игрока изменён на <code>{text}</code>!")
        try:
            bot.send_message(target_id, f"🎮 Администратор изменил ваш Game ID на <code>{text}</code>!")
        except Exception:
            pass
    else:
        conn.close()


# ==================== ПРОФИЛЬ ====================
@bot.callback_query_handler(func=lambda c: c.data == "profile")
def cb_profile(c):
    uid = c.from_user.id
    p = get_player(uid)
    if not p:
        bot.edit_message_text("❌ Ошибка", c.message.chat.id, c.message.message_id)
        bot.answer_callback_query(c.id)
        return
    games = p[6] + p[7]
    winrate = round(p[6] / games * 100, 1) if games > 0 else 0
    kd = round(p[8] / p[9], 2) if p[9] > 0 else p[8]
    warns = p[15] if len(p) > 15 else 0
    quals = "✅" if (len(p) > 16 and p[16] == 1) else "❌"
    premium = has_active_premium(uid)
    crown = " 👑 Premium" if premium else ""
    lvl = get_faceit_level(p[4])
    bar = elo_bar(p[4], lvl)
    muted = is_muted_check(uid)
    mute_text = ""
    if muted:
        mins = get_mute_remaining(uid) // 60
        mute_text = f"\n🔇 Мут: {mins} мин."
    text = (
        f"👤 <b>{p[1]}</b>{crown}\n"
        f"🆔 Telegram ID: <code>{p[0]}</code>\n"
        f"🎮 Game ID: <code>{p[2]}</code>\n"
        f"📱 Device: {p[3]}\n"
        f"📊 ELO: {p[4]} · Lvl {lvl}\n"
        f"{bar}\n"
        f"💰 Баланс: {p[5]} AC\n"
        f"⭐ Quals: {quals}\n"
        f"⚠️ Варны: {warns}/3{mute_text}\n\n"
        f"🏆 {p[6]}W · ❌ {p[7]}L · 📈 {winrate}%\n"
        f"🔫 K: {p[8]} · 💀 D: {p[9]} · 🤝 A: {p[10]} · K/D: {kd}"
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✏️ Изменить ник", callback_data="change_nick"),
        types.InlineKeyboardButton("🎮 Изменить Game ID", callback_data="change_game_id"),
        types.InlineKeyboardButton("🔙 Назад", callback_data="back"),
    )
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


# ==================== ТОП ====================
@bot.callback_query_handler(func=lambda c: c.data == "top")
def cb_top(c):
    players = get_all_players()
    text = "🏆 <b>ТОП ИГРОКОВ ПО ELO</b>\n\n" if players else "🏆 <b>ТОП</b>\n\nИгроков нет."
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, p in enumerate(players[:10], 1):
        uid2, name, elo, wins, losses, kills, deaths, coins, banned, warns = p
        games = wins + losses
        winrate = round(wins / games * 100, 1) if games > 0 else 0
        kd = round(kills / deaths, 2) if deaths > 0 else kills
        lvl = get_faceit_level(elo)
        prem = " 👑" if has_active_premium(uid2) else ""
        text += f"{medals.get(i, f'{i}.')} <b>{name}</b>{prem} [Lvl {lvl}]\n   ELO: {elo} | {wins}W/{losses}L ({winrate}%) | K/D: {kd}\n\n"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


# ==================== НАЗАД ====================
@bot.callback_query_handler(func=lambda c: c.data == "back")
def cb_back(c):
    uid = c.from_user.id
    bot.edit_message_text("⚡ ACTUAL FACEIT", c.message.chat.id, c.message.message_id, reply_markup=main_menu(uid))
    bot.answer_callback_query(c.id)


# ==================== ЛОББИ ====================
def build_lobby_text(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return ""
    parts = lobby_id.split("_")
    league, device, slot = parts[0], parts[1], parts[2]
    text = (
        f"🎮 <b>Лобби #{slot} ({league.upper()}/{device.upper()})</b>\n"
        f"👥 Игроков: {len(lobby['players'])}/10\n\n"
    )
    for i, pid in enumerate(lobby["players"], 1):
        p = get_player(pid)
        if p:
            icon = "🤖" if p[13] else "👤"
            prem = " 👑" if (not p[13] and has_active_premium(pid)) else ""
            text += f"{i}. {icon} {p[1]}{prem} [Lvl {get_faceit_level(p[4])} | {p[4]} ELO]\n"
        else:
            text += f"{i}. {pid}\n"
    return text

def build_lobby_kb(lobby_id, uid):
    parts = lobby_id.split("_")
    league = parts[0]
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🚪 Выйти из лобби", callback_data=f"leave_{lobby_id}"))
    kb.add(types.InlineKeyboardButton("🔙 К списку", callback_data=f"lobby_{league}"))
    return kb

def broadcast_lobby_update(lobby_id, exclude_uid=None):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return
    text = build_lobby_text(lobby_id)
    for pid, (cid, mid) in list(lobby_player_messages.get(lobby_id, {}).items()):
        if pid == exclude_uid or pid not in lobby.get("players", []):
            continue
        try:
            bot.edit_message_text(text, cid, mid, reply_markup=build_lobby_kb(lobby_id, pid))
        except Exception:
            pass


@bot.callback_query_handler(func=lambda c: c.data == "find")
def cb_find(c):
    uid = c.from_user.id
    err = check_blocked(uid)
    if err:
        bot.answer_callback_query(c.id, "⚠️ Доступ ограничен", show_alert=True)
        return
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎮 Default", callback_data="lobby_default"),
        types.InlineKeyboardButton("⭐ Quals", callback_data="lobby_quals"),
        types.InlineKeyboardButton("🔙 Назад", callback_data="back"),
    )
    bot.edit_message_text("🎮 Выбери лигу:", c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("lobby_") and len(c.data.split("_")) == 2)
def cb_lobby(c):
    uid = c.from_user.id
    league = c.data.split("_")[1]
    if league == "quals" and not has_quals_access(uid):
        bot.answer_callback_query(c.id, "⭐ Доступ к QUALS закрыт!", show_alert=True)
        return
    text = f"🎮 <b>ЛОББИ {league.upper()}</b>\n\nPC и Mobile могут играть вместе\n\n"
    kb = types.InlineKeyboardMarkup(row_width=2)
    for slot in range(1, 6):
        m_cnt = len(active_lobbies.get(f"{league}_mobile_{slot}", {}).get("players", []))
        p_cnt = len(active_lobbies.get(f"{league}_pc_{slot}", {}).get("players", []))
        text += f"Лобби #{slot}: Mobile({m_cnt}/10) | PC({p_cnt}/10)\n"
    for slot in range(1, 6):
        m_cnt = len(active_lobbies.get(f"{league}_mobile_{slot}", {}).get("players", []))
        p_cnt = len(active_lobbies.get(f"{league}_pc_{slot}", {}).get("players", []))
        kb.add(
            types.InlineKeyboardButton(f"M{slot}({m_cnt})", callback_data=f"join_{league}_mobile_{slot}"),
            types.InlineKeyboardButton(f"P{slot}({p_cnt})", callback_data=f"join_{league}_pc_{slot}"),
        )
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="find"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("join_"))
def cb_join(c):
    try:
        parts = c.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(c.id, "❌ Ошибка формата")
            return
        league, device, slot = parts[1], parts[2], int(parts[3])
        uid = c.from_user.id
        if c.from_user.username:
            update_tg_username(uid, c.from_user.username)
        err = check_blocked(uid)
        if err:
            bot.answer_callback_query(c.id, "⚠️ Доступ ограничен", show_alert=True)
            return
        if league == "quals" and not has_quals_access(uid):
            bot.answer_callback_query(c.id, "⭐ Доступ к QUALS закрыт!", show_alert=True)
            return
        if not is_registered(uid):
            bot.answer_callback_query(c.id, "❌ Вы не зарегистрированы! Напишите /start")
            return
        if is_muted_check(uid):
            mins = get_mute_remaining(uid) // 60
            bot.answer_callback_query(c.id, f"🔇 Вы замучены! Осталось: {mins} мин.", show_alert=True)
            return
        lobby_id = f"{league}_{device}_{slot}"
        old = user_lobby.get(uid)
        if old and old in active_lobbies and uid in active_lobbies[old].get("players", []):
            active_lobbies[old]["players"].remove(uid)
            lobby_player_messages.get(old, {}).pop(uid, None)
            if not active_lobbies[old]["players"]:
                del active_lobbies[old]
                lobby_player_messages.pop(old, None)
            else:
                broadcast_lobby_update(old)
            user_lobby.pop(uid, None)
        if lobby_id not in active_lobbies:
            active_lobbies[lobby_id] = {"players": [], "league": league, "device": device, "slot": slot, "status": "waiting"}
        lobby = active_lobbies[lobby_id]
        if lobby["status"] != "waiting":
            bot.answer_callback_query(c.id, "❌ Лобби уже в игре!", show_alert=True)
            return
        if len(lobby["players"]) >= 10:
            bot.answer_callback_query(c.id, "❌ Лобби полное!", show_alert=True)
            return
        if uid in lobby["players"]:
            bot.answer_callback_query(c.id, "✅ Вы уже в этом лобби!")
            return
        # Собираем всех кто будет заходить: лидер + участники пати
        party_obj = get_party_of(uid)
        party_leader = party_obj["leader"] if party_obj else None
        if party_obj and party_leader == uid:
            members_to_join = [m for m in party_obj["members"] if m != uid]
        else:
            members_to_join = []

        # Проверяем место
        free_slots = 10 - len(lobby["players"])
        needed = 1 + len(members_to_join)
        if free_slots < needed:
            bot.answer_callback_query(
                c.id,
                f"❌ В лобби недостаточно мест для пати ({needed} нужно, {free_slots} свободно)!",
                show_alert=True,
            )
            return

        # Добавляем лидера
        lobby["players"].append(uid)
        user_lobby[uid] = lobby_id
        if lobby_player_messages.get(lobby_id) is None:
            lobby_player_messages[lobby_id] = {}

        # Обновляем сообщение лидера
        text = build_lobby_text(lobby_id)
        kb = build_lobby_kb(lobby_id, uid)
        try:
            bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
            lobby_player_messages[lobby_id][uid] = (c.message.chat.id, c.message.message_id)
        except Exception:
            pass
        bot.answer_callback_query(c.id, f"✅ Вы вошли в лобби #{slot}!")

        # Добавляем участников пати
        for m in members_to_join:
            if m in lobby["players"]:
                continue
            # Убираем из старого лобби если был
            old_m = user_lobby.get(m)
            if old_m and old_m in active_lobbies and m in active_lobbies[old_m].get("players", []):
                active_lobbies[old_m]["players"].remove(m)
                lobby_player_messages.get(old_m, {}).pop(m, None)
                broadcast_lobby_update(old_m)
            lobby["players"].append(m)
            user_lobby[m] = lobby_id
            # Отправляем участнику пати лобби-сообщение
            try:
                p_data = get_player(m)
                p_name = p_data[1] if p_data else str(m)
                m_text = build_lobby_text(lobby_id)
                m_kb = build_lobby_kb(lobby_id, m)
                sent = bot.send_message(m, m_text, reply_markup=m_kb)
                lobby_player_messages[lobby_id][m] = (sent.chat.id, sent.message_id)
            except Exception:
                pass

        broadcast_lobby_update(lobby_id, exclude_uid=uid)
        if len(lobby["players"]) >= 10:
            start_accept_phase(lobby_id)
    except Exception as e:
        print(f"Join error: {e}")
        bot.answer_callback_query(c.id, "❌ Ошибка")


@bot.callback_query_handler(func=lambda c: c.data.startswith("leave_"))
def cb_leave(c):
    uid = c.from_user.id
    lobby_id = c.data.split("leave_", 1)[1]
    lobby = active_lobbies.get(lobby_id)
    if lobby and lobby.get("status") == "accepting":
        bot.answer_callback_query(c.id, "❌ Нельзя выйти во время принятия матча!", show_alert=True)
        return
    if lobby and uid in lobby.get("players", []):
        lobby["players"].remove(uid)
        lobby_player_messages.get(lobby_id, {}).pop(uid, None)
        if not lobby["players"]:
            del active_lobbies[lobby_id]
            lobby_player_messages.pop(lobby_id, None)
        else:
            broadcast_lobby_update(lobby_id)
        user_lobby.pop(uid, None)
        bot.answer_callback_query(c.id, "✅ Вы вышли из лобби")
        bot.edit_message_text("⚡ ACTUAL FACEIT", c.message.chat.id, c.message.message_id, reply_markup=main_menu(uid))
    else:
        bot.answer_callback_query(c.id, "❌ Вы не в этом лобби")


# ==================== ФАЗА ПРИНЯТИЯ ====================
def build_accept_text(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return ""
    accepted = lobby.get("accepted", [])
    real_players = [u for u in lobby["players"] if not is_bot_player(u)]
    text = "🔔 <b>Матч найден! Статус принятия:</b>\n\n"
    for u in real_players:
        p = get_player(u)
        name = p[1] if p else str(u)
        prem = " 👑" if has_active_premium(u) else ""
        icon = "✅" if u in accepted else "⏳"
        text += f"{icon} {name}{prem}\n"
    accepted_cnt = len([u for u in accepted if not is_bot_player(u)])
    text += f"\n<b>{accepted_cnt}/{len(real_players)}</b> приняли"
    return text

def update_accept_status(lobby_id):
    msgs = accept_status_messages.get(lobby_id, {})
    if not msgs:
        return
    text = build_accept_text(lobby_id)
    for uid, (cid, mid) in list(msgs.items()):
        try:
            bot.edit_message_text(text, cid, mid)
        except Exception:
            pass

def delete_accept_status(lobby_id):
    msgs = accept_status_messages.pop(lobby_id, {})
    for uid, (cid, mid) in msgs.items():
        try:
            bot.delete_message(cid, mid)
        except Exception:
            pass

def delete_match_found(lobby_id):
    msgs = match_found_messages.pop(lobby_id, {})
    for uid, (cid, mid) in msgs.items():
        try:
            bot.delete_message(cid, mid)
        except Exception:
            pass

def delete_ban_status(lobby_id):
    msgs = ban_status_messages.pop(lobby_id, {})
    for uid, (cid, mid) in msgs.items():
        try:
            bot.delete_message(cid, mid)
        except Exception:
            pass

def start_accept_phase(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return
    lobby["status"] = "accepting"
    lobby["accepted"] = []
    lobby_player_messages.pop(lobby_id, None)
    accept_status_messages[lobby_id] = {}
    match_found_messages[lobby_id] = {}
    for uid in lobby["players"]:
        if is_bot_player(uid):
            lobby["accepted"].append(uid)
            continue
        try:
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("✅ Принять матч", callback_data=f"accept_{lobby_id}"))
            sent = bot.send_message(
                uid,
                f"🔔 <b>Матч найден!</b>\n\n"
                f"🏷 Лига: {lobby['league'].upper()}\n📱 Устройство: {lobby['device'].upper()}\n\n"
                f"⏱ У вас <b>{ACCEPT_TIMEOUT} секунд</b> чтобы принять.\nПри непринятии — предупреждение ⚠️",
                reply_markup=kb,
            )
            match_found_messages[lobby_id][uid] = (sent.chat.id, sent.message_id)
        except Exception:
            pass
    for uid in lobby["players"]:
        if is_bot_player(uid):
            continue
        try:
            text = build_accept_text(lobby_id)
            sent = bot.send_message(uid, text)
            accept_status_messages[lobby_id][uid] = (sent.chat.id, sent.message_id)
        except Exception:
            pass

    def check_accept():
        time.sleep(ACCEPT_TIMEOUT)
        lobby2 = active_lobbies.get(lobby_id)
        if not lobby2 or lobby2["status"] != "accepting":
            return
        not_accepted = [u for u in lobby2["players"] if u not in lobby2.get("accepted", []) and not is_bot_player(u)]
        delete_accept_status(lobby_id)
        delete_match_found(lobby_id)
        if not not_accepted:
            lobby2["status"] = "pre_mapban"
            threading.Thread(target=start_map_ban_phase, args=(lobby_id,), daemon=True).start()
            return
        for uid in not_accepted:
            warns = add_warn_to_player(uid)
            lobby2["players"].remove(uid)
            user_lobby.pop(uid, None)
            lobby_player_messages.get(lobby_id, {}).pop(uid, None)
            accept_status_messages.get(lobby_id, {}).pop(uid, None)
            try:
                if warns >= 3:
                    until = apply_mute(uid, hours=2)
                    dt = datetime.datetime.fromtimestamp(until).strftime("%H:%M")
                    bot.send_message(uid, f"⚠️ Варн {warns}/3 за непринятие.\n🔇 Замучен на 2 часа (до {dt}).\n❌ Вы исключены из лобби.")
                else:
                    bot.send_message(uid, f"⚠️ Варн {warns}/3 за непринятие матча.\n❌ Вы исключены из лобби.")
            except Exception:
                pass
        if len(lobby2["players"]) >= 10:
            lobby2["status"] = "pre_mapban"
            threading.Thread(target=start_map_ban_phase, args=(lobby_id,), daemon=True).start()
        elif not lobby2["players"]:
            lobby_player_messages.pop(lobby_id, None)
            ban_status_messages.pop(lobby_id, None)
            del active_lobbies[lobby_id]
        else:
            lobby2["status"] = "waiting"
            lobby2.pop("accepted", None)
            if lobby_player_messages.get(lobby_id) is None:
                lobby_player_messages[lobby_id] = {}
            lobby_text = build_lobby_text(lobby_id)
            cnt = len(lobby2["players"])
            for uid in lobby2["players"]:
                if is_bot_player(uid):
                    continue
                try:
                    bot.send_message(
                        uid,
                        f"⚠️ Игрок не принял матч и исключён.\n"
                        f"Вы остаётесь в очереди ({cnt}/10).",
                    )
                except Exception:
                    pass
                try:
                    kb = build_lobby_kb(lobby_id, uid)
                    sent = bot.send_message(uid, lobby_text, reply_markup=kb)
                    lobby_player_messages[lobby_id][uid] = (sent.chat.id, sent.message_id)
                except Exception:
                    pass

    threading.Thread(target=check_accept, daemon=True).start()


@bot.callback_query_handler(func=lambda c: c.data.startswith("accept_"))
def cb_accept(c):
    uid = c.from_user.id
    lobby_id = c.data.split("accept_", 1)[1]
    lobby = active_lobbies.get(lobby_id)
    if not lobby or lobby["status"] not in ("accepting",):
        bot.answer_callback_query(c.id, "❌ Матч уже недоступен")
        return
    if uid not in lobby.get("accepted", []):
        lobby["accepted"].append(uid)
    bot.answer_callback_query(c.id, "✅ Принято!")
    # Удаляем сообщение "матч найден" у принявшего
    try:
        bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception:
        try:
            bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
        except Exception:
            pass
    update_accept_status(lobby_id)
    if len(lobby["accepted"]) >= len(lobby["players"]) and lobby["status"] == "accepting":
        lobby["status"] = "pre_mapban"
        delete_accept_status(lobby_id)
        threading.Thread(target=start_map_ban_phase, args=(lobby_id,), daemon=True).start()


# ==================== БАН КАРТ ====================
def build_ban_status_text(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return ""
    ct_p = get_player(lobby.get("ct_captain"))
    t_p  = get_player(lobby.get("t_captain"))
    ct_name = ct_p[1] if ct_p else "CT капитан"
    t_name  = t_p[1]  if t_p  else "T капитан"
    bans = lobby.get("map_bans", [])
    remaining = lobby.get("maps_remaining", [])
    turn = lobby.get("ban_turn", "ct")
    lines = [f"🗺 <b>Бан карт</b>", "", f"💙 CT: <b>{ct_name}</b>", f"🧡 T: <b>{t_name}</b>", ""]
    if bans:
        lines.append("🚫 <b>Забанено:</b>")
        for b in bans:
            lines.append(f"  {'💙' if b['team']=='ct' else '🧡'} {b['map']}")
        lines.append("")
    if remaining:
        lines.append("✅ <b>Остались:</b>")
        for m in remaining:
            lines.append(f"  • {m}")
        lines.append("")
        turn_name = ct_name if turn == "ct" else t_name
        lines.append(f"⏳ Ход: {'💙' if turn=='ct' else '🧡'} <b>{turn_name}</b>")
    else:
        lines.append(f"🗺 <b>Карта выбрана: {lobby.get('map_name', '?')}</b>")
    return "\n".join(lines)

def send_ban_status_to_all(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return
    text = build_ban_status_text(lobby_id)
    if ban_status_messages.get(lobby_id) is None:
        ban_status_messages[lobby_id] = {}
    for uid in lobby["players"]:
        if is_bot_player(uid):
            continue
        existing = ban_status_messages[lobby_id].get(uid)
        if existing:
            cid, mid = existing
            try:
                bot.edit_message_text(text, cid, mid)
                continue
            except Exception:
                pass
        try:
            sent = bot.send_message(uid, text)
            ban_status_messages[lobby_id][uid] = (sent.chat.id, sent.message_id)
        except Exception:
            pass

def delete_lobby_messages(lobby_id):
    msgs = lobby_player_messages.pop(lobby_id, {})
    for uid, (cid, mid) in msgs.items():
        try:
            bot.delete_message(cid, mid)
        except Exception:
            pass

def start_map_ban_phase(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return
    if lobby["status"] not in ("accepting", "pre_mapban"):
        return
    lobby["status"] = "mapban"
    lobby["maps_remaining"] = list(MAPS)
    lobby["map_bans"] = []
    lobby["ban_turn"] = "ct"
    lobby["ban_count"] = 0
    players = lobby["players"]
    delete_match_found(lobby_id)
    delete_accept_status(lobby_id)
    delete_lobby_messages(lobby_id)
    lobby["ct_captain"] = pick_captain(players[:5] if len(players) >= 5 else players)
    lobby["t_captain"]  = pick_captain(players[5:] if len(players) > 5 else players[-1:])
    send_ban_status_to_all(lobby_id)
    _do_ban_turn(lobby_id)

def _do_ban_turn(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby or lobby["status"] != "mapban":
        return
    turn = lobby["ban_turn"]
    captain_uid = lobby["ct_captain"] if turn == "ct" else lobby["t_captain"]
    if is_bot_player(captain_uid):
        def bot_auto_ban():
            time.sleep(random.uniform(1, 2))
            lobby2 = active_lobbies.get(lobby_id)
            if not lobby2 or lobby2["status"] != "mapban" or not lobby2["maps_remaining"]:
                return
            _apply_ban(lobby_id, captain_uid, random.choice(lobby2["maps_remaining"]))
        threading.Thread(target=bot_auto_ban, daemon=True).start()
    else:
        _send_ban_keyboard(lobby_id, captain_uid)

def _send_ban_keyboard(lobby_id, captain_uid):
    lobby = active_lobbies.get(lobby_id)
    if not lobby:
        return
    turn = lobby["ban_turn"]
    kb = types.InlineKeyboardMarkup(row_width=2)
    for m in lobby["maps_remaining"]:
        kb.add(types.InlineKeyboardButton(f"❌ {m}", callback_data=f"banmap_{lobby_id}_{m}"))
    try:
        sent = bot.send_message(
            captain_uid,
            f"{'💙' if turn=='ct' else '🧡'} <b>Твой ход — забань карту:</b>",
            reply_markup=kb,
        )
        ban_turn_messages[lobby_id] = (sent.chat.id, sent.message_id)
    except Exception as e:
        print(f"Ban keyboard error: {e}")

def _apply_ban(lobby_id, banner_uid, map_name):
    lobby = active_lobbies.get(lobby_id)
    if not lobby or lobby["status"] != "mapban":
        return
    if map_name not in lobby["maps_remaining"]:
        return
    turn = lobby["ban_turn"]
    lobby["maps_remaining"].remove(map_name)
    lobby["map_bans"].append({"team": turn, "map": map_name})
    lobby["ban_count"] += 1
    if len(lobby["maps_remaining"]) == 1:
        lobby["map_name"] = lobby["maps_remaining"][0]
        send_ban_status_to_all(lobby_id)
        threading.Thread(target=lambda: (time.sleep(2), launch_match(lobby_id)), daemon=True).start()
    else:
        lobby["ban_turn"] = "t" if turn == "ct" else "ct"
        send_ban_status_to_all(lobby_id)
        _do_ban_turn(lobby_id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("banmap_"))
def cb_ban_map(c):
    uid = c.from_user.id
    raw = c.data[len("banmap_"):]
    raw_parts = raw.split("_")
    map_name = raw_parts[-1]
    lobby_id = "_".join(raw_parts[:-1])
    lobby = active_lobbies.get(lobby_id)
    if not lobby or lobby["status"] != "mapban":
        bot.answer_callback_query(c.id, "❌ Фаза бана уже завершена")
        return
    turn = lobby["ban_turn"]
    expected_cap = lobby["ct_captain"] if turn == "ct" else lobby["t_captain"]
    if uid != expected_cap:
        bot.answer_callback_query(c.id, "❌ Сейчас не ваш ход!", show_alert=True)
        return
    if map_name not in lobby["maps_remaining"]:
        bot.answer_callback_query(c.id, "❌ Карта уже забанена")
        return
    bot.answer_callback_query(c.id, f"✅ {map_name} забанена!")
    try:
        bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception:
        pass
    ban_turn_messages.pop(lobby_id, None)
    _apply_ban(lobby_id, uid, map_name)


# ==================== ЗАПУСК МАТЧА ====================
def pline(uid):
    p = get_player(uid)
    if p:
        icon = "🤖" if p[13] else "👤"
        prem = " 👑" if (not p[13] and has_active_premium(uid)) else ""
        return f"{icon} {p[1]}{prem} [Lvl {get_faceit_level(p[4])} | {p[4]} ELO]"
    return str(uid)

def launch_match(lobby_id):
    lobby = active_lobbies.get(lobby_id)
    if not lobby or lobby["status"] not in ("accepting", "waiting", "mapban", "pre_mapban"):
        return
    lobby["status"] = "active"
    if not lobby.get("map_name"):
        lobby["map_name"] = random.choice(MAPS)
    match_id = get_next_match_id()
    lobby["match_id"] = match_id
    lobby["screenshots_count"] = 0
    lobby["reg_taken_by"] = None

    players = list(lobby["players"])

    # Сохраняем капитанов из фазы бана карт
    ct_cap = lobby.get("ct_captain")
    t_cap  = lobby.get("t_captain")

    # Группируем пати и соло
    placed, party_groups, solo_players = set(), [], []
    for uid2 in players:
        if uid2 in placed:
            continue
        p_obj = get_party_of(uid2)
        if p_obj and len(p_obj["members"]) > 1:
            grp = [m for m in p_obj["members"] if m in players and m not in placed]
            if grp:
                party_groups.append(grp)
                for m in grp:
                    placed.add(m)
        else:
            solo_players.append(uid2)
            placed.add(uid2)

    random.shuffle(party_groups)
    random.shuffle(solo_players)

    team_ct, team_t = [], []

    # Сначала фиксируем капитанов в нужных командах
    if ct_cap and ct_cap in players:
        team_ct.append(ct_cap)
        for grp in party_groups:
            if ct_cap in grp:
                for m in grp:
                    if m != ct_cap and m not in team_ct:
                        team_ct.append(m)
                break
    if t_cap and t_cap in players:
        team_t.append(t_cap)
        for grp in party_groups:
            if t_cap in grp:
                for m in grp:
                    if m != t_cap and m not in team_t:
                        team_t.append(m)
                break

    already_placed = set(team_ct + team_t)

    # Размещаем оставшиеся пати-группы целиком в одну команду
    for grp in party_groups:
        if all(m in already_placed for m in grp):
            continue
        remaining_grp = [m for m in grp if m not in already_placed]
        if not remaining_grp:
            continue
        if len(team_ct) + len(remaining_grp) <= 5:
            team_ct.extend(remaining_grp)
        elif len(team_t) + len(remaining_grp) <= 5:
            team_t.extend(remaining_grp)
        else:
            for m in remaining_grp:
                if len(team_ct) < 5:
                    team_ct.append(m)
                else:
                    team_t.append(m)
        for m in remaining_grp:
            already_placed.add(m)

    # Соло игроки
    for uid2 in solo_players:
        if uid2 in already_placed:
            continue
        if len(team_ct) < 5:
            team_ct.append(uid2)
        else:
            team_t.append(uid2)

    lobby["team_ct"] = team_ct
    lobby["team_t"]  = team_t

    ct_captain_uid = lobby.get("ct_captain")
    if ct_captain_uid and not is_bot_player(ct_captain_uid) and ct_captain_uid in team_ct:
        host_uid = ct_captain_uid
    else:
        host_uid = next((u for u in team_ct if not is_bot_player(u)), None)
    host_p    = get_player(host_uid) if host_uid else None
    host_game_id = host_p[2] if host_p else "—"
    host_name    = host_p[1] if host_p else "—"
    lobby["host_uid"]     = host_uid
    lobby["host_game_id"] = host_game_id

    # Формат карточки для админа с ①②③ и ELO
    def admin_pline(idx, u):
        p = get_player(u)
        num = NUMBER_EMOJI[idx] if idx < len(NUMBER_EMOJI) else f"{idx+1}."
        if p:
            icon = "🤖" if p[13] else ""
            prem = " 👑" if (not p[13] and has_active_premium(u)) else ""
            return f"{num} {icon}{p[1]}{prem} | ID: <code>{u}</code> | ELO: {p[4]}"
        return f"{num} <code>{u}</code>"

    ct_lines = "\n".join([admin_pline(i, u) for i, u in enumerate(team_ct)])
    t_lines  = "\n".join([admin_pline(i, u) for i, u in enumerate(team_t)])
    match_text = (
        f"🎮 <b>МАТЧ #{match_id} НАЧАЛСЯ</b>\n\n"
        f"🏷 Лига: {lobby['league'].upper()}\n📱 Устройство: {lobby['device'].upper()}\n"
        f"🗺 Карта: <b>{lobby['map_name']}</b>\n"
        f"👑 Хост: <b>{host_name}</b> | Game ID: <code>{host_game_id}</code>\n\n"
        f"💙 <b>Команда CT</b>\n{ct_lines}\n\n"
        f"🧡 <b>Команда T</b>\n{t_lines}"
    )

    if ADMIN_CHAT_ID:
        kb_admin = _build_admin_match_kb(lobby_id, match_id, 0)
        try:
            topic = bot.create_forum_topic(ADMIN_CHAT_ID, f"Match #{match_id}")
            thread_id = topic.message_thread_id
            lobby["admin_thread_id"] = thread_id
            sent = bot.send_message(ADMIN_CHAT_ID, match_text, reply_markup=kb_admin, message_thread_id=thread_id)
            lobby["admin_msg_id"] = sent.message_id
            try:
                bot.pin_chat_message(ADMIN_CHAT_ID, sent.message_id, disable_notification=True)
            except Exception:
                pass
        except Exception:
            try:
                sent = bot.send_message(ADMIN_CHAT_ID, match_text, reply_markup=kb_admin)
                lobby["admin_msg_id"] = sent.message_id
                lobby["admin_thread_id"] = None
                try:
                    bot.pin_chat_message(ADMIN_CHAT_ID, sent.message_id, disable_notification=True)
                except Exception:
                    pass
            except Exception as e:
                print(f"Admin chat error: {e}")

    for uid in players:
        if is_bot_player(uid):
            continue
        team = "💙 CT" if uid in team_ct else "🧡 T"
        player_text = (
            f"🎮 <b>МАТЧ #{match_id} НАЧАЛСЯ!</b>\n\n"
            f"🗺 Карта: <b>{lobby['map_name']}</b>\n"
            f"👥 Ваша команда: <b>{team}</b>\n"
            f"👑 Хост: <b>{host_name}</b>\n"
            f"🎮 Game ID хоста: <code>{host_game_id}</code>\n\n"
            f"💙 <b>Команда CT</b>\n"
            + "\n".join([f"  {i+1}. {pline(u)}" for i, u in enumerate(team_ct)])
            + f"\n\n🧡 <b>Команда T</b>\n"
            + "\n".join([f"  {i+1}. {pline(u)}" for i, u in enumerate(team_t)])
            + f"\n\n📸 После матча нажми кнопку и отправь скриншот результатов."
        )
        kb_player = types.InlineKeyboardMarkup()
        kb_player.add(types.InlineKeyboardButton("📸 Отправить результаты", callback_data=f"send_result_{lobby_id}"))
        try:
            bot.send_message(uid, player_text, reply_markup=kb_player)
            awaiting_screenshot[uid] = lobby_id
        except Exception:
            pass

    running_matches[lobby_id] = lobby
    parts = lobby_id.split("_")
    if len(parts) >= 3:
        league_r, device_r, slot_r = parts[0], parts[1], parts[2]
        active_lobbies[lobby_id] = {"players": [], "league": league_r, "device": device_r, "slot": slot_r, "status": "waiting"}
    else:
        active_lobbies.pop(lobby_id, None)

    lobby_player_messages.pop(lobby_id, None)
    delete_ban_status(lobby_id)
    for uid in players:
        user_lobby.pop(uid, None)


def _build_admin_match_kb(lobby_id, match_id, screenshots_count, taken_by=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    if taken_by:
        p = get_player(taken_by)
        name = p[1] if p else str(taken_by)
        kb.add(
            types.InlineKeyboardButton(f"📝 Регистрирует: {name} — ЗАНЯТО", callback_data="noop"),
            types.InlineKeyboardButton("🔓 Освободить регистрацию", callback_data=f"reg_release|{lobby_id}"),
        )
    else:
        kb.add(types.InlineKeyboardButton(f"✅ Зарегистрировать матч ({screenshots_count}📸)", callback_data=f"reg_match|{lobby_id}"))
    kb.add(
        types.InlineKeyboardButton("❌ Отменить матч",  callback_data=f"cancel_match|{lobby_id}"),
        types.InlineKeyboardButton("🔄 Перерегать",     callback_data=f"reregister_match|{lobby_id}"),
    )
    return kb


# ==================== СКРИНШОТЫ ====================
@bot.callback_query_handler(func=lambda c: c.data.startswith("send_result_"))
def cb_send_result(c):
    uid = c.from_user.id
    lobby_id = c.data.split("send_result_", 1)[1]
    lobby = running_matches.get(lobby_id)
    if not lobby or lobby.get("status") != "active":
        bot.answer_callback_query(c.id, "❌ Матч уже завершён", show_alert=True)
        return
    awaiting_screenshot[uid] = lobby_id
    bot.answer_callback_query(c.id)
    try:
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
    except Exception:
        pass
    bot.send_message(uid, "📸 <b>Отправь скриншот прямо в этот чат</b>\n\nПрикрепи фото или документ:")


@bot.message_handler(content_types=["photo", "document"])
def handle_player_screenshot(msg):
    uid = msg.from_user.id
    if not is_registered(uid) or is_bot_player(uid):
        return
    lobby_id = awaiting_screenshot.get(uid)
    if not lobby_id:
        return
    lobby = running_matches.get(lobby_id)
    if not lobby or lobby.get("status") != "active":
        awaiting_screenshot.pop(uid, None)
        return
    awaiting_screenshot.pop(uid, None)
    p = get_player(uid)
    name = p[1] if p else str(uid)
    match_id = lobby.get("match_id", "?")
    lobby["screenshots_count"] = lobby.get("screenshots_count", 0) + 1
    sc = lobby["screenshots_count"]
    if ADMIN_CHAT_ID:
        try:
            caption = f"📸 {name} ({uid}) — Match #{match_id}"
            thread_id = lobby.get("admin_thread_id")
            kw = {"caption": caption}
            if thread_id:
                kw["message_thread_id"] = thread_id
            elif lobby.get("admin_msg_id"):
                kw["reply_to_message_id"] = lobby["admin_msg_id"]
            if msg.photo:
                bot.send_photo(ADMIN_CHAT_ID, msg.photo[-1].file_id, **kw)
            elif msg.document:
                bot.send_document(ADMIN_CHAT_ID, msg.document.file_id, **kw)
            if lobby.get("admin_msg_id"):
                new_kb = _build_admin_match_kb(lobby_id, match_id, sc, lobby.get("reg_taken_by"))
                edit_kw = {"reply_markup": new_kb}
                if thread_id:
                    edit_kw["message_thread_id"] = thread_id
                try:
                    bot.edit_message_reply_markup(ADMIN_CHAT_ID, lobby["admin_msg_id"], **edit_kw)
                except Exception:
                    pass
        except Exception as e:
            print(f"Screenshot error: {e}")
    try:
        bot.reply_to(msg, f"✅ Скриншот принят! Всего: {sc}/10")
    except Exception:
        pass


# ==================== РЕГИСТРАЦИЯ РЕЗУЛЬТАТОВ ====================
match_registration = {}

def reg_send(uid, text, **kwargs):
    data = match_registration.get(uid, {})
    chat_id   = data.get("reply_chat_id", uid)
    thread_id = data.get("reply_thread_id")
    if thread_id:
        kwargs["message_thread_id"] = thread_id
    bot.send_message(chat_id, text, **kwargs)


@bot.callback_query_handler(func=lambda c: c.data.startswith("reg_match|"))
def cb_reg_match(c):
    uid = c.from_user.id
    if not is_game_reg_check(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    lobby_id = c.data.split("|", 1)[1]
    lobby = running_matches.get(lobby_id)
    if not lobby or lobby.get("status") != "active":
        bot.answer_callback_query(c.id, "❌ Матч не найден или завершён")
        return
    taken = lobby.get("reg_taken_by")
    if taken and taken != uid:
        p = get_player(taken)
        bot.answer_callback_query(c.id, f"❌ Регистрацию взял {p[1] if p else taken}", show_alert=True)
        return
    lobby["reg_taken_by"] = uid
    match_id = lobby.get("match_id", "?")
    sc = lobby.get("screenshots_count", 0)
    try:
        new_kb = _build_admin_match_kb(lobby_id, match_id, sc, taken_by=uid)
        thread_id = lobby.get("admin_thread_id")
        edit_kw = {"reply_markup": new_kb}
        if thread_id:
            edit_kw["message_thread_id"] = thread_id
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, **edit_kw)
    except Exception:
        pass
    bot.answer_callback_query(c.id, "✅ Регистрация захвачена!")
    def pln(uid2):
        p = get_player(uid2)
        return f"{p[1]} — <code>{uid2}</code>" if p else str(uid2)
    ct_list = "\n".join([pln(u) for u in lobby.get("team_ct", [])])
    t_list  = "\n".join([pln(u) for u in lobby.get("team_t",  [])])
    reply_chat_id   = c.message.chat.id
    reply_thread_id = getattr(c.message, "message_thread_id", None)
    instructions = (
        f"📋 <b>Регистрация матча #{match_id}</b>\n\n"
        f"💙 <b>CT</b>\n{ct_list}\n\n🧡 <b>T</b>\n{t_list}\n\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"<b>Шаг 1/3</b> — Введи счёт матча:\n"
        f"Формат: <code>13:11</code>"
    )
    match_registration[uid] = {
        "lobby_id": lobby_id,
        "step": "score",
        "reply_chat_id": reply_chat_id,
        "reply_thread_id": reply_thread_id,
    }
    send_kw = {"parse_mode": "HTML"}
    if reply_thread_id:
        send_kw["message_thread_id"] = reply_thread_id
    bot.send_message(reply_chat_id, instructions, **send_kw)


@bot.callback_query_handler(func=lambda c: c.data.startswith("reg_release|"))
def cb_reg_release(c):
    uid = c.from_user.id
    if not is_game_reg_check(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    lobby_id = c.data.split("|", 1)[1]
    lobby = running_matches.get(lobby_id)
    if not lobby:
        bot.answer_callback_query(c.id, "❌ Матч не найден")
        return
    lobby["reg_taken_by"] = None
    match_id = lobby.get("match_id", "?")
    sc = lobby.get("screenshots_count", 0)
    try:
        new_kb = _build_admin_match_kb(lobby_id, match_id, sc, taken_by=None)
        thread_id = lobby.get("admin_thread_id")
        edit_kw = {"reply_markup": new_kb}
        if thread_id:
            edit_kw["message_thread_id"] = thread_id
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, **edit_kw)
    except Exception:
        pass
    bot.answer_callback_query(c.id, "🔓 Регистрация освобождена")


@bot.message_handler(func=lambda m: m.from_user.id in match_registration and match_registration[m.from_user.id].get("step") == "score")
def reg_step_score(msg):
    uid = msg.from_user.id
    if not is_game_reg_check(uid):
        return
    text = msg.text.strip() if msg.text else ""
    m = re.match(r'^(\d+)\s*[:\-]\s*(\d+)$', text)
    if not m:
        reg_send(uid, "❌ Неверный формат. Введи счёт: <code>13:11</code>", parse_mode="HTML")
        return
    score_w, score_l = int(m.group(1)), int(m.group(2))
    match_registration[uid]["score_w"] = score_w
    match_registration[uid]["score_l"] = score_l
    match_registration[uid]["step"] = "winner"
    lobby_id = match_registration[uid]["lobby_id"]
    lobby = running_matches.get(lobby_id)
    ct_list = "\n".join([f"  {i+1}. {pline(u)}" for i, u in enumerate(lobby.get("team_ct", []) if lobby else [])])
    t_list  = "\n".join([f"  {i+1}. {pline(u)}" for i, u in enumerate(lobby.get("team_t",  []) if lobby else [])])
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("💙 CT победила", callback_data=f"reg_winner_ct|{lobby_id}"),
        types.InlineKeyboardButton("🧡 T победила",  callback_data=f"reg_winner_t|{lobby_id}"),
    )
    reply_chat_id   = match_registration[uid].get("reply_chat_id", uid)
    reply_thread_id = match_registration[uid].get("reply_thread_id")
    send_kw = {"reply_markup": kb, "parse_mode": "HTML"}
    if reply_thread_id:
        send_kw["message_thread_id"] = reply_thread_id
    bot.send_message(
        reply_chat_id,
        f"<b>Шаг 2/3</b> — Счёт принят: <b>{score_w}:{score_l}</b>\n\n"
        f"💙 CT:\n{ct_list}\n\n🧡 T:\n{t_list}\n\n"
        f"Кто победил?",
        **send_kw,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("reg_winner_"))
def reg_winner(c):
    uid = c.from_user.id
    if not is_game_reg_check(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    if uid not in match_registration or match_registration[uid].get("step") != "winner":
        bot.answer_callback_query(c.id, "❌ Нет активной регистрации")
        return
    raw = c.data[len("reg_winner_"):]
    parts = raw.split("|", 1)
    winner = parts[0]
    lobby_id = parts[1] if len(parts) > 1 else match_registration[uid]["lobby_id"]
    match_registration[uid]["winner"] = winner
    match_registration[uid]["step"] = "all_kills"
    bot.answer_callback_query(c.id)
    try:
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
    except Exception:
        pass
    lobby = running_matches.get(lobby_id)
    ct_players = [u for u in (lobby.get("team_ct", []) if lobby else []) if not is_bot_player(u)]
    t_players  = [u for u in (lobby.get("team_t",  []) if lobby else []) if not is_bot_player(u)]
    match_registration[uid]["ct_players"] = ct_players
    match_registration[uid]["t_players"]  = t_players
    match_registration[uid]["kills_data"] = {}
    _ask_all_kda(uid, ct_players, t_players)


def _ask_all_kda(reg_uid, ct_players, t_players):
    all_players = ct_players + t_players
    lines = []
    for u in all_players:
        p = get_player(u)
        name = p[1] if p else str(u)
        team = "💙 CT" if u in ct_players else "🧡 T"
        lines.append(f"  {team} {name} — <code>{u}</code>")
    player_list = "\n".join(lines)
    all_count = len(all_players)
    example_parts = [f"{u} 20 5 3" for u in all_players[:3]]
    example = ", ".join(example_parts)
    reg_send(
        reg_uid,
        f"<b>Шаг 3/3 — Статистика игроков</b>\n\n"
        f"Список игроков:\n{player_list}\n\n"
        f"💡 Скопируй ID рядом с ником и введи <b>всех одной строкой</b>:\n"
        f"<code>ID K A D, ID K A D, ...</code>\n\n"
        f"🔫 K — киллы  🤝 A — помощи  💀 D — смерти\n\n"
        f"Пример:\n<code>{example}</code>\n\n"
        f"⚠️ Между цифрами — пробел, после каждого игрока — запятая\n"
        f"Нужно ввести всех {all_count} игроков.",
        parse_mode="HTML",
    )


def _parse_all_kda(text, all_players):
    known_ids = set(all_players)
    entries = [e.strip() for e in text.split(",") if e.strip()]
    if len(entries) != len(all_players):
        return None, f"❌ Нужно {len(all_players)} записей через запятую, ты ввёл {len(entries)}."
    result = {}
    seen_ids = set()
    for i, entry in enumerate(entries):
        parts = entry.split()
        if len(parts) != 4:
            return None, f"❌ Запись #{i+1}: нужно 4 значения через пробел — <code>ID K A D</code>."
        try:
            pid = int(parts[0])
            k, a, d = int(parts[1]), int(parts[2]), int(parts[3])
        except ValueError:
            return None, f"❌ Запись #{i+1}: только цифры."
        if pid not in known_ids:
            return None, f"❌ Запись #{i+1}: ID <code>{pid}</code> не найден в этом матче."
        if pid in seen_ids:
            return None, f"❌ Запись #{i+1}: ID <code>{pid}</code> уже введён."
        seen_ids.add(pid)
        result[pid] = {"kills": k, "assists": a, "deaths": d}
    return result, None


@bot.message_handler(func=lambda m: m.from_user.id in match_registration and match_registration[m.from_user.id].get("step") == "all_kills")
def reg_step_all_kills(msg):
    uid = msg.from_user.id
    if not is_game_reg_check(uid):
        return
    data = match_registration[uid]
    ct_players = data.get("ct_players", [])
    t_players  = data.get("t_players",  [])
    all_players = ct_players + t_players
    parsed, err = _parse_all_kda(msg.text.strip() if msg.text else "", all_players)
    if err:
        reg_send(uid, err, parse_mode="HTML")
        return
    data["kills_data"].update(parsed)
    _finalize_match(uid, data["lobby_id"])


def _finalize_match(reg_uid, lobby_id):
    data = match_registration.pop(reg_uid, {})
    lobby = running_matches.get(lobby_id)
    if not lobby:
        reg_send(reg_uid, "❌ Матч не найден")
        return
    winner   = data.get("winner", "ct")
    score_w  = data.get("score_w", 0)
    score_l  = data.get("score_l", 0)
    kills_data = data.get("kills_data", {})
    team_ct  = lobby.get("team_ct", [])
    team_t   = lobby.get("team_t",  [])
    winner_team = team_ct if winner == "ct" else team_t
    loser_team  = team_t  if winner == "ct" else team_ct
    all_stats = {}
    conn = _db()
    cur = conn.cursor()
    for uid in team_ct + team_t:
        if is_bot_player(uid):
            continue
        won = uid in winner_team
        kda = kills_data.get(uid, {"kills": 0, "deaths": 0, "assists": 0})
        kills = kda["kills"]
        if won:
            elo_change = 25 if kills >= 12 else 17
            coins_reward = 15
        else:
            elo_change = -15 if kills >= 12 else -23
            coins_reward = 4
        p = get_player(uid)
        if p:
            prem = has_active_premium(uid)
            if prem:
                elo_change = int(elo_change * 1.5) if won else elo_change
                coins_reward = int(coins_reward * 1.5)
        if won:
            cur.execute(
                "UPDATE players SET wins=wins+1, elo=GREATEST(0, elo+%s), kills=kills+%s, deaths=deaths+%s, assists=assists+%s, coins=coins+%s WHERE user_id=%s",
                (elo_change, kda["kills"], kda["deaths"], kda["assists"], coins_reward, uid),
            )
        else:
            cur.execute(
                "UPDATE players SET losses=losses+1, elo=GREATEST(0, elo+%s), kills=kills+%s, deaths=deaths+%s, assists=assists+%s, coins=coins+%s WHERE user_id=%s",
                (elo_change, kda["kills"], kda["deaths"], kda["assists"], coins_reward, uid),
            )
        all_stats[uid] = {**kda, "won": won, "elo_change": elo_change, "coins_reward": coins_reward}
    conn.commit()
    conn.close()
    save_match_to_history(lobby, {"winner": winner, "score_w": score_w, "score_l": score_l}, all_stats)
    lobby["status"] = "finished"
    running_matches.pop(lobby_id, None)
    winner_lines = []
    loser_lines  = []
    for uid, s in all_stats.items():
        p = get_player(uid)
        name = p[1] if p else str(uid)
        sign = "+" if s["elo_change"] >= 0 else ""
        line = (
            f"👤 <b>{name}</b>\n"
            f"   🔫 Киллы: {s['kills']}  🤝 Помощи: {s['assists']}  💀 Смерти: {s['deaths']}\n"
            f"   📊 ELO: {sign}{s['elo_change']}  🪙 Коины: +{s['coins_reward']}"
        )
        if s["won"]:
            winner_lines.append(line)
        else:
            loser_lines.append(line)

    winner_team_label = "💙 CT" if winner == "ct" else "🧡 T"
    loser_team_label  = "🧡 T"  if winner == "ct" else "💙 CT"

    result_text = (
        f"🏁 <b>Матч #{lobby.get('match_id','?')} завершён!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🗺 Карта: {lobby.get('map_name', '?')}  |  Счёт: <b>{score_w}:{score_l}</b>\n"
        f"🏆 Победитель: <b>{winner_team_label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"✅ <b>{winner_team_label} — Победа</b>\n"
        + "\n\n".join(winner_lines)
        + f"\n\n❌ <b>{loser_team_label} — Поражение</b>\n"
        + "\n\n".join(loser_lines)
    )
    for uid in all_stats:
        try:
            bot.send_message(uid, result_text, parse_mode="HTML")
        except Exception:
            pass
    reg_send(reg_uid, f"✅ Матч зарегистрирован!\n\n{result_text}", parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data.startswith("cancel_match|"))
def cb_cancel_match(c):
    uid = c.from_user.id
    if not is_game_reg_check(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    lobby_id = c.data.split("|", 1)[1]
    lobby = running_matches.get(lobby_id)
    if not lobby:
        bot.answer_callback_query(c.id, "❌ Матч не найден")
        return
    for puid in lobby.get("team_ct", []) + lobby.get("team_t", []):
        if is_bot_player(puid):
            continue
        try:
            bot.send_message(puid, "❌ Матч отменён администратором.")
        except Exception:
            pass
    lobby["status"] = "cancelled"
    running_matches.pop(lobby_id, None)
    bot.answer_callback_query(c.id, "✅ Матч отменён")
    try:
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("reregister_match|"))
def cb_reregister_match(c):
    uid = c.from_user.id
    if not is_game_reg_check(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    lobby_id = c.data.split("|", 1)[1]
    lobby = running_matches.get(lobby_id)
    if not lobby:
        bot.answer_callback_query(c.id, "❌ Матч не найден")
        return
    lobby["reg_taken_by"] = None
    match_registration.pop(uid, None)
    bot.answer_callback_query(c.id, "🔄 Регистрация сброшена")


@bot.callback_query_handler(func=lambda c: c.data == "noop")
def cb_noop(c):
    bot.answer_callback_query(c.id)


# ==================== МАГАЗИН ====================
@bot.callback_query_handler(func=lambda c: c.data == "shop")
def cb_shop(c):
    uid = c.from_user.id
    err = check_blocked(uid)
    if err:
        bot.answer_callback_query(c.id, "⚠️ Доступ ограничен", show_alert=True)
        return
    kb = types.InlineKeyboardMarkup(row_width=1)
    for cat, name in CATEGORY_NAMES.items():
        kb.add(types.InlineKeyboardButton(name, callback_data=f"shop_cat_{cat}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    bot.edit_message_text("🛒 <b>МАГАЗИН</b>\n\nВыберите категорию:", c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("shop_cat_"))
def cb_shop_category(c):
    uid = c.from_user.id
    category = c.data.split("shop_cat_")[1]
    items = get_shop_items_by_category(category)
    p = get_player(uid)
    coins = p[5] if p else 0
    cat_name = CATEGORY_NAMES.get(category, category)
    kb = types.InlineKeyboardMarkup(row_width=1)
    for item_id, name, desc, price, item_type in items:
        owned = has_item_in_inventory(uid, item_id)
        label = f"{'✅ ' if owned else ''}{name} — {price} AC"
        kb.add(types.InlineKeyboardButton(label, callback_data=f"shop_item_{item_id}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="shop"))
    bot.edit_message_text(
        f"{cat_name}\n💰 Баланс: <b>{coins} AC</b>\n\nВыберите товар:",
        c.message.chat.id, c.message.message_id, reply_markup=kb,
    )
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("shop_item_"))
def cb_shop_item(c):
    uid = c.from_user.id
    item_id = int(c.data.split("shop_item_")[1])
    item = get_shop_item(item_id)
    if not item:
        bot.answer_callback_query(c.id, "❌ Товар не найден")
        return
    _, name, desc, category, price, item_type = item
    p = get_player(uid)
    coins = p[5] if p else 0
    owned = has_item_in_inventory(uid, item_id)
    icon = CATEGORY_ICONS.get(category, "")
    text = (
        f"{icon} <b>{name}</b>\n\n"
        f"📝 {desc}\n"
        f"💰 Цена: <b>{price} AC</b>\n"
        f"💳 Ваш баланс: <b>{coins} AC</b>\n"
        + ("✅ Уже куплено\n" if owned else "")
    )
    kb = types.InlineKeyboardMarkup()
    if not owned or item_type in {"sticker", "unwarn", "x2coins", "rename"}:
        kb.add(types.InlineKeyboardButton(f"💳 Купить за {price} AC", callback_data=f"shop_buy_{item_id}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data=f"shop_cat_{category}"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("shop_buy_"))
def cb_shop_buy(c):
    uid = c.from_user.id
    item_id = int(c.data.split("shop_buy_")[1])
    ok, msg = buy_item(uid, item_id)
    bot.answer_callback_query(c.id, msg[:200], show_alert=not ok)
    if ok:
        item = get_shop_item(item_id)
        if item:
            bot.edit_message_text(msg, c.message.chat.id, c.message.message_id)


# ==================== ИНВЕНТАРЬ ====================
@bot.callback_query_handler(func=lambda c: c.data == "inv")
def cb_inventory(c):
    uid = c.from_user.id
    err = check_blocked(uid)
    if err:
        bot.answer_callback_query(c.id, "⚠️ Доступ ограничен", show_alert=True)
        return
    items = get_inventory(uid)
    if not items:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🛒 В магазин", callback_data="shop"))
        kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
        bot.edit_message_text("🎒 <b>Инвентарь пуст</b>", c.message.chat.id, c.message.message_id, reply_markup=kb)
        bot.answer_callback_query(c.id)
        return
    kb = types.InlineKeyboardMarkup(row_width=1)
    for inv_id, name, category, item_type, purchased_at, is_activated, shop_id in items:
        status = "✅ " if is_activated else ""
        kb.add(types.InlineKeyboardButton(f"{status}{name}", callback_data=f"inv_item_{inv_id}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    bot.edit_message_text("🎒 <b>Ваш инвентарь:</b>", c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("inv_item_"))
def cb_inv_item(c):
    uid = c.from_user.id
    inv_id = int(c.data.split("inv_item_")[1])
    items = get_inventory(uid)
    item = next((i for i in items if i[0] == inv_id), None)
    if not item:
        bot.answer_callback_query(c.id, "❌ Предмет не найден")
        return
    inv_id2, name, category, item_type, purchased_at, is_activated, shop_id = item
    dt = datetime.datetime.fromtimestamp(purchased_at).strftime("%d.%m.%Y") if purchased_at else "?"
    text = (
        f"🎒 <b>{name}</b>\n\n"
        f"Категория: {CATEGORY_NAMES.get(category, category)}\n"
        f"Куплено: {dt}\n"
        f"Статус: {'✅ Активировано' if is_activated else '⏳ Не активировано'}"
    )
    kb = types.InlineKeyboardMarkup()
    if not is_activated:
        kb.add(types.InlineKeyboardButton("⚡ Активировать", callback_data=f"inv_activate_{inv_id}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="inv"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("inv_activate_"))
def cb_inv_activate(c):
    uid = c.from_user.id
    inv_id = int(c.data.split("inv_activate_")[1])
    items = get_inventory(uid)
    item = next((i for i in items if i[0] == inv_id), None)
    if not item:
        bot.answer_callback_query(c.id, "❌ Предмет не найден")
        return
    inv_id2, name, category, item_type, purchased_at, is_activated, shop_id = item
    result, msg = activate_inventory_item(inv_id, uid, item_type, name)
    if result == "rename":
        rename_flow[uid] = inv_id
        bot.answer_callback_query(c.id)
        bot.send_message(uid, msg)
        return
    bot.answer_callback_query(c.id, msg[:200], show_alert=not result)
    if result:
        try:
            bot.edit_message_text(msg, c.message.chat.id, c.message.message_id)
        except Exception:
            pass


@bot.message_handler(func=lambda m: m.from_user.id in rename_flow and m.text is not None)
def handle_rename(msg):
    uid = msg.from_user.id
    inv_id = rename_flow.pop(uid)
    new_nick = msg.text.strip()
    if not (2 <= len(new_nick) <= 20):
        bot.send_message(uid, "❌ Никнейм 2-20 символов")
        return
    if nick_taken(new_nick, exclude_uid=uid):
        bot.send_message(uid, "❌ Этот никнейм уже занят!")
        return
    conn = _db()
    cur = conn.cursor()
    cur.execute("UPDATE players SET username=%s WHERE user_id=%s", (new_nick, uid))
    cur.execute(
        "UPDATE inventory SET is_activated=1, activated_at=%s WHERE id=%s",
        (int(time.time()), inv_id),
    )
    conn.commit()
    conn.close()
    bot.send_message(uid, f"✅ Никнейм изменён на <b>{new_nick}</b>!")


# ==================== ПАТИ ====================
@bot.callback_query_handler(func=lambda c: c.data == "party_menu")
def cb_party_menu(c):
    uid = c.from_user.id
    err = check_blocked(uid)
    if err:
        bot.answer_callback_query(c.id, "⚠️ Доступ ограничен", show_alert=True)
        return
    party = get_party_of(uid)
    if party:
        members_text = "\n".join([
            f"  {'👑' if m == party['leader'] else '👤'} {get_player(m)[1] if get_player(m) else m}"
            for m in party["members"]
        ])
        max_size = get_party_max_size(party)
        text = f"👥 <b>Ваша пати</b> ({len(party['members'])}/{max_size}):\n{members_text}"
        kb = types.InlineKeyboardMarkup(row_width=1)
        if uid == party["leader"]:
            kb.add(
                types.InlineKeyboardButton("➕ Пригласить", callback_data="party_invite"),
                types.InlineKeyboardButton("🗑 Распустить пати", callback_data="party_disband"),
            )
        else:
            kb.add(types.InlineKeyboardButton("🚪 Покинуть пати", callback_data="party_leave"))
        kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    else:
        text = "👥 У вас нет пати.\nСоздайте пати или примите приглашение."
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("➕ Создать пати", callback_data="party_create"),
            types.InlineKeyboardButton("🔙 Назад", callback_data="back"),
        )
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "party_create")
def cb_party_create(c):
    uid = c.from_user.id
    if uid in user_party:
        bot.answer_callback_query(c.id, "❌ Вы уже в пати")
        return
    party_id = f"party_{uid}_{int(time.time())}"
    parties[party_id] = {"leader": uid, "members": [uid]}
    user_party[uid] = party_id
    bot.answer_callback_query(c.id, "✅ Пати создана!")
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("👥 Управление пати", callback_data="party_menu"))
    bot.edit_message_text("✅ <b>Пати создана!</b>", c.message.chat.id, c.message.message_id, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data == "party_invite")
def cb_party_invite(c):
    uid = c.from_user.id
    party = get_party_of(uid)
    if not party or party["leader"] != uid:
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    max_size = get_party_max_size(party)
    if len(party["members"]) >= max_size:
        bot.answer_callback_query(c.id, f"❌ Пати полная ({max_size} чел.)", show_alert=True)
        return
    awaiting_party_invite[uid] = True
    bot.answer_callback_query(c.id)
    bot.send_message(uid, "👤 Введите Telegram ID или никнейм игрока для приглашения:")


@bot.message_handler(func=lambda m: m.from_user.id in awaiting_party_invite and m.text is not None)
def handle_party_invite(msg):
    uid = msg.from_user.id
    awaiting_party_invite.pop(uid, None)
    text = msg.text.strip()
    target = None
    if text.isdigit():
        target = get_player(int(text))
    else:
        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM players WHERE username=%s AND is_bot=0", (text,))
        target = cur.fetchone()
        conn.close()
    if not target:
        bot.send_message(uid, "❌ Игрок не найден")
        return
    target_id = target[0]
    if target_id == uid:
        bot.send_message(uid, "❌ Нельзя пригласить себя")
        return
    if target_id in user_party:
        bot.send_message(uid, "❌ Игрок уже в пати")
        return
    party_id = user_party.get(uid)
    party = get_party_of(uid)
    if not party:
        bot.send_message(uid, "❌ У вас нет пати")
        return
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Принять", callback_data=f"party_accept_{party_id}_{uid}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data="party_decline"),
    )
    try:
        inviter = get_player(uid)
        bot.send_message(
            target_id,
            f"👥 <b>{inviter[1] if inviter else uid}</b> приглашает вас в пати!\nМакс. размер: {get_party_max_size(party)}",
            reply_markup=kb,
        )
        bot.send_message(uid, f"✅ Приглашение отправлено игроку <b>{target[1]}</b>!")
    except Exception:
        bot.send_message(uid, "❌ Не удалось отправить приглашение")


@bot.callback_query_handler(func=lambda c: c.data.startswith("party_accept_"))
def cb_party_accept(c):
    uid = c.from_user.id
    parts = c.data.split("_")
    # Формат: party_accept_party_LEADER_TIMESTAMP_INVITERID
    # party_id это всё что после "party_accept_" до последнего "_<inviterID>"
    raw = c.data[len("party_accept_"):]
    party_id = "_".join(raw.split("_")[:-1])
    party = parties.get(party_id)
    if not party:
        bot.answer_callback_query(c.id, "❌ Пати не существует")
        return
    if uid in user_party:
        bot.answer_callback_query(c.id, "❌ Вы уже в пати")
        return
    max_size = get_party_max_size(party)
    if len(party["members"]) >= max_size:
        bot.answer_callback_query(c.id, "❌ Пати уже полная", show_alert=True)
        return
    party["members"].append(uid)
    user_party[uid] = party_id
    bot.answer_callback_query(c.id, "✅ Вы вступили в пати!")
    try:
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
    except Exception:
        pass
    p = get_player(uid)
    for m in party["members"]:
        if m == uid:
            continue
        try:
            bot.send_message(m, f"✅ <b>{p[1] if p else uid}</b> вступил в пати!")
        except Exception:
            pass


@bot.callback_query_handler(func=lambda c: c.data == "party_decline")
def cb_party_decline(c):
    bot.answer_callback_query(c.id, "❌ Приглашение отклонено")
    try:
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data == "party_leave")
def cb_party_leave(c):
    uid = c.from_user.id
    party = get_party_of(uid)
    if not party:
        bot.answer_callback_query(c.id, "❌ Вы не в пати")
        return
    party_id = user_party.pop(uid)
    party["members"].remove(uid)
    if not party["members"]:
        parties.pop(party_id, None)
    elif party["leader"] == uid:
        party["leader"] = party["members"][0]
    bot.answer_callback_query(c.id, "✅ Вы покинули пати")
    bot.edit_message_text("⚡ ACTUAL FACEIT", c.message.chat.id, c.message.message_id, reply_markup=main_menu(uid))


@bot.callback_query_handler(func=lambda c: c.data == "party_disband")
def cb_party_disband(c):
    uid = c.from_user.id
    party = get_party_of(uid)
    if not party or party["leader"] != uid:
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    party_id = user_party.get(uid)
    for m in list(party["members"]):
        user_party.pop(m, None)
        if m != uid:
            try:
                bot.send_message(m, "👥 Пати распущена лидером.")
            except Exception:
                pass
    parties.pop(party_id, None)
    bot.answer_callback_query(c.id, "✅ Пати распущена")
    bot.edit_message_text("⚡ ACTUAL FACEIT", c.message.chat.id, c.message.message_id, reply_markup=main_menu(uid))


# ==================== ПОКУПКА МОНЕТ ====================
@bot.callback_query_handler(func=lambda c: c.data == "buy_coins")
def cb_buy_coins(c):
    uid = c.from_user.id
    err = check_blocked(uid)
    if err:
        bot.answer_callback_query(c.id, "⚠️ Доступ ограничен", show_alert=True)
        return
    if not is_registered(uid):
        bot.answer_callback_query(c.id, "❌ Сначала зарегистрируйтесь /start")
        return
    p = get_player(uid)
    coins = p[5] if p else 0
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, (name, coins_amount, stars, price_label) in enumerate(COIN_PACKAGES):
        kb.add(types.InlineKeyboardButton(f"⭐ {name}: {coins_amount} AC — {stars} Stars ({price_label})", callback_data=f"buy_pkg_{i}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    bot.edit_message_text(
        f"💳 <b>КУПИТЬ ActualCoin</b>\n💰 Баланс: <b>{coins} AC</b>\n\n⭐ Telegram Stars\nВыберите пакет:",
        c.message.chat.id, c.message.message_id, reply_markup=kb,
    )
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("buy_pkg_"))
def cb_buy_package(c):
    uid = c.from_user.id
    pkg_idx = int(c.data.split("buy_pkg_")[1])
    if pkg_idx < 0 or pkg_idx >= len(COIN_PACKAGES):
        bot.answer_callback_query(c.id, "❌ Пакет не найден")
        return
    name, coins_amount, stars, price_label = COIN_PACKAGES[pkg_idx]
    bot.answer_callback_query(c.id)
    try:
        bot.send_invoice(
            chat_id=uid,
            title=f"💰 {coins_amount} ActualCoin",
            description=f"Пакет «{name}»: {coins_amount} AC для ACTUAL FACEIT",
            invoice_payload=f"coins_{pkg_idx}_{uid}",
            provider_token="",
            currency="XTR",
            prices=[types.LabeledPrice(label=f"{coins_amount} AC", amount=stars)],
            start_parameter=f"buy_coins_{pkg_idx}",
        )
    except Exception as e:
        bot.send_message(uid, f"❌ Ошибка создания счёта: {e}")


@bot.pre_checkout_query_handler(func=lambda q: True)
def pre_checkout(query):
    bot.answer_pre_checkout_query(query.id, ok=True)


@bot.message_handler(content_types=["successful_payment"])
def successful_payment(msg):
    uid = msg.from_user.id
    payload = msg.successful_payment.invoice_payload
    try:
        _, pkg_idx_str, _ = payload.split("_", 2)
        name, coins_amount, stars, _ = COIN_PACKAGES[int(pkg_idx_str)]
        add_coins_to_player(uid, coins_amount)
        p = get_player(uid)
        bot.send_message(uid, f"✅ <b>Оплата прошла!</b>\n💰 Начислено: <b>{coins_amount} AC</b>\n💳 Баланс: <b>{p[5] if p else '?'} AC</b>")
        if ADMIN_ID:
            bot.send_message(ADMIN_ID, f"💳 Покупка!\nПользователь: {uid}\nПакет: {name} ({coins_amount} AC)\nОплачено: {stars} Stars")
    except Exception as e:
        bot.send_message(uid, f"✅ Оплата получена, монеты будут начислены вручную. Ошибка: {e}")


# ==================== РЕДАКТИРОВАНИЕ СТАТЫ ====================
STAT_FIELDS = {
    "kills":   ("kills",   "🔫 Убийства"),
    "deaths":  ("deaths",  "💀 Смерти"),
    "assists": ("assists", "🤝 Ассисты"),
    "wins":    ("wins",    "🏆 Победы"),
    "losses":  ("losses",  "❌ Поражения"),
    "coins":   ("coins",   "💰 Монеты"),
    "elo":     ("elo",     "📊 ELO"),
}

@bot.callback_query_handler(func=lambda c: c.data.startswith("editstat_"))
def cb_editstat_pick(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    parts = c.data.split("_")
    field = parts[1]
    target_id = int(parts[2])
    p = get_player(target_id)
    if not p:
        bot.answer_callback_query(c.id, "❌ Игрок не найден")
        return
    editstat_flow[uid] = {"field": field, "target_id": target_id}
    bot.answer_callback_query(c.id)
    _, label = STAT_FIELDS.get(field, (field, field))
    bot.send_message(uid, f"✏️ Введите новое значение для <b>{label}</b> игрока <b>{p[1]}</b>:", parse_mode="HTML")

@bot.message_handler(func=lambda m: m.from_user.id in editstat_flow and m.text is not None)
def handle_editstat_flow(msg):
    uid = msg.from_user.id
    if not is_admin(uid):
        return
    data = editstat_flow.pop(uid)
    field = data["field"]
    target_id = data["target_id"]
    p = get_player(target_id)
    if not p:
        bot.send_message(uid, "❌ Игрок не найден")
        return
    try:
        value = int(msg.text.strip())
        if value < 0:
            raise ValueError
    except ValueError:
        bot.send_message(uid, "❌ Введите целое неотрицательное число")
        return
    db_field, label = STAT_FIELDS.get(field, (field, field))
    conn = _db()
    cur = conn.cursor()
    cur.execute(f"UPDATE players SET {db_field}=%s WHERE user_id=%s", (value, target_id))
    conn.commit()
    conn.close()
    bot.send_message(uid, f"✅ <b>{label}</b> игрока <b>{p[1]}</b> изменено на <b>{value}</b>!", parse_mode="HTML")
    try:
        bot.send_message(target_id, f"✏️ Администратор изменил вашу статистику (<b>{label}</b>: {value}).", parse_mode="HTML")
    except Exception:
        pass


# ==================== АДМИН ПАНЕЛЬ ====================
@bot.callback_query_handler(func=lambda c: c.data == "admin_panel")
def cb_admin_panel(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    players = get_all_players()
    active_count = sum(1 for l in running_matches.values() if l.get("status") == "active")
    text = (
        f"⚙️ <b>АДМИН ПАНЕЛЬ</b>\n\n"
        f"👥 Игроков: <b>{len(players)}</b>\n🎮 Лобби: <b>{len(active_lobbies)}</b>\n"
        f"🔴 Матчей: <b>{active_count}</b>\n\nВыберите действие:"
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("👥 Список игроков",       callback_data="admin_players"),
        types.InlineKeyboardButton("🔍 Поиск по нику/ID",    callback_data="admin_search"),
        types.InlineKeyboardButton("🔍 Поиск по Game ID",    callback_data="admin_search_gameid"),
        types.InlineKeyboardButton("💰 Выдать монеты",        callback_data="admin_give_coins"),
        types.InlineKeyboardButton("📊 Изменить ELO",         callback_data="admin_set_elo"),
        types.InlineKeyboardButton("✏️ Изм. ник игрока",     callback_data="admin_change_nick"),
        types.InlineKeyboardButton("🎮 Изм. Game ID игрока", callback_data="admin_change_gid"),
        types.InlineKeyboardButton("📈 Редактировать стату",  callback_data="admin_edit_stats"),
        types.InlineKeyboardButton("⚠️ Выдать варн",          callback_data="admin_warn"),
        types.InlineKeyboardButton("🔇 Мут",                  callback_data="admin_mute"),
        types.InlineKeyboardButton("🔊 Размутить",            callback_data="admin_unmute"),
        types.InlineKeyboardButton("🔎 Вызвать на проверку",  callback_data="admin_check"),
        types.InlineKeyboardButton("✅ Снять проверку",       callback_data="admin_uncheck"),
        types.InlineKeyboardButton("🚫 Бан / Разбан",         callback_data="admin_ban"),
        types.InlineKeyboardButton("👑 Выдать/Снять админку", callback_data="admin_give_admin"),
        types.InlineKeyboardButton("🎮 Роль Гейм Рег",       callback_data="admin_give_game_reg"),
        types.InlineKeyboardButton("⭐ Quals доступ",         callback_data="admin_quals_access"),
        types.InlineKeyboardButton("🎁 Промокоды",            callback_data="admin_promos"),
        types.InlineKeyboardButton("🎮 Управление матчами",   callback_data="admin_matches"),
        types.InlineKeyboardButton("📋 История матчей",       callback_data="admin_match_history"),
        types.InlineKeyboardButton("📢 Рассылка",             callback_data="admin_broadcast"),
        types.InlineKeyboardButton("🔙 Назад",                callback_data="back"),
    )
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)



# ==================== ПРОМОКОДЫ (АДМИН) ====================
@bot.callback_query_handler(func=lambda c: c.data == "admin_promos")
def cb_admin_promos(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    codes = get_all_promo_codes()
    text = "🎁 <b>ПРОМОКОДЫ</b>\n\n"
    if codes:
        for code, rtype, rval, max_uses, uses, is_active in codes:
            status = "✅" if is_active else "❌"
            max_str = f"/{max_uses}" if max_uses > 0 else "/∞"
            rtype_names = {"coins": f"💰 {rval} AC", "premium": "👑 Premium", "quals": "⭐ Quals"}
            text += f"{status} <code>{code}</code> — {rtype_names.get(rtype, rtype)} | {uses}{max_str} исп.\n"
    else:
        text += "Промокодов нет."
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("➕ Создать промокод",        callback_data="admin_promo_create"),
        types.InlineKeyboardButton("❌ Деактивировать промокод", callback_data="admin_promo_deactivate"),
        types.InlineKeyboardButton("🔙 Назад",                   callback_data="admin_panel"),
    )
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "admin_promo_create")
def cb_admin_promo_create(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    promo_admin_flow[uid] = {"step": "code"}
    bot.answer_callback_query(c.id)
    bot.send_message(
        uid,
        "🎁 <b>Создание промокода</b>\n\n"
        "<b>Шаг 1/4</b> — Введите код промокода (только буквы и цифры):\n"
        "Пример: <code>SUMMER2024</code>",
        parse_mode="HTML",
    )


@bot.callback_query_handler(func=lambda c: c.data == "admin_promo_deactivate")
def cb_admin_promo_deactivate(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    promo_admin_flow[uid] = {"step": "deactivate"}
    bot.answer_callback_query(c.id)
    bot.send_message(uid, "❌ Введите код промокода для деактивации:")


@bot.callback_query_handler(func=lambda c: c.data.startswith("promo_reward_"))
def cb_promo_reward_type(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    if uid not in promo_admin_flow:
        bot.answer_callback_query(c.id, "❌ Сессия не найдена")
        return
    reward_type = c.data.split("promo_reward_")[1]
    promo_admin_flow[uid]["reward_type"] = reward_type
    promo_admin_flow[uid]["step"] = "value"
    bot.answer_callback_query(c.id)
    try:
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
    except Exception:
        pass
    if reward_type == "coins":
        bot.send_message(uid, "<b>Шаг 3/4</b> — Сколько монет выдавать?", parse_mode="HTML")
    elif reward_type == "premium":
        promo_admin_flow[uid]["reward_value"] = 0
        promo_admin_flow[uid]["step"] = "max_uses"
        bot.send_message(uid, "<b>Шаг 4/4</b> — Сколько раз можно использовать? (0 = неограничено)", parse_mode="HTML")
    elif reward_type == "quals":
        promo_admin_flow[uid]["reward_value"] = 0
        promo_admin_flow[uid]["step"] = "max_uses"
        bot.send_message(uid, "<b>Шаг 4/4</b> — Сколько раз можно использовать? (0 = неограничено)", parse_mode="HTML")


@bot.message_handler(func=lambda m: m.from_user.id in promo_admin_flow and m.text is not None)
def handle_promo_admin_flow(msg):
    uid = msg.from_user.id
    if not is_admin(uid):
        return
    data = promo_admin_flow.get(uid, {})
    step = data.get("step")
    text = msg.text.strip()

    if step == "code":
        if not re.match(r'^[A-Za-z0-9]+$', text):
            bot.send_message(uid, "❌ Только буквы и цифры. Попробуйте снова:")
            return
        data["code"] = text.upper()
        data["step"] = "reward_type"
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(
            types.InlineKeyboardButton("💰 Монеты",  callback_data="promo_reward_coins"),
            types.InlineKeyboardButton("👑 Premium", callback_data="promo_reward_premium"),
            types.InlineKeyboardButton("⭐ Quals",   callback_data="promo_reward_quals"),
        )
        bot.send_message(uid, "<b>Шаг 2/4</b> — Выберите тип награды:", parse_mode="HTML", reply_markup=kb)

    elif step == "value":
        try:
            value = int(text)
        except ValueError:
            bot.send_message(uid, "❌ Введите число")
            return
        data["reward_value"] = value
        data["step"] = "max_uses"
        bot.send_message(uid, "<b>Шаг 4/4</b> — Сколько раз можно использовать? (0 = неограничено)", parse_mode="HTML")

    elif step == "max_uses":
        try:
            max_uses = int(text)
        except ValueError:
            bot.send_message(uid, "❌ Введите число")
            return
        code = data["code"]
        reward_type = data["reward_type"]
        reward_value = data.get("reward_value", 0)
        promo_admin_flow.pop(uid, None)
        ok = create_promo_code(code, reward_type, reward_value, max_uses)
        if ok:
            max_str = f"{max_uses}" if max_uses > 0 else "неограничено"
            rtype_names = {"coins": f"💰 {reward_value} AC", "premium": "👑 Premium", "quals": "⭐ Quals"}
            bot.send_message(
                uid,
                f"✅ <b>Промокод создан!</b>\n\n"
                f"Код: <code>{code}</code>\n"
                f"Награда: {rtype_names.get(reward_type, reward_type)}\n"
                f"Использований: {max_str}",
                parse_mode="HTML",
            )
        else:
            bot.send_message(uid, f"❌ Промокод <code>{code}</code> уже существует!", parse_mode="HTML")

    elif step == "deactivate":
        promo_admin_flow.pop(uid, None)
        deactivate_promo_code(text)
        bot.send_message(uid, f"✅ Промокод <code>{text.upper()}</code> деактивирован.", parse_mode="HTML")


# ==================== УПРАВЛЕНИЕ МАТЧАМИ (АДМИН) ====================
@bot.callback_query_handler(func=lambda c: c.data == "admin_matches")
def cb_admin_matches(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    active = [(lid, l) for lid, l in running_matches.items() if l.get("status") == "active"]
    if not active:
        text = "🎮 <b>Управление матчами</b>\n\nАктивных матчей нет."
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
        bot.answer_callback_query(c.id)
        return
    text = "🎮 <b>Активные матчи</b>\n\n"
    kb = types.InlineKeyboardMarkup(row_width=1)
    for lid, l in active:
        mid = l.get("match_id", "?")
        sc = l.get("screenshots_count", 0)
        text += f"• Match #{mid} — {lid} | 📸{sc}\n"
        kb.add(types.InlineKeyboardButton(f"⚙️ Match #{mid}", callback_data=f"admin_match_manage_{lid}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_match_manage_"))
def cb_admin_match_manage(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    lobby_id = c.data[len("admin_match_manage_"):]
    lobby = running_matches.get(lobby_id)
    if not lobby:
        bot.answer_callback_query(c.id, "❌ Матч не найден")
        return
    match_id = lobby.get("match_id", "?")
    sc = lobby.get("screenshots_count", 0)
    text = (
        f"⚙️ <b>Match #{match_id}</b>\n🏷 {lobby.get('league','').upper()}/{lobby.get('device','').upper()}\n"
        f"🗺 {lobby.get('map_name','?')}\n📸 {sc}"
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🔄 Перерегать",  callback_data=f"reregister_match|{lobby_id}"),
        types.InlineKeyboardButton("❌ Отменить",    callback_data=f"cancel_match|{lobby_id}"),
        types.InlineKeyboardButton("🔙 Назад",       callback_data="admin_matches"),
    )
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "admin_players")
def cb_admin_players(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    players = get_all_players()
    text = "👥 <b>СПИСОК ИГРОКОВ</b>\n\n"
    for p in players[:20]:
        uid2, name, elo, wins, losses, kills, deaths, coins, banned, warns = p
        ban_mark = " 🚫" if banned else ""
        warn_mark = f" ⚠️{warns}" if warns > 0 else ""
        prem = " 👑" if has_active_premium(uid2) else ""
        text += f"• <b>{name}</b>{prem}{ban_mark}{warn_mark} | ELO: {elo} | {wins}W/{losses}L\n"
    if len(players) > 20:
        text += f"\n<i>...и ещё {len(players)-20}</i>"
    if not players:
        text += "Игроков нет."
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data == "admin_match_history")
def cb_admin_match_history(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    matches = get_match_history(10)
    text = "📋 <b>ИСТОРИЯ МАТЧЕЙ</b>\n\nМатчей нет." if not matches else "📋 <b>ИСТОРИЯ МАТЧЕЙ</b>\n\n"
    for row in matches:
        match_id, league, device, map_name, winner, score_w, score_l, finished_at = row
        dt = datetime.datetime.fromtimestamp(finished_at).strftime("%d.%m %H:%M") if finished_at else "?"
        winner_str = "💙 CT" if winner == "ct" else "🧡 T"
        text += f"🔢 <b>Match #{match_id}</b> | {dt}\n   {league.upper()}/{device.upper()} | {map_name}\n   {winner_str} | {score_w}:{score_l}\n\n"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="admin_panel"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data in [
    "admin_search", "admin_search_gameid", "admin_give_coins", "admin_set_elo",
    "admin_warn", "admin_ban", "admin_broadcast", "admin_give_admin",
    "admin_quals_access", "admin_give_game_reg", "admin_mute", "admin_unmute",
    "admin_check", "admin_uncheck", "admin_change_nick", "admin_change_gid",
    "admin_edit_stats",
])
def cb_admin_action(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    action = c.data.split("admin_")[1]
    prompts = {
        "search":         "🔍 Введите Telegram ID или никнейм:",
        "search_gameid":  "🔍 Введите Game ID игрока:",
        "give_coins":     "💰 Формат: <code>USER_ID КОЛИЧЕСТВО</code>",
        "set_elo":        "📊 Формат: <code>USER_ID НОВОЕ_ELO</code>",
        "change_nick":    "✏️ Введите Telegram ID для смены ника:",
        "change_gid":     "🎮 Введите Telegram ID для смены Game ID:",
        "warn":           "⚠️ Введите Telegram ID или никнейм:",
        "ban":            "🚫 Введите Telegram ID или никнейм:",
        "broadcast":      "📢 Введите текст рассылки:",
        "give_admin":     "👑 Введите Telegram ID или никнейм:",
        "quals_access":   "⭐ Введите Telegram ID или никнейм:",
        "give_game_reg":  "🎮 Введите Telegram ID или никнейм:",
        "mute":           "🔇 Введите Telegram ID или никнейм:",
        "unmute":         "🔊 Введите Telegram ID или никнейм:",
        "check":          "🔎 Введите Telegram ID или никнейм:",
        "uncheck":        "✅ Введите Telegram ID или никнейм:",
        "edit_stats":     "📈 Введите Telegram ID или никнейм игрока:",
    }
    prompt = prompts.get(action, "Введите данные:")
    admin_action[uid] = action
    bot.answer_callback_query(c.id)
    bot.send_message(uid, prompt, parse_mode="HTML")


@bot.message_handler(func=lambda m: m.from_user.id in admin_action and m.text is not None)
def handle_admin_action(msg):
    uid = msg.from_user.id
    if not is_admin(uid):
        return
    action = admin_action.pop(uid)
    text = msg.text.strip()

    def find_player_by_input(inp):
        if inp.isdigit():
            return get_player(int(inp))
        conn2 = _db()
        cur2 = conn2.cursor()
        cur2.execute("SELECT * FROM players WHERE username=%s AND is_bot=0", (inp,))
        row = cur2.fetchone()
        conn2.close()
        return row

    if action == "search":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        games = p[6] + p[7]
        winrate = round(p[6] / games * 100, 1) if games > 0 else 0
        kd = round(p[8] / p[9], 2) if p[9] > 0 else p[8]
        tg_u = p[22] if len(p) > 22 else ""
        resp = (
            f"👤 <b>{p[1]}</b>\n🆔 TG: <code>{p[0]}</code>\n"
            f"🐦 @{tg_u}\n🎮 Game ID: <code>{p[2]}</code>\n📱 {p[3]}\n"
            f"📊 ELO: {p[4]} | 💰 {p[5]} AC\n"
            f"🏆 {p[6]}W/{p[7]}L ({winrate}%) | K/D: {kd}\n"
            f"⚠️ Варны: {p[15] if len(p)>15 else 0} | 🚫 Бан: {'Да' if p[14] else 'Нет'}\n"
            f"👑 Админ: {'Да' if p[11] else 'Нет'} | 🔇 Мут: {'Да' if p[18] else 'Нет'}"
        )
        kb = types.InlineKeyboardMarkup(row_width=2)
        target_id = p[0]
        kb.add(
            types.InlineKeyboardButton("🚫 Бан/Разбан",   callback_data=f"admin_do_ban_{target_id}"),
            types.InlineKeyboardButton("⚠️ Варн",          callback_data=f"admin_do_warn_{target_id}"),
            types.InlineKeyboardButton("🔇 Мут",           callback_data=f"admin_do_mute_{target_id}"),
            types.InlineKeyboardButton("🔊 Размутить",     callback_data=f"admin_do_unmute_{target_id}"),
            types.InlineKeyboardButton("🔎 Проверка",      callback_data=f"admin_do_check_{target_id}"),
            types.InlineKeyboardButton("✅ Снять проверку",callback_data=f"admin_do_uncheck_{target_id}"),
            types.InlineKeyboardButton("👑 Дать/Снять адм",callback_data=f"admin_do_give_admin_{target_id}"),
            types.InlineKeyboardButton("⭐ Quals",          callback_data=f"admin_do_quals_{target_id}"),
        )
        for field_key, (db_f, label) in STAT_FIELDS.items():
            kb.add(types.InlineKeyboardButton(f"✏️ {label}", callback_data=f"editstat_{field_key}_{target_id}"))
        bot.send_message(uid, resp, parse_mode="HTML", reply_markup=kb)

    elif action == "search_gameid":
        p = get_player_by_game_id(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        bot.send_message(uid, f"👤 <b>{p[1]}</b>\n🆔 TG: <code>{p[0]}</code>\n🎮 Game ID: <code>{p[2]}</code>\n📊 ELO: {p[4]}", parse_mode="HTML")

    elif action == "give_coins":
        parts = text.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].lstrip("-").isdigit():
            bot.send_message(uid, "❌ Формат: USER_ID КОЛИЧЕСТВО")
            return
        target_id, amount = int(parts[0]), int(parts[1])
        add_coins_to_player(target_id, amount)
        bot.send_message(uid, f"✅ Выдано {amount} AC игроку <code>{target_id}</code>", parse_mode="HTML")
        try:
            bot.send_message(target_id, f"💰 Вам начислено <b>{amount} AC</b> администратором!", parse_mode="HTML")
        except Exception:
            pass

    elif action == "set_elo":
        parts = text.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            bot.send_message(uid, "❌ Формат: USER_ID НОВОЕ_ELO")
            return
        target_id, new_elo = int(parts[0]), int(parts[1])
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE players SET elo=%s WHERE user_id=%s", (new_elo, target_id))
        conn.commit(); conn.close()
        bot.send_message(uid, f"✅ ELO игрока <code>{target_id}</code> изменено на {new_elo}", parse_mode="HTML")

    elif action == "change_nick":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        change_flow[uid] = {"field": "admin_nick", "target_id": p[0]}
        bot.send_message(uid, f"✏️ Введите новый никнейм для <b>{p[1]}</b>:", parse_mode="HTML")

    elif action == "change_gid":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        change_flow[uid] = {"field": "admin_id", "target_id": p[0]}
        bot.send_message(uid, f"🎮 Введите новый Game ID для <b>{p[1]}</b>:", parse_mode="HTML")

    elif action == "warn":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        warns = add_warn_to_player(p[0])
        bot.send_message(uid, f"⚠️ Игроку <b>{p[1]}</b> выдан варн. Итого: {warns}/3", parse_mode="HTML")
        try:
            bot.send_message(p[0], f"⚠️ Вам выдан варн от администратора. Всего: {warns}/3")
        except Exception:
            pass

    elif action == "ban":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        new_ban = 0 if p[14] else 1
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE players SET is_banned=%s WHERE user_id=%s", (new_ban, p[0]))
        conn.commit(); conn.close()
        status = "заблокирован" if new_ban else "разблокирован"
        bot.send_message(uid, f"✅ Игрок <b>{p[1]}</b> {status}", parse_mode="HTML")
        try:
            bot.send_message(p[0], f"{'🚫 Вы заблокированы в боте.' if new_ban else '✅ Вы разблокированы.'}")
        except Exception:
            pass

    elif action == "broadcast":
        players = get_all_players()
        sent_count = 0
        for row in players:
            try:
                bot.send_message(row[0], f"📢 <b>Сообщение от администрации:</b>\n\n{text}", parse_mode="HTML")
                sent_count += 1
                time.sleep(0.05)
            except Exception:
                pass
        bot.send_message(uid, f"✅ Рассылка отправлена {sent_count}/{len(players)} игрокам")

    elif action == "give_admin":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        new_val = 0 if p[11] else 1
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE players SET is_admin=%s WHERE user_id=%s", (new_val, p[0]))
        conn.commit(); conn.close()
        status = "выдана" if new_val else "снята"
        bot.send_message(uid, f"✅ Админка {status} игроку <b>{p[1]}</b>", parse_mode="HTML")

    elif action == "quals_access":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        cur_val = p[16] if len(p) > 16 else 0
        new_val = 0 if cur_val else 1
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE players SET quals_access=%s WHERE user_id=%s", (new_val, p[0]))
        conn.commit(); conn.close()
        status = "выдан" if new_val else "снят"
        bot.send_message(uid, f"✅ Quals доступ {status} игроку <b>{p[1]}</b>", parse_mode="HTML")
        try:
            bot.send_message(p[0], f"{'⭐ Вам выдан доступ к QUALS!' if new_val else '❌ Ваш доступ к QUALS снят.'}")
        except Exception:
            pass

    elif action == "give_game_reg":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        cur_val = p[17] if len(p) > 17 else 0
        new_val = 0 if cur_val else 1
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE players SET is_game_reg=%s WHERE user_id=%s", (new_val, p[0]))
        conn.commit(); conn.close()
        status = "выдана" if new_val else "снята"
        bot.send_message(uid, f"✅ Роль Гейм Рег {status} игроку <b>{p[1]}</b>", parse_mode="HTML")

    elif action == "mute":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        until = apply_mute(p[0], hours=2)
        dt = datetime.datetime.fromtimestamp(until).strftime("%H:%M %d.%m")
        bot.send_message(uid, f"🔇 Игрок <b>{p[1]}</b> замучен до {dt}", parse_mode="HTML")
        try:
            bot.send_message(p[0], f"🔇 Вы замучены администратором до {dt}.")
        except Exception:
            pass

    elif action == "unmute":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE players SET is_muted=0, mute_until=0 WHERE user_id=%s", (p[0],))
        conn.commit(); conn.close()
        bot.send_message(uid, f"🔊 Мут снят с игрока <b>{p[1]}</b>", parse_mode="HTML")
        try:
            bot.send_message(p[0], "🔊 Ваш мут снят администратором.")
        except Exception:
            pass

    elif action == "check":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE players SET is_on_check=1, check_admin_id=%s WHERE user_id=%s", (uid, p[0]))
        conn.commit(); conn.close()
        bot.send_message(uid, f"🔎 Игрок <b>{p[1]}</b> вызван на проверку", parse_mode="HTML")
        admin_p = get_player(uid)
        tg_u = admin_p[22] if admin_p and len(admin_p) > 22 else ""
        try:
            bot.send_message(
                p[0],
                f"⚠️ <b>Вас вызвал на проверку {'@'+tg_u if tg_u else 'администратор'}!</b>\n\nОбратитесь к администратору.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    elif action == "uncheck":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        conn = _db(); cur = conn.cursor()
        cur.execute("UPDATE players SET is_on_check=0, check_admin_id=0 WHERE user_id=%s", (p[0],))
        conn.commit(); conn.close()
        bot.send_message(uid, f"✅ Проверка снята с игрока <b>{p[1]}</b>", parse_mode="HTML")
        try:
            bot.send_message(p[0], "✅ Проверка снята. Доступ к боту восстановлен.")
        except Exception:
            pass

    elif action == "edit_stats":
        p = find_player_by_input(text)
        if not p:
            bot.send_message(uid, "❌ Игрок не найден")
            return
        kb = types.InlineKeyboardMarkup(row_width=2)
        for field_key, (db_f, label) in STAT_FIELDS.items():
            kb.add(types.InlineKeyboardButton(f"✏️ {label}", callback_data=f"editstat_{field_key}_{p[0]}"))
        bot.send_message(uid, f"📈 Редактирование статы <b>{p[1]}</b>:", parse_mode="HTML", reply_markup=kb)


# ==================== БЫСТРЫЕ ДЕЙСТВИЯ (кнопки из поиска) ====================
@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_do_"))
def cb_admin_do_action(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    parts = c.data.split("_")
    action = "_".join(parts[2:-1])
    target_id = int(parts[-1])
    p = get_player(target_id)
    if not p:
        bot.answer_callback_query(c.id, "❌ Игрок не найден")
        return
    conn = _db()
    cur = conn.cursor()
    msg_text = ""
    if action == "ban":
        new_val = 0 if p[14] else 1
        cur.execute("UPDATE players SET is_banned=%s WHERE user_id=%s", (new_val, target_id))
        msg_text = f"{'🚫 Заблокирован' if new_val else '✅ Разблокирован'}: {p[1]}"
    elif action == "warn":
        conn.close()
        warns = add_warn_to_player(target_id)
        bot.answer_callback_query(c.id, f"⚠️ Варн {warns}/3 выдан {p[1]}", show_alert=True)
        return
    elif action == "mute":
        conn.close()
        until = apply_mute(target_id, hours=2)
        dt = datetime.datetime.fromtimestamp(until).strftime("%H:%M")
        bot.answer_callback_query(c.id, f"🔇 {p[1]} замучен до {dt}", show_alert=True)
        return
    elif action == "unmute":
        cur.execute("UPDATE players SET is_muted=0, mute_until=0 WHERE user_id=%s", (target_id,))
        msg_text = f"🔊 Мут снят: {p[1]}"
    elif action == "check":
        cur.execute("UPDATE players SET is_on_check=1, check_admin_id=%s WHERE user_id=%s", (uid, target_id))
        msg_text = f"🔎 На проверке: {p[1]}"
    elif action == "uncheck":
        cur.execute("UPDATE players SET is_on_check=0, check_admin_id=0 WHERE user_id=%s", (target_id,))
        msg_text = f"✅ Проверка снята: {p[1]}"
    elif action == "give_admin":
        new_val = 0 if p[11] else 1
        cur.execute("UPDATE players SET is_admin=%s WHERE user_id=%s", (new_val, target_id))
        msg_text = f"{'👑 Админка выдана' if new_val else '❌ Админка снята'}: {p[1]}"
    elif action == "quals":
        cur_val = p[16] if len(p) > 16 else 0
        new_val = 0 if cur_val else 1
        cur.execute("UPDATE players SET quals_access=%s WHERE user_id=%s", (new_val, target_id))
        msg_text = f"{'⭐ Quals выдан' if new_val else '❌ Quals снят'}: {p[1]}"
    else:
        conn.close()
        bot.answer_callback_query(c.id, "❌ Неизвестное действие")
        return
    conn.commit()
    conn.close()
    bot.answer_callback_query(c.id, msg_text, show_alert=True)


# ==================== ДОБАВЛЕНИЕ БОТОВ (АДМИН) ====================
@bot.callback_query_handler(func=lambda c: c.data == "add_bots_admin")
def cb_add_bots(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    kb = types.InlineKeyboardMarkup(row_width=1)
    for lobby_id, lobby in active_lobbies.items():
        if lobby.get("status") == "waiting" and len(lobby["players"]) < 10:
            slots = 10 - len(lobby["players"])
            kb.add(types.InlineKeyboardButton(
                f"Лобби {lobby_id} ({len(lobby['players'])}/10) — добавить {slots} ботов",
                callback_data=f"fill_bots_{lobby_id}",
            ))
    if not kb.keyboard:
        bot.answer_callback_query(c.id, "❌ Нет доступных лобби", show_alert=True)
        return
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    bot.edit_message_text("🤖 Выберите лобби для заполнения ботами:", c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("fill_bots_"))
def cb_fill_bots(c):
    uid = c.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    lobby_id = c.data[len("fill_bots_"):]
    lobby = active_lobbies.get(lobby_id)
    if not lobby or lobby["status"] != "waiting":
        bot.answer_callback_query(c.id, "❌ Лобби недоступно")
        return
    bots = get_bots()
    needed = 10 - len(lobby["players"])
    available_bots = [b for b in bots if b[0] not in lobby["players"]]
    fill = random.sample(available_bots, min(needed, len(available_bots)))
    for b_id, _ in fill:
        lobby["players"].append(b_id)
    bot.answer_callback_query(c.id, f"✅ Добавлено {len(fill)} ботов")
    if len(lobby["players"]) >= 10:
        start_accept_phase(lobby_id)
    else:
        broadcast_lobby_update(lobby_id)


# ==================== GAME REG ПАНЕЛЬ ====================
@bot.callback_query_handler(func=lambda c: c.data == "game_reg_panel")
def cb_game_reg_panel(c):
    uid = c.from_user.id
    if not is_game_reg_check(uid):
        bot.answer_callback_query(c.id, "❌ Нет доступа")
        return
    active = [(lid, l) for lid, l in running_matches.items() if l.get("status") == "active"]
    if not active:
        text = "📋 <b>Регистрация матчей</b>\n\nАктивных матчей нет."
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
        bot.answer_callback_query(c.id)
        return
    text = "📋 <b>Активные матчи</b>\n\n"
    kb = types.InlineKeyboardMarkup(row_width=1)
    for lid, l in active:
        mid = l.get("match_id", "?")
        sc = l.get("screenshots_count", 0)
        text += f"• Match #{mid} — {lid} | 📸{sc}\n"
        kb.add(types.InlineKeyboardButton(f"📝 Зарегистрировать Match #{mid}", callback_data=f"reg_match|{lid}"))
    kb.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


# ==================== ЗАПУСК ====================
if __name__ == "__main__":
    print("🚀 Инициализация БД...")
    init_db()
    print("✅ Бот запущен!")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
