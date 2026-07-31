import asyncio
import sqlite3
import os
import io
import datetime
import random
import logging
import re as _re_ocr
import difflib as _difflib
from typing import Optional

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, MessageEntity, WebAppInfo,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters, ApplicationHandlerStop,
)
try:
    from card_renderer import (
        generate_profile_card, generate_tasks_card,
        generate_shop_card, generate_leaderboard_card,
    )
    CARDS_ENABLED = True
except Exception as _card_import_err:
    # Playwright/card_renderer недоступны на этом хосте — работаем без картинок,
    # все карточки будут отправляться текстом (см. фоллбэки в обработчиках).
    print(f"[card_renderer] Рендер карточек отключён: {_card_import_err}")
    CARDS_ENABLED = False

    async def generate_profile_card(*args, **kwargs):
        raise RuntimeError("card_renderer недоступен (Playwright не установлен)")

    async def generate_tasks_card(*args, **kwargs):
        raise RuntimeError("card_renderer недоступен (Playwright не установлен)")

    async def generate_shop_card(*args, **kwargs):
        raise RuntimeError("card_renderer недоступен (Playwright не установлен)")

    async def generate_leaderboard_card(*args, **kwargs):
        raise RuntimeError("card_renderer недоступен (Playwright не установлен)")


import re as _re_html

def _parse_msg(html: str):
    """Parse HTML with <tg-emoji> / <b> / <code> / <i> into (plain_text, entities)."""
    from telegram import MessageEntity
    TAGS = {'b':'bold','strong':'bold','i':'italic','em':'italic',
            'code':'code','pre':'pre','s':'strikethrough','u':'underline'}
    tag_pat = '|'.join(TAGS)
    tok = _re_html.compile(
        r'<tg-emoji emoji-id=["\'](\d+)["\']>(.*?)</tg-emoji>'
        r'|<(/)?(' + tag_pat + r')(?:\s[^>]*)?>',
        _re_html.DOTALL)
    entities, stack, plain = [], [], []
    pos = last = 0
    for m in tok.finditer(html):
        before = html[last:m.start()].replace('&lt;','<').replace('&gt;','>').replace('&amp;','&').replace('&quot;','"')
        plain.append(before); pos += len(before.encode('utf-16-le'))//2; last = m.end()
        if m.group(1) is not None:
            eid, ec = m.group(1), m.group(2)
            el = len(ec.encode('utf-16-le'))//2
            entities.append(MessageEntity(type='custom_emoji', offset=pos, length=el, custom_emoji_id=eid))
            plain.append(ec); pos += el
        else:
            closing = m.group(3)=='/'
            et = TAGS.get((m.group(4) or '').lower())
            if et:
                if not closing: stack.append((et, pos))
                else:
                    for j in range(len(stack)-1,-1,-1):
                        if stack[j][0]==et:
                            _, s=stack.pop(j)
                            if pos>s: entities.append(MessageEntity(type=et,offset=s,length=pos-s))
                            break
    after = html[last:].replace('&lt;','<').replace('&gt;','>').replace('&amp;','&').replace('&quot;','"')
    plain.append(after)
    return ''.join(plain), entities or None


def _esc(text: str) -> str:
    """Экранирует пользовательский ввод для вставки в HTML-шаблоны (_parse_msg).
    Заменяет символы, которые _parse_msg мог бы интерпретировать как разметку."""
    return (str(text)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;'))


async def _reply_html(obj, html, **kw):
    t, e = _parse_msg(html); return await obj.reply_text(t, entities=e, **kw)

async def _send_html(bot, chat_id, html, **kw):
    t, e = _parse_msg(html); return await bot.send_message(chat_id=chat_id, text=t, entities=e, **kw)

import re as _re_tge
def _strip_tg_emoji(html: str) -> str:
    """Убирает <tg-emoji> теги, оставляя только fallback-символ. Нужно для сообщений где бот не может слать custom emoji."""
    return _re_tge.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', html)

async def _edit_html(query, html, **kw):
    t, e = _parse_msg(html); return await query.edit_message_text(t, entities=e, **kw)


# ══════════════════════════════════════════════════════════════════════════════
#  РЕЖИМЫ
# ══════════════════════════════════════════════════════════════════════════════

MODES: dict[str, dict] = {
    '5v5': {
        'team_size':    5,
        'match_size':   10,
        'votes_to_win': 6,
        'pick_order':   [1, 2, 2, 1, 1, 2, 2, 1],
        'calib_games':  10,
    },
    '3v3': {
        'team_size':    3,
        'match_size':   6,
        'votes_to_win': 4,
        'pick_order':   [1, 2, 1, 2],
        'calib_games':  5,
    },
    '2v2': {
        'team_size':    2,
        'match_size':   4,
        'votes_to_win': 3,
        'pick_order':   [1, 2],
        'calib_games':  5,
    },
}
# 'label' и 'emoji' задаются ниже, после блока premium-эмодзи (E_SWORD/E_SHIELD/E_HANDSHAKE) —
# сюда статичные ⚔️/🛡/🤝 больше не кладём, чтобы не дублировать уже стоящие premium-иконки.

# ══════════════════════════════════════════════════════════════════════════════
#  СОСТОЯНИЕ
# ══════════════════════════════════════════════════════════════════════════════

# mode → lobby_id → [uid, ...]
lobby_queues: dict[str, dict[int, list[int]]] = {
    m: {i: [] for i in range(1, 6)} for m in MODES
}

draft_state:    dict[int, dict] = {}
map_vote_state: dict[int, dict] = {}
pending_kd:     dict[int, set]  = {}
confirm_state:  dict[int, dict] = {}

# Пати: creator_uid → [member_uid, ...]
_parties: dict[int, list[int]] = {}

def _get_party_of(uid: int) -> tuple[int, list[int]] | None:
    """Возвращает (creator_uid, members) если uid состоит в пати, иначе None."""
    if uid in _parties:
        return uid, _parties[uid]
    for creator, members in _parties.items():
        if uid in members:
            return creator, members
    return None

# Локи для защиты от гонок при одновременном ручном пике и авто-пике по таймауту
_draft_locks: dict[int, asyncio.Lock] = {}

def _get_draft_lock(match_id: int) -> asyncio.Lock:
    if match_id not in _draft_locks:
        _draft_locks[match_id] = asyncio.Lock()
    return _draft_locks[match_id]

def _release_draft_lock(match_id: int):
    """Убираем лок матча после завершения драфта."""
    _draft_locks.pop(match_id, None)
# session_id → {uid → (chat_id, msg_id)}
_confirm_msg_info: dict[int, dict[int, tuple[int, int]]] = {}
_confirm_counter: int = 0
_bot_counter:     int = 0

# uid → (chat_id, msg_id, lobby_id, mode)
_lobby_msg_info:    dict[int, tuple[int, int, int, str]] = {}
# uid → (chat_id, msg_id)
_lobby_selector_msg: dict[int, tuple[int, int]]          = {}
# uid → время входа в очередь
_queue_join_time:   dict[int, datetime.datetime]         = {}

AFK_TIMEOUT_MIN     = 30
CONFIRM_TIMEOUT_SEC = 60
PICK_TIMEOUT_SEC    = 30

MAPS      = ["Sandstone", "Rust", "Province", "Zone 9"]
MAPS_EMOJI = {
    "Sandstone": "5258314450309500181",
    "Province":  "5260231534731871969",
    "Rust":      "5260245609339700014",
    "Zone 9":    "5258180533229207771",
}
RULES_URL = "https://telegra.ph/Pravila-Enhanced-Faceit-5v5-05-30"

# Канал, подписка на который обязательна перед регистрацией в /start
SUB_CHANNEL_USERNAME = "@MoonFaceitNew"
SUB_CHANNEL_URL      = "https://t.me/MoonFaceitNew"

# Публичный https-адрес собранного Mini App (см. telegram-miniapp/ в архиве).
# Пока переменная окружения не задана, кнопка Mini App в меню просто не
# показывается — бот продолжает работать как обычно.
MINI_APP_URL = os.environ.get('MINI_APP_URL', '').strip()


def is_bot(uid: int) -> bool:
    return uid < 0


async def _is_channel_subscribed(bot, user_id: int) -> bool:
    """Проверяет подписку пользователя на SUB_CHANNEL_USERNAME.
    Подписка обязательна, поэтому при сбое проверки (бот не админ канала,
    канал не найден, таймаут Telegram и т.п.) считаем, что пользователь
    НЕ подписан — иначе достаточно было бы один раз словить ошибку API,
    чтобы обойти требование подписки. Ошибка при этом громко логируется,
    чтобы админ бота заметил и починил права бота в канале."""
    try:
        member = await bot.get_chat_member(SUB_CHANNEL_USERNAME, user_id)
        return member.status not in ("left", "kicked")
    except Exception as e:
        print(f"[sub_check] Не удалось проверить подписку uid={user_id}, считаем неподписанным: {e}")
        return False


def _sub_gate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Перейти в канал", url=SUB_CHANNEL_URL)],
        [InlineKeyboardButton("✅ Я подписался", callback_data="check_sub")],
    ])


# ══════════════════════════════════════════════════════════════════════════════
#  ПРЕМИУМ ЭМОДЗИ — текстовые константы
# ══════════════════════════════════════════════════════════════════════════════

LEVEL_ICONS = {
    0:  '<tg-emoji emoji-id="5298928159276704110">🔒</tg-emoji>',
    1:  '<tg-emoji emoji-id="5299013852464193665">1️⃣</tg-emoji>',
    2:  '<tg-emoji emoji-id="5298657872689798239">2️⃣</tg-emoji>',
    3:  '<tg-emoji emoji-id="5298564770683722095">3️⃣</tg-emoji>',
    4:  '<tg-emoji emoji-id="5298699284764467308">4️⃣</tg-emoji>',
    5:  '<tg-emoji emoji-id="5299005296889337460">5️⃣</tg-emoji>',
    6:  '<tg-emoji emoji-id="5298920149162698598">6️⃣</tg-emoji>',
    7:  '<tg-emoji emoji-id="5298649682187171763">7️⃣</tg-emoji>',
    8:  '<tg-emoji emoji-id="5298892446623636712">8️⃣</tg-emoji>',
    9:  '<tg-emoji emoji-id="5298888203195950440">9️⃣</tg-emoji>',
    10: '<tg-emoji emoji-id="5298813225951865599">🔟</tg-emoji>',
}

E_CHECK  = '<tg-emoji emoji-id="5208897394120364245">✅</tg-emoji>'
E_ADMIN  = '<tg-emoji emoji-id="5298946533146797274">✅</tg-emoji>'
E_FIRE   = '<tg-emoji emoji-id="5208585145702978799">🔥</tg-emoji>'
E_CROWN    = '<tg-emoji emoji-id="5217822164362739968">👑</tg-emoji>'
E_CIRCLE_M = '<tg-emoji emoji-id="5314508432315817301">Ⓜ️</tg-emoji>'
E_TROPHY = '<tg-emoji emoji-id="5280769763398671636">🏆</tg-emoji>'
E_ZAP        = '<tg-emoji emoji-id="5224607267797606837">☄️</tg-emoji>'
E_AIM        = '<tg-emoji emoji-id="5314345172018961818">🎯</tg-emoji>'
E_SWORD      = '<tg-emoji emoji-id="5408935401442267103">⚔️</tg-emoji>'
E_SHIELD     = '<tg-emoji emoji-id="5465154440287757794">🛡</tg-emoji>'
E_HANDSHAKE  = '<tg-emoji emoji-id="5352795355635276043">🤝</tg-emoji>'

E_SEARCH = '<tg-emoji emoji-id="5231012545799666522">🔍</tg-emoji>'
E_DOOR   = '<tg-emoji emoji-id="6035130900075777681">🚪</tg-emoji>'
E_PEOPLE = '<tg-emoji emoji-id="5958460691550572213">👥</tg-emoji>'
E_CROSS  = '<tg-emoji emoji-id="5210952531676504517">❌</tg-emoji>'
E_BAN    = '<tg-emoji emoji-id="5240241223632954241">🚫</tg-emoji>'
E_RELOAD = '<tg-emoji emoji-id="5346321684574003384">🔄</tg-emoji>'
E_SPARK  = '<tg-emoji emoji-id="5325547803936572038">✨</tg-emoji>'
E_TEAM    = '<tg-emoji emoji-id="5956527135928617699">👥</tg-emoji>'
E_RIGHT   = '<tg-emoji emoji-id="5215695279377884733">➡️</tg-emoji>'
E_OFF1    = '<tg-emoji emoji-id="5314334546269872179">✅</tg-emoji>'
E_OFF2    = '<tg-emoji emoji-id="5314666276658912691">✅</tg-emoji>'
E_OFF3    = '<tg-emoji emoji-id="5314302200871164289">✅</tg-emoji>'
E_MOON    = '<tg-emoji emoji-id="5314345172018961818">🌒</tg-emoji>'
E_CONFIRM = '<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji>'
E_WAITING = '<tg-emoji emoji-id="5451646226975955576">⌛️</tg-emoji>'


EP_USER  = '<tg-emoji emoji-id="5879770735999717115">👤</tg-emoji>'
EP_TIMER = '<tg-emoji emoji-id="5382194935057372936">⏱</tg-emoji>'
EP_GAME  = '<tg-emoji emoji-id="5319247469165433798">🎮</tg-emoji>'
EP_WIN   = '<tg-emoji emoji-id="5413566144986503832">🏆</tg-emoji>'
EP_LOSS  = '<tg-emoji emoji-id="5229007809684724433">🌟</tg-emoji>'
EP_WR    = '<tg-emoji emoji-id="5440539497383087970">🥇</tg-emoji>'
EP_KD    = '<tg-emoji emoji-id="5224607267797606837">☄️</tg-emoji>'
EP_WARN  = '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji>'
E_INFO   = '<tg-emoji emoji-id="5334544901428229844">ℹ️</tg-emoji>'
E_RULES  = '<tg-emoji emoji-id="5956561916573782596">📄</tg-emoji>'
E_GREEN  = '<tg-emoji emoji-id="5416081784641168838">🟢</tg-emoji>'
E_ORANGE = '<tg-emoji emoji-id="5339390195768774311">🟠</tg-emoji>'
E_SHOP   = '<tg-emoji emoji-id="5226656353744862682">🛒</tg-emoji>'
E_PRICE  = '<tg-emoji emoji-id="5377631390571472449">🪙</tg-emoji>'

# Иконки, используемые только в карточке профиля (/profile)
EPV_USER    = '<tg-emoji emoji-id="5391112412445288650">🥸</tg-emoji>'
EPV_MATCHES = '<tg-emoji emoji-id="5377634281084463888">Ⓜ️</tg-emoji>'
EPV_WL      = '<tg-emoji emoji-id="5438496463044752972">⭐️</tg-emoji>'

MODES['5v5']['emoji'] = E_SWORD
MODES['5v5']['label'] = f'{E_SWORD} 5v5'
MODES['3v3']['emoji'] = E_SHIELD
MODES['3v3']['label'] = f'{E_SHIELD} 3v3'
MODES['2v2']['emoji'] = E_HANDSHAKE
MODES['2v2']['label'] = f'{E_HANDSHAKE} 2v2'

E_DIAMOND = '<tg-emoji emoji-id="5427168083074628963">💎</tg-emoji>'
E_CAL     = '<tg-emoji emoji-id="5377612479830453771">🗓</tg-emoji>'

# ── Milestone-задания (одноразовые) ──────────────────────────────────────────
TASKS_MILESTONE = [
    {
        'id': 'task_m_1',
        'title': '<tg-emoji emoji-id="5319247469165433798">🎮</tg-emoji> Первый шаг',
        'desc': 'Сыграй 1 матч',
        'reward': 5,
        'check':    lambda s: s['matches'] >= 1,
        'progress': lambda s: (min(s['matches'], 1), 1),
    },
    {
        'id': 'task_m_10',
        'title': f'{E_SWORD} Ветеран арены',
        'desc': 'Сыграй 10 матчей',
        'reward': 10,
        'check':    lambda s: s['matches'] >= 10,
        'progress': lambda s: (min(s['matches'], 10), 10),
    },
    {
        'id': 'task_m_25',
        'title': f'{E_SHIELD} Закалённый боец',
        'desc': 'Сыграй 25 матчей',
        'reward': 25,
        'check':    lambda s: s['matches'] >= 25,
        'progress': lambda s: (min(s['matches'], 25), 25),
    },
    {
        'id': 'task_m_50',
        'title': '<tg-emoji emoji-id="5208585145702978799">🔥</tg-emoji> Профессионал',
        'desc': 'Сыграй 50 матчей',
        'reward': 50,
        'check':    lambda s: s['matches'] >= 50,
        'progress': lambda s: (min(s['matches'], 50), 50),
    },
    {
        'id': 'task_m_100',
        'title': '<tg-emoji emoji-id="5217822164362739968">👑</tg-emoji> Легенда Moon',
        'desc': 'Сыграй 100 матчей',
        'reward': 100,
        'check':    lambda s: s['matches'] >= 100,
        'progress': lambda s: (min(s['matches'], 100), 100),
    },
    {
        'id': 'task_w_1',
        'title': '<tg-emoji emoji-id="5280769763398671636">🏆</tg-emoji> Первая победа',
        'desc': 'Одержи 1 победу',
        'reward': 5,
        'check':    lambda s: s['wins'] >= 1,
        'progress': lambda s: (min(s['wins'], 1), 1),
    },
    {
        'id': 'task_w_10',
        'title': '<tg-emoji emoji-id="5314345172018961818">🎯</tg-emoji> Победная серия',
        'desc': 'Одержи 10 побед',
        'reward': 15,
        'check':    lambda s: s['wins'] >= 10,
        'progress': lambda s: (min(s['wins'], 10), 10),
    },
    {
        'id': 'task_w_50',
        'title': '<tg-emoji emoji-id="5229007809684724433">🌟</tg-emoji> Доминатор',
        'desc': 'Одержи 50 побед',
        'reward': 75,
        'check':    lambda s: s['wins'] >= 50,
        'progress': lambda s: (min(s['wins'], 50), 50),
    },
    {
        'id': 'task_mode_5v5',
        'title': f'{E_SWORD} Дуэлянт',
        'desc': 'Сыграй матч в режиме 5v5',
        'reward': 5,
        'check':    lambda s: s['matches_5v5'] >= 1,
        'progress': lambda s: (min(s['matches_5v5'], 1), 1),
    },
    {
        'id': 'task_mode_3v3',
        'title': f'{E_SHIELD} Командный игрок',
        'desc': 'Сыграй матч в режиме 3v3',
        'reward': 5,
        'check':    lambda s: s['matches_3v3'] >= 1,
        'progress': lambda s: (min(s['matches_3v3'], 1), 1),
    },
    {
        'id': 'task_mode_2v2',
        'title': f'{E_HANDSHAKE} Напарник',
        'desc': 'Сыграй матч в режиме 2v2',
        'reward': 5,
        'check':    lambda s: s['matches_2v2'] >= 1,
        'progress': lambda s: (min(s['matches_2v2'], 1), 1),
    },
    {
        'id': 'task_lvl_3',
        'title': '<tg-emoji emoji-id="5231200819986047254">📊</tg-emoji> Ранговый игрок',
        'desc': 'Достигни уровня 3 в любом режиме',
        'reward': 20,
        'check':    lambda s: max(s['lvl_5v5'], s['lvl_3v3'], s['lvl_2v2']) >= 3,
        'progress': lambda s: (min(max(s['lvl_5v5'], s['lvl_3v3'], s['lvl_2v2']), 3), 3),
    },
    {
        'id': 'task_lvl_5',
        'title': '<tg-emoji emoji-id="5438496463044752972">⭐️</tg-emoji> Элита',
        'desc': 'Достигни уровня 5 в любом режиме',
        'reward': 50,
        'check':    lambda s: max(s['lvl_5v5'], s['lvl_3v3'], s['lvl_2v2']) >= 5,
        'progress': lambda s: (min(max(s['lvl_5v5'], s['lvl_3v3'], s['lvl_2v2']), 5), 5),
    },
    {
        'id': 'task_lvl_8',
        'title': '<tg-emoji emoji-id="5361837567463399422">🔮</tg-emoji> Мастер',
        'desc': 'Достигни уровня 8 в любом режиме',
        'reward': 100,
        'check':    lambda s: max(s['lvl_5v5'], s['lvl_3v3'], s['lvl_2v2']) >= 8,
        'progress': lambda s: (min(max(s['lvl_5v5'], s['lvl_3v3'], s['lvl_2v2']), 8), 8),
    },
]

# ── Ежедневные задания (сбрасываются раз в сутки UTC) ────────────────────────
TASKS_DAILY = [
    {
        'id': 'daily_play_1',
        'title': '<tg-emoji emoji-id="5319247469165433798">🎮</tg-emoji> На разминку',
        'desc': 'Сыграй 1 матч сегодня',
        'reward': 5,
        'target': 1,
        'type': 'matches',
    },
    {
        'id': 'daily_play_3',
        'title': '<tg-emoji emoji-id="5208585145702978799">🔥</tg-emoji> В ударе',
        'desc': 'Сыграй 3 матча сегодня',
        'reward': 15,
        'target': 3,
        'type': 'matches',
    },
    {
        'id': 'daily_win_1',
        'title': '<tg-emoji emoji-id="5280769763398671636">🏆</tg-emoji> Вкус победы',
        'desc': 'Одержи 1 победу сегодня',
        'reward': 8,
        'target': 1,
        'type': 'wins',
    },
    {
        'id': 'daily_win_3',
        'title': '<tg-emoji emoji-id="5217822164362739968">👑</tg-emoji> Непобедимый',
        'desc': 'Одержи 10 побед сегодня',
        'reward': 25,
        'target': 10,
        'type': 'wins',
    },
]

# ══════════════════════════════════════════════════════════════════════════════
#  ПРЕМИУМ ЭМОДЗИ В КНОПКАХ
# ══════════════════════════════════════════════════════════════════════════════

def _ebtn(label: str, emoji_id: str, emoji_len: int, callback_data: str) -> InlineKeyboardButton:
    """icon_custom_emoji_id — официальное поле InlineKeyboardButton (PTB 20.8+).
    Требует Telegram Premium у владельца бота. Передаётся как нативный параметр PTB,
    а не через api_kwargs — иначе PTB не сериализует поле в JSON кнопки и Telegram
    молча игнорирует всю клавиатуру (кнопка пропадает)."""
    try:
        # PTB 20.8+ — нативный параметр, правильно сериализуется в JSON кнопки
        return InlineKeyboardButton(label, callback_data=callback_data,
                                     icon_custom_emoji_id=emoji_id)
    except TypeError:
        # Старая версия PTB (<20.8) — fallback через api_kwargs
        return InlineKeyboardButton(label, callback_data=callback_data,
                                     api_kwargs={'icon_custom_emoji_id': emoji_id})


# ══════════════════════════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════════════════════════

def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton("Меню")]], resize_keyboard=True)


def inline_main_menu_keyboard() -> InlineKeyboardMarkup:
    rows = []
    if MINI_APP_URL:
        # Mini App-кнопка открывает наше веб-приложение прямо внутри Telegram.
        # Требует https-адрес — регистрируется через переменную окружения
        # MINI_APP_URL (см. main()/README в архиве экспорта).
        rows.append([InlineKeyboardButton("🎮 Moon Faceit Mini App", web_app=WebAppInfo(url=MINI_APP_URL))])
    rows += [
        [_ebtn("Найти матч",        "5231012545799666522", 2, "menu_find")],
        [
            _ebtn("Профиль",         "5879770735999717115", 2, "menu_profile"),
            _ebtn("Таблица лидеров", "5280769763398671636", 2, "menu_leaderboard"),
        ],
        [
            _ebtn("История",          "5282843764451195532", 2, "menu_history"),
            _ebtn("Правила",          "5956561916573782596", 2, "menu_rules"),
        ],
        [
            _ebtn("О сезоне",        "5377612479830453771", 2, "menu_season"),
            _ebtn("Поддержка",        "5854841392899036819", 2, "menu_support"),
        ],
        [
            _ebtn("Магазин",  "5226656353744862682", 2, "menu_shop"),
            _ebtn("Донат",    "5409048419211682843", 2, "menu_donate"),
        ],
        [
            _ebtn("Задания",         "5427168083074628963", 2, "menu_tasks"),
            _ebtn("Пати",            "5461117441612462242", 2, "menu_party"),
        ],
        [_ebtn("Промокод",           "5224607267797606837", 2, "menu_promo")],
        [_ebtn("Репорт на игрока",   "5395695537687123235", 2, "menu_report")],
    ]
    return InlineKeyboardMarkup(rows)


def mode_select_keyboard() -> InlineKeyboardMarkup:
    """Выбор режима: 5v5 / 3v3 / 2v2."""
    return InlineKeyboardMarkup([[
        _ebtn("5v5", "5411474818035909166", 2, "mode_5v5"),
        _ebtn("3v3", "5465154440287757794", 2, "mode_3v3"),
        _ebtn("2v2", "5352795355635276043", 2, "mode_2v2"),
    ]])


_MODE_EMOJI_IDS = {
    '5v5': '5411474818035909166',
    '3v3': '5465154440287757794',
    '2v2': '5352795355635276043',
}
_MODE_LABELS = {'5v5': '5v5', '3v3': '3v3', '2v2': '2v2'}


def lobby_list_keyboard(mode: str, uid_in_lobby: bool = False) -> InlineKeyboardMarkup:
    """5 лобби для выбранного режима + кнопки переключения режима."""
    cfg   = MODES[mode]
    match = cfg['match_size']
    q_set = lobby_queues[mode]
    rows  = [
        [_ebtn(
            f"Лобби {i}  ({len(q_set[i])}/{match})",
            "5319247469165433798", 2,
            f"lobby_join_{mode}_{i}"
        )]
        for i in range(1, 6)
    ]
    other_modes = [m for m in MODES if m != mode]
    rows.append([
        _ebtn(_MODE_LABELS[m], _MODE_EMOJI_IDS[m], 2, f"mode_{m}")
        for m in other_modes
    ])
    if uid_in_lobby:
        rows.append([_ebtn("Выйти из очереди", "6035130900075777681", 2, "lobby_leave")])
    return InlineKeyboardMarkup(rows)


def _leaderboard_keyboard(season: str = 'curr', mode: str = '5v5') -> InlineKeyboardMarkup:
    """
    Строит клавиатуру лидерборда.
    Строка 1: Нынешний / Прошлый сезон (выделен нынешний/прошлый)
    Строка 2: ⚔️ 5v5 / 🛡 3v3 / 🤝 2v2 с premium-эмодзи
    """
    _TROPHY_EMOJI_ID = "5280769763398671636"

    def _s(label, s_val):
        mark = "▸ " if season == s_val else ""
        return _ebtn(f"{mark}{label}", _TROPHY_EMOJI_ID, 2, f"lb_{s_val}_{mode}")

    def _m(label, eid, m_val):
        mark = "▸ " if mode == m_val else ""
        return _ebtn(f"{mark}{label}", eid, 2, f"lb_{season}_{m_val}")

    return InlineKeyboardMarkup([
        [_s("Нынешний сезон", "curr"), _s("Прошлый сезон", "past")],
        [
            _m("5v5", "5411474818035909166", "5v5"),
            _m("3v3", "5465154440287757794", "3v3"),
            _m("2v2", "5352795355635276043", "2v2"),
        ],
    ])


def profile_actions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            _ebtn("Сменить ID",  "5319247469165433798", 2, "profile_change_id"),
            _ebtn("Сменить ник", "5319247469165433798", 2, "profile_change_nick"),
        ],
        [_ebtn("Найти игрока", "5231012545799666522", 2, "menu_search")],
    ])


def shop_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            _ebtn("Ножи",      "5062267305124168856", 2, "shop_cat_knives"),
            _ebtn("Скины",     "5409048419211682843", 2, "shop_cat_skins"),
        ],
        [
            _ebtn("Наклейки",  "5393619629669097759", 2, "shop_cat_stickers"),
            _ebtn("Назад",     "5255703720078879038", 2, "shop_back"),
        ],
    ])


# ──────────────────────────────────────────────────────────────────────────────
#  ДАННЫЕ МАГАЗИНА
# ──────────────────────────────────────────────────────────────────────────────

_SHOP_SKINS_ORDER = ['akr', 'awm']
_SHOP_SKINS: dict[str, tuple[str, list]] = {
    'akr': ('AKR', [
        ('AKR «Treasure Hunter»',           450),
        ('AKR «Necromancer»',               100),
        ('AKR «Treasure Hunter» StatTrack', 500),
        ('AKR «Necromancer» StatTrack',     125),
    ]),
    'awm': ('AWM', [
        ('AWM «Sport»',                     500),
        ('AWM «Winter Sport»',              700),
        ('AWM «Winter Sport» StatTrack',    750),
    ]),
}

_SHOP_KNIVES_ORDER = ['m9', 'kerambit', 'butterfly', 'kunai']
_SHOP_KNIVES: dict[str, tuple[str, list]] = {
    'm9': ('M9', [
        ('M9 «Scratch»',      175),
        ('M9 «Universe»',     160),
        ('M9 «Blue Blood»',   250),
        ('M9 «Ancient»',      100),
        ('M9 «Dragon Glass»', 150),
    ]),
    'kerambit': ('Kerambit', [
        ('Kerambit «Claw»',         200),
        ('Kerambit «Dragon Glass»', 125),
        ('Kerambit «Universe»',     150),
        ('Kerambit «Gold»',         400),
        ('Kerambit «Scratch»',      175),
    ]),
    'butterfly': ('Butterfly', [
        ('Butterfly «Dragon Glass»', 150),
        ('Butterfly «Black Widow»',  175),
        ('Butterfly «Fire Storm»',   300),
        ('Butterfly «Legacy»',       250),
    ]),
    'kunai': ('Kunai', [
        ('Kunai «Poison»',    125),
        ('Kunai «Luxury»',    150),
        ('Kunai «Bone»',      150),
        ('Kunai «Radiation»', 250),
        ('Kunai «Reaper»',    200),
    ]),
}

_SHOP_STICKERS = [
    ('Sticker «Dragon»',        50),
    ('Sticker «Samurai»',       60),
    ('Sticker «Metal Rat»',    200),
    ('Sticker «Gold Skull»',   500),
    ('Sticker «Phoenix Blazon»', 250),
]


def _shop_tab_keyboard(cat: str, active: str, order: list, labels: dict,
                        items: list | None = None) -> InlineKeyboardMarkup:
    rows: list[list] = []
    if items:
        for name, price in items:
            rows.append([_ebtn(
                f"{price}  —  {name}",
                "5377631390571472449",
                2,
                f"shop_buy_{cat}_{active}"
            )])
    row: list = []
    for t in order:
        marker = "› " if t == active else ""
        row.append(InlineKeyboardButton(f"{marker}{labels[t]}", callback_data=f"shop_nav_{cat}_{t}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([_ebtn("Назад", "5255703720078879038", 2, "shop_back")])
    return InlineKeyboardMarkup(rows)


def _shop_page_text(header_emoji: str, title: str, items: list | None = None) -> str:
    return f"{header_emoji} <b>{title}</b>\n\n{E_RIGHT} Выбери товар:"


# ══════════════════════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════════════════════════

def mode_cols(mode: str):
    """Возвращает (elo_col, matches_col, wins_col, level_col) для заданного режима."""
    s = mode  # '5v5', '3v3', '2v2'
    return f'elo_{s}', f'matches_{s}', f'wins_{s}', f'level_{s}'


def init_db():
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id        INTEGER PRIMARY KEY,
        username       TEXT,
        elo            INTEGER DEFAULT 0,
        level          INTEGER DEFAULT 1,
        matches_played INTEGER DEFAULT 0,
        wins           INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS team_matches (
        match_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        status    TEXT DEFAULT 'active',
        winner    INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        mode      TEXT DEFAULT '5v5'
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS match_players (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id   INTEGER,
        user_id    INTEGER,
        team       INTEGER,
        elo_before INTEGER,
        elo_change INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS match_votes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id   INTEGER,
        user_id    INTEGER,
        voted_team INTEGER,
        UNIQUE(match_id, user_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS match_screenshots (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id   INTEGER,
        user_id    INTEGER,
        file_id    TEXT,
        timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS reports (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        from_id    INTEGER,
        target_id  INTEGER,
        reason     TEXT,
        timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS season_archive (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        season_num INTEGER,
        user_id    INTEGER,
        username   TEXT,
        elo        INTEGER,
        wins       INTEGER,
        matches    INTEGER,
        timestamp  DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_tasks (
        user_id  INTEGER,
        task_id  TEXT,
        claimed  INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, task_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_daily_tasks (
        user_id   INTEGER,
        task_id   TEXT,
        task_date TEXT,
        claimed   INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, task_id, task_date)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS bot_settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS promo_codes (
        code             TEXT PRIMARY KEY,
        reward           INTEGER NOT NULL,
        activations_left INTEGER NOT NULL,
        created_at       DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS promo_redemptions (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        code      TEXT,
        user_id   INTEGER,
        reward    INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    for col_sql in [
        'ALTER TABLE team_matches  ADD COLUMN screenshot_submitted INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN is_admin    INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN first_name  TEXT',
        'ALTER TABLE users ADD COLUMN is_banned   INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN ban_reason  TEXT',
        'ALTER TABLE users ADD COLUMN is_creator  INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN warns       INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN ban_until   TEXT',
        'ALTER TABLE users ADD COLUMN kills       INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN deaths      INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN game_id          TEXT',
        'ALTER TABLE users ADD COLUMN last_match_at    TEXT',
        'ALTER TABLE users ADD COLUMN last_nick_change TEXT',
        'ALTER TABLE users ADD COLUMN last_id_change   TEXT',
        'ALTER TABLE team_matches ADD COLUMN mode TEXT DEFAULT "5v5"',
        'ALTER TABLE users ADD COLUMN moon_coins INTEGER DEFAULT 0',
        'ALTER TABLE match_players ADD COLUMN kills  INTEGER DEFAULT 0',
        'ALTER TABLE match_players ADD COLUMN deaths INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN official_badge INTEGER DEFAULT 0',
        'ALTER TABLE team_matches ADD COLUMN ct_team INTEGER DEFAULT 1',
        'ALTER TABLE team_matches ADD COLUMN base_gain INTEGER DEFAULT 0',
        'ALTER TABLE match_players ADD COLUMN elo_applied INTEGER DEFAULT 0',
        'ALTER TABLE match_players ADD COLUMN kd_entered  INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN admin_badge_id INTEGER DEFAULT 1',
        # Per-mode ELO / calibration
        'ALTER TABLE users ADD COLUMN elo_5v5     INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN matches_5v5 INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN wins_5v5    INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN level_5v5   INTEGER DEFAULT 1',
        'ALTER TABLE users ADD COLUMN elo_3v3     INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN matches_3v3 INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN wins_3v3    INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN level_3v3   INTEGER DEFAULT 1',
        'ALTER TABLE users ADD COLUMN elo_2v2     INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN matches_2v2 INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN wins_2v2    INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN level_2v2   INTEGER DEFAULT 1',
        # Персистентный шаг первичной регистрации (переживает перезапуск бота,
        # в отличие от context.user_data): 'awaiting_nickname' / 'awaiting_game_id' / NULL.
        'ALTER TABLE users ADD COLUMN pending_reg_step    TEXT',
        'ALTER TABLE users ADD COLUMN pending_reg_step_at TEXT',
        # Per-mode архив сезонов
        'ALTER TABLE season_archive ADD COLUMN elo_5v5     INTEGER DEFAULT 0',
        'ALTER TABLE season_archive ADD COLUMN matches_5v5 INTEGER DEFAULT 0',
        'ALTER TABLE season_archive ADD COLUMN wins_5v5    INTEGER DEFAULT 0',
        'ALTER TABLE season_archive ADD COLUMN elo_3v3     INTEGER DEFAULT 0',
        'ALTER TABLE season_archive ADD COLUMN matches_3v3 INTEGER DEFAULT 0',
        'ALTER TABLE season_archive ADD COLUMN wins_3v3    INTEGER DEFAULT 0',
        'ALTER TABLE season_archive ADD COLUMN elo_2v2     INTEGER DEFAULT 0',
        'ALTER TABLE season_archive ADD COLUMN matches_2v2 INTEGER DEFAULT 0',
        'ALTER TABLE season_archive ADD COLUMN wins_2v2    INTEGER DEFAULT 0',
        # MVP матча — больше всех килов в победившей команде
        'ALTER TABLE team_matches ADD COLUMN mvp_uid     INTEGER',
        'ALTER TABLE team_matches ADD COLUMN mvp_awarded INTEGER DEFAULT 0',
        'ALTER TABLE users ADD COLUMN mvp_count INTEGER DEFAULT 0',
        # Победитель, определённый автоматически OCR по слову "ПОБЕДА" на скриншоте
        # (1/2 — номер команды, NULL — не удалось распознать).
        'ALTER TABLE team_matches ADD COLUMN ocr_winner_team INTEGER',
        # Модератор — облегчённая роль: может только выдавать/снимать варны и баны,
        # без доступа к остальным админ-командам.
        'ALTER TABLE users ADD COLUMN is_moderator INTEGER DEFAULT 0',
    ]:
        try:
            c.execute(col_sql)
        except Exception:
            pass
    conn.commit()
    conn.close()


def get_setting(key: str, default: str = None) -> str:
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT value FROM bot_settings WHERE key=?', (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(key: str, value: str):
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute(
        'INSERT INTO bot_settings (key, value) VALUES (?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (key, value)
    )
    conn.commit()
    conn.close()


def _add_months(date_str: str, months: int) -> str:
    """Прибавляет months к дате в формате DD.MM.YYYY, возвращает тот же формат."""
    d = datetime.datetime.strptime(date_str, '%d.%m.%Y')
    total_month = d.month - 1 + months
    year = d.year + total_month // 12
    month = total_month % 12 + 1
    import calendar
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day).strftime('%d.%m.%Y')


def get_creator_ids() -> set:
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT user_id FROM users WHERE is_creator = 1')
    ids = {r[0] for r in c.fetchall()}
    conn.close()
    return ids


def get_admin_ids() -> set:
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT user_id FROM users WHERE is_admin = 1')
    ids = {r[0] for r in c.fetchall()}
    conn.close()
    return ids


def creator_badge(uid: int, creator_ids: set) -> str:
    return f" {E_CHECK}" if uid in creator_ids else ""


def official_badge_str(uid: int) -> str:
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT official_badge FROM users WHERE user_id=?', (uid,))
    row = c.fetchone()
    conn.close()
    val = row[0] if row else 0
    if val == 1:
        return f" {E_OFF1}"
    if val == 2:
        return f" {E_OFF2}"
    if val == 3:
        return f" {E_OFF3}"
    return ""


def admin_badge(uid: int, admin_ids: set) -> str:
    if uid not in admin_ids:
        return ""
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT admin_badge_id FROM users WHERE user_id=?', (uid,))
    row = c.fetchone()
    conn.close()
    badge_id = (row[0] or 1) if row else 1
    if badge_id == 2:
        return f" {E_OFF2}"
    if badge_id == 3:
        return f" {E_OFF3}"
    return f" {E_OFF1}"


def get_user(user_id: int):
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('''SELECT user_id, COALESCE(first_name, username) as display_name,
                        elo, level, matches_played, wins, is_admin
                 FROM users WHERE user_id = ?''', (user_id,))
    row = c.fetchone()
    conn.close()
    return row


def get_admins() -> list[int]:
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT user_id FROM users WHERE is_admin = 1')
    admins = [r[0] for r in c.fetchall()]
    conn.close()
    return admins


def find_user_by_name(name: str):
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('''SELECT user_id, COALESCE(first_name, username)
                 FROM users
                 WHERE LOWER(COALESCE(first_name, username)) = LOWER(?)
                    OR LOWER(username) = LOWER(?)
                 LIMIT 1''', (name, name.lstrip('@')))
    row = c.fetchone()
    conn.close()
    return row


def elo_to_level(elo: int) -> int:
    for lvl, threshold in enumerate([801,951,1101,1251,1401,1551,1701,1851,2001], 1):
        if elo < threshold:
            return lvl
    return 10


def calculate_calibration_elo(wins: int, total: int, kills: int = 0, deaths: int = 0) -> int:
    ratio = wins / total if total > 0 else 0
    if ratio >= 0.9:  base = 1700
    elif ratio >= 0.8: base = 1500
    elif ratio >= 0.7: base = 1300
    elif ratio >= 0.6: base = 1100
    elif ratio >= 0.5: base = 900
    elif ratio >= 0.4: base = 750
    elif ratio >= 0.3: base = 650
    elif ratio >= 0.2: base = 550
    elif ratio >= 0.1: base = 450
    else: base = 400
    kd = kills / deaths if deaths > 0 else (float(kills) if kills > 0 else 1.0)
    if kd >= 2.0:    kd_bonus = 100
    elif kd >= 1.5:  kd_bonus = 50
    elif kd >= 1.2:  kd_bonus = 25
    elif kd >= 0.8:  kd_bonus = 0
    elif kd >= 0.5:  kd_bonus = -25
    else:            kd_bonus = -50
    return max(400, base + kd_bonus)


def calculate_elo_change(avg_winner: float, avg_loser: float, K: int = 32) -> int:
    expected = 1 / (1 + 10 ** ((avg_loser - avg_winner) / 400))
    return max(1, int(K * (1 - expected)))


def calculate_individual_elo_change(base_gain: int, kills: int, deaths: int, won: bool) -> int:
    """Индивидуальное изменение ELO по матчевому K/D.
    Победители всегда получают +ELO (больше при хорошей игре).
    Проигравшие всегда получают -ELO (меньше при хорошей игре).
    """
    kd = kills / deaths if deaths > 0 else (float(kills) if kills > 0 else 1.0)
    if won:
        if kd >= 3.0:    multiplier = 1.8
        elif kd >= 2.5:  multiplier = 1.6
        elif kd >= 2.0:  multiplier = 1.4
        elif kd >= 1.5:  multiplier = 1.25
        elif kd >= 1.2:  multiplier = 1.1
        elif kd >= 0.8:  multiplier = 1.0
        elif kd >= 0.5:  multiplier = 0.85
        else:            multiplier = 0.7
        return max(1, round(base_gain * multiplier))
    else:
        if kd >= 3.0:    multiplier = 0.25  # героическая игра — теряет минимум
        elif kd >= 2.5:  multiplier = 0.35
        elif kd >= 2.0:  multiplier = 0.5
        elif kd >= 1.5:  multiplier = 0.65
        elif kd >= 1.2:  multiplier = 0.8
        elif kd >= 0.8:  multiplier = 1.0
        elif kd >= 0.5:  multiplier = 1.2
        else:            multiplier = 1.4   # плохая игра — теряет больше
        return -max(1, round(base_gain * multiplier))


def elo_bar(elo: int) -> str:
    thresholds = [801, 951, 1101, 1251, 1401, 1551, 1701, 1851, 2001, 9999]
    prev       = [0,   801, 951,  1101, 1251, 1401, 1551, 1701, 1851, 2001]
    for i, threshold in enumerate(thresholds):
        if elo < threshold:
            low, high = prev[i], threshold
            filled = round((elo - low) / (high - low) * 8)
            return f"[{'█' * filled}{'░' * (8 - filled)}]"
    return "[████████]"


def queue_list_text(lobby_id: int, mode: str) -> str:
    q = lobby_queues[mode].get(lobby_id, [])
    if not q:
        return ""
    calib_games = MODES[mode]['calib_games']
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    creator_ids = get_creator_ids()
    admin_ids   = get_admin_ids()
    lines = []
    _qec, _qmc, _qwc, _qlc = mode_cols(mode)
    for i, uid in enumerate(q, 1):
        c.execute(
            f'SELECT COALESCE(first_name, username), {_qec}, {_qmc}, {_qlc} FROM users WHERE user_id = ?',
            (uid,)
        )
        row = c.fetchone()
        if row:
            name, elo, matches_played, level = row
            badge = creator_badge(uid, creator_ids) + admin_badge(uid, admin_ids)
            if (matches_played or 0) < calib_games:
                icon = LEVEL_ICONS[0]
                elo_str = f"калибровка {matches_played or 0}/{calib_games}"
            else:
                icon = LEVEL_ICONS.get(level or 1, LEVEL_ICONS[1])
                elo_str = f"ELO: {elo}"
            lines.append(f"  {i}. {icon} {name or str(uid)}{badge} ({elo_str})")
        else:
            lines.append(f"  {i}. id:{uid}")
    conn.close()
    return "\n".join(lines)


async def _send_match_notification(bot, uid: int, match_id: int, info: dict, calib_games: int):
    """Уведомление игроку об итогах матча — ELO начисляется автоматически."""
    if not info or is_bot(uid):
        return
    try:
        result_icon = f"{E_CHECK} Победа" if info['won'] else f"{E_CROSS} Поражение"
        if info['is_calibrating']:
            if info['calib_done']:
                lvl_icon = LEVEL_ICONS.get(info['calib_lvl'], str(info['calib_lvl']))
                text = (
                    f"<tg-emoji emoji-id='5461151367559141950'>🎉</tg-emoji> <b>Калибровка завершена!</b>\n\n"
                    f"{result_icon}\n"
                    f"{E_PEOPLE} Результат: <b>{info['wins']}/{calib_games}</b> побед\n\n"
                    f"{E_ZAP} ELO присвоен: <code>{info['calib_elo']}</code>\n"
                    f"〔 {lvl_icon} 〕 Уровень <b>{info['calib_lvl']}</b>\n\n"
                    f"Удачи в матчах! {E_FIRE}"
                )
            else:
                remaining = calib_games - info['matches_played']
                text = (
                    f"<tg-emoji emoji-id='5231200819986047254'>📊</tg-emoji> <b>Матч #{match_id} засчитан</b>\n\n"
                    f"{result_icon}\n\n"
                    f"{E_PEOPLE} Прогресс калибровки: <b>{info['matches_played']}/{calib_games}</b>\n"
                    f"Осталось игр: <b>{remaining}</b>\n\n"
                    f"<i>ELO будет присвоен после завершения калибровки.</i>"
                )
        else:
            sign = "+" if info['elo_change'] > 0 else ""
            kills  = info.get('kills', 0)
            deaths = info.get('deaths', 0)
            kd_str = f"{kills}/{deaths}  ({kills/deaths:.2f})" if deaths > 0 else f"{kills}/0"
            if kills or deaths:
                kd_line = f"\n<tg-emoji emoji-id='5222486447306602688'>🔫</tg-emoji> Карьерный К/Д: <b>{kd_str}</b> <i>(влияет на множитель ELO)</i>"
            else:
                kd_line = f"\n<tg-emoji emoji-id='5222486447306602688'>🔫</tg-emoji> <i>К/Д не указан — ELO начислен с нейтральным множителем</i>"
            coins = info.get('coins_earned', 0)
            coins_line = f"\n{E_PRICE} Moon Coins: <b>+{coins}</b> <tg-emoji emoji-id='5461151367559141950'>🎉</tg-emoji>" if coins else ""
            text = (
                f"<tg-emoji emoji-id='5231200819986047254'>📊</tg-emoji> <b>Матч #{match_id} завершён</b>\n\n"
                f"{result_icon}\n\n"
                f"{E_ZAP} ELO: <b>{sign}{info['elo_change']}</b>"
                f"{kd_line}"
                f"{coins_line}"
            )
        await _send_html(bot, uid, text)
    except Exception:
        pass


def finalize_match(match_id: int, winning_team: int, mode: str = '5v5') -> tuple[int, list, list, dict]:
    """
    Завершает матч и СРАЗУ начисляет ELO каждому игроку индивидуально.
    ELO считается автоматически по карьерному K/D каждого игрока из профиля.
    Для игроков на калибровке — просто засчитывает игру, ELO не меняется.
    Возвращает (gain, winners, losers, per_uid).
    """
    losing_team = 2 if winning_team == 1 else 1
    calib_games = MODES.get(mode, MODES['5v5'])['calib_games']
    now_iso     = datetime.datetime.utcnow().isoformat()
    conn = sqlite3.connect('faceit_bot.db')
    c    = conn.cursor()

    c.execute('SELECT AVG(elo_before) FROM match_players WHERE match_id=? AND team=?', (match_id, winning_team))
    avg_w = c.fetchone()[0] or 0
    c.execute('SELECT AVG(elo_before) FROM match_players WHERE match_id=? AND team=?', (match_id, losing_team))
    avg_l = c.fetchone()[0] or 0
    gain  = calculate_elo_change(avg_w, avg_l)

    c.execute('SELECT user_id, elo_before FROM match_players WHERE match_id=? AND team=?', (match_id, winning_team))
    winners = c.fetchall()
    c.execute('SELECT user_id, elo_before FROM match_players WHERE match_id=? AND team=?', (match_id, losing_team))
    losers = c.fetchall()

    per_uid: dict = {}

    ec, mc, wc, lc = mode_cols(mode)

    def _process(uid: int, elo_before: int, won: bool):
        # Читаем per-mode счётчики
        c.execute(
            f'SELECT {mc}, {wc} FROM users WHERE user_id=?',
            (uid,)
        )
        row       = c.fetchone()
        mp_before = (row[0] or 0) if row else 0   # матчей в этом режиме
        w_before  = (row[1] or 0) if row else 0   # побед в этом режиме
        # К/Д этого матча уже распознано автоматически со скриншота результата
        # (см. _apply_ocr_stats_to_match) — используем его напрямую, без оценки
        # по прошлым матчам.
        c.execute(
            'SELECT kills, deaths FROM match_players WHERE match_id=? AND user_id=?',
            (match_id, uid)
        )
        kd_row = c.fetchone()
        kills  = (kd_row[0] or 0) if kd_row else 0
        deaths = (kd_row[1] or 0) if kd_row else 0
        calibrated = mp_before >= calib_games

        # Начисляем Moon Coins при победе (1-10)
        coins_earned = random.randint(1, 10) if won else 0
        if coins_earned:
            c.execute('UPDATE users SET moon_coins=moon_coins+? WHERE user_id=?', (coins_earned, uid))

        if calibrated:
            # ELO индивидуально по K/D профиля
            elo_chg = calculate_individual_elo_change(gain, kills, deaths, won)
            new_elo = max(0, elo_before + elo_chg)
            new_lvl = elo_to_level(new_elo)
            if won:
                c.execute(
                    f'UPDATE users SET {ec}=?, {lc}=?, {mc}={mc}+1, {wc}={wc}+1, '
                    f'matches_played=matches_played+1, wins=wins+1, last_match_at=? WHERE user_id=?',
                    (new_elo, new_lvl, now_iso, uid)
                )
            else:
                c.execute(
                    f'UPDATE users SET {ec}=?, {lc}=?, {mc}={mc}+1, '
                    f'matches_played=matches_played+1, last_match_at=? WHERE user_id=?',
                    (new_elo, new_lvl, now_iso, uid)
                )
            c.execute('UPDATE match_players SET elo_change=?, elo_applied=1 WHERE match_id=? AND user_id=?',
                      (elo_chg, match_id, uid))
            per_uid[uid] = {
                'is_calibrating': False,
                'calib_done':     False,
                'calib_elo':      None,
                'calib_lvl':      None,
                'elo_change':     elo_chg,
                'kills':          kills,
                'deaths':         deaths,
                'matches_played': mp_before + 1,
                'wins':           w_before + (1 if won else 0),
                'won':            won,
                'coins_earned':   coins_earned,
            }
        else:
            # Калибровка — засчитываем игру, ELO не трогаем
            if won:
                c.execute(
                    f'UPDATE users SET {mc}={mc}+1, {wc}={wc}+1, '
                    f'matches_played=matches_played+1, wins=wins+1, last_match_at=? WHERE user_id=?',
                    (now_iso, uid)
                )
            else:
                c.execute(
                    f'UPDATE users SET {mc}={mc}+1, '
                    f'matches_played=matches_played+1, last_match_at=? WHERE user_id=?',
                    (now_iso, uid)
                )
            c.execute('UPDATE match_players SET elo_change=0, elo_applied=1 WHERE match_id=? AND user_id=?',
                      (match_id, uid))
            new_mp     = mp_before + 1
            new_wins   = w_before + (1 if won else 0)
            calib_done = new_mp >= calib_games
            calib_elo, calib_lvl = None, None
            if calib_done:
                calib_elo = calculate_calibration_elo(new_wins, calib_games, kills, deaths)
                calib_lvl = elo_to_level(calib_elo)
                c.execute(f'UPDATE users SET {ec}=?, {lc}=? WHERE user_id=?', (calib_elo, calib_lvl, uid))
            per_uid[uid] = {
                'is_calibrating': True,
                'calib_done':     calib_done,
                'calib_elo':      calib_elo,
                'calib_lvl':      calib_lvl,
                'elo_change':     0,
                'kills':          kills,
                'deaths':         deaths,
                'matches_played': new_mp,
                'wins':           new_wins,
                'won':            won,
                'coins_earned':   coins_earned,
            }

    for uid, elo_before in winners:
        _process(uid, elo_before, True)
    for uid, elo_before in losers:
        _process(uid, elo_before, False)

    c.execute('UPDATE team_matches SET status="finished", winner=?, base_gain=? WHERE match_id=?', (winning_team, gain, match_id))
    conn.commit()
    conn.close()
    return gain, winners, losers, per_uid


# ══════════════════════════════════════════════════════════════════════════════
#  MVP МАТЧА
# ══════════════════════════════════════════════════════════════════════════════

MVP_BONUS_COINS = 20


def _check_and_award_mvp(match_id: int) -> Optional[dict]:
    """Как только ВСЕ игроки победившей команды получили распознанный K/D со
    скриншота, определяет MVP матча — больше всех килов среди победителей (при равенстве
    килов побеждает тот, у кого меньше смертей, затем — кто раньше присоединился
    к матчу). Начисляет бонус Moon Coins и помечает матч как обработанный, чтобы
    награда не выдавалась повторно. Возвращает данные MVP или None, если рано /
    уже начислено / победили только боты.
    """
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT status, winner, mode, mvp_awarded FROM team_matches WHERE match_id=?', (match_id,))
    row = c.fetchone()
    if not row or row[0] != 'finished' or not row[1] or row[3]:
        conn.close()
        return None
    winner, mode = row[1], (row[2] or '5v5')
    team_size = MODES.get(mode, MODES['5v5'])['team_size']

    c.execute(
        'SELECT user_id, kills, deaths, kd_entered FROM match_players '
        'WHERE match_id=? AND team=? ORDER BY id ASC',
        (match_id, winner)
    )
    winners = c.fetchall()
    if len(winners) < team_size or any(w[3] == 0 for w in winners):
        conn.close()
        return None  # не все победители ещё ввели K/D

    real_winners = [w for w in winners if not is_bot(w[0])]
    if not real_winners:
        c.execute('UPDATE team_matches SET mvp_awarded=1 WHERE match_id=?', (match_id,))
        conn.commit()
        conn.close()
        return None

    mvp_uid, mvp_kills, mvp_deaths, _ = max(real_winners, key=lambda w: (w[1], -w[2]))

    c.execute('UPDATE users SET mvp_count = mvp_count + 1, moon_coins = moon_coins + ? WHERE user_id=?',
              (MVP_BONUS_COINS, mvp_uid))
    c.execute('UPDATE team_matches SET mvp_uid=?, mvp_awarded=1 WHERE match_id=?', (mvp_uid, match_id))
    conn.commit()
    conn.close()
    return {'mvp_uid': mvp_uid, 'kills': mvp_kills, 'deaths': mvp_deaths, 'bonus_coins': MVP_BONUS_COINS}


async def _announce_mvp(bot, match_id: int, mvp_info: dict):
    """Рассылает объявление об MVP матча всем его участникам (кроме ботов)."""
    mvp_uid = mvp_info['mvp_uid']
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT COALESCE(first_name, username) FROM users WHERE user_id=?', (mvp_uid,))
    name_row = c.fetchone()
    mvp_name = _esc(name_row[0]) if name_row and name_row[0] else str(mvp_uid)
    c.execute('SELECT user_id FROM match_players WHERE match_id=?', (match_id,))
    participants = [r[0] for r in c.fetchall()]
    conn.close()

    kills, deaths = mvp_info['kills'], mvp_info['deaths']
    bonus = mvp_info['bonus_coins']
    text = (
        f"{E_TROPHY} <b>MVP матча #{match_id}</b>\n\n"
        f"{E_ZAP} <b>{mvp_name}</b> — лучший игрок победившей команды!\n"
        f"<tg-emoji emoji-id='5222486447306602688'>🔫</tg-emoji> Убийства: <b>{kills}</b>  Смерти: <b>{deaths}</b>\n\n"
        f"{E_PRICE} Бонус: <b>+{bonus} Moon Coins</b> {E_FIRE}"
    )
    for uid in participants:
        if is_bot(uid):
            continue
        try:
            await _send_html(bot, uid, text)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
#  АВТОМАТИЧЕСКОЕ РАСПОЗНАВАНИЕ K/D СО СКРИНШОТА (без ИИ, локальный OCR)
# ══════════════════════════════════════════════════════════════════════════════
#
# Требуется установить (не входит в стандартную библиотеку Python):
#   pip install pytesseract pillow
#   + системный пакет tesseract-ocr с языковыми пакетами rus и eng
#     (например: apt install tesseract-ocr tesseract-ocr-rus)
#
# Никакие внешние ИИ-сервисы и API-ключи не используются — распознавание
# полностью локальное, по расположению слов и цифр на скриншоте.

def _normalize_ocr_name(name: str) -> str:
    """Приводит игровой ник к сравнимому виду: убирает клан-теги вида [VP]/[VX],
    двоеточия, пробелы и спецсимволы, приводит к нижнему регистру."""
    if not name:
        return ""
    name = _re_ocr.sub(r'\[[^\]]*\]', '', name)   # клан-теги: [VP], [VX] и т.п.
    name = name.replace(':', ' ')
    name = _re_ocr.sub(r'[^0-9a-zA-Zа-яА-ЯёЁ]+', '', name)
    return name.strip().lower()


def _ocr_extract_scoreboard(image_bytes: bytes) -> Optional[dict]:
    """Читает таблицу результатов матча со скриншота локальным OCR (Tesseract),
    без обращения к каким-либо ИИ-сервисам.

    Логика устойчива к разному разрешению скриншота: вместо фиксированных
    координат ищет слова по их взаимному расположению — таблица слева и
    таблица справа, а в каждой строке последние 4 числовых значения — это
    столбцы «У» (убийства), «П» (помощь), «С» (смерти), «СЧЁТ» — именно в
    этом порядке в интерфейсе игры.

    Возвращает:
        {'sides': {'left': [{'name','kills','deaths'}...], 'right': [...]},
         'winner_side': 'left' | 'right' | None}
        или None, если распознать изображение не удалось.
    """
    import io as _io_ocr
    try:
        from PIL import Image
        import pytesseract
    except ImportError:
        logging.error("OCR недоступен: не установлены pytesseract/Pillow.")
        return None

    try:
        img = Image.open(_io_ocr.BytesIO(image_bytes)).convert('L')
    except Exception:
        logging.exception("Не удалось открыть скриншот для OCR.")
        return None

    # Увеличиваем мелкий текст для более точного распознавания
    scale = 2 if max(img.size) < 2200 else 1
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)

    try:
        data = pytesseract.image_to_data(
            img, lang='rus+eng', config='--psm 11',
            output_type=pytesseract.Output.DICT
        )
    except Exception:
        logging.exception("Ошибка Tesseract OCR при разборе скриншота.")
        return None

    words = []
    for i in range(len(data['text'])):
        text = (data['text'][i] or '').strip()
        if not text:
            continue
        try:
            conf = float(data['conf'][i])
        except (ValueError, TypeError):
            conf = -1
        if conf < 0:
            continue
        words.append({
            'text': text,
            'x': data['left'][i],
            'y': data['top'][i],
            'w': data['width'][i],
        })
    if not words:
        return None

    width = img.width
    left_words  = [w for w in words if w['x'] + w['w'] / 2 < width / 2]
    right_words = [w for w in words if w['x'] + w['w'] / 2 >= width / 2]

    def _has_word(ws, needle):
        needle = needle.upper()
        return any(needle in w['text'].upper() for w in ws)

    winner_side = None
    if _has_word(left_words, 'ПОБЕД'):
        winner_side = 'left'
    elif _has_word(right_words, 'ПОБЕД'):
        winner_side = 'right'

    def _group_rows(ws):
        """Группирует слова в строки по вертикальной координате."""
        ws = sorted(ws, key=lambda w: w['y'])
        rows, current, current_y = [], [], None
        row_tol = 14 * scale
        for w in ws:
            if current and abs(w['y'] - current_y) > row_tol:
                rows.append(current)
                current = []
            current.append(w)
            current_y = w['y'] if current_y is None else (current_y + w['y']) / 2
        if current:
            rows.append(current)
        return rows

    def _parse_side(ws):
        players = []
        for row in _group_rows(ws):
            row = sorted(row, key=lambda w: w['x'])
            digit_tokens = [w for w in row if _re_ocr.fullmatch(r'\d+', w['text'])]
            if len(digit_tokens) < 4:
                continue  # не строка статистики игрока (например, заголовок таблицы)
            stat_tokens = digit_tokens[-4:]
            kills, _assists, deaths, _score = (int(t['text']) for t in stat_tokens)
            first_digit_x = digit_tokens[0]['x']   # начало столбца "пинг"
            first_stat_x  = stat_tokens[0]['x']    # начало столбца "У" (убийства)
            name_tokens = [
                w['text'] for w in row
                if first_digit_x + 5 < w['x'] < first_stat_x - 5
                and not _re_ocr.fullmatch(r'\$?\d+', w['text'])
            ]
            name = ' '.join(name_tokens).strip()
            if not name:
                continue
            players.append({'name': name, 'kills': kills, 'deaths': deaths})
        return players

    return {
        'sides': {'left': _parse_side(left_words), 'right': _parse_side(right_words)},
        'winner_side': winner_side,
    }


def _apply_ocr_stats_to_match(match_id: int, ocr: dict) -> tuple[bool, list]:
    """Сопоставляет распознанных со скриншота игроков с участниками матча
    по игровому нику (game_id) и сразу сохраняет их kills/deaths в БД.

    Сопоставление стороны таблицы (left/right) с командой (1/2) идёт через
    ct_team матча: левая панель в интерфейсе игры — всегда СПЕЦНАЗ (CT).

    Возвращает (все_сопоставлены: bool, unmatched_uids: list[int]).
    """
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT ct_team FROM team_matches WHERE match_id=?', (match_id,))
    row = c.fetchone()
    ct_team = (row[0] or 1) if row else 1
    t_team  = 2 if ct_team == 1 else 1
    side_to_team = {'left': ct_team, 'right': t_team}

    c.execute(
        'SELECT mp.user_id, mp.team, u.game_id FROM match_players mp '
        'JOIN users u ON u.user_id = mp.user_id WHERE mp.match_id=?',
        (match_id,)
    )
    participants = c.fetchall()

    unmatched = []
    for side, team_num in side_to_team.items():
        ocr_players = (ocr.get('sides') or {}).get(side, [])
        team_participants = [
            (uid, gid) for uid, team, gid in participants
            if team == team_num and not is_bot(uid)
        ]
        used = set()
        for uid, gid in team_participants:
            gid_norm = _normalize_ocr_name(gid or '')
            best_idx, best_score = None, 0.0
            for idx, p in enumerate(ocr_players):
                if idx in used:
                    continue
                p_norm = _normalize_ocr_name(p['name'])
                if not gid_norm or not p_norm:
                    continue
                if gid_norm == p_norm:
                    score = 1.0
                elif gid_norm in p_norm or p_norm in gid_norm:
                    score = 0.9
                else:
                    score = _difflib.SequenceMatcher(None, gid_norm, p_norm).ratio()
                if score > best_score:
                    best_score, best_idx = score, idx
            if best_idx is not None and best_score >= 0.6:
                used.add(best_idx)
                p = ocr_players[best_idx]
                c.execute(
                    'UPDATE match_players SET kills=?, deaths=?, kd_entered=1 '
                    'WHERE match_id=? AND user_id=?',
                    (p['kills'], p['deaths'], match_id, uid)
                )
                c.execute(
                    'UPDATE users SET kills=kills+?, deaths=deaths+? WHERE user_id=?',
                    (p['kills'], p['deaths'], uid)
                )
            else:
                unmatched.append(uid)
    conn.commit()
    conn.close()
    return (len(unmatched) == 0), unmatched


def _reset_match_kd(match_id: int):
    """Сбрасывает распознанную статистику матча (используется при отклонении
    скриншота администратором, чтобы можно было загрузить и распознать заново)."""
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT user_id, kills, deaths, kd_entered FROM match_players WHERE match_id=?', (match_id,))
    rows = c.fetchall()
    for uid, kills, deaths, kd_entered in rows:
        if kd_entered:
            c.execute(
                'UPDATE users SET kills=MAX(0, kills-?), deaths=MAX(0, deaths-?) WHERE user_id=?',
                (kills or 0, deaths or 0, uid)
            )
    c.execute(
        'UPDATE match_players SET kills=0, deaths=0, kd_entered=0 WHERE match_id=?',
        (match_id,)
    )
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════════════════
#  LIVE-ОБНОВЛЕНИЕ ЛОББИ
# ══════════════════════════════════════════════════════════════════════════════

def _cleanup_uid(uid: int):
    _queue_join_time.pop(uid, None)
    _lobby_msg_info.pop(uid, None)


async def _refresh_lobby_messages(context: ContextTypes.DEFAULT_TYPE,
                                   lobby_id: int, mode: str, exclude_uid: int = None):
    """Обновляет сообщение лобби у всех игроков внутри него."""
    lobby  = lobby_queues[mode].get(lobby_id, [])
    total  = len(lobby)
    match  = MODES[mode]['match_size']
    markup = lobby_list_keyboard(mode, uid_in_lobby=True)
    players_text = queue_list_text(lobby_id, mode)
    text = (
        f"{E_CHECK} <b>Вы в лобби {lobby_id}  [{mode}]</b>  ({total}/{match})\n"
        f"Ожидаем ещё <b>{match - total}</b> игрок(ов)...\n\n"
        f"{E_PEOPLE} Игроки:\n{players_text}\n\n"
        f"{E_SEARCH} <i>Другие лобби:</i>"
    )
    _t, _e = _parse_msg(text)
    for uid in list(lobby):
        if uid == exclude_uid or is_bot(uid):
            continue
        info = _lobby_msg_info.get(uid)
        if not info:
            # Нет сохранённого сообщения — шлём новое и запоминаем
            try:
                sent = await context.bot.send_message(
                    chat_id=uid, text=_t, entities=_e, reply_markup=markup
                )
                _lobby_msg_info[uid] = (sent.chat_id, sent.message_id, lobby_id, mode)
            except Exception:
                pass
            continue
        chat_id, msg_id, _, _ = info
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=_t,
                entities=_e,
                reply_markup=markup,
            )
        except Exception:
            # Сообщение удалено или недоступно — шлём новое
            try:
                sent = await context.bot.send_message(
                    chat_id=uid, text=_t, entities=_e, reply_markup=markup
                )
                _lobby_msg_info[uid] = (sent.chat_id, sent.message_id, lobby_id, mode)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
#  /start  +  регистрация
# ══════════════════════════════════════════════════════════════════════════════

PENDING_REG_STEP_TTL_SEC = 2 * 3600  # старше 2 часов — считаем регистрацию заброшенной


def _set_pending_reg_step(uid: int, step) -> None:
    """Персистентно сохраняет шаг регистрации вместе с меткой времени (или
    очищает оба поля при step=None), чтобы его можно было восстановить после
    перезапуска бота — в отличие от context.user_data, который живёт только
    в памяти процесса. Метка времени нужна, чтобы не воскрешать регистрацию,
    заброшенную много дней назад."""
    ts = datetime.datetime.utcnow().isoformat() if step else None
    try:
        conn = sqlite3.connect('faceit_bot.db')
        conn.execute(
            'UPDATE users SET pending_reg_step=?, pending_reg_step_at=? WHERE user_id=?',
            (step, ts, uid)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    user = update.effective_user

    if not await _is_channel_subscribed(context.bot, user.id):
        await _reply_html(
            msg,
            "<tg-emoji emoji-id='5208585145702978799'>🔥</tg-emoji> Добро пожаловать в Moon Faceit!\n\n"
            "Для продолжения подпишись на канал, а затем нажми «Я подписался»:",
            reply_markup=_sub_gate_keyboard(),
        )
        return

    try:
        conn = sqlite3.connect('faceit_bot.db')
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO users (user_id, username, elo, level) VALUES (?,?,0,1)',
                  (user.id, user.username or str(user.id)))
        conn.commit()
        c.execute('SELECT first_name, game_id FROM users WHERE user_id = ?', (user.id,))
        row = c.fetchone()
        conn.close()
    except Exception as e:
        print(f"[start] DB error: {e}")
        await _reply_html(msg, "<tg-emoji emoji-id='5447644880824181073'>⚠️</tg-emoji> Ошибка базы данных. Попробуйте ещё раз.")
        return

    already_registered = row and row[0] and row[1]
    half_registered    = row and row[0] and not row[1]

    try:
        if already_registered:
            nick = row[0]
            await _reply_html(msg,
                f"<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> Привет! Рад тебя видеть, {nick}!\n\n<tg-emoji emoji-id='5406745015365943482'>⬇️</tg-emoji> Выбери действие:",
                reply_markup=inline_main_menu_keyboard()
            )
            return

        if half_registered:
            context.user_data['reg_step'] = 'awaiting_game_id'
            context.user_data['reg_nick'] = row[0]
            _set_pending_reg_step(user.id, 'awaiting_game_id')
            await _reply_html(msg,
                f"{E_WAITING} Ты уже ввёл никнейм {row[0]}, но не завершил регистрацию.\n\n"
                "<tg-emoji emoji-id='5319247469165433798'>🎮</tg-emoji> Введи свой ID в игре:"
            )
            return

        context.user_data['reg_step'] = 'awaiting_nickname'
        context.user_data.pop('reg_nick', None)
        _set_pending_reg_step(user.id, 'awaiting_nickname')
        await _reply_html(msg,
            "<tg-emoji emoji-id='5314508432315817301'>Ⓜ️</tg-emoji> Привет! Ты попал в Moon Faceit!\n\n"
            "Для начала пройди регистрацию.\n\n<tg-emoji emoji-id='5319247469165433798'>📝</tg-emoji> Введи свой никнейм:"
        )
    except Exception as e:
        print(f"[start] send error: {e}")
        try:
            await _reply_html(msg, "<tg-emoji emoji-id='5447644880824181073'>⚠️</tg-emoji> Произошла ошибка. Попробуйте /start ещё раз.")
        except Exception:
            pass


async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка «Я подписался» — перепроверяет подписку и, если всё ок,
    продолжает туда же, куда ведёт /start (регистрация или главное меню)."""
    query = update.callback_query
    uid = query.from_user.id

    if not await _is_channel_subscribed(context.bot, uid):
        await query.answer("Подписка не найдена. Подпишись на канал и попробуй снова.", show_alert=True)
        return

    await query.answer("Подписка подтверждена!")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await start(update, context)


async def registration_step_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get('reg_step')
    uid  = update.effective_user.id

    if not step:
        # reg_step хранится только в памяти (context.user_data) и теряется при
        # перезапуске бота. Если это произошло между вводом никнейма и ID —
        # пользователь пишет ID, а бот "молчит", что и выглядит как баг.
        # Восстанавливаем шаг из отдельной колонки pending_reg_step — она
        # выставляется/очищается только самим шагами регистрации, поэтому,
        # в отличие от проверки "first_name задан, а game_id — нет", не
        # путает мид-регистрацию с другими путями, которые тоже могут менять
        # first_name без game_id (админский /setnick, смена ника и т.п.).
        try:
            _conn = sqlite3.connect('faceit_bot.db')
            _row = _conn.execute(
                'SELECT first_name, pending_reg_step, pending_reg_step_at FROM users WHERE user_id=?',
                (uid,)
            ).fetchone()
            _conn.close()
        except Exception:
            _row = None

        _stale = False
        if _row and _row[1]:
            if not _row[2]:
                _stale = True  # шаг есть, а метки времени нет — не доверяем, считаем устаревшим
            else:
                try:
                    _age = (datetime.datetime.utcnow() - datetime.datetime.fromisoformat(_row[2])).total_seconds()
                    _stale = _age > PENDING_REG_STEP_TTL_SEC
                except Exception:
                    _stale = True  # битая метка времени — тоже не доверяем

        if _stale:
            # Регистрация была заброшена много дней назад — не воскрешаем её
            # случайным сообщением, просто чистим метку и ждём нового /start.
            _set_pending_reg_step(uid, None)
            return
        elif _row and _row[1] == 'awaiting_game_id':
            step = 'awaiting_game_id'
            context.user_data['reg_step'] = step
            context.user_data['reg_nick'] = _row[0] or ''
        elif _row and _row[1] == 'awaiting_nickname':
            step = 'awaiting_nickname'
            context.user_data['reg_step'] = step
        else:
            return

    text = update.message.text.strip()

    # Кнопка «Меню» всегда отменяет текущий шаг и возвращает в главное меню
    if text == "Меню":
        context.user_data.pop('reg_step', None)
        _set_pending_reg_step(uid, None)
        await action_show_menu(update, context)
        return

    if step == 'awaiting_nickname':
        import re as _re
        if not text or not _re.fullmatch(r'\S{2,32}', text):
            await _reply_html(update.message,
                "<tg-emoji emoji-id='5274099962655816924'>❗️</tg-emoji> Никнейм не должен:\n"
                "• Содержать пробелы\n"
                "• Быть короче 2 или длиннее 32 символов"
            )
            raise ApplicationHandlerStop
        # Сохраняем никнейм в БД сразу, а не только в context.user_data —
        # это позволяет восстановить шаг выше, если бот перезапустится до
        # того, как пользователь введёт ID. Шаг+таймстамп ставим через
        # _set_pending_reg_step, чтобы оба поля всегда обновлялись вместе.
        try:
            _conn = sqlite3.connect('faceit_bot.db')
            _conn.execute('UPDATE users SET first_name=? WHERE user_id=?', (text, uid))
            _conn.commit()
            _conn.close()
        except Exception:
            pass
        _set_pending_reg_step(uid, 'awaiting_game_id')
        context.user_data['reg_nick'] = text
        context.user_data['reg_step'] = 'awaiting_game_id'
        await _reply_html(update.message, f"{E_CHECK} Никнейм: <b>{text}</b>\n\n{EP_GAME} Введите ваш <b>ID в игре</b> (буквы и цифры):")
        raise ApplicationHandlerStop

    elif step == 'awaiting_game_id':
        import re as _re
        if not text or not _re.fullmatch(r'\S{1,40}', text):
            await _reply_html(update.message,
                "<tg-emoji emoji-id='5274099962655816924'>❗️</tg-emoji> ID в игре не должен:\n"
                "• Содержать пробелы\n"
                "• Быть длиннее 40 символов"
            )
            raise ApplicationHandlerStop
        conn = sqlite3.connect('faceit_bot.db')
        c = conn.cursor()
        nick = context.user_data.get('reg_nick', '')
        if not nick:
            # reg_nick мог не сохраниться в user_data (например, шаг был
            # восстановлен из БД без него) — не затираем first_name пустотой.
            c.execute('SELECT first_name FROM users WHERE user_id=?', (uid,))
            _row = c.fetchone()
            nick = (_row[0] if _row and _row[0] else '') or 'Игрок'
        c.execute(
            'UPDATE users SET first_name=?, game_id=?, pending_reg_step=NULL, pending_reg_step_at=NULL '
            'WHERE user_id=?',
            (nick, text, uid)
        )
        conn.commit()
        conn.close()
        context.user_data.pop('reg_step', None)
        context.user_data.pop('reg_nick', None)

        # Подтверждение обязательно должно дойти до пользователя — регистрация
        # в БД уже сохранена выше, поэтому при сбое красивого HTML-сообщения
        # (например, из-за необычных символов в никнейме) отправляем
        # простой текст, а не оставляем пользователя без единого ответа.
        try:
            await _reply_html(update.message, f"{E_MOON} <b>Регистрация завершена!</b>\n\n"
                f"{EP_USER} Никнейм: <b>{_esc(nick)}</b>\n"
                f"{EP_GAME} ID в игре: <code>{_esc(text)}</code>\n"
                f'<tg-emoji emoji-id="5231200819986047254">📊</tg-emoji> ELO: <b>0</b>', reply_markup=main_keyboard())
        except Exception:
            await update.message.reply_text(
                f"Регистрация завершена!\nНикнейм: {nick}\nID в игре: {text}\nELO: 0",
                reply_markup=main_keyboard()
            )
        await update.message.reply_text(
            "Выбери действие:", reply_markup=inline_main_menu_keyboard()
        )
        raise ApplicationHandlerStop

    elif step == 'change_nick':
        import re as _re
        if not text or not _re.fullmatch(r'\S{2,32}', text):
            await _reply_html(update.message,
                "<tg-emoji emoji-id='5274099962655816924'>❗️</tg-emoji> Никнейм не должен:\n"
                "• Содержать пробелы\n"
                "• Быть короче 2 или длиннее 32 символов"
            )
            raise ApplicationHandlerStop
        conn = sqlite3.connect('faceit_bot.db')
        c = conn.cursor()
        c.execute('SELECT last_nick_change FROM users WHERE user_id=?', (uid,))
        row = c.fetchone()
        if row and row[0]:
            last_change = datetime.datetime.fromisoformat(row[0])
            diff = datetime.datetime.utcnow() - last_change
            if diff.total_seconds() < 43200:
                remaining = 43200 - int(diff.total_seconds())
                hours, rem = divmod(remaining, 3600)
                minutes = rem // 60
                conn.close()
                await _reply_html(update.message, f"{E_WAITING} Сменить никнейм можно раз в 12 часов.\n"
                    f"Следующая смена доступна через: <b>{hours}ч {minutes}м</b>")
                raise ApplicationHandlerStop
        now_str = datetime.datetime.utcnow().isoformat()
        c.execute('UPDATE users SET first_name=?, last_nick_change=? WHERE user_id=?', (text, now_str, uid))
        conn.commit()
        conn.close()
        context.user_data.pop('reg_step', None)
        await _reply_html(update.message, f'{E_CHECK} Никнейм успешно изменён на <b>{text}</b>')
        raise ApplicationHandlerStop

    elif step == 'change_id':
        import re as _re
        if not text or not _re.fullmatch(r'\S{1,40}', text):
            await _reply_html(update.message,
                "<tg-emoji emoji-id='5274099962655816924'>❗️</tg-emoji> ID в игре не должен:\n"
                "• Содержать пробелы\n"
                "• Быть длиннее 40 символов"
            )
            raise ApplicationHandlerStop
        conn = sqlite3.connect('faceit_bot.db')
        c = conn.cursor()
        c.execute('SELECT last_id_change FROM users WHERE user_id=?', (uid,))
        row = c.fetchone()
        if row and row[0]:
            last_change = datetime.datetime.fromisoformat(row[0])
            diff = datetime.datetime.utcnow() - last_change
            if diff.total_seconds() < 43200:
                remaining = 43200 - int(diff.total_seconds())
                hours, rem = divmod(remaining, 3600)
                minutes = rem // 60
                conn.close()
                await _reply_html(update.message, f"{E_WAITING} Сменить ID можно раз в 12 часов.\n"
                    f"Следующая смена доступна через: <b>{hours}ч {minutes}м</b>")
                raise ApplicationHandlerStop
        now_str = datetime.datetime.utcnow().isoformat()
        c.execute('UPDATE users SET game_id=?, last_id_change=? WHERE user_id=?', (text, now_str, uid))
        conn.commit()
        conn.close()
        context.user_data.pop('reg_step', None)
        await _reply_html(update.message, f'{E_CHECK} ID в игре успешно изменён на <code>{text}</code>')
        raise ApplicationHandlerStop

    elif step == 'awaiting_promo':
        code = text.strip().upper()
        if not code:
            await _reply_html(update.message, f"{E_CROSS} Введите промокод.", reply_markup=_promo_cancel_keyboard())
            raise ApplicationHandlerStop
        conn = sqlite3.connect('faceit_bot.db')
        c = conn.cursor()
        c.execute('SELECT reward, activations_left FROM promo_codes WHERE code=?', (code,))
        row = c.fetchone()
        if not row or row[1] <= 0:
            conn.close()
            await _reply_html(
                update.message,
                f"{E_CROSS} Промокод <b>{_esc(code)}</b> недействителен или уже исчерпан.",
                reply_markup=_promo_cancel_keyboard()
            )
            raise ApplicationHandlerStop
        reward, activations_left = row
        c.execute('UPDATE promo_codes SET activations_left = activations_left - 1 WHERE code=?', (code,))
        c.execute('UPDATE users SET moon_coins = COALESCE(moon_coins, 0) + ? WHERE user_id=?', (reward, uid))
        c.execute('INSERT INTO promo_redemptions (code, user_id, reward) VALUES (?, ?, ?)', (code, uid, reward))
        conn.commit()
        conn.close()
        context.user_data.pop('reg_step', None)
        await _reply_html(
            update.message,
            f"{E_CHECK} Промокод <b>{_esc(code)}</b> активирован! Начислено {E_PRICE} <b>{reward}</b> Moon Coins."
        )
        raise ApplicationHandlerStop

    elif step == 'search_player':
        # Ищем игрока по нику или @юзернейму
        query_name = text.lstrip('@').strip()
        if not query_name:
            await _reply_html(update.message, f"{E_CROSS} Введите имя или @юзернейм для поиска.")
            raise ApplicationHandlerStop
        target = find_user_by_name(query_name)
        if not target:
            await _reply_html(
                update.message,
                f"{E_CROSS} Игрок <b>{query_name}</b> не найден.\n\n"
                f"Проверьте написание и попробуйте снова:",
                reply_markup=InlineKeyboardMarkup([[
                    _ebtn("Выйти", "6035130900075777681", 2, "search_exit")
                ]])
            )
            raise ApplicationHandlerStop
        target_id, _ = target
        # Сбрасываем шаг — пользователь увидит кнопки в профиле
        context.user_data.pop('reg_step', None)
        await _show_found_player(update.message, target_id, context.bot)
        raise ApplicationHandlerStop

    elif step == 'party_add_friend':
        query_name = text.lstrip('@').strip()
        if not query_name:
            await _reply_html(update.message, f"{E_CROSS} Введите никнейм или @юзернейм.")
            raise ApplicationHandlerStop
        target = find_user_by_name(query_name)
        if not target:
            await _reply_html(
                update.message,
                f"{E_CROSS} Игрок <b>{query_name}</b> не найден.\n\nПроверь написание и попробуй снова:",
            )
            raise ApplicationHandlerStop
        target_id, target_name = target
        if target_id == uid:
            await _reply_html(update.message, f"{E_CROSS} Нельзя добавить самого себя в пати.")
            raise ApplicationHandlerStop
        context.user_data.pop('reg_step', None)
        # Получаем имя приглашающего
        conn = sqlite3.connect('faceit_bot.db')
        c = conn.cursor()
        c.execute('SELECT COALESCE(first_name, username) FROM users WHERE user_id=?', (uid,))
        row = c.fetchone()
        conn.close()
        inviter_name = row[0] if row and row[0] else str(uid)
        # Отправляем приглашение другу
        invite_kb = InlineKeyboardMarkup([[
            _ebtn('Принять', "5206607081334906820", 2, f"party_invite_accept_{uid}"),
            _ebtn('Отклонить', "5210952531676504517", 2, f"party_invite_decline_{uid}"),
        ]])
        try:
            await _send_html(
                context.bot, target_id,
                f"<tg-emoji emoji-id='5956527135928617699'>👥</tg-emoji> <b>Вас хотят добавить в пати!</b>\n\n"
                f"Игрок <b>{inviter_name}</b> приглашает вас в свою пати.",
                reply_markup=invite_kb,
            )
            await _reply_html(
                update.message,
                f"{E_CHECK} Приглашение отправлено игроку <b>{target_name}</b>!"
            )
        except Exception:
            await _reply_html(update.message, f"{E_CROSS} Не удалось отправить приглашение игроку <b>{target_name}</b>.")
        raise ApplicationHandlerStop


# ══════════════════════════════════════════════════════════════════════════════
#  МЕНЮ
# ══════════════════════════════════════════════════════════════════════════════

async def action_show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT COALESCE(first_name, username) FROM users WHERE user_id = ?', (uid,))
    row = c.fetchone()
    conn.close()
    nick = row[0] if row and row[0] else update.effective_user.first_name or str(uid)
    await _reply_html(update.message, f'<tg-emoji emoji-id="5458797798495377338">🔥</tg-emoji> Привет! Рад тебя видеть, <b>{nick}</b>!\n\n'
        f'<tg-emoji emoji-id="5406745015365943482">⬇️</tg-emoji> Выбери действие:', reply_markup=inline_main_menu_keyboard())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action_map = {
        "menu_profile":     action_profile,
        "menu_find":        action_find_match,
        "menu_leaderboard": action_leaderboard,
        "menu_rules":       action_rules,
        "menu_report":      action_report,
        "menu_season":      action_season,
        "menu_support":     action_support,
        "menu_history":     action_history,
        "menu_shop":        action_shop,
        "menu_donate":      action_donate_menu,
        "menu_search":      action_search_player,
        "menu_tasks":       action_tasks,
        "menu_party":       action_party,
        "menu_promo":       action_promo,
        "menu_promo_cancel": action_promo_cancel,
    }
    handler = action_map.get(query.data)
    if handler:
        await handler(update, context)


# ══════════════════════════════════════════════════════════════════════════════
#  ЗАДАНИЯ
# ══════════════════════════════════════════════════════════════════════════════

def _tasks_get_user_stats(uid: int) -> dict:
    """Возвращает словарь со статистикой игрока для проверки заданий.
    Не бросает исключений — при сбое подключения к БД возвращает нулевую
    статистику вместо падения (текстовый вид заданий не должен пропадать)."""
    today = datetime.datetime.utcnow().strftime('%Y-%m-%d')
    row = mr = dr = None
    try:
        conn = sqlite3.connect('faceit_bot.db')
        try:
            c = conn.cursor()

            # Общая статистика из match_players
            try:
                c.execute('''
                    SELECT COUNT(*),
                           COALESCE(SUM(CASE WHEN mp.team = tm.winner THEN 1 ELSE 0 END), 0)
                    FROM match_players mp
                    JOIN team_matches tm ON mp.match_id = tm.match_id
                    WHERE mp.user_id = ? AND tm.status = "finished"
                ''', (uid,))
                row = c.fetchone()
            except Exception:
                row = None

            # Per-mode матчи / уровни (колонки могут отсутствовать в старой БД)
            try:
                c.execute(
                    'SELECT matches_5v5, matches_3v3, matches_2v2, '
                    'level_5v5, level_3v3, level_2v2 FROM users WHERE user_id=?', (uid,)
                )
                mr = c.fetchone()
            except Exception:
                mr = None

            # Сегодняшние матчи (UTC)
            try:
                c.execute('''
                    SELECT COUNT(*),
                           COALESCE(SUM(CASE WHEN mp.team = tm.winner THEN 1 ELSE 0 END), 0)
                    FROM match_players mp
                    JOIN team_matches tm ON mp.match_id = tm.match_id
                    WHERE mp.user_id = ? AND tm.status = "finished"
                      AND DATE(tm.timestamp) = ?
                ''', (uid, today))
                dr = c.fetchone()
            except Exception:
                dr = None
        finally:
            conn.close()
    except Exception:
        pass

    return {
        'matches':       (row[0] or 0) if row else 0,
        'wins':          (row[1] or 0) if row else 0,
        'matches_5v5':   (mr[0] or 0) if mr else 0,
        'matches_3v3':   (mr[1] or 0) if mr else 0,
        'matches_2v2':   (mr[2] or 0) if mr else 0,
        'lvl_5v5':       (mr[3] or 1) if mr else 1,
        'lvl_3v3':       (mr[4] or 1) if mr else 1,
        'lvl_2v2':       (mr[5] or 1) if mr else 1,
        'today_matches': (dr[0] or 0) if dr else 0,
        'today_wins':    (dr[1] or 0) if dr else 0,
        'today_date':    today,
    }


def _tasks_get_claimed(uid: int) -> set:
    """Возвращает множество task_id уже полученных milestone-заданий.
    Не бросает исключений — при сбое БД (например, database is locked)
    возвращает пустое множество, чтобы текстовый вид заданий не падал целиком."""
    try:
        conn = sqlite3.connect('faceit_bot.db')
        try:
            c = conn.cursor()
            c.execute('SELECT task_id FROM user_tasks WHERE user_id=? AND claimed=1', (uid,))
            return {r[0] for r in c.fetchall()}
        finally:
            conn.close()
    except Exception:
        return set()


def _tasks_get_daily_claimed(uid: int, today: str) -> set:
    """Возвращает множество task_id ежедневных заданий, полученных сегодня.
    Не бросает исключений — при сбое БД возвращает пустое множество (см. выше)."""
    try:
        conn = sqlite3.connect('faceit_bot.db')
        try:
            c = conn.cursor()
            c.execute(
                'SELECT task_id FROM user_daily_tasks WHERE user_id=? AND task_date=? AND claimed=1',
                (uid, today)
            )
            return {r[0] for r in c.fetchall()}
        finally:
            conn.close()
    except Exception:
        return set()


def _mini_bar(current: int, total: int, width: int = 6) -> str:
    """Мини прогресс-бар: [████░░]"""
    filled = round(current / total * width) if total > 0 else 0
    return f"[{'█' * filled}{'░' * (width - filled)}]"


def _build_tasks_text_and_kb(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    """Строит текст и клавиатуру вкладки заданий."""
    stats   = _tasks_get_user_stats(uid)
    claimed = _tasks_get_claimed(uid)
    d_claimed = _tasks_get_daily_claimed(uid, stats['today_date'])

    lines = [f"{E_DIAMOND} <b>Задания Moon Faceit</b>\n"]
    kb_rows: list[list] = []

    # ── Milestone-задания ────────────────────────────────────────────────────
    lines.append(f"<b><tg-emoji emoji-id='5282843764451195532'>📋</tg-emoji> Разовые задания</b>")
    for t in TASKS_MILESTONE:
        done    = t['check'](stats)
        is_cl   = t['id'] in claimed
        cur, mx = t['progress'](stats)

        if is_cl:
            status = f"{E_CHECK} <i>Получено</i>"
        elif done:
            status = f"<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Готово!</b>"
        else:
            status = f"{_mini_bar(cur, mx)} {cur}/{mx}"

        reward_str = f"+{t['reward']} {E_DIAMOND}"
        lines.append(f"\n{t['title']} — <b>{reward_str}</b>\n  {t['desc']}\n  {status}")

        if done and not is_cl:
            kb_rows.append([_ebtn(
                f"Получить  +{t['reward']} 💎  —  {t['desc']}",
                "5427168083074628963", 2,
                f"task_claim_ms_{t['id']}"
            )])

    # ── Ежедневные задания ───────────────────────────────────────────────────
    lines.append(f"\n\n<b>🗓 Ежедневные задания</b>  <i>(сброс в 00:00 UTC)</i>")
    for t in TASKS_DAILY:
        is_cl  = t['id'] in d_claimed
        cur    = stats['today_matches'] if t['type'] == 'matches' else stats['today_wins']
        target = t['target']
        done   = cur >= target

        if is_cl:
            status = f"{E_CHECK} <i>Получено</i>"
        elif done:
            status = f"<tg-emoji emoji-id='5325547803936572038'>✨</tg-emoji> <b>Готово!</b>"
        else:
            status = f"{_mini_bar(cur, target)} {cur}/{target}"

        reward_str = f"+{t['reward']} {E_DIAMOND}"
        lines.append(f"\n{t['title']} — <b>{reward_str}</b>\n  {t['desc']}\n  {status}")

        if done and not is_cl:
            kb_rows.append([_ebtn(
                f"Получить  +{t['reward']} 💎  —  {t['desc']}",
                "5427168083074628963", 2,
                f"task_claim_dy_{t['id']}"
            )])

    # Кнопка обновить + выйти в меню
    kb_rows.append([
        _ebtn("Обновить",  "5346321684574003384", 2, "tasks_refresh"),
    ])

    return "\n".join(lines), InlineKeyboardMarkup(kb_rows)


async def _build_party_players(uid: int, bot) -> list[dict]:
    """Собирает список игроков пати (creator первым) для карточки."""
    import io as _io, base64 as _b64

    party_info = _get_party_of(uid)
    if party_info:
        creator_uid, members = party_info
        all_uids = [creator_uid] + [m for m in members if m != creator_uid]
    else:
        all_uids = [uid]
        _parties[uid] = []

    players = []
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    for idx, puid in enumerate(all_uids):
        c.execute(
            'SELECT COALESCE(first_name, username), elo, kills, deaths FROM users WHERE user_id=?',
            (puid,)
        )
        row = c.fetchone()
        if not row:
            continue
        name, elo, kills, deaths = row
        kd = round(kills / deaths, 2) if deaths else (float(kills) if kills else 0.0)
        avatar_b64 = None
        try:
            photos = await bot.get_user_profile_photos(puid, limit=1)
            if photos.total_count > 0:
                file_obj = await bot.get_file(photos.photos[0][-1].file_id)
                buf = _io.BytesIO()
                await file_obj.download_to_memory(buf)
                avatar_b64 = _b64.b64encode(buf.getvalue()).decode()
        except Exception:
            pass
        players.append({
            'name': name or str(puid),
            'elo': elo or 0,
            'kd': kd,
            'avatar_b64': avatar_b64,
            'is_creator': idx == 0,
        })
    conn.close()
    return players


async def action_party(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Раздел пати — рендерит лобби-карточку."""
    uid = update.effective_user.id
    if not get_user(uid):
        await update.effective_message.reply_text("Сначала нажмите /start.")
        return

    keyboard = InlineKeyboardMarkup([
        [
            _ebtn('Добавить друга', "5271604874419647061", 2, "party_add_friend"),
            _ebtn('Выйти из пати',  "5240241223632954241", 2, "party_exit"),
        ]
    ])

    party_info = _get_party_of(uid)
    members_count = 1 + len(party_info[1]) if party_info else 1

    caption_t, caption_e = _parse_msg(
        f"{E_TEAM} <b>Пати</b>  {members_count}/5\n\n"
        "Добавляй друзей и залетайте вместе в очередь!"
    )

    try:
        from card_renderer import generate_lobby_card
        players = await _build_party_players(uid, context.bot)
        png = await generate_lobby_card(players, mode="5v5")
        await update.effective_message.reply_photo(
            png,
            caption=caption_t,
            caption_entities=caption_e,
            reply_markup=keyboard,
        )
    except Exception:
        await _reply_html(
            update.effective_message,
            f"{E_TEAM} <b>Пати</b>  {members_count}/5\n\n"
            "Добавляй друзей и залетайте вместе в очередь!",
            reply_markup=keyboard,
        )


async def party_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if query.data == "party_exit":
        # Убираем пользователя из пати
        if uid in _parties:
            # Создатель выходит — распускаем пати, уведомляем членов
            for member in list(_parties[uid]):
                try:
                    await _send_html(context.bot, member,
                        f"{E_CROSS} Создатель пати вышел — пати распущена.")
                except Exception:
                    pass
            del _parties[uid]
        else:
            for creator, members in list(_parties.items()):
                if uid in members:
                    members.remove(uid)
                    try:
                        await _send_html(context.bot, creator,
                            f"{E_CROSS} Игрок покинул пати.")
                    except Exception:
                        pass
                    break
        await action_show_menu(update, context)

    elif query.data == "party_add_friend":
        context.user_data['reg_step'] = 'party_add_friend'
        await _send_html(
            context.bot, uid,
            f"{E_TEAM} <b>Добавить друга в пати</b>\n\n"
            "Введи юзернейм или никнейм друга чтобы добавить его в пати!"
        )

    elif query.data.startswith("party_invite_accept_"):
        inviter_id = int(query.data.split("_")[-1])
        conn = sqlite3.connect('faceit_bot.db')
        c = conn.cursor()
        c.execute('SELECT COALESCE(first_name, username) FROM users WHERE user_id=?', (uid,))
        row = c.fetchone()
        conn.close()
        accepter_name = row[0] if row and row[0] else str(uid)

        # Добавляем принявшего в пати создателя
        if inviter_id not in _parties:
            _parties[inviter_id] = []
        if uid not in _parties[inviter_id]:
            _parties[inviter_id].append(uid)

        await _edit_html(query,
            f"{E_CHECK} <b>Вы приняли приглашение в пати!</b>\n\n"
            f"Ждите — хозяин пати скоро начнёт игру. {E_TEAM}"
        )
        try:
            await _send_html(
                context.bot, inviter_id,
                f'{E_CONFIRM} <b>{accepter_name}</b> принял(а) ваше приглашение в пати!'
            )
        except Exception:
            pass

    elif query.data.startswith("party_invite_decline_"):
        inviter_id = int(query.data.split("_")[-1])
        conn = sqlite3.connect('faceit_bot.db')
        c = conn.cursor()
        c.execute('SELECT COALESCE(first_name, username) FROM users WHERE user_id=?', (uid,))
        row = c.fetchone()
        conn.close()
        decliner_name = row[0] if row and row[0] else str(uid)
        await _edit_html(query,
            f'{E_CROSS} Вы отклонили приглашение в пати.'
        )
        try:
            await _send_html(
                context.bot, inviter_id,
                f'{E_CROSS} <b>{decliner_name}</b> отклонил(а) ваше приглашение в пати.'
            )
        except Exception:
            pass


async def action_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает вкладку заданий — текстом, плюс PNG-карточка если рендер доступен."""
    uid = update.effective_user.id
    user = get_user(uid)
    if not user:
        await update.effective_message.reply_text("Сначала нажмите /start.")
        return

    # PNG-карточка — необязательное украшение. Пробуем её только если рендер
    # вообще подключён (CARDS_ENABLED); если нет — не тратим время на попытку
    # и не шумим в логах, а сразу переходим к тексту.
    if CARDS_ENABLED:
        try:
            stats = _tasks_get_user_stats(uid)

            daily_list, special_list = [], []

            for t in TASKS_DAILY:
                prog = stats.get(f"today_{t['type']}", 0)
                daily_list.append({
                    'icon': '🎮', 'title': _re_html.sub(r'<[^>]+>', '', t['title']),
                    'desc': t['desc'], 'progress': prog,
                    'total': t['target'], 'reward': t['reward'],
                })

            for t in TASKS_MILESTONE:
                prog, total = t['progress'](stats)
                special_list.append({
                    'icon': '⭐', 'title': _re_html.sub(r'<[^>]+>', '', t['title']),
                    'desc': t['desc'], 'progress': prog,
                    'total': total, 'reward': t['reward'],
                })

            try:
                import sqlite3 as _sq
                _c2 = _sq.connect('faceit_bot.db')
                try:
                    _cur2 = _c2.cursor()
                    _cur2.execute('SELECT moon_coins FROM users WHERE user_id=?', (uid,))
                    _r2 = _cur2.fetchone()
                    moon_coins = int(_r2[0] or 0) if _r2 else 0
                finally:
                    _c2.close()
            except Exception:
                moon_coins = 0

            png = await generate_tasks_card(
                daily_tasks=daily_list if daily_list else None,
                weekly_tasks=None,
                special_tasks=special_list if special_list else None,
                moon_coins=moon_coins,
            )
            # Карточка + текст с кнопками как два отдельных сообщения
            await update.effective_message.reply_photo(png)
        except Exception:
            import traceback
            print(f"[action_tasks] Ошибка рендера карточки заданий для uid={uid}:")
            traceback.print_exc()

    # Текстовый вид всегда отправляется — это основной способ показать задания,
    # PNG выше лишь дополняет его, если рендер настроен.
    try:
        text, kb = _build_tasks_text_and_kb(uid)
        await _reply_html(update.effective_message, text, reply_markup=kb)
    except Exception as _tasks_err:
        import traceback as _tb_mod
        _tb = _tb_mod.format_exc()
        print(f"[action_tasks] Ошибка текстового вида заданий uid={uid}: {_tasks_err}\n{_tb}")
        await update.effective_message.reply_text(
            f"{E_CROSS} Не удалось загрузить задания, попробуйте ещё раз."
        )


async def tasks_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает кнопки во вкладке заданий: получить награду / обновить."""
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    data = query.data

    # ── Обновить ────────────────────────────────────────────────────────────
    if data == "tasks_refresh":
        try:
            text, kb = _build_tasks_text_and_kb(uid)
            await _edit_html(query, text, reply_markup=kb)
        except Exception:
            await query.answer("Не удалось обновить. Попробуйте ещё раз.", show_alert=True)
        return

    # ── Получить milestone ───────────────────────────────────────────────────
    if data.startswith("task_claim_ms_"):
        task_id = data[len("task_claim_ms_"):]
        task    = next((t for t in TASKS_MILESTONE if t['id'] == task_id), None)
        if not task:
            await query.answer("Задание не найдено.", show_alert=True)
            return

        # Финальная проверка выполнения (до входа в БД)
        stats = _tasks_get_user_stats(uid)
        if not task['check'](stats):
            await query.answer("Задание ещё не выполнено.", show_alert=True)
            return

        # Атомарная вставка — если строка уже существует (claimed=1),
        # INSERT OR IGNORE ничего не делает и rowcount == 0.
        conn = sqlite3.connect('faceit_bot.db')
        c = conn.cursor()
        c.execute(
            'INSERT OR IGNORE INTO user_tasks (user_id, task_id, claimed) VALUES (?,?,1)',
            (uid, task_id)
        )
        inserted = c.rowcount  # 1 — первый клик, 0 — уже получено
        if inserted:
            c.execute(
                'UPDATE users SET moon_coins=moon_coins+? WHERE user_id=?',
                (task['reward'], uid)
            )
        conn.commit()
        c.execute('SELECT moon_coins FROM users WHERE user_id=?', (uid,))
        _row = c.fetchone()
        new_coins = _row[0] if _row else 0
        conn.close()

        if not inserted:
            await query.answer("Награда уже получена!", show_alert=True)
            return

        await query.answer(f"🎉 +{task['reward']} Moon Coins!", show_alert=True)
        try:
            await _send_html(
                context.bot, uid,
                f"{E_DIAMOND} <b>Задание выполнено!</b>\n\n"
                f"{task['title']}\n"
                f"{task['desc']}\n\n"
                f"Награда: <b>+{task['reward']} Moon Coins</b> <tg-emoji emoji-id='5461151367559141950'>🎉</tg-emoji>\n"
                f"Баланс: <b>{new_coins}</b> {E_DIAMOND}"
            )
        except Exception:
            pass

        text, kb = _build_tasks_text_and_kb(uid)
        await _edit_html(query, text, reply_markup=kb)
        return

    # ── Получить ежедневное ──────────────────────────────────────────────────
    if data.startswith("task_claim_dy_"):
        task_id = data[len("task_claim_dy_"):]
        task    = next((t for t in TASKS_DAILY if t['id'] == task_id), None)
        if not task:
            await query.answer("Задание не найдено.", show_alert=True)
            return

        today = datetime.datetime.utcnow().strftime('%Y-%m-%d')

        # Финальная проверка выполнения
        stats = _tasks_get_user_stats(uid)
        cur   = stats['today_matches'] if task['type'] == 'matches' else stats['today_wins']
        if cur < task['target']:
            await query.answer("Задание ещё не выполнено.", show_alert=True)
            return

        # Атомарная вставка — предотвращает двойное начисление
        conn = sqlite3.connect('faceit_bot.db')
        c = conn.cursor()
        c.execute(
            'INSERT OR IGNORE INTO user_daily_tasks (user_id, task_id, task_date, claimed) '
            'VALUES (?,?,?,1)',
            (uid, task_id, today)
        )
        inserted = c.rowcount
        if inserted:
            c.execute(
                'UPDATE users SET moon_coins=moon_coins+? WHERE user_id=?',
                (task['reward'], uid)
            )
        conn.commit()
        c.execute('SELECT moon_coins FROM users WHERE user_id=?', (uid,))
        _row = c.fetchone()
        new_coins = _row[0] if _row else 0
        conn.close()

        if not inserted:
            await query.answer("Награда уже получена!", show_alert=True)
            return

        await query.answer(f"🎉 +{task['reward']} Moon Coins!", show_alert=True)
        try:
            await _send_html(
                context.bot, uid,
                f"{E_DIAMOND} <b>Ежедневное задание выполнено!</b>\n\n"
                f"{task['title']}\n"
                f"{task['desc']}\n\n"
                f"Награда: <b>+{task['reward']} Moon Coins</b> <tg-emoji emoji-id='5461151367559141950'>🎉</tg-emoji>\n"
                f"Баланс: <b>{new_coins}</b> {E_DIAMOND}"
            )
        except Exception:
            pass

        text, kb = _build_tasks_text_and_kb(uid)
        await _edit_html(query, text, reply_markup=kb)


# ══════════════════════════════════════════════════════════════════════════════
#  ПОИСК ИГРОКА
# ══════════════════════════════════════════════════════════════════════════════

def _player_search_result_keyboard() -> InlineKeyboardMarkup:
    """Кнопки под профилем найденного игрока."""
    return InlineKeyboardMarkup([
        [_ebtn("Искать другого", "5231012545799666522", 2, "search_another")],
        [_ebtn("Выйти",          "6035130900075777681", 2, "search_exit")],
    ])


async def action_search_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запускает поиск — просит ввести ник или юзернейм."""
    context.user_data['reg_step'] = 'search_player'
    await _reply_html(
        update.effective_message,
        f"{E_SEARCH} <b>Поиск игрока</b>\n\n"
        f"Введите <b>никнейм</b> или <b>@юзернейм</b> игрока:"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ПРОМОКОД
# ══════════════════════════════════════════════════════════════════════════════

_PROMO_EMOJI = '<tg-emoji emoji-id="5224607267797606837">☄️</tg-emoji>'


def _promo_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Отмена", callback_data="menu_promo_cancel")
    ]])


async def action_promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открывает вкладку ввода промокода."""
    context.user_data['reg_step'] = 'awaiting_promo'
    await _reply_html(
        update.effective_message,
        f"{_PROMO_EMOJI} <b>Промокод</b>\n\nВведите промокод:",
        reply_markup=_promo_cancel_keyboard()
    )


async def action_promo_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отменяет ввод промокода и возвращает в главное меню."""
    query = update.callback_query
    context.user_data.pop('reg_step', None)
    uid = update.effective_user.id
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT COALESCE(first_name, username) FROM users WHERE user_id = ?', (uid,))
    row = c.fetchone()
    conn.close()
    nick = row[0] if row and row[0] else update.effective_user.first_name or str(uid)
    await _edit_html(
        query,
        f'<tg-emoji emoji-id="5458797798495377338">🔥</tg-emoji> Привет! Рад тебя видеть, <b>{nick}</b>!\n\n'
        f'<tg-emoji emoji-id="5406745015365943482">⬇️</tg-emoji> Выбери действие:',
        reply_markup=inline_main_menu_keyboard()
    )


async def _show_found_player(msg_obj, uid_found: int, bot=None):
    """Формирует и отправляет профиль найденного игрока."""
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute(
        'SELECT COALESCE(first_name, username), warns, game_id, moon_coins, '
        'elo_5v5, matches_5v5, level_5v5, '
        'elo_3v3, matches_3v3, level_3v3, '
        'elo_2v2, matches_2v2, level_2v2, '
        'is_banned, mvp_count '
        'FROM users WHERE user_id=?', (uid_found,)
    )
    row = c.fetchone()
    if not row:
        conn.close()
        await _reply_html(msg_obj, f"{E_CROSS} Игрок не найден.")
        return

    display_name  = row[0] or str(uid_found)
    warns_count   = row[1] or 0
    game_id_val   = row[2]
    moon_coins    = row[3] or 0
    is_banned     = row[13] or 0
    mvp_count     = row[14] or 0

    mode_stats = {
        '5v5': {'elo': row[4]  or 0, 'matches': row[5]  or 0, 'level': row[6]  or 1, 'calib': 10},
        '3v3': {'elo': row[7]  or 0, 'matches': row[8]  or 0, 'level': row[9]  or 1, 'calib': 5},
        '2v2': {'elo': row[10] or 0, 'matches': row[11] or 0, 'level': row[12] or 1, 'calib': 5},
    }

    c.execute('''
        SELECT
            COUNT(*),
            COALESCE(SUM(CASE WHEN mp.team = tm.winner THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(mp.kills),  0),
            COALESCE(SUM(mp.deaths), 0)
        FROM match_players mp
        JOIN team_matches tm ON mp.match_id = tm.match_id
        WHERE mp.user_id = ? AND tm.status = 'finished'
    ''', (uid_found,))
    stat = c.fetchone()
    conn.close()

    matches = stat[0] if stat else 0
    wins    = stat[1] if stat else 0
    kills   = stat[2] if stat else 0
    deaths  = stat[3] if stat else 0
    losses  = matches - wins
    winrate = round(wins / matches * 100) if matches > 0 else 0
    kd_str  = f"{kills/deaths:.2f}" if deaths > 0 else f"{float(kills):.2f}"

    creator_ids = get_creator_ids()
    admin_ids   = get_admin_ids()
    badge = creator_badge(uid_found, creator_ids) + admin_badge(uid_found, admin_ids) + official_badge_str(uid_found)

    gid_line   = f"\n{E_INFO}  ID в игре — <code>{game_id_val}</code>" if game_id_val else ""
    ban_line   = f"\n{E_BAN}  <b>Игрок заблокирован</b>" if is_banned else ""
    warn_icons = '<tg-emoji emoji-id="5393551318214257273">🟡</tg-emoji>' * warns_count

    MODE_ICONS = {'5v5': E_SWORD, '3v3': E_SHIELD, '2v2': E_HANDSHAKE}
    calib_lock = LEVEL_ICONS.get(0, '🔒')
    mode_lines = []
    for mname in ('5v5', '3v3', '2v2'):
        ms = mode_stats[mname]
        mico = MODE_ICONS[mname]
        if ms['matches'] < ms['calib']:
            mode_lines.append(
                f"{mico} <b>{mname}</b> — {calib_lock} Калибровка <b>{ms['matches']}/{ms['calib']}</b>"
            )
        else:
            lvl_ico = LEVEL_ICONS.get(ms['level'], str(ms['level']))
            mode_lines.append(
                f"{mico} <b>{mname}</b> — {lvl_ico} <code>{ms['elo']}</code> ELO"
            )
    mode_block = "\n".join(mode_lines)

    text = (
        f"{EPV_USER} <b>{display_name}{badge}</b>{gid_line}{ban_line}\n\n"
        f"{mode_block}\n\n"
        f"{EPV_MATCHES}  Матчей — <b>{matches}</b>\n"
        f"{EPV_WL}  Побед / Поражений — <b>{wins}</b> / <b>{losses}</b>\n"
        f"{EP_WR}  Винрейт — <b>{winrate}%</b>\n"
        f"{EP_KD}  К/Д — <b>{kd_str}</b>\n\n"
        f"{EP_WARN}  Варны — {warn_icons} <b>{warns_count}/3</b>\n"
        f"{E_PRICE}  Moon Coins — <b>{moon_coins}</b>"
    )
    # Генерируем PNG-карточку профиля найденного игрока
    try:
        png = await generate_profile_card(uid_found, bot)
        caption_text, caption_entities = _parse_msg(text)
        _TG_CAP_MAX = 1024
        if len(caption_text) > _TG_CAP_MAX:
            caption_text = caption_text[:_TG_CAP_MAX - 1] + "…"
            if caption_entities:
                caption_entities = [
                    e for e in caption_entities if e.offset + e.length <= len(caption_text)
                ]
        await msg_obj.reply_photo(
            photo=png,
            caption=caption_text,
            caption_entities=caption_entities or [],
            reply_markup=_player_search_result_keyboard(),
        )
    except Exception:
        # Fallback: просто текст
        await _reply_html(msg_obj, text, reply_markup=_player_search_result_keyboard())


async def search_result_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок 'Искать другого' и 'Выйти' под профилем игрока."""
    query = update.callback_query
    await query.answer()

    if query.data == "search_another":
        context.user_data['reg_step'] = 'search_player'
        await _edit_html(
            query,
            f"{E_SEARCH} <b>Поиск игрока</b>\n\n"
            f"Введите <b>никнейм</b> или <b>@юзернейм</b> игрока:"
        )
    elif query.data == "search_exit":
        context.user_data.pop('reg_step', None)
        uid = update.effective_user.id
        conn = sqlite3.connect('faceit_bot.db')
        c = conn.cursor()
        c.execute('SELECT COALESCE(first_name, username) FROM users WHERE user_id = ?', (uid,))
        row = c.fetchone()
        conn.close()
        nick = row[0] if row and row[0] else update.effective_user.first_name or str(uid)
        await _edit_html(
            query,
            f'<tg-emoji emoji-id="5458797798495377338">🔥</tg-emoji> Привет! Рад тебя видеть, <b>{nick}</b>!\n\n'
            f'<tg-emoji emoji-id="5406745015365943482">⬇️</tg-emoji> Выбери действие:',
            reply_markup=inline_main_menu_keyboard()
        )


# ══════════════════════════════════════════════════════════════════════════════
#  ПРОФИЛЬ
# ══════════════════════════════════════════════════════════════════════════════

async def action_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    user = get_user(uid)
    if not user:
        await update.effective_message.reply_text("Сначала нажмите /start.")
        return
    badge    = creator_badge(uid, get_creator_ids()) + admin_badge(uid, get_admin_ids()) + official_badge_str(uid)
    conn_w   = sqlite3.connect('faceit_bot.db')
    c_w      = conn_w.cursor()
    c_w.execute(
        'SELECT warns, game_id, moon_coins, '
        'elo_5v5, matches_5v5, level_5v5, '
        'elo_3v3, matches_3v3, level_3v3, '
        'elo_2v2, matches_2v2, level_2v2, '
        'mvp_count '
        'FROM users WHERE user_id=?', (uid,)
    )
    w_row    = c_w.fetchone()
    warns_count = (w_row[0] or 0) if w_row else 0
    game_id_val = w_row[1] if w_row and w_row[1] else None
    moon_coins  = (w_row[2] or 0) if w_row else 0
    mvp_count   = (w_row[12] or 0) if w_row else 0
    mode_stats = {
        '5v5': {'elo': (w_row[3]  or 0) if w_row else 0, 'matches': (w_row[4]  or 0) if w_row else 0, 'level': (w_row[5]  or 1) if w_row else 1, 'calib': 10},
        '3v3': {'elo': (w_row[6]  or 0) if w_row else 0, 'matches': (w_row[7]  or 0) if w_row else 0, 'level': (w_row[8]  or 1) if w_row else 1, 'calib': 5},
        '2v2': {'elo': (w_row[9]  or 0) if w_row else 0, 'matches': (w_row[10] or 0) if w_row else 0, 'level': (w_row[11] or 1) if w_row else 1, 'calib': 5},
    }
    c_w.execute('''
        SELECT
            COUNT(*)                                                           AS total,
            COALESCE(SUM(CASE WHEN mp.team = tm.winner THEN 1 ELSE 0 END), 0) AS wins,
            COALESCE(SUM(mp.kills),  0)                                        AS kills,
            COALESCE(SUM(mp.deaths), 0)                                        AS deaths
        FROM match_players mp
        JOIN team_matches tm ON mp.match_id = tm.match_id
        WHERE mp.user_id = ? AND tm.status = 'finished'
    ''', (uid,))
    stat    = c_w.fetchone()
    conn_w.close()
    matches = stat[0] if stat else 0
    wins    = stat[1] if stat else 0
    kills   = stat[2] if stat else 0
    deaths  = stat[3] if stat else 0
    losses  = matches - wins
    winrate = round(wins / matches * 100) if matches > 0 else 0
    kd_str  = f"{kills/deaths:.2f}" if deaths > 0 else f"{float(kills):.2f}"
    warn_icons = '<tg-emoji emoji-id="5393551318214257273">🟡</tg-emoji>' * warns_count
    gid_line   = f"\n{E_INFO}  ID в игре — <code>{game_id_val}</code>" if game_id_val else ""

    MODE_ICONS_PROFILE = {'5v5': E_SWORD, '3v3': E_SHIELD, '2v2': E_HANDSHAKE}
    calib_lock = LEVEL_ICONS.get(0, '🔒')
    mode_lines = []
    for mname in ('5v5', '3v3', '2v2'):
        ms   = mode_stats[mname]
        mico = MODE_ICONS_PROFILE[mname]
        if ms['matches'] < ms['calib']:
            mode_lines.append(
                f"{mico} <b>{mname}</b> — {calib_lock} Калибровка <b>{ms['matches']}/{ms['calib']}</b>"
            )
        else:
            lvl_ico = LEVEL_ICONS.get(ms['level'], str(ms['level']))
            mode_lines.append(
                f"{mico} <b>{mname}</b> — {lvl_ico} <code>{ms['elo']}</code> ELO"
            )
    mode_block = "\n".join(mode_lines)

    caption_html = (
        f"{EPV_USER} <b>{user[1]}{badge}</b>{gid_line}\n\n"
        f"{mode_block}\n\n"
        f"{EPV_MATCHES}  Матчей — <b>{matches}</b>\n"
        f"{EPV_WL}  Побед / Поражений — <b>{wins}</b> / <b>{losses}</b>\n"
        f"{EP_WR}  Винрейт — <b>{winrate}%</b>\n"
        f"{EP_KD}  К/Д — <b>{kd_str}</b>\n\n"
        f"{EP_WARN}  Варны — {warn_icons} <b>{warns_count}/3</b>\n"
        f"{E_PRICE}  Moon Coins — <b>{moon_coins}</b>"
    )
    caption_text, caption_entities = _parse_msg(caption_html)

    # Telegram caption limit = 1024 chars; при превышении обрезаем безопасно
    _TG_CAP_MAX = 1024
    if len(caption_text) > _TG_CAP_MAX:
        caption_text     = caption_text[:_TG_CAP_MAX - 1] + "…"
        # Убираем сущности, выходящие за границу
        if caption_entities:
            caption_entities = [
                e for e in caption_entities if e.offset + e.length <= len(caption_text)
            ]

    # Генерируем карточку → фото + текст с кнопками двумя сообщениями
    try:
        png = await generate_profile_card(uid, context.bot)
        await update.effective_message.reply_photo(photo=png)
        await _reply_html(update.effective_message, caption_html, reply_markup=profile_actions_keyboard())
    except Exception:
        # Фоллбэк: если рендер упал — обычный текст
        await _reply_html(update.effective_message, caption_html, reply_markup=profile_actions_keyboard())


async def profile_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    user = get_user(uid)
    if not user:
        await query.message.reply_text("Сначала нажмите /start.")
        return

    _cancel_btn = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Отмена", callback_data="profile_cancel_change")
    ]])

    if query.data == "profile_change_nick":
        context.user_data['reg_step'] = 'change_nick'
        await _reply_html(query.message,
            f'<tg-emoji emoji-id="5319247469165433798">🎮</tg-emoji> Введите новый <b>никнейм</b>:',
            reply_markup=_cancel_btn)
    elif query.data == "profile_change_id":
        context.user_data['reg_step'] = 'change_id'
        await _reply_html(query.message,
            f'<tg-emoji emoji-id="5319247469165433798">🎮</tg-emoji> Введите новый <b>ID в игре</b> (буквы и цифры):',
            reply_markup=_cancel_btn)
    elif query.data == "profile_cancel_change":
        context.user_data.pop('reg_step', None)
        await query.message.delete()
        await action_profile(update, context)


# ══════════════════════════════════════════════════════════════════════════════
#  ИСТОРИЯ
# ══════════════════════════════════════════════════════════════════════════════

async def action_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not get_user(uid):
        await update.effective_message.reply_text("Сначала нажмите /start.")
        return
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('''SELECT tm.match_id, mp.team, tm.winner, mp.elo_before, mp.elo_change, tm.timestamp, tm.mode
                 FROM match_players mp
                 JOIN team_matches tm ON mp.match_id = tm.match_id
                 WHERE mp.user_id = ? AND tm.status = "finished"
                 ORDER BY tm.timestamp DESC LIMIT 5''', (uid,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.effective_message.reply_text("У вас ещё нет завершённых матчей.")
        return
    msg = "<tg-emoji emoji-id='5282843764451195532'>📋</tg-emoji> <b>Последние матчи:</b>\n\n"
    for match_id, team, winner, elo_before, elo_change, timestamp, mode in rows:
        won    = (team == winner)
        result = f"{E_CHECK} Победа" if won else f"{E_CROSS} Поражение"
        elo_change = elo_change or 0
        elo_before = elo_before or 0
        sign   = "+" if elo_change >= 0 else ""
        mode_s = f" [{mode}]" if mode else ""
        msg   += (
            f"Матч #{match_id}{mode_s} — {(timestamp or '')[:10]}\n"
            f"  {result} | Команда {team}\n"
            f"  ELO: {elo_before} → {elo_before + elo_change} ({sign}{elo_change})\n\n"
        )
    await _reply_html(update.effective_message, msg)


# ══════════════════════════════════════════════════════════════════════════════
#  ТАБЛИЦА ЛИДЕРОВ
# ══════════════════════════════════════════════════════════════════════════════

_LB_MODE_CALIB = {'5v5': 10, '3v3': 5, '2v2': 5}
_LB_MODE_LABEL = {'5v5': f'{E_SWORD} 5v5', '3v3': f'{E_SHIELD} 3v3', '2v2': f'{E_HANDSHAKE} 2v2'}


def _leaderboard_text(season: str = 'curr', mode: str = '5v5') -> str:
    """Единая функция текста лидерборда — режим + сезон."""
    ec, mc, wc, _ = mode_cols(mode)
    calib = _LB_MODE_CALIB.get(mode, 10)
    mode_label = _LB_MODE_LABEL.get(mode, mode)
    MEDALS = {1: E_CROWN, 2: E_FIRE, 3: E_ZAP}
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()

    if season == 'curr':
        c.execute(
            f'SELECT user_id, COALESCE(first_name, username), {ec}, {wc}, {mc} '
            f'FROM users WHERE {mc} >= ? AND is_banned = 0 '
            f'ORDER BY {ec} DESC LIMIT 10',
            (calib,)
        )
        top = c.fetchall()
        conn.close()
        if not top:
            return f"Пока нет игроков в таблице {mode_label} (нужно {calib}+ матчей)."
        creator_ids = get_creator_ids()
        admin_ids   = get_admin_ids()
        msg = f"{E_TROPHY} <b>Таблица лидеров — Нынешний сезон  {mode_label}</b>\n\n"
        for i, u in enumerate(top, 1):
            medal    = MEDALS.get(i, f"<code>{i}.</code>")
            winrate  = round(u[3] / u[4] * 100) if u[4] > 0 else 0
            lvl_icon = LEVEL_ICONS.get(elo_to_level(u[2]), str(elo_to_level(u[2])))
            badge    = creator_badge(u[0], creator_ids) + admin_badge(u[0], admin_ids) + official_badge_str(u[0])
            msg += f"{medal}  <b>{u[1]}{badge}</b>  〔{lvl_icon}〕\n"
            msg += f"      <code>{u[2]}</code> ELO  ·  {u[3]} побед  ·  {winrate}%\n\n"
        return msg
    else:
        c.execute('SELECT MAX(season_num) FROM season_archive')
        row = c.fetchone()
        season_num = row[0] if row and row[0] is not None else None
        if season_num is None:
            conn.close()
            return "Архив прошлых сезонов пуст."
        c.execute(
            f'SELECT user_id, username, {ec}, {wc}, {mc} FROM season_archive '
            f'WHERE season_num = ? ORDER BY {ec} DESC LIMIT 10',
            (season_num,)
        )
        top = c.fetchall()
        conn.close()
        if not top:
            return f"В прошлом сезоне ({season_num}) нет данных для {mode_label}."
        msg = f"{E_TROPHY} <b>Таблица лидеров — Прошлый сезон ({season_num})  {mode_label}</b>\n\n"
        for i, u in enumerate(top, 1):
            medal    = MEDALS.get(i, f"<code>{i}.</code>")
            winrate  = round(u[3] / u[4] * 100) if u[4] > 0 else 0
            lvl_icon = LEVEL_ICONS.get(elo_to_level(u[2] or 0), str(elo_to_level(u[2] or 0)))
            msg += f"{medal}  <b>{u[1]}</b>  〔{lvl_icon}〕\n"
            msg += f"      <code>{u[2] or 0}</code> ELO  ·  {u[3] or 0} побед  ·  {winrate}%\n\n"
        return msg


# ── Legacy wrappers (не используются, оставлены для совместимости) ────────────
def _leaderboard_current_text() -> str:
    return _leaderboard_text('curr', '5v5')


def _leaderboard_past_text() -> str:
    return _leaderboard_text('past', '5v5')




def _get_leaderboard_rows(season: str = 'curr', mode: str = '5v5') -> list[dict]:
    """Возвращает список строк для PNG-карточки таблицы лидеров."""
    ec, mc, wc, _ = mode_cols(mode)
    calib = _LB_MODE_CALIB.get(mode, 10)
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    if season == 'curr':
        c.execute(
            f'SELECT user_id, COALESCE(first_name, username), {ec}, {wc}, {mc} '
            f'FROM users WHERE {mc} >= ? AND is_banned = 0 '
            f'ORDER BY {ec} DESC LIMIT 10',
            (calib,)
        )
    else:
        c.execute('SELECT MAX(season_num) FROM season_archive')
        row = c.fetchone()
        season_num = row[0] if row and row[0] is not None else None
        if season_num is None:
            conn.close()
            return []
        c.execute(
            f'SELECT user_id, username, {ec}, {wc}, {mc} FROM season_archive '
            f'WHERE season_num = ? ORDER BY {ec} DESC LIMIT 10',
            (season_num,)
        )
    top = c.fetchall()
    conn.close()
    rows = []
    for i, u in enumerate(top, 1):
        matches_played = u[4] or 0
        wins = u[3] or 0
        winrate = round(wins / matches_played * 100) if matches_played > 0 else 0
        rows.append({'rank': i, 'name': u[1] or '-', 'elo': u[2] or 0, 'wins': wins, 'winrate': winrate})
    return rows

async def action_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    user = get_user(uid)

    # Имя и game_id из профиля
    display_name = "Игрок"
    game_id      = "0"
    official     = False
    if user:
        try:
            display_name = str(user[1] or "Игрок")
            # game_id stored in a separate field; use user_id as fallback
            game_id = str(user[0])
        except Exception:
            pass
    # Fetch game_id from full profile
    try:
        import sqlite3 as _sq
        _c3 = _sq.connect('faceit_bot.db')
        try:
            _cur3 = _c3.cursor()
            _cur3.execute('SELECT game_id FROM users WHERE user_id=?', (uid,))
            _r3 = _cur3.fetchone()
            if _r3 and _r3[0]:
                game_id = str(_r3[0])
        finally:
            _c3.close()
    except Exception:
        pass

    # Данные таблицы лидеров
    rows = _get_leaderboard_rows('curr', '5v5')

    # Аватар (опционально)
    avatar_b64 = None
    try:
        import base64, io
        photos = await context.bot.get_user_profile_photos(uid, limit=1)
        if photos.total_count > 0:
            file_obj = await context.bot.get_file(photos.photos[0][-1].file_id)
            buf = io.BytesIO()
            await file_obj.download_to_memory(buf)
            avatar_b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        pass

    try:
        png = await generate_leaderboard_card(
            rows=rows, mode='5v5', region='Все',
            username=display_name, game_id=game_id,
            official=official, avatar_b64=avatar_b64,
        )
        # Карточка + текст с кнопками как два отдельных сообщения
        await update.effective_message.reply_photo(png)
        text = _leaderboard_text('curr', '5v5')
        await _reply_html(update.effective_message, text, reply_markup=_leaderboard_keyboard('curr', '5v5'))
    except Exception:
        text = _leaderboard_text('curr', '5v5')
        await _reply_html(update.effective_message, text, reply_markup=_leaderboard_keyboard('curr', '5v5'))


async def leaderboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts  = query.data.split("_")   # ['lb', 'curr'/'past', '5v5'/'3v3'/'2v2']
    season = parts[1] if len(parts) > 1 else 'curr'
    mode   = parts[2] if len(parts) > 2 else '5v5'
    # Обратная совместимость со старыми callback_data
    if season == 'current': season = 'curr'
    if season == 'past' and mode not in ('5v5', '3v3', '2v2'): mode = '5v5'
    if mode not in ('5v5', '3v3', '2v2'): mode = '5v5'
    text = _leaderboard_text(season, mode)
    await _edit_html(query, text, reply_markup=_leaderboard_keyboard(season, mode))


# ══════════════════════════════════════════════════════════════════════════════
#  О СЕЗОНЕ / ПРАВИЛА / ПОДДЕРЖКА / РЕПОРТ
# ══════════════════════════════════════════════════════════════════════════════

async def action_season(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('''SELECT COALESCE(first_name, username), elo, wins, matches_played
                 FROM users WHERE is_banned = 0 AND elo > 0 ORDER BY elo DESC LIMIT 3''')
    top = c.fetchall()
    conn.close()
    medals = [
        '<tg-emoji emoji-id="5801004944211317728">🥇</tg-emoji>',
        '<tg-emoji emoji-id="5303516017572460859">🥈</tg-emoji>',
        '<tg-emoji emoji-id="5801072138974663924">🥉</tg-emoji>',
    ]
    top_lines = ""
    if top:
        top_lines = f"\n\n{E_CROWN} <b>Топ-3 игрока:</b>\n"
        for i, (name, elo, wins, matches) in enumerate(top):
            winrate = round(wins / matches * 100) if matches > 0 else 0
            top_lines += f"{medals[i]} <b>{name}</b> — ELO: <b>{elo}</b> | WR: {winrate}%\n"
    intro = (
        f'<tg-emoji emoji-id="5402477260982731644">☀️</tg-emoji> <b>Летний сезон фейсита ждёт вас!</b>\n\n'
        f"{E_ZAP} Мы подготовили для вас награды, которые дадут вам мотивацию "
        f"играть усердно и сильно, удачи в бою!"
    )
    prizes = (
        f"\n\n{E_TROPHY} <b>Призовые места:</b>\n"
        f"{medals[0]} 1 место — <b>400к голды</b>\n"
        f"{medals[1]} 2 место — <b>200к голды + приз</b>\n"
        f"{medals[2]} 3 место — <b>100к голды + утеш.приз</b>"
    )
    season_end_date = get_setting('season_end_date', '01.09.2026')
    await _reply_html(update.effective_message, f"{intro}\n\n{E_CAL} Дата окончания сезона: <b>{season_end_date}</b>" + prizes + top_lines)


async def action_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply_html(update.effective_message,
        f'{E_RULES} <b>Правила Moon Faceit</b>\n\nНажмите кнопку ниже:',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📄 Открыть правила", url=RULES_URL)
        ]])
    )


def _make_custom_emoji_message(parts: list[tuple[str, str | None]]) -> tuple[str, list[MessageEntity]]:
    """Build message text + MessageEntity list for custom emoji.
    parts: list of (text, emoji_id_or_None). emoji_id triggers a custom_emoji entity.
    Offsets are computed in UTF-16 code units as required by Telegram Bot API."""
    text = ""
    entities: list[MessageEntity] = []
    for part_text, emoji_id in parts:
        if emoji_id:
            offset = len(text.encode("utf-16-le")) // 2
            length = len(part_text.encode("utf-16-le")) // 2
            entities.append(MessageEntity(
                type=MessageEntity.CUSTOM_EMOJI,
                offset=offset,
                length=length,
                custom_emoji_id=emoji_id,
            ))
        text += part_text
    return text, entities


async def action_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply_html(update.effective_message,
        '<tg-emoji emoji-id="5854841392899036819">💪</tg-emoji> <b>Поддержка Moon Faceit</b>\n\n'
        f'• {E_HANDSHAKE} Сотрудничество\n'
        '• <tg-emoji emoji-id="5436113877181941026">❓</tg-emoji> Вопросы\n'
        '• <tg-emoji emoji-id="5445397693805373887">🪲</tg-emoji> Баги\n\n'
        f'{E_RIGHT} По всем вопросам: @MoonFaceitGroup'
    )


async def action_donate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply_html(update.effective_message,
        '<tg-emoji emoji-id="5409048419211682843">💰</tg-emoji> <b>Донат</b>\n\n'
        '<tg-emoji emoji-id="5282843764451195532">📋</tg-emoji> <b>Прайс-лист:</b>\n'
        '  <tg-emoji emoji-id="5393551318214257273">🟡</tg-emoji> Снятие варна — <b>50₽</b>\n'
        '  <tg-emoji emoji-id="5319247469165433798">🔓</tg-emoji> Снятие бана — <b>200₽</b>\n'
        '  <tg-emoji emoji-id="5319247469165433798">✏️</tg-emoji> Смена ника — <b>30₽</b>\n'
        '  <tg-emoji emoji-id="5319247469165433798">🎮</tg-emoji> Смена айди — <b>30₽</b>\n\n'
        '  <tg-emoji emoji-id="5282843764451195532">💳</tg-emoji> <b>Сбербанк:</b> <code>2202202358610735</code>\n\n'
        '<i>После оплаты обратись к администратору.</i>',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Telegram Stars", url="https://t.me/tainygod", api_kwargs={'icon_custom_emoji_id': '5438496463044752972'})
        ]]))


async def action_donate_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply_html(update.effective_message, f'<tg-emoji emoji-id="5409048419211682843">💵</tg-emoji> <b>Донат</b>\n\n'
        f'{E_PRICE} <b>100 Moon Coins</b> — <b>80</b>\n'
        f'{E_PRICE} <b>500 Moon Coins</b> — <b>350</b>\n'
        f'{E_PRICE} <b>Своё количество Moon Coins</b>\n\n'
        f'<tg-emoji emoji-id="5465198403573012261">👉</tg-emoji>'
        f' За донатом — @MoonFaceitGroup')


async def action_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    user = get_user(uid)
    moon_coins = 0
    if user:
        try:
            import sqlite3 as _sq
            _c4 = _sq.connect('faceit_bot.db')
            try:
                _cur4 = _c4.cursor()
                _cur4.execute('SELECT moon_coins FROM users WHERE user_id=?', (uid,))
                _r4 = _cur4.fetchone()
                moon_coins = int(_r4[0] or 0) if _r4 else 0
            finally:
                _c4.close()
        except Exception:
            pass

    # Собираем все предметы из магазина
    items = []
    for cat_key in _SHOP_SKINS_ORDER:
        _, cat_items = _SHOP_SKINS[cat_key]
        for name, price in cat_items:
            items.append({'name': name, 'price': price})
    for cat_key in _SHOP_KNIVES_ORDER:
        _, cat_items = _SHOP_KNIVES[cat_key]
        for name, price in cat_items:
            items.append({'name': name, 'price': price})
    for name, price in _SHOP_STICKERS:
        items.append({'name': name, 'price': price})

    try:
        png = await generate_shop_card(items=items or None, moon_coins=moon_coins)
        # Карточка + текст с кнопками как два отдельных сообщения
        await update.effective_message.reply_photo(png)
        await _reply_html(update.effective_message,
            f'{E_SHOP} <b>Магазин Moon Faceit</b>\n\n{E_RIGHT} Выбери категорию:',
            reply_markup=shop_keyboard())
    except Exception:
        await _reply_html(update.effective_message, f'{E_SHOP} <b>Магазин Moon Faceit</b>\n\n'
            f'{E_RIGHT} Выбери категорию:', reply_markup=shop_keyboard())


async def shop_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # ── Назад → главное меню магазина ──────────────────────────────────────
    if data == "shop_back":
        _t, _e = _parse_msg(f'{E_SHOP} <b>Магазин Moon Faceit</b>\n\n{E_RIGHT} Выбери категорию:')
        await query.message.edit_text(_t, entities=_e, reply_markup=shop_keyboard())
        return

    # ── Скины ──────────────────────────────────────────────────────────────
    if data == "shop_cat_skins":
        tab = _SHOP_SKINS_ORDER[0]
    elif data.startswith("shop_nav_skins_"):
        tab = data[len("shop_nav_skins_"):]
    else:
        tab = None

    if tab and tab in _SHOP_SKINS:
        label, items = _SHOP_SKINS[tab]
        labels = {k: v[0] for k, v in _SHOP_SKINS.items()}
        kb = _shop_tab_keyboard("skins", tab, _SHOP_SKINS_ORDER, labels, items)
        text = _shop_page_text(
            '<tg-emoji emoji-id="5409048419211682843">💵</tg-emoji>',
            f"Скины — {label}"
        )
        _t, _e = _parse_msg(text); await query.message.edit_text(_t, entities=_e, reply_markup=kb)
        return

    # ── Ножи ───────────────────────────────────────────────────────────────
    if data == "shop_cat_knives":
        tab = _SHOP_KNIVES_ORDER[0]
    elif data.startswith("shop_nav_knives_"):
        tab = data[len("shop_nav_knives_"):]
    else:
        tab = None

    if tab and tab in _SHOP_KNIVES:
        label, items = _SHOP_KNIVES[tab]
        labels = {k: v[0] for k, v in _SHOP_KNIVES.items()}
        kb = _shop_tab_keyboard("knives", tab, _SHOP_KNIVES_ORDER, labels, items)
        text = _shop_page_text(
            '<tg-emoji emoji-id="5062267305124168856">🔪</tg-emoji>',
            f"Ножи — {label}"
        )
        _t, _e = _parse_msg(text); await query.message.edit_text(_t, entities=_e, reply_markup=kb)
        return

    # ── Наклейки ───────────────────────────────────────────────────────────
    if data == "shop_cat_stickers":
        text = _shop_page_text(
            '<tg-emoji emoji-id="5393619629669097759">💎</tg-emoji>',
            "Наклейки"
        )
        sticker_rows = [
            [_ebtn(f"{price}  —  {name}", "5377631390571472449", 2, "shop_buy_stickers")]
            for name, price in _SHOP_STICKERS
        ]
        sticker_rows.append([_ebtn("Назад", "5255703720078879038", 2, "shop_back")])
        _t, _e = _parse_msg(text); await query.message.edit_text(_t, entities=_e, reply_markup=InlineKeyboardMarkup(sticker_rows))
        return

    # ── Покупка товара ─────────────────────────────────────────────────────
    if data.startswith("shop_buy_"):
        buy_text = (
            'Привет <tg-emoji emoji-id="5458797798495377338">🔥</tg-emoji> '
            'Хочешь купить товар? Пиши — @MoonFaceitGroup — '
            'Отвечаю быстро <tg-emoji emoji-id="5458908492687497206">🔥</tg-emoji>'
        )
        suffix = data[len("shop_buy_"):]
        if "_" in suffix:
            cat_part, tab_part = suffix.rsplit("_", 1)
            back_cb = f"shop_nav_{cat_part}_{tab_part}"
        else:
            back_cb = "shop_cat_stickers"
        kb = InlineKeyboardMarkup([[
            _ebtn("Назад", "5255703720078879038", 2, back_cb)
        ]])
        _t, _e = _parse_msg(buy_text); await query.message.edit_text(_t, entities=_e, reply_markup=kb)


async def action_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply_html(update.effective_message, '<tg-emoji emoji-id="5395695537687123235">🚨</tg-emoji> <b>Репорт на игрока</b>\n\n'
        "Для репорта укажи:\n• ID игрока\n• Причину\n• Скриншот/запись\n\n"
        '<tg-emoji emoji-id="5377355726685497939">✉️</tg-emoji> Писать — @MoonFaceitGroup')


# ══════════════════════════════════════════════════════════════════════════════
#  ПОИСК МАТЧА — выбор режима → лобби
# ══════════════════════════════════════════════════════════════════════════════

async def _check_ban(uid: int) -> Optional[str]:
    """Возвращает сообщение о бане или None если не забанен."""
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT is_banned, ban_reason, ban_until FROM users WHERE user_id=?', (uid,))
    row = c.fetchone()
    if row and row[0]:
        ban_until = row[2]
        reason = row[1] or "не указана"
        if ban_until:
            if datetime.datetime.utcnow() >= datetime.datetime.fromisoformat(ban_until):
                c.execute('UPDATE users SET is_banned=0, ban_reason=NULL, ban_until=NULL WHERE user_id=?', (uid,))
                conn.commit()
                conn.close()
                return None
            else:
                conn.close()
                days = (datetime.datetime.fromisoformat(ban_until) - datetime.datetime.utcnow()).days + 1
                return f"{E_BAN} <b>Вы заблокированы!</b>\nПричина: <b>{reason}</b>\nОсталось: ~{days} дн."
        else:
            conn.close()
            return f"{E_BAN} <b>Вы заблокированы!</b>\nПричина: <b>{reason}</b>"
    conn.close()
    return None


async def action_find_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not get_user(uid):
        await update.effective_message.reply_text("Сначала нажмите /start.")
        return
    ban_msg = await _check_ban(uid)
    if ban_msg:
        await _reply_html(update.effective_message, ban_msg)
        return

    # Уже в лобби?
    for mode, lobbies in lobby_queues.items():
        for lid, q in lobbies.items():
            if uid in q:
                info = _lobby_msg_info.get(uid)
                if info:
                    chat_id, msg_id, _, _ = info
                    match = MODES[mode]['match_size']
                    text  = (
                        f"{E_CHECK} <b>Вы в лобби {lid}  [{mode}]</b>  ({len(q)}/{match})\n"
                        f"Ожидаем ещё <b>{match - len(q)}</b> игрок(ов)...\n\n"
                        f"{E_PEOPLE} Игроки:\n{queue_list_text(lid, mode)}\n\n"
                        f"{E_SEARCH} <i>Другие лобби:</i>"
                    )
                    try:
                        _t, _e = _parse_msg(text)
                        await context.bot.edit_message_text(
                            chat_id=chat_id, message_id=msg_id,
                            text=_t, entities=_e,
                            reply_markup=lobby_list_keyboard(mode, uid_in_lobby=True)
                        )
                        return
                    except Exception:
                        pass
                await update.effective_message.reply_text(
                    f"Вы уже в лобби {lid} [{mode}]."
                )
                return

    # Показываем выбор режима
    await _reply_html(update.effective_message, f"{EP_GAME} <b>Выберите режим игры:</b>", reply_markup=mode_select_keyboard())


async def mode_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатие mode_5v5 / mode_3v3."""
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    mode = query.data.split("_", 1)[1]   # "5v5", "3v3" или "2v2"

    if not get_user(uid):
        await query.answer("Сначала нажмите /start.", show_alert=True)
        return
    ban_msg = await _check_ban(uid)
    if ban_msg:
        await query.answer("Вы заблокированы.", show_alert=True)
        return

    # Уже в очереди?
    for m, lobbies in lobby_queues.items():
        for lid, q in lobbies.items():
            if uid in q:
                await query.answer(f"Вы уже в лобби {lid} [{m}]. Выйдите сначала.", show_alert=True)
                return

    cfg   = MODES[mode]
    text  = (
        f"{cfg['emoji']} <b>Режим {mode}</b>\n\n"
        f"{E_SEARCH} <b>Выберите лобби:</b>"
    )
    try:
        await _edit_html(query, text, reply_markup=lobby_list_keyboard(mode))
    except Exception:
        sent = await _reply_html(query.message, text, reply_markup=lobby_list_keyboard(mode))
        _lobby_selector_msg[uid] = (sent.chat_id, sent.message_id)


async def lobby_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """callback_data = lobby_join_{mode}_{lobby_id}"""

    query    = update.callback_query
    parts    = query.data.split("_")         # ['lobby','join','5v5','1']
    mode     = parts[2]
    lobby_id = int(parts[3])
    uid      = query.from_user.id

    if not get_user(uid):
        await query.answer("Сначала нажмите /start.", show_alert=True)
        return
    ban_msg = await _check_ban(uid)
    if ban_msg:
        await query.answer("Вы заблокированы.", show_alert=True)
        return

    # Уже в какой-то очереди?
    for m, lobbies in lobby_queues.items():
        for lid, q in lobbies.items():
            if uid in q:
                await query.answer(f"Вы уже в лобби {lid} [{m}]. Выйдите сначала.", show_alert=True)
                return

    if mode not in MODES:
        await query.answer("Неизвестный режим.", show_alert=True)
        return

    lobby      = lobby_queues[mode][lobby_id]
    match_size = MODES[mode]['match_size']
    lobby.append(uid)
    pos = len(lobby)
    _queue_join_time[uid] = datetime.datetime.utcnow()
    await query.answer(f"✅ Вы вступили в лобби {lobby_id} [{mode}]!")

    if pos < match_size:
        markup = lobby_list_keyboard(mode, uid_in_lobby=True)
        text = (
            f"{E_CHECK} <b>Вы в лобби {lobby_id}  [{mode}]</b>  ({pos}/{match_size})\n"
            f"Ожидаем ещё <b>{match_size - pos}</b> игрок(ов)...\n\n"
            f"{E_PEOPLE} Игроки:\n{queue_list_text(lobby_id, mode)}\n\n"
            f"{E_SEARCH} <i>Другие лобби:</i>"
        )
        try:
            await _edit_html(query, text, reply_markup=markup)
        except Exception:
            sent = await _reply_html(query.message, text, reply_markup=markup)
            _lobby_msg_info[uid] = (sent.chat_id, sent.message_id, lobby_id, mode)
            _lobby_selector_msg.pop(uid, None)
            await _refresh_lobby_messages(context, lobby_id, mode, exclude_uid=uid)
            return
        _lobby_msg_info[uid] = (query.message.chat_id, query.message.message_id, lobby_id, mode)
        _lobby_selector_msg.pop(uid, None)
        await _refresh_lobby_messages(context, lobby_id, mode, exclude_uid=uid)
        return

    # Лобби заполнено — запускаем подтверждение
    players_ids = list(lobby[:match_size])
    # Сохраняем ссылки на сообщения ДО cleanup, чтобы start_confirmation отредактировал их на месте
    prior_msg_info: dict[int, tuple[int, int]] = {}
    for u in players_ids:
        if u in _lobby_msg_info:
            ch, mi, _, _ = _lobby_msg_info[u]
            prior_msg_info[u] = (ch, mi)
    # Сообщение самого последнего вошедшего доступно напрямую через query
    prior_msg_info[uid] = (query.message.chat_id, query.message.message_id)
    lobby_queues[mode][lobby_id].clear()
    for u in players_ids:
        _cleanup_uid(u)
    # Немедленный фидбек для последнего вошедшего: даже если edit в start_confirmation
    # и fallback send оба не сработают — игрок увидит хоть что-то
    _MODE_PREMIUM_EMOJI = {'5v5': E_SWORD, '3v3': E_SHIELD, '2v2': E_HANDSHAKE}
    try:
        mode_icon = _MODE_PREMIUM_EMOJI.get(mode, E_SWORD)
        await _edit_html(query,
            f"{mode_icon} <b>Лобби {lobby_id} [{mode}]:</b> набралось {match_size} игроков! "
            "Ожидаем подтверждения...")
    except Exception:
        pass
    await start_confirmation(context, players_ids, mode, prior_msg_info=prior_msg_info)


async def lobby_leave_inline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id

    left_lobby = None
    left_mode  = None
    for m, lobbies in lobby_queues.items():
        for lid, q in lobbies.items():
            if uid in q:
                q.remove(uid)
                left_lobby = lid
                left_mode  = m
                break
        if left_lobby:
            break

    _lobby_msg_info.pop(uid, None)
    _queue_join_time.pop(uid, None)

    if left_lobby is not None:
        await _refresh_lobby_messages(context, left_lobby, left_mode)
        text   = f"{E_DOOR} Вы вышли из лобби {left_lobby} [{left_mode}].\n\n{EP_GAME} <b>Выберите режим:</b>"
        markup = mode_select_keyboard()
        try:
            await _edit_html(query, text, reply_markup=markup)
        except Exception:
            await _reply_html(query.message, text, reply_markup=markup)
    else:
        await query.answer("Вы не в очереди.", show_alert=True)


async def action_leave_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    left_lobby = None
    left_mode  = None
    for m, lobbies in lobby_queues.items():
        for lid, q in lobbies.items():
            if uid in q:
                q.remove(uid)
                left_lobby = lid
                left_mode  = m
                break
        if left_lobby:
            break
    _lobby_msg_info.pop(uid, None)
    _queue_join_time.pop(uid, None)
    if left_lobby is not None:
        await _refresh_lobby_messages(context, left_lobby, left_mode)
        await _reply_html(update.message, f"{E_DOOR} Вы вышли из лобби {left_lobby} [{left_mode}].\n\n{EP_GAME} Выберите режим:", reply_markup=mode_select_keyboard())
    else:
        await update.message.reply_text("Вы не в очереди.")


async def action_queue_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = f"{E_PEOPLE} <b>Статус лобби:</b>\n"
    for mode in MODES:
        match = MODES[mode]['match_size']
        msg  += f"\n{MODES[mode]['emoji']} <b>Режим {mode}</b>\n"
        for i in range(1, 6):
            q = lobby_queues[mode][i]
            msg += f"  Лобби {i}: {len(q)}/{match}\n"
            if txt := queue_list_text(i, mode):
                msg += txt + "\n"
    await _reply_html(update.effective_message, msg.strip())


# ══════════════════════════════════════════════════════════════════════════════
#  ПОДТВЕРЖДЕНИЕ МАТЧА
# ══════════════════════════════════════════════════════════════════════════════

async def _job_confirm_timeout(context: ContextTypes.DEFAULT_TYPE):
    session_id: int = context.job.data['session_id']
    state = confirm_state.get(session_id)
    if not state:
        return

    mode        = state['mode']
    afk_players = [uid for uid in state['real_players'] if uid not in state['confirmed']]
    ok_players  = [uid for uid in state['real_players'] if uid in state['confirmed']]

    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    afk_names = []
    for uid in afk_players:
        c.execute('SELECT COALESCE(first_name, username) FROM users WHERE user_id=?', (uid,))
        row = c.fetchone()
        afk_names.append(row[0] if row else str(uid))
    conn.close()

    del confirm_state[session_id]
    afk_list = ", ".join(afk_names) if afk_names else "—"

    for uid in afk_players:
        _cleanup_uid(uid)
        try:
            await _send_html(context.bot, uid, (
                    f"{E_BAN} <b>Вы были кикнуты с лобби.</b>\n\n"
                    f"Причина: Игнорирование подтверждения матча."
                ))
        except Exception:
            pass

    if not ok_players:
        return

    # Возвращаем подтвердивших в очередь того же режима
    target_lobby = min(range(1, 6), key=lambda i: len(lobby_queues[mode][i]))
    rejoined     = []
    for uid in ok_players:
        in_any = any(uid in q for m in MODES for q in lobby_queues[m].values())
        if not in_any:
            lobby_queues[mode][target_lobby].append(uid)
            _queue_join_time[uid] = datetime.datetime.utcnow()
            rejoined.append(uid)

    match_size = MODES[mode]['match_size']
    lobby_size = len(lobby_queues[mode][target_lobby])
    queue_txt  = queue_list_text(target_lobby, mode)

    for uid in rejoined:
        try:
            sent = await _send_html(context.bot, uid, (
                    f"{E_CROSS} <b>Матч отменён</b>\n\n"
                    f"{E_BAN} Не подтвердили за {CONFIRM_TIMEOUT_SEC} сек: <b>{afk_list}</b>\n\n"
                    f"{E_RELOAD} Вы возвращены в <b>лобби {target_lobby} [{mode}]</b> "
                    f"({lobby_size}/{match_size}).\n\n{E_PEOPLE} Игроки:\n{queue_txt}"
                ))
            _lobby_msg_info[uid] = (sent.chat_id, sent.message_id, target_lobby, mode)
        except Exception:
            pass

    if lobby_size >= match_size:
        players_to_start = list(lobby_queues[mode][target_lobby][:match_size])
        prior_msg_info: dict[int, tuple[int, int]] = {}
        for u in players_to_start:
            if u in _lobby_msg_info:
                ch, mi, _, _ = _lobby_msg_info[u]
                prior_msg_info[u] = (ch, mi)
        lobby_queues[mode][target_lobby].clear()
        for u in players_to_start:
            _cleanup_uid(u)
        await start_confirmation(context, players_to_start, mode, prior_msg_info=prior_msg_info)


async def start_confirmation(context, players_ids: list, mode: str,
                             prior_msg_info: dict | None = None):
    global _confirm_counter
    _confirm_counter += 1
    session_id = _confirm_counter

    real_players = [uid for uid in players_ids if not is_bot(uid)]
    confirmed    = set(uid for uid in players_ids if is_bot(uid))

    confirm_state[session_id] = {
        'players':      list(players_ids),
        'real_players': real_players,
        'confirmed':    confirmed,
        'mode':         mode,
    }

    keyboard   = InlineKeyboardMarkup([[
        InlineKeyboardButton("✔️ Подтвердить", callback_data=f"confirm_{session_id}")
    ]])
    match_size = MODES[mode]['match_size']

    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    names = []
    for uid in players_ids:
        c.execute('SELECT COALESCE(first_name, username) FROM users WHERE user_id=?', (uid,))
        row = c.fetchone()
        names.append(_esc(row[0]) if row else str(uid))
    conn.close()

    _PREMIUM_MODE_ICON = {'5v5': E_SWORD, '3v3': E_SHIELD, '2v2': E_HANDSHAKE}
    mode_icon_confirm = _PREMIUM_MODE_ICON.get(mode, E_SWORD)
    player_list = "\n".join(
        f"  {E_CONFIRM if is_bot(uid) else E_WAITING} {names[i]}"
        for i, uid in enumerate(players_ids)
    )
    msg = (
        f"{mode_icon_confirm} <b>Матч [{mode}] найден!</b>\n\n"
        f"{E_TEAM} Игроки:\n{player_list}\n\n"
        f"Нажми <b>Подтвердить</b> для участия.\n"
        f"Подтвердили: <b>{len(confirmed)}/{match_size}</b>\n"
        f"{EP_TIMER} Осталось: <b>{CONFIRM_TIMEOUT_SEC} сек</b>"
    )
    _confirm_msg_info[session_id] = {}
    for uid in real_players:
        # Пробуем отредактировать существующее сообщение (лобби-карточку),
        # чтобы кнопка «Подтвердить» появилась прямо на том месте, где игрок уже смотрит.
        # Если редактирование не удаётся — отправляем новое сообщение.
        prior = prior_msg_info.get(uid) if prior_msg_info else None
        if prior:
            chat_id_p, msg_id_p = prior
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id_p,
                    message_id=msg_id_p,
                    text=msg,
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
                _confirm_msg_info[session_id][uid] = (chat_id_p, msg_id_p)
                continue
            except Exception as _edit_err:
                logger.warning("[start_confirmation] edit failed uid=%s chat=%s msg=%s: %s",
                               uid, chat_id_p, msg_id_p, _edit_err)
                # не удалось — пробуем отправить новое
        try:
            sent = await context.bot.send_message(
                chat_id=uid,
                text=msg,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
            _confirm_msg_info[session_id][uid] = (uid, sent.message_id)
        except Exception as _err:
            logger.error("[start_confirmation] uid=%s send error: %s", uid, _err, exc_info=True)

    if len(confirmed) >= match_size:
        del confirm_state[session_id]
        await start_draft(context, players_ids, mode)
        return

    if context.job_queue:
        context.job_queue.run_once(
            _job_confirm_timeout,
            when=CONFIRM_TIMEOUT_SEC,
            data={'session_id': session_id},
            name=f"confirm_timeout_{session_id}"
        )


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query      = update.callback_query
    session_id = int(query.data.split("_")[1])
    uid        = query.from_user.id

    state = confirm_state.get(session_id)
    if not state:
        await query.answer("Это подтверждение уже истекло.", show_alert=True)
        return
    if uid not in state['real_players']:
        await query.answer("Вы не участник этого матча.", show_alert=True)
        return
    if uid in state['confirmed']:
        await query.answer("Вы уже подтвердили участие.", show_alert=True)
        return

    state['confirmed'].add(uid)
    await query.answer("✅ Участие подтверждено!")

    mode       = state['mode']
    total      = len(state['players'])
    confirmed  = len(state['confirmed'])

    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    names = []
    for pid in state['players']:
        c.execute('SELECT COALESCE(first_name, username) FROM users WHERE user_id=?', (pid,))
        row = c.fetchone()
        names.append(_esc(row[0]) if row else str(pid))
    conn.close()

    _PREMIUM_MODE_ICON = {'5v5': E_SWORD, '3v3': E_SHIELD, '2v2': E_HANDSHAKE}
    mode_icon_confirm = _PREMIUM_MODE_ICON.get(mode, E_SWORD)
    player_list = "\n".join(
        f"  {E_CONFIRM if pid in state['confirmed'] else E_WAITING} {names[i]}"
        for i, pid in enumerate(state['players'])
    )
    updated_msg = (
        f"{mode_icon_confirm} <b>Матч [{mode}] найден!</b>\n\n"
        f"{E_TEAM} Игроки:\n{player_list}\n\n"
        f"Подтвердили: <b>{confirmed}/{total}</b>"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✔️ Подтвердить", callback_data=f"confirm_{session_id}")
    ]])

    if confirmed >= total:
        players_ids = state['players']
        msg_info = _confirm_msg_info.pop(session_id, {})
        del confirm_state[session_id]
        # Обновляем сообщение кликнувшего (убираем кнопку)
        try:
            await query.edit_message_text(
                text=updated_msg, parse_mode='HTML', reply_markup=None
            )
        except Exception:
            pass
        # Обновляем сообщения всех остальных игроков (убираем кнопку)
        for pid in state['real_players']:
            if pid == uid:
                continue
            chat_id_p, msg_id_p = msg_info.get(pid, (None, None))
            if chat_id_p is None:
                continue
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id_p,
                    message_id=msg_id_p,
                    text=updated_msg,
                    parse_mode='HTML',
                    reply_markup=None,
                )
            except Exception:
                pass
        for pid in state['real_players']:
            try:
                await context.bot.send_message(chat_id=pid, text=f"{E_CIRCLE_M} <b>Все подтвердили! Начинается выбор состава...</b>", parse_mode='HTML')
            except Exception:
                pass
        await start_draft(context, players_ids, mode)
    else:
        msg_info = _confirm_msg_info.get(session_id, {})
        # Сообщение кликающего — через query (иначе Telegram отклоняет pending callback)
        try:
            await query.edit_message_text(
                text=updated_msg, parse_mode='HTML', reply_markup=keyboard
            )
        except Exception:
            pass
        # Все остальные — через bot.edit_message_text
        for pid in state['real_players']:
            if pid == uid:
                continue  # уже обновили через query выше
            chat_id_p, msg_id_p = msg_info.get(pid, (None, None))
            if chat_id_p is None:
                continue
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id_p,
                    message_id=msg_id_p,
                    text=updated_msg,
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
#  ДРАФТ
# ══════════════════════════════════════════════════════════════════════════════

def draft_status_text(draft: dict, title: str = None) -> str:
    data        = draft['player_data']
    mode        = draft['mode']
    pick_order  = MODES[mode]['pick_order']
    calib_games = MODES[mode]['calib_games']
    match_size  = MODES[mode]['match_size']
    team_size   = match_size // 2
    cap1        = draft['captain1']
    cap2        = draft['captain2']

    def player_badge(uid):
        p = data[uid]
        off_val = p.get('official_badge', 0)
        off = (f" {E_OFF1}" if off_val == 1 else f" {E_OFF2}" if off_val == 2 else f" {E_OFF3}" if off_val == 3 else "")
        return (f" {E_CHECK}" if p.get('is_creator') else "") + (f" {E_ADMIN}" if p.get('is_admin') else "") + off

    def player_link(uid, name):
        # tg://user?id=<uid> открывает профиль игрока в Telegram по его id —
        # работает даже без юзернейма, в отличие от @username-упоминаний.
        return f'<a href="tg://user?id={uid}">{_esc(name)}</a>'

    def team_lines(uids):
        lines = []
        for uid in uids:
            p   = data[uid]
            mp  = p.get('matches_played', 0)
            badge = player_badge(uid)
            name_link = player_link(uid, p['name'])
            if mp < calib_games:
                lvl_icon = LEVEL_ICONS[0]
                lines.append(f"  • <b>{name_link}</b>{badge}  〔{lvl_icon}〕")
            else:
                lvl_icon = LEVEL_ICONS.get(elo_to_level(p['elo']), "")
                lines.append(f"  • <b>{name_link}</b>{badge}  〔{lvl_icon}〕  <code>{p['elo']}</code> ELO")
        return "\n".join(lines) if lines else "  —"

    pick_idx      = draft['pick_index']
    draft_over    = pick_idx >= len(pick_order)
    lines: list[str] = []

    if title:
        lines.append(title)

    if not draft_over:
        current_team = pick_order[pick_idx]
        cap = cap1 if current_team == 1 else cap2
        lines.append(f"{E_RIGHT} Сейчас выбирает: <b>{_esc(data[cap]['name'])}</b> (Команда {current_team})")

    lines.append("")
    lines.append(f"{E_CROWN} Капитан <tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> Команда 1: <b>{player_link(cap1, data[cap1]['name'])}</b>")
    lines.append(f"{E_CROWN} Капитан <tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> Команда 2: <b>{player_link(cap2, data[cap2]['name'])}</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append(f"{E_TEAM} Команда 1 ({len(draft['team1'])}/{team_size})")
    lines.append(team_lines(draft['team1']))
    lines.append("")
    lines.append(f"{E_TEAM} Команда 2 ({len(draft['team2'])}/{team_size})")
    lines.append(team_lines(draft['team2']))
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("")

    if draft_over:
        lines.append(f"<tg-emoji emoji-id=\"5206607081334906820\">✔️</tg-emoji> Драфт завершён!")
    else:
        lines.append(f"<tg-emoji emoji-id=\"5123230779593196220\">⏰</tg-emoji> Выберите игрока ({len(draft['remaining'])} свободных):")

    return "\n".join(lines)


def pick_keyboard(match_id: int, remaining: list, player_data: dict, calib_games: int = 0) -> InlineKeyboardMarkup:
    import re as _re_eid
    _eid_re = _re_eid.compile(r'emoji-id=["\'](\d+)["\']')

    def _btn(uid):
        p  = player_data[uid]
        mp = p.get('matches_played', 0)
        cb = f"pick_{match_id}_{uid}"
        if mp < calib_games:
            # Калибровка: только имя + значок калибровки, ELO не показываем
            icon_html = LEVEL_ICONS[0]
            label     = p['name']
        else:
            # Откалиброван: имя + ELO, уровень — премиум-значком на кнопке
            icon_html = LEVEL_ICONS.get(elo_to_level(p['elo']), "")
            label     = f"{p['name']} — {p['elo']} ELO"
        m = _eid_re.search(icon_html)
        if m:
            return _ebtn(label, m.group(1), 2, cb)
        return InlineKeyboardButton(label, callback_data=cb)

    return InlineKeyboardMarkup([
        [_btn(uid)]
        for uid in remaining
    ])


def _cancel_pick_timer(context, match_id: int):
    """Отменяет текущий таймер авто-пика для матча."""
    if not context.job_queue:
        return
    for job in context.job_queue.get_jobs_by_name(f"pick_timeout_{match_id}"):
        job.schedule_removal()


async def _job_pick_timeout(context: ContextTypes.DEFAULT_TYPE):
    """Job: капитан не выбрал игрока за PICK_TIMEOUT_SEC — авто-пик случайного."""
    match_id:          int = context.job.data['match_id']
    expected_pick_idx: int = context.job.data['expected_pick_idx']

    async with _get_draft_lock(match_id):
        draft = draft_state.get(match_id)
        # Если драфт уже завершён или ход сменился (ручной пик успел) — ничего не делаем
        if not draft or draft.get('finalized') or not draft['remaining']:
            return

        pick_order = MODES[draft['mode']]['pick_order']
        pick_idx   = draft['pick_index']
        if pick_idx >= len(pick_order):
            return
        # Ключевая проверка: этот job был запланирован для другого хода
        if pick_idx != expected_pick_idx:
            return

        current_team = pick_order[pick_idx]
        current_cap  = draft['captain1'] if current_team == 1 else draft['captain2']
        cap_name     = draft['player_data'][current_cap]['name']

        # Авто-пик: случайный свободный игрок
        auto_uid  = random.choice(draft['remaining'])
        auto_name = draft['player_data'][auto_uid]['name']

        draft['remaining'].remove(auto_uid)
        if current_team == 1:
            draft['team1'].append(auto_uid)
        else:
            draft['team2'].append(auto_uid)
        draft['pick_index'] += 1

        should_finalize = draft['pick_index'] >= len(pick_order)
        if should_finalize:
            draft['finalized'] = True
            draft_state.pop(match_id, None)
            _release_draft_lock(match_id)

    # Уведомляем всех об авто-пике (вне лока — только IO)
    notify_msg = (
        f"{EP_TIMER} <b>{_esc(cap_name)}</b> не выбрал игрока за {PICK_TIMEOUT_SEC} сек.\n"
        f"Авто-выбран: <b>{_esc(auto_name)}</b> в Команду {current_team}."
    )
    for uid in draft['all_players']:
        if is_bot(uid):
            continue
        try:
            await context.bot.send_message(chat_id=uid, text=notify_msg, parse_mode='HTML')
        except Exception:
            pass

    if should_finalize:
        await finalize_draft(context, draft)
    else:
        status = draft_status_text(draft)
        for uid in draft['all_players']:
            if is_bot(uid):
                continue
            try:
                await context.bot.send_message(chat_id=uid, text=status, parse_mode='HTML')
            except Exception:
                pass
        await send_pick_keyboard(context, draft)


async def send_pick_keyboard(context, draft: dict):
    pick_order = MODES[draft['mode']]['pick_order']
    pick_idx   = draft['pick_index']
    if pick_idx >= len(pick_order):
        return
    current_team = pick_order[pick_idx]
    cap          = draft['captain1'] if current_team == 1 else draft['captain2']
    match_id     = draft['match_id']
    calib_games  = MODES[draft['mode']]['calib_games']
    keyboard     = pick_keyboard(match_id, draft['remaining'], draft['player_data'], calib_games)

    # Отменяем предыдущий таймер и планируем новый
    _cancel_pick_timer(context, match_id)

    if not is_bot(cap):
        try:
            pick_msg = (
                f"{E_AIM} Ваш ход! Выберите игрока в <b>Команду {current_team}</b>:"
                f"\n{EP_TIMER} У вас <b>{PICK_TIMEOUT_SEC} сек</b>, иначе выберется случайный."
            )
            await context.bot.send_message(
                chat_id=cap,
                text=pick_msg,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
        except Exception:
            pass

    if context.job_queue:
        context.job_queue.run_once(
            _job_pick_timeout,
            when=PICK_TIMEOUT_SEC,
            data={'match_id': match_id, 'expected_pick_idx': pick_idx},
            name=f"pick_timeout_{match_id}",
        )


async def start_draft(context, players_ids: list, mode: str):
    conn        = sqlite3.connect('faceit_bot.db')
    c           = conn.cursor()
    creator_ids = get_creator_ids()
    admin_ids   = get_admin_ids()
    player_data = {}
    _ec, _mc, _wc, _lc = mode_cols(mode)
    for pid in players_ids:
        c.execute(
            f'SELECT user_id, COALESCE(first_name, username), {_ec}, game_id, {_mc}, official_badge FROM users WHERE user_id=?',
            (pid,)
        )
        row = c.fetchone()
        if row:
            player_data[row[0]] = {'name': row[1], 'elo': row[2] or 0,
                                   'is_creator': row[0] in creator_ids,
                                   'is_admin':   row[0] in admin_ids,
                                   'game_id': row[3] or '',
                                   'matches_played': row[4] or 0,
                                   'official_badge': row[5] or 0}
    c.execute("INSERT INTO team_matches (status, mode) VALUES ('draft', ?)", (mode,))
    match_id = c.lastrowid
    conn.commit()
    conn.close()

    sorted_ids   = sorted(player_data.keys(), key=lambda uid: player_data[uid]['elo'], reverse=True)
    real_players = [uid for uid in sorted_ids if uid > 0]
    bots_sorted  = [uid for uid in sorted_ids if uid <= 0]
    if len(real_players) == 0:
        captain1, captain2 = sorted_ids[0], sorted_ids[1]
    elif len(real_players) == 1:
        # Единственный реальный игрок — всегда капитан 1, бот — капитан 2
        captain1 = real_players[0]
        captain2 = bots_sorted[0] if bots_sorted else sorted_ids[1]
    else:
        captain1, captain2 = real_players[0], real_players[1]

    remaining = [uid for uid in sorted_ids if uid not in (captain1, captain2)]
    draft = {
        'match_id':   match_id,
        'mode':       mode,
        'captain1':   captain1,
        'captain2':   captain2,
        'remaining':  list(remaining),
        'team1':      [captain1],
        'team2':      [captain2],
        'pick_index': 0,
        'player_data':player_data,
        'all_players':list(players_ids),
    }
    draft_state[match_id] = draft

    announce  = draft_status_text(draft, title=f"{E_CIRCLE_M} <b>Матч #{match_id} [{mode}] — Драфт начался!</b>")
    for uid in players_ids:
        if is_bot(uid):
            continue  # боты не имеют реальных чатов
        try:
            await context.bot.send_message(chat_id=uid, text=announce, parse_mode='HTML')
        except Exception:
            pass
    await send_pick_keyboard(context, draft)


async def pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query      = update.callback_query
    parts      = query.data.split("_")
    match_id   = int(parts[1])
    picked_uid = int(parts[2])
    captain_id = query.from_user.id

    # Быстрая проверка без лока — если матча нет вообще
    if not draft_state.get(match_id):
        await query.answer("Драфт уже завершён.", show_alert=True)
        return

    async with _get_draft_lock(match_id):
        draft = draft_state.get(match_id)
        if not draft or draft.get('finalized'):
            await query.answer("Драфт уже завершён.", show_alert=True)
            return

        pick_order   = MODES[draft['mode']]['pick_order']
        pick_idx     = draft['pick_index']
        current_team = pick_order[pick_idx]
        current_cap  = draft['captain1'] if current_team == 1 else draft['captain2']
        if captain_id != current_cap:
            await query.answer("Сейчас не ваш ход!", show_alert=True)
            return
        if picked_uid not in draft['remaining']:
            await query.answer("Этот игрок уже выбран.", show_alert=True)
            return

        # Отменяем таймер авто-пика — капитан успел выбрать сам
        _cancel_pick_timer(context, match_id)

        draft['remaining'].remove(picked_uid)
        if current_team == 1:
            draft['team1'].append(picked_uid)
        else:
            draft['team2'].append(picked_uid)
        draft['pick_index'] += 1

        picked_name = draft['player_data'][picked_uid]['name']
        should_finalize = draft['pick_index'] >= len(pick_order)
        if should_finalize:
            draft['finalized'] = True
            draft_state.pop(match_id, None)
            _release_draft_lock(match_id)

    # IO вне лока
    await query.answer(f"Выбран: {picked_name}!")
    await _edit_html(query, f"{E_CHECK} <b>{_esc(picked_name)}</b> добавлен в Команду {current_team}")

    if should_finalize:
        await finalize_draft(context, draft)
    else:
        status = draft_status_text(draft)
        for uid in draft['all_players']:
            if is_bot(uid):
                continue
            try:
                await context.bot.send_message(chat_id=uid, text=status, parse_mode='HTML')
            except Exception:
                pass
        await send_pick_keyboard(context, draft)


def build_team_msg(match_id: int, draft: dict, chosen_map: str) -> str:
    player_data = draft['player_data']
    mode        = draft['mode']
    cap1, cap2  = draft['captain1'], draft['captain2']
    mode_icon   = MODES.get(mode, MODES['5v5'])['emoji']
    ct_team     = draft.get('ct_team', 1)

    def side_label(num):
        if ct_team == num:
            return f"{E_GREEN} Спецназы (CT)"
        return f"{E_ORANGE} Террористы (T)"

    def team_block(uids, num):
        team  = [player_data[uid] for uid in uids]
        avg   = sum(p['elo'] for p in team) // len(team)
        lines = ""
        for uid_item in uids:
            p        = player_data[uid_item]
            lvl_icon = LEVEL_ICONS.get(elo_to_level(p['elo']), "")
            off_val  = p.get('official_badge', 0)
            off_b    = (f" {E_OFF1}" if off_val == 1 else f" {E_OFF2}" if off_val == 2 else f" {E_OFF3}" if off_val == 3 else "")
            badge    = (f" {E_CHECK}" if p.get('is_creator') else "") + (f" {E_ADMIN}" if p.get('is_admin') else "") + off_b
            cap_tag  = f" {E_CROWN}" if uid_item in (cap1, cap2) else ""
            gid_str  = f"  <code>{p['game_id']}</code>" if uid_item in (cap1, cap2) and p.get('game_id') else ""
            lines   += f"  <b>{_esc(p['name'])}</b>{badge}{cap_tag}{gid_str}  〔{lvl_icon}〕  <code>{p['elo']}</code> ELO\n"
        return f"{side_label(num)} <b>Команда {num}</b>  ·  ср. ELO <code>{avg}</code>\n{lines}"

    return (
        f"{E_AIM} {mode_icon} <b>Матч #{match_id} [{mode}] готов!</b>\n\n"
        f"<tg-emoji emoji-id='5377612479830453771'>🗺</tg-emoji>  <b>Карта: {chosen_map}</b>\n\n"
        f"{team_block(draft['team1'], 1)}\n"
        f"{team_block(draft['team2'], 2)}\n"
        f"<tg-emoji emoji-id='5319247469165433798'>📸</tg-emoji> <b>После игры:</b>\n"
        f"<i>1. Скиньте скриншот с результатом в этот чат с ботом.</i>\n"
        f"<i>2. Победитель и статистика определяются автоматически — результат зачтётся "
        f"сразу после проверки скриншота.</i>"
    )


async def finalize_draft(context, draft: dict):
    match_id    = draft['match_id']
    # Гасим таймер и освобождаем лок (idempotent — если уже сделано, ничего не происходит)
    _cancel_pick_timer(context, match_id)
    _release_draft_lock(match_id)
    player_data = draft['player_data']
    ct_team     = random.choice([1, 2])
    draft['ct_team'] = ct_team
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute("UPDATE team_matches SET status='active', ct_team=? WHERE match_id=?", (ct_team, match_id))
    for uid in draft['team1']:
        c.execute('INSERT INTO match_players (match_id, user_id, team, elo_before) VALUES (?,?,?,?)',
                  (match_id, uid, 1, player_data[uid]['elo']))
    for uid in draft['team2']:
        c.execute('INSERT INTO match_players (match_id, user_id, team, elo_before) VALUES (?,?,?,?)',
                  (match_id, uid, 2, player_data[uid]['elo']))
    conn.commit()
    conn.close()
    await start_map_vote(context, draft)


# ══════════════════════════════════════════════════════════════════════════════
#  БАН КАРТ
#  Порядок банов: Капитан 1 → Капитан 2 → Капитан 1  (итого 3 бана, 1 карта остаётся)
# ══════════════════════════════════════════════════════════════════════════════

# Порядок банов: какой капитан (1 или 2) банит на каждом шаге
MAP_BAN_ORDER = [1, 2, 1]  # 3 бана → 1 карта остаётся из 4


def map_ban_keyboard(match_id: int, remaining_indices: list) -> InlineKeyboardMarkup:
    """Клавиатура с доступными для бана картами."""
    rows = []
    for i in remaining_indices:
        name = MAPS[i]
        eid  = MAPS_EMOJI.get(name)
        if eid:
            btn = _ebtn(f"Забанить {name}", eid, 2, f"mb_{match_id}_{i}")
        else:
            btn = InlineKeyboardButton(f"🚫 Забанить {name}", callback_data=f"mb_{match_id}_{i}")
        rows.append([btn])
    return InlineKeyboardMarkup(rows)


def _map_ban_status_text(match_id: int, state: dict) -> str:
    """Текстовое состояние процесса банов."""
    mode      = state['mode']
    mode_icon = MODES.get(mode, MODES['5v5'])['emoji']
    ban_step  = state['ban_step']          # сколько банов уже сделано
    draft     = state['draft']
    cap1_name = _esc(draft['player_data'][draft['captain1']]['name'])
    cap2_name = _esc(draft['player_data'][draft['captain2']]['name'])

    lines = [f"<tg-emoji emoji-id='5377612479830453771'>🗺</tg-emoji> {mode_icon} <b>Матч #{match_id} [{mode}] — Бан карт</b>\n"]
    lines.append(f"{E_CROWN} Капитан <tg-emoji emoji-id=\"5416081784641168838\">🟢</tg-emoji> <b>{cap1_name}</b>  |  Капитан <tg-emoji emoji-id=\"5411225014148014586\">🔴</tg-emoji> <b>{cap2_name}</b>\n")

    # Показываем все карты: забаненные и доступные
    for i, name in enumerate(MAPS):
        eid = MAPS_EMOJI.get(name)
        emoji_tag = f'<tg-emoji emoji-id="{eid}">🤩</tg-emoji>' if eid else "<tg-emoji emoji-id='5377612479830453771'>🗺</tg-emoji>"
        if i in state['banned']:
            banner_cap = state['banned'][i]
            banner_name = _esc(draft['player_data'][banner_cap]['name'])
            lines.append(f"  {E_BAN} <s>{name}</s> — <i>забанено ({banner_name})</i>")
        else:
            lines.append(f"  {emoji_tag} {name}")

    if ban_step < len(MAP_BAN_ORDER):
        current_cap_num = MAP_BAN_ORDER[ban_step]
        current_cap_uid = draft['captain1'] if current_cap_num == 1 else draft['captain2']
        current_cap_name = _esc(draft['player_data'][current_cap_uid]['name'])
        color = '<tg-emoji emoji-id="5416081784641168838">🟢</tg-emoji>' if current_cap_num == 1 else '<tg-emoji emoji-id="5411225014148014586">🔴</tg-emoji>'
        lines.append(f"\n{E_RIGHT} Банит: {color} <b>{current_cap_name}</b>  (бан {ban_step + 1} из {len(MAP_BAN_ORDER)})")
    return "\n".join(lines)


async def _finish_map_ban(context, match_id: int, state: dict, chosen_map: str):
    """Общая логика завершения банов — выбор карты и уведомления."""
    draft    = state['draft']
    mode     = state['mode']
    mode_icon = MODES.get(mode, MODES['5v5'])['emoji']

    del map_vote_state[match_id]

    # Строим итоговый список банов
    ban_lines = []
    for i, name in enumerate(MAPS):
        if i in state['banned']:
            banner_uid  = state['banned'][i]
            banner_name = _esc(draft['player_data'][banner_uid]['name'])
            ban_lines.append(f"  {E_BAN} {name} — <i>забанено ({banner_name})</i>")
        else:
            ban_lines.append(f"  {E_CHECK} <b>{name}</b> — выбрана!")

    result_msg = (
        f"<tg-emoji emoji-id='5377612479830453771'>🗺</tg-emoji> {mode_icon} <b>Карта выбрана: {chosen_map}!</b>\n\n"
        + "\n".join(ban_lines)
    )
    for uid in draft['all_players']:
        if is_bot(uid):
            continue
        try:
            await context.bot.send_message(chat_id=uid, text=result_msg, parse_mode='HTML')
        except Exception:
            pass

    final_msg = build_team_msg(match_id, draft, chosen_map)
    for uid in draft['all_players']:
        if is_bot(uid):
            continue
        try:
            await context.bot.send_message(chat_id=uid, text=final_msg, parse_mode='HTML')
        except Exception:
            pass

    # Случайно выбираем одного из двух капитанов для создания лобби
    lobby_creator = random.choice([draft['captain1'], draft['captain2']])
    creator_name  = draft['player_data'][lobby_creator]['name']
    creator_gid   = draft['player_data'][lobby_creator].get('game_id', '')
    gid_line      = f"\n{EP_GAME} ID в игре: <code>{creator_gid}</code>" if creator_gid else ""

    creator_msg = (
        f"{E_CROWN} <b>Ты выбран для создания лобби!</b>\n\n"
        f"{EP_GAME} Матч #{match_id} [{mode}] · Карта: <b>{chosen_map}</b>\n\n"
        f"Создай комнату в игре и пригласи всех участников матча.\n"
        f"Остальные игроки уже знают, что лобби создаёшь <b>ты</b>."
    )
    if not is_bot(lobby_creator):
        try:
            await context.bot.send_message(chat_id=lobby_creator, text=creator_msg, parse_mode='HTML')
        except Exception:
            pass

    others_msg = (
        f"{E_PEOPLE} <b>Лобби создаёт: {_esc(creator_name)}</b>{gid_line}\n\n"
        f"Ожидайте приглашения в игру от капитана <b>{_esc(creator_name)}</b>."
    )
    for uid in draft['all_players']:
        if uid == lobby_creator or is_bot(uid):
            continue
        try:
            await context.bot.send_message(chat_id=uid, text=others_msg, parse_mode='HTML')
        except Exception:
            pass


async def start_map_vote(context, draft: dict):
    """Точка входа из finalize_draft — запускает бан карт."""
    await start_map_ban(context, draft)


async def start_map_ban(context, draft: dict):
    """Инициализирует процесс банов карт."""
    match_id  = draft['match_id']
    mode      = draft['mode']
    remaining = list(range(len(MAPS)))  # индексы оставшихся карт

    map_vote_state[match_id] = {
        'draft':     draft,
        'mode':      mode,
        'banned':    {},            # {map_idx: captain_uid}
        'remaining': remaining,
        'ban_step':  0,             # текущий шаг в MAP_BAN_ORDER
        'players':   set(draft['all_players']),
    }
    state = map_vote_state[match_id]

    # Если капитан — бот, он банит автоматически прямо сейчас
    await _process_bot_bans(context, match_id, state)
    if match_id not in map_vote_state:
        return  # все баны уже завершены ботами

    await _send_ban_step(context, match_id, state)


async def _process_bot_bans(context, match_id: int, state: dict):
    """Автоматически выполняет баны за ботов."""
    draft = state['draft']
    while state['ban_step'] < len(MAP_BAN_ORDER) and match_id in map_vote_state:
        cap_num = MAP_BAN_ORDER[state['ban_step']]
        cap_uid = draft['captain1'] if cap_num == 1 else draft['captain2']
        if not is_bot(cap_uid):
            break  # живой капитан — останавливаемся
        # Бот банит случайную карту
        ban_idx = random.choice(state['remaining'])
        state['remaining'].remove(ban_idx)
        state['banned'][ban_idx] = cap_uid
        state['ban_step'] += 1
        # Проверяем финал
        if state['ban_step'] >= len(MAP_BAN_ORDER):
            if not state['remaining']:
                # Нештатная ситуация — забанено больше карт, чем должно быть
                del map_vote_state[match_id]
                return
            chosen_map = MAPS[state['remaining'][0]]
            await _finish_map_ban(context, match_id, state, chosen_map)
            return


async def _send_ban_step(context, match_id: int, state: dict):
    """Отправляет всем игрокам текущее состояние банов и клавиатуру капитану."""
    if match_id not in map_vote_state:
        return
    draft     = state['draft']
    ban_step  = state['ban_step']

    # Текст состояния для всех
    status_text = _map_ban_status_text(match_id, state)
    _t, _e = _parse_msg(status_text)

    cap_num = MAP_BAN_ORDER[ban_step]
    cap_uid = draft['captain1'] if cap_num == 1 else draft['captain2']
    keyboard = map_ban_keyboard(match_id, state['remaining'])

    for uid in draft['all_players']:
        if is_bot(uid):
            continue
        try:
            if uid == cap_uid:
                # Капитану — с клавиатурой бана
                await context.bot.send_message(
                    chat_id=uid, text=_t, entities=_e, reply_markup=keyboard
                )
            else:
                # Остальным — без кнопок
                await context.bot.send_message(chat_id=uid, text=_t, entities=_e)
        except Exception:
            pass


async def map_ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатия кнопки бана карты (callback_data = mb_{match_id}_{map_idx})."""
    query    = update.callback_query
    parts    = query.data.split("_")
    match_id = int(parts[1])
    map_idx  = int(parts[2])
    voter_id = query.from_user.id

    state = map_vote_state.get(match_id)
    if not state:
        await query.answer("Бан карт уже завершён.", show_alert=True)
        return

    draft    = state['draft']
    ban_step = state['ban_step']

    if ban_step >= len(MAP_BAN_ORDER):
        await query.answer("Все баны уже выполнены.", show_alert=True)
        return

    # Проверяем, что жмёт именно нужный капитан
    cap_num = MAP_BAN_ORDER[ban_step]
    cap_uid = draft['captain1'] if cap_num == 1 else draft['captain2']
    if voter_id != cap_uid:
        cap_name = draft['player_data'][cap_uid]['name']
        await query.answer(f"Сейчас банит {cap_name}!", show_alert=True)
        return

    if map_idx not in state['remaining']:
        await query.answer("Эта карта уже забанена.", show_alert=True)
        return

    # Выполняем бан
    state['remaining'].remove(map_idx)
    state['banned'][map_idx] = voter_id
    state['ban_step'] += 1
    banned_map_name = MAPS[map_idx]

    await query.answer(f"🚫 Вы забанили «{banned_map_name}»!")

    # Обновляем сообщение капитана
    try:
        await _edit_html(query, f"{E_BAN} Вы забанили карту <b>{banned_map_name}</b>.")
    except Exception:
        pass

    # Если все баны выполнены — выбираем оставшуюся карту
    if state['ban_step'] >= len(MAP_BAN_ORDER):
        if not state['remaining']:
            del map_vote_state[match_id]
            return
        chosen_map = MAPS[state['remaining'][0]]
        await _finish_map_ban(context, match_id, state, chosen_map)
        return

    # Обрабатываем возможные боты-капитаны на следующем шаге
    await _process_bot_bans(context, match_id, state)
    if match_id not in map_vote_state:
        return

    # Уведомляем всех о текущем состоянии (включая voter_id — он может банить снова)
    update_text = _map_ban_status_text(match_id, state)
    _t, _e = _parse_msg(update_text)

    next_ban_step = state['ban_step']
    next_cap_num  = MAP_BAN_ORDER[next_ban_step]
    next_cap_uid  = draft['captain1'] if next_cap_num == 1 else draft['captain2']
    next_keyboard = map_ban_keyboard(match_id, state['remaining'])

    for uid in draft['all_players']:
        if is_bot(uid):
            continue
        try:
            if uid == next_cap_uid:
                await context.bot.send_message(
                    chat_id=uid, text=_t, entities=_e, reply_markup=next_keyboard
                )
            else:
                await context.bot.send_message(chat_id=uid, text=_t, entities=_e)
        except Exception:
            pass


# Обратная совместимость — старый callback (голосование) больше не используется
async def map_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await map_ban_callback(update, context)


# ══════════════════════════════════════════════════════════════════════════════
#  ГОЛОСОВАНИЕ ЗА ПОБЕДИТЕЛЯ
# ══════════════════════════════════════════════════════════════════════════════

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data

    if data == "resetelo_confirm":
        uid = query.from_user.id
        conn = sqlite3.connect('faceit_bot.db')
        c = conn.cursor()
        c.execute('SELECT is_admin FROM users WHERE user_id=?', (uid,))
        row = c.fetchone()
        if not row or not row[0]:
            await query.answer("Нет прав.", show_alert=True)
            conn.close()
            return
        c.execute('UPDATE users SET elo=0, level=1, matches_played=0, wins=0')
        conn.commit()
        conn.close()
        await _edit_html(query, f"{E_CONFIRM} <b>ELO сброшен. Новый сезон!</b>")
        await query.answer("Сброс выполнен!")
        return

    if data == "resetelo_cancel":
        await _edit_html(query, f"{E_CROSS} Сброс ELO отменён.")
        await query.answer()
        return

    if data.startswith("newseason_confirm_"):
        uid = query.from_user.id
        conn = sqlite3.connect('faceit_bot.db')
        c = conn.cursor()
        c.execute('SELECT is_admin FROM users WHERE user_id=?', (uid,))
        row = c.fetchone()
        if not row or not row[0]:
            await query.answer("Нет прав.", show_alert=True)
            conn.close()
            return
        season_num = int(data.split("_")[2])
        c.execute('''INSERT INTO season_archive
                     (season_num, user_id, username, elo, wins, matches,
                      elo_5v5, matches_5v5, wins_5v5,
                      elo_3v3, matches_3v3, wins_3v3,
                      elo_2v2, matches_2v2, wins_2v2)
                     SELECT ?, user_id, COALESCE(first_name, username),
                            elo, wins, matches_played,
                            elo_5v5, matches_5v5, wins_5v5,
                            elo_3v3, matches_3v3, wins_3v3,
                            elo_2v2, matches_2v2, wins_2v2
                     FROM users WHERE matches_played > 0''', (season_num - 1,))
        c.execute('''UPDATE users SET
                      elo=0, level=1, matches_played=0, wins=0,
                      elo_5v5=0, matches_5v5=0, wins_5v5=0, level_5v5=1,
                      elo_3v3=0, matches_3v3=0, wins_3v3=0, level_3v3=1,
                      elo_2v2=0, matches_2v2=0, wins_2v2=0, level_2v2=1''')
        conn.commit()
        c.execute('SELECT user_id FROM users')
        all_users = [r[0] for r in c.fetchall()]
        conn.close()
        await _edit_html(query, f"<tg-emoji emoji-id='5411520005386806155'>🏁</tg-emoji> <b>Сезон {season_num} начался!</b>\n\nELO всех игроков сброшен.")
        await query.answer("Новый сезон запущен!")
        for user_id in all_users:
            try:
                await _send_html(context.bot, user_id, f"<tg-emoji emoji-id='5411520005386806155'>🏁</tg-emoji> <b>Начался новый сезон {season_num}!</b>\n\nELO сброшен до 0. Удачи!")
            except Exception:
                pass
        return

    if data == "newseason_cancel":
        await _edit_html(query, f"{E_CROSS} Новый сезон отменён.")
        await query.answer()
        return

    if data.startswith("resetseason_confirm_"):
        uid = query.from_user.id
        conn = sqlite3.connect('faceit_bot.db')
        c = conn.cursor()
        c.execute('SELECT is_admin FROM users WHERE user_id=?', (uid,))
        row = c.fetchone()
        if not row or not row[0]:
            await query.answer("Нет прав.", show_alert=True)
            conn.close()
            return
        _, _, season_num_str, new_date = data.split("_", 3)
        season_num = int(season_num_str)
        c.execute('''INSERT INTO season_archive
                     (season_num, user_id, username, elo, wins, matches,
                      elo_5v5, matches_5v5, wins_5v5,
                      elo_3v3, matches_3v3, wins_3v3,
                      elo_2v2, matches_2v2, wins_2v2)
                     SELECT ?, user_id, COALESCE(first_name, username),
                            elo, wins, matches_played,
                            elo_5v5, matches_5v5, wins_5v5,
                            elo_3v3, matches_3v3, wins_3v3,
                            elo_2v2, matches_2v2, wins_2v2
                     FROM users WHERE matches_played > 0
                        OR matches_5v5 > 0 OR matches_3v3 > 0 OR matches_2v2 > 0''', (season_num,))
        c.execute('''UPDATE users SET
                      elo=0, level=1, matches_played=0, wins=0,
                      elo_5v5=0, matches_5v5=0, wins_5v5=0, level_5v5=1,
                      elo_3v3=0, matches_3v3=0, wins_3v3=0, level_3v3=1,
                      elo_2v2=0, matches_2v2=0, wins_2v2=0, level_2v2=1,
                      kills=0, deaths=0''')
        conn.commit()
        c.execute('SELECT user_id FROM users')
        all_users = [r[0] for r in c.fetchall()]
        conn.close()
        set_setting('season_end_date', new_date)
        if get_setting('season_extended_once') != '1':
            set_setting('season_extended_once', '1')
        await _edit_html(
            query,
            f"{E_TROPHY} <b>Сезон сброшен!</b>\n\n"
            f"Статистика всех игроков и таблицы лидеров обнулены.\n"
            f"Прошлый сезон сохранён в архив (Сезон {season_num}).\n"
            f"{E_CAL} Новая дата окончания сезона: <b>{new_date}</b>"
        )
        await query.answer("Сезон сброшен!")
        for user_id in all_users:
            try:
                await _send_html(
                    context.bot, user_id,
                    f"{E_TROPHY} <b>Начался новый сезон!</b>\n\n"
                    f"Статистика и ELO сброшены. Удачи в бою!\n"
                    f"{E_CAL} Дата окончания: <b>{new_date}</b>"
                )
            except Exception:
                pass
        return

    if data == "resetseason_cancel":
        await _edit_html(query, f"{E_CROSS} Сброс сезона отменён.")
        await query.answer()
        return

    # Голосование за победителя удалено — результат матча теперь определяется
    # автоматически по OCR-распознаванию скриншота (см. screenshot_handler).
    await query.answer()


# ══════════════════════════════════════════════════════════════════════════════
#  СКРИНШОТ
# ══════════════════════════════════════════════════════════════════════════════

async def screenshot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender_id = update.effective_user.id
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('''SELECT mp.match_id FROM match_players mp
                 JOIN team_matches tm ON mp.match_id = tm.match_id
                 WHERE mp.user_id = ? AND tm.status = 'active'
                 ORDER BY tm.timestamp DESC LIMIT 1''', (sender_id,))
    row = c.fetchone()
    if not row:
        await update.message.reply_text("У вас нет активного матча.")
        conn.close()
        return
    match_id = row[0]
    c.execute('SELECT id FROM match_screenshots WHERE match_id=?', (match_id,))
    if c.fetchone():
        await _reply_html(update.message, f"{E_WAITING} Скриншот матча #{match_id} уже загружен и ожидает проверки.")
        conn.close()
        return
    file_id = update.message.photo[-1].file_id
    c.execute('INSERT INTO match_screenshots (match_id, user_id, file_id) VALUES (?,?,?)',
              (match_id, sender_id, file_id))
    conn.commit()
    c.execute('SELECT COALESCE(first_name, username) FROM users WHERE user_id=?', (sender_id,))
    name_row    = c.fetchone()
    sender_name = name_row[0] if name_row else str(sender_id)
    c.execute('SELECT user_id FROM match_players WHERE match_id=?', (match_id,))
    players     = [r[0] for r in c.fetchall()]
    c.execute('SELECT user_id FROM users WHERE is_admin=1')
    admins      = [r[0] for r in c.fetchall()]
    c.execute('SELECT mode FROM team_matches WHERE match_id=?', (match_id,))
    mode_row = c.fetchone()
    mode     = (mode_row[0] or '5v5') if mode_row else '5v5'
    c.execute('SELECT ct_team FROM team_matches WHERE match_id=?', (match_id,))
    ct_row = c.fetchone()
    ct_team = (ct_row[0] or 1) if ct_row else 1
    conn.close()

    # Автоматически считываем К/Д каждого игрока прямо со скриншота (без ИИ,
    # локальный OCR) — ручной ввод статистики больше не нужен.
    kd_status_line = ""
    try:
        file_obj = await context.bot.get_file(file_id)
        buf = io.BytesIO()
        await file_obj.download_to_memory(buf)
        ocr_result = _ocr_extract_scoreboard(buf.getvalue())
    except Exception:
        logging.exception("Не удалось загрузить скриншот для OCR (матч #%s).", match_id)
        ocr_result = None

    ct_label  = f"{E_GREEN} Спецназы" if ct_team == 1 else f"{E_ORANGE} Террористы"
    t_label   = f"{E_ORANGE} Террористы" if ct_team == 1 else f"{E_GREEN} Спецназы"

    # Определяем победившую команду автоматически по слову "ПОБЕДА" на скриншоте
    # (сторона left/right сопоставляется с командой через ct_team матча).
    ocr_winner_team = None
    if ocr_result:
        all_matched, unmatched_uids = _apply_ocr_stats_to_match(match_id, ocr_result)
        if all_matched:
            kd_status_line = f"\n{E_CHECK} К/Д всех игроков распознано автоматически."
        elif unmatched_uids:
            kd_status_line = f"\n⚠️ Не удалось распознать К/Д для {len(unmatched_uids)} игрок(ов) — им засчитан нейтральный результат."
        winner_side = ocr_result.get('winner_side')
        if winner_side:
            t_team_num = 2 if ct_team == 1 else 1
            ocr_winner_team = ct_team if winner_side == 'left' else t_team_num
    else:
        kd_status_line = "\n⚠️ Не удалось распознать таблицу результатов на скриншоте — К/Д не будет учтён при начислении ELO."

    if ocr_winner_team:
        winner_label = ct_label if ocr_winner_team == ct_team else t_label
        winner_status_line = f"\n{E_CHECK} Победитель распознан автоматически: {winner_label}"
    else:
        winner_status_line = "\n⚠️ Не удалось автоматически определить победителя — будет засчитана команда загрузившего скриншот."

    conn3 = sqlite3.connect('faceit_bot.db')
    conn3.execute('UPDATE team_matches SET ocr_winner_team=? WHERE match_id=?', (ocr_winner_team, match_id))
    conn3.commit()
    conn3.close()

    admin_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Одобрить", callback_data=f"ss_approve_{match_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"ss_reject_{match_id}"),
    ]])
    admin_caption = (
        f"<tg-emoji emoji-id='5231012545799666522'>🔎</tg-emoji> <b>Проверка скриншота — Матч #{match_id} [{mode}]</b>\n"
        f"Загрузил: <b>{sender_name}</b>\n"
        f"Команда 1 = {ct_label} · Команда 2 = {t_label}"
        f"{winner_status_line}"
        f"{kd_status_line}"
    )

    if not admins:
        # Нет администраторов — результат зачитывается сразу автоматически,
        # без ручного подтверждения.
        conn2 = sqlite3.connect('faceit_bot.db')
        c2 = conn2.cursor()
        c2.execute('UPDATE team_matches SET screenshot_submitted=1 WHERE match_id=?', (match_id,))
        conn2.commit()
        conn2.close()

        caption = (
            f"<tg-emoji emoji-id='5319247469165433798'>📸</tg-emoji> <b>Скриншот матча #{match_id} [{mode}]</b>\n"
            f"Загрузил: <b>{sender_name}</b>"
            f"{winner_status_line}"
            f"{kd_status_line}"
        )
        for uid in players:
            try:
                _ct, _ce = _parse_msg(caption)
                await context.bot.send_photo(chat_id=uid, photo=file_id, caption=_ct, caption_entities=_ce)
            except Exception:
                pass

        winning_team = ocr_winner_team or ct_team
        calib_games  = MODES.get(mode, MODES['5v5'])['calib_games']
        gain, winners, losers, per_uid = finalize_match(match_id, winning_team, mode)
        winner_label = ct_label if winning_team == ct_team else t_label
        await _reply_html(
            update.message,
            f"{E_CHECK} Матч #{match_id} [{mode}] завершён!\n"
            f"Победила {winner_label} {E_TROPHY}\n\n"
            f"ELO начислен автоматически по статистике каждого игрока.\n"
            f"(базовое изменение: ±{gain} ELO)"
        )
        all_uids = [uid for uid, _ in winners + losers]
        for uid in all_uids:
            await _send_match_notification(context.bot, uid, match_id, per_uid.get(uid, {}), calib_games)
        mvp_info = _check_and_award_mvp(match_id)
        if mvp_info:
            await _announce_mvp(context.bot, match_id, mvp_info)
        return

    for admin_id in admins:
        try:
            _act, _ace = _parse_msg(admin_caption)
            await context.bot.send_photo(
                chat_id=admin_id, photo=file_id,
                caption=_act, caption_entities=_ace, reply_markup=admin_keyboard
            )
        except Exception:
            pass
    for uid in players:
        try:
            await _send_html(context.bot, uid,
                f"<tg-emoji emoji-id='5319247469165433798'>📸</tg-emoji> Скриншот матча #{match_id} отправлен на проверку администратору."
            )
        except Exception:
            pass
    await _reply_html(update.message, f"{E_WAITING} Скриншот отправлен администратору.")


async def admin_screenshot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    data     = query.data
    admin_id = query.from_user.id
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT is_admin FROM users WHERE user_id=?', (admin_id,))
    row = c.fetchone()
    if not row or not row[0]:
        await query.answer("Нет прав.", show_alert=True)
        conn.close()
        return
    parts    = data.split("_")
    action   = parts[1]
    match_id = int(parts[2])
    c.execute('SELECT user_id FROM match_players WHERE match_id=?', (match_id,))
    players  = [r[0] for r in c.fetchall()]
    c.execute('SELECT mode FROM team_matches WHERE match_id=?', (match_id,))
    mode_row = c.fetchone()
    mode     = (mode_row[0] or '5v5') if mode_row else '5v5'
    conn.close()

    if action == "approve":
        conn2 = sqlite3.connect('faceit_bot.db')
        try:
            c2 = conn2.cursor()
            c2.execute('SELECT ocr_winner_team FROM team_matches WHERE match_id=?', (match_id,))
            ocr_row = c2.fetchone()
            winning_team = ocr_row[0] if ocr_row and ocr_row[0] else None
            if not winning_team:
                # OCR не распознал победителя — засчитываем команду загрузившего скриншот.
                c2.execute('SELECT user_id FROM match_screenshots WHERE match_id=? ORDER BY id DESC LIMIT 1', (match_id,))
                uploader_row = c2.fetchone()
                uploader_id  = uploader_row[0] if uploader_row else None
                c2.execute('SELECT team FROM match_players WHERE match_id=? AND user_id=?', (match_id, uploader_id))
                team_row     = c2.fetchone()
                winning_team = team_row[0] if team_row else 1
        finally:
            conn2.close()

        calib_games = MODES.get(mode, MODES['5v5'])['calib_games']
        gain, winners, losers, per_uid = finalize_match(match_id, winning_team, mode)

        conn_ct2 = sqlite3.connect('faceit_bot.db')
        try:
            c_ct2 = conn_ct2.cursor()
            c_ct2.execute('SELECT ct_team FROM team_matches WHERE match_id=?', (match_id,))
            ct_row2 = c_ct2.fetchone()
        finally:
            conn_ct2.close()
        ct_team2 = (ct_row2[0] or 1) if ct_row2 else 1
        winner_side = f"{E_GREEN} Спецназы (CT)" if ct_team2 == winning_team else f"{E_ORANGE} Террористы (T)"
        _cpt = (
            f"{E_CHECK} Скриншот матча #{match_id} <b>одобрен</b>.\n"
            f"Победила {winner_side} — "
            f"ELO начислен автоматически (база: ±{gain})"
        )
        _ct, _ce = _parse_msg(_cpt)
        await query.edit_message_caption(_ct, caption_entities=_ce)
        await query.answer("Одобрено!")

        all_uids = [uid for uid, _ in winners + losers]
        for uid in all_uids:
            await _send_match_notification(context.bot, uid, match_id, per_uid.get(uid, {}), calib_games)

        mvp_info = _check_and_award_mvp(match_id)
        if mvp_info:
            await _announce_mvp(context.bot, match_id, mvp_info)
    elif action == "reject":
        _reset_match_kd(match_id)
        conn2 = sqlite3.connect('faceit_bot.db')
        try:
            c2 = conn2.cursor()
            c2.execute('DELETE FROM match_screenshots WHERE match_id=?', (match_id,))
            conn2.commit()
        finally:
            conn2.close()
        _ct, _ce = _parse_msg(f"{E_CROSS} Скриншот матча #{match_id} <b>отклонён</b>.")
        await query.edit_message_caption(_ct, caption_entities=_ce)
        await query.answer("Отклонено.")
        for uid in players:
            try:
                await _send_html(context.bot, uid, f"{E_CROSS} Скриншот матча #{match_id} отклонён. Загрузите новый.")
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
#  РОУТЕР + СТАТИСТИКА K/D
# ══════════════════════════════════════════════════════════════════════════════

BUTTON_ROUTES = {
    "👤 Профиль":          action_profile,
    "🔍 Найти матч":       action_find_match,
    "👥 Очередь":          action_queue_status,
    "🚪 Выйти из очереди": action_leave_queue,
    "<tg-emoji emoji-id='5282843764451195532'>📋</tg-emoji> История":          action_history,
    "<tg-emoji emoji-id='5280769763398671636'>🏆</tg-emoji> Таблица лидеров":  action_leaderboard,
    "📄 Правила":          action_rules,
    "💰 Донат":            action_donate,
    "Репорт <tg-emoji emoji-id='5395695537687123235'>🚨</tg-emoji>":           action_report,
}


async def keyboard_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "Меню":
        # Сбрасываем любой активный шаг при явном возврате в меню
        context.user_data.pop('reg_step', None)
        await action_show_menu(update, context)
        return
    handler = BUTTON_ROUTES.get(text)
    if handler:
        await handler(update, context)


async def cmd_mystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """K/D теперь считывается автоматически со скриншота результата матча —
    ручной ввод отключён, чтобы исключить ошибки и накрутку статистики."""
    uid = update.effective_user.id
    if not get_user(uid):
        await update.message.reply_text("Сначала нажмите /start.")
        return

    match_id = None
    if context.args:
        try:
            match_id = int(context.args[0])
        except ValueError:
            match_id = None

    kd_str = None
    if match_id is not None:
        conn = sqlite3.connect('faceit_bot.db')
        c = conn.cursor()
        c.execute('SELECT kills, deaths, kd_entered FROM match_players WHERE match_id=? AND user_id=?',
                  (match_id, uid))
        row = c.fetchone()
        conn.close()
        if row:
            kills, deaths, kd_entered = row
            if kd_entered:
                kd_str = f"\n\n<tg-emoji emoji-id='5222486447306602688'>🔫</tg-emoji> Матч #{match_id}: убийства <b>{kills}</b>, смерти <b>{deaths}</b>."
            else:
                kd_str = f"\n\n<i>К/Д матча #{match_id} ещё не распознано — дождитесь проверки скриншота.</i>"

    await _reply_html(
        update.message,
        f"{E_ZAP} К/Д считывается автоматически со скриншота результата матча — "
        f"ручной ввод статистики больше не требуется."
        f"{kd_str or ''}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN КОМАНДЫ
# ══════════════════════════════════════════════════════════════════════════════

async def _require_admin(update: Update) -> bool:
    uid = update.effective_user.id
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT is_admin FROM users WHERE user_id=?', (uid,))
    row = c.fetchone()
    conn.close()
    if not row or not row[0]:
        await _reply_html(update.message, f"{E_CROSS} Только для администраторов.")
        return False
    return True


async def _require_mod_or_admin(update: Update) -> bool:
    """Доступ к варнам/банам — для админов и для модераторов (лёгкая роль,
    выдаваемая через /moder, без остальных прав администратора)."""
    uid = update.effective_user.id
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT is_admin, is_moderator FROM users WHERE user_id=?', (uid,))
    row = c.fetchone()
    conn.close()
    if not row or not (row[0] or row[1]):
        await _reply_html(update.message, f"{E_CROSS} Только для администраторов и модераторов.")
        return False
    return True


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '111qqq111!')
    if not context.args or context.args[0] != ADMIN_PASSWORD:
        await _reply_html(update.message, f"{E_CROSS} Неверный пароль.")
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Значок 1", callback_data="admin_self_badge_1",
                             api_kwargs={'icon_custom_emoji_id': '5314334546269872179'}),
        InlineKeyboardButton("Значок 2", callback_data="admin_self_badge_2",
                             api_kwargs={'icon_custom_emoji_id': '5314666276658912691'}),
        InlineKeyboardButton("Значок 3", callback_data="admin_self_badge_3",
                             api_kwargs={'icon_custom_emoji_id': '5314302200871164289'}),
    ]])
    await _reply_html(update.message, f"{E_SPARK} Пароль верный! Выберите значок администратора:", reply_markup=keyboard)


async def cmd_renderstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Диагностика рендера карточек. Только для администраторов."""
    uid = update.effective_user.id
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '111qqq111!')
    creator_ids = get_creator_ids()
    admin_ids   = get_admin_ids()
    # Разрешено только создателям/администраторам (или по паролю)
    is_privileged = uid in creator_ids or uid in admin_ids
    if not is_privileged:
        if not context.args or context.args[0] != ADMIN_PASSWORD:
            await _reply_html(update.message, f"{E_CROSS} Нет доступа.")
            return

    lines = [f"{E_ZAP} <b>Статус рендера карточек</b>\n"]

    # 1. Playwright
    try:
        import playwright  # noqa: F401
        import importlib.metadata as _imeta
        pw_ver = _imeta.version("playwright")
        lines.append(f"{E_CHECK} <b>Playwright</b> установлен — v{pw_ver}")
    except Exception:
        lines.append(
            f"{E_CROSS} <b>Playwright</b> НЕ установлен\n"
            f"   Запустите: <code>pip install playwright</code>"
        )

    # 2. Chromium / Chrome
    try:
        from card_renderer import _find_chromium
        chrome_path = _find_chromium()
        if chrome_path:
            lines.append(f"{E_CHECK} <b>Chromium</b> найден:\n   <code>{chrome_path}</code>")
        else:
            lines.append(
                f"{E_CROSS} <b>Chromium</b> НЕ найден\n"
                f"   Запустите: <code>playwright install chromium</code>\n"
                f"   Или укажите путь в переменной CHROMIUM_PATH"
            )
    except Exception as e:
        lines.append(f"{EP_WARN} <b>Chromium</b>: ошибка проверки — <code>{e}</code>")

    # 3. Тестовый рендер (быстрый)
    lines.append(f"\n{E_WAITING} Тестовый рендер…")
    await _reply_html(update.message, "\n".join(lines))

    try:
        from card_renderer import render_card_bytes
        _test_html = (
            '<html><body style="width:200px;height:100px;background:#1a1a2e;">'
            '<div class="card" style="width:200px;height:100px;background:#16213e;'
            'border-radius:8px;display:flex;align-items:center;justify-content:center;'
            'color:#fff;font-size:14px;">OK</div></body></html>'
        )
        import asyncio as _aio
        png = await _aio.wait_for(render_card_bytes(_test_html), timeout=25)
        _ok_t, _ok_e = _parse_msg(f"{E_CHECK} Рендер работает — {len(png)} байт")
        await update.message.reply_photo(png, caption=_ok_t, caption_entities=_ok_e)
    except Exception as err:
        await _reply_html(
            update.message,
            f"{E_CROSS} <b>Тестовый рендер провалился:</b>\n<code>{err}</code>\n\n"
            f"Карточки будут отправляться в текстовом виде."
        )


async def cmd_setadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '111qqq111!')
    if len(context.args) < 2:
        await _reply_html(update.message,
            f"{E_CROSS} Использование: /setadmin \"пароль\" \"юзернейм\"\n"
            "Пример: /setadmin 111qqq111! username"
        )
        return
    password = context.args[0]
    if password != ADMIN_PASSWORD:
        await _reply_html(update.message, f"{E_CROSS} Неверный пароль.")
        return
    target_name = context.args[1].lstrip('@')
    target = find_user_by_name(target_name)
    if not target:
        await _reply_html(update.message, f"{E_CROSS} Игрок «{target_name}» не найден в базе.")
        return
    target_id, display_name = target
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT is_admin FROM users WHERE user_id=?', (target_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        conn2 = sqlite3.connect('faceit_bot.db')
        conn2.execute('UPDATE users SET is_admin=0 WHERE user_id=?', (target_id,))
        conn2.commit()
        conn2.close()
        await _reply_html(update.message, f"{E_OFF1} Статус администратора снят с <b>{display_name}</b>.")
    else:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Значок 1", callback_data=f"setadmin_badge_1_{target_id}",
                                 api_kwargs={'icon_custom_emoji_id': '5314334546269872179'}),
            InlineKeyboardButton("Значок 2", callback_data=f"setadmin_badge_2_{target_id}",
                                 api_kwargs={'icon_custom_emoji_id': '5314666276658912691'}),
            InlineKeyboardButton("Значок 3", callback_data=f"setadmin_badge_3_{target_id}",
                                 api_kwargs={'icon_custom_emoji_id': '5314302200871164289'}),
        ]])
        await _reply_html(update.message,
            f"{E_SPARK} Выберите значок администратора для <b>{display_name}</b>:",
            reply_markup=keyboard)


async def admin_self_badge_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    badge_id = int(query.data.split('_')[-1])
    conn = sqlite3.connect('faceit_bot.db')
    conn.execute('UPDATE users SET is_admin=1, admin_badge_id=? WHERE user_id=?', (badge_id, uid))
    conn.commit()
    conn.close()
    badge_icons = {1: E_OFF1, 2: E_OFF2, 3: E_OFF3}
    icon = badge_icons.get(badge_id, E_OFF1)
    await _edit_html(query, f"{icon} Вы назначены администратором! Значок добавлен в профиль и лобби.")


async def setadmin_badge_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split('_')
    badge_id  = int(parts[2])
    target_id = int(parts[3])
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT COALESCE(first_name, username) FROM users WHERE user_id=?', (target_id,))
    row = c.fetchone()
    display_name = row[0] if row else str(target_id)
    c.execute('UPDATE users SET is_admin=1, admin_badge_id=? WHERE user_id=?', (badge_id, target_id))
    conn.commit()
    conn.close()
    badge_icons = {1: E_OFF1, 2: E_OFF2, 3: E_OFF3}
    icon = badge_icons.get(badge_id, E_OFF1)
    await _edit_html(query, f"{icon} <b>{display_name}</b> теперь администратор!")
    try:
        await _send_html(context.bot, target_id,
            f"{icon} <b>Вам выдан статус администратора!</b>\n"
            f"Значок отображается в профиле, лобби и матчах.")
    except Exception:
        pass


async def cmd_moder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выдаёт/снимает статус модератора (пароль администратора).
    Модератор может только выдавать/снимать варны и баны — /warn, /unwarn,
    /ban, /unban, /tempban. Остальные админ-команды ему недоступны."""
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '111qqq111!')
    if len(context.args) < 2:
        await _reply_html(update.message,
            f"{E_CROSS} Использование: /moder \"пароль\" \"юзернейм\"\n"
            "Пример: /moder 111qqq111! username"
        )
        return
    password = context.args[0]
    if password != ADMIN_PASSWORD:
        await _reply_html(update.message, f"{E_CROSS} Неверный пароль.")
        return
    target_name = context.args[1].lstrip('@')
    target = find_user_by_name(target_name)
    if not target:
        await _reply_html(update.message, f"{E_CROSS} Игрок «{target_name}» не найден в базе.")
        return
    target_id, display_name = target
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT is_moderator FROM users WHERE user_id=?', (target_id,))
    row = c.fetchone()
    if row and row[0]:
        c.execute('UPDATE users SET is_moderator=0 WHERE user_id=?', (target_id,))
        conn.commit()
        conn.close()
        await _reply_html(update.message, f"{E_OFF1} Статус модератора снят с <b>{display_name}</b>.")
        try:
            await _send_html(context.bot, target_id, f"{E_OFF1} Вы больше не модератор.")
        except Exception:
            pass
    else:
        c.execute('UPDATE users SET is_moderator=1 WHERE user_id=?', (target_id,))
        conn.commit()
        conn.close()
        await _reply_html(update.message, f"{E_SPARK} <b>{display_name}</b> назначен модератором (варны/баны).")
        try:
            await _send_html(context.bot, target_id,
                f"{E_SPARK} <b>Вам выдан статус модератора!</b>\n"
                f"Доступны команды: /warn, /unwarn, /ban, /unban, /tempban.")
        except Exception:
            pass


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_mod_or_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /ban <имя> [причина]")
        return
    target = find_user_by_name(context.args[0])
    if not target:
        await _reply_html(update.message, f"{E_CROSS} Игрок «{context.args[0]}» не найден.")
        return
    target_id, display_name = target
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "нарушение правил"
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('UPDATE users SET is_banned=1, ban_reason=? WHERE user_id=?', (reason, target_id))
    conn.commit()
    conn.close()
    for m in MODES:
        for q in lobby_queues[m].values():
            if target_id in q:
                q.remove(target_id)
                break
    await _reply_html(update.message, f"{E_BAN} <b>{display_name}</b> заблокирован. Причина: {reason}")
    try:
        await _send_html(context.bot, target_id, f"{E_BAN} Вы заблокированы.\nПричина: {reason}")
    except Exception:
        pass


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_mod_or_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /unban <имя>")
        return
    target = find_user_by_name(context.args[0])
    if not target:
        await _reply_html(update.message, f"{E_CROSS} Игрок «{context.args[0]}» не найден.")
        return
    target_id, display_name = target
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('UPDATE users SET is_banned=0, ban_reason=NULL, ban_until=NULL WHERE user_id=?', (target_id,))
    conn.commit()
    conn.close()
    await _reply_html(update.message, f"{E_CHECK} <b>{display_name}</b> разблокирован.")
    try:
        await _send_html(context.bot, target_id, f"{E_CHECK} Ваш бан снят!")
    except Exception:
        pass


async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /kick <юзернейм>")
        return
    target = find_user_by_name(context.args[0])
    if not target:
        await _reply_html(update.message, f"{E_CROSS} Игрок «{context.args[0]}» не найден.")
        return
    target_id, display_name = target
    kicked = False
    for m in MODES:
        for q in lobby_queues[m].values():
            if target_id in q:
                q.remove(target_id)
                kicked = True
    _cleanup_uid(target_id)
    if kicked:
        await _reply_html(update.message, f"🦵 <b>{display_name}</b> выкинут из очереди.")
        try:
            await _send_html(context.bot, target_id, f"Вы были кикнуты из очереди {E_BAN}. Зайдите в лобби снова {E_SPARK}")
        except Exception:
            pass
    else:
        await _reply_html(update.message, f"{E_SEARCH} <b>{display_name}</b> не находился ни в одной очереди.")


async def cmd_tempban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_mod_or_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /tempban <имя> <дней> [причина]")
        return
    target = find_user_by_name(context.args[0])
    if not target:
        await _reply_html(update.message, f"{E_CROSS} Игрок «{context.args[0]}» не найден.")
        return
    target_id, display_name = target
    try:
        days = int(context.args[1])
    except ValueError:
        await _reply_html(update.message, f"{E_CROSS} Укажи количество дней числом.")
        return
    reason    = " ".join(context.args[2:]) if len(context.args) > 2 else "нарушение правил"
    ban_until = (datetime.datetime.utcnow() + datetime.timedelta(days=days)).isoformat()
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('UPDATE users SET is_banned=1, ban_reason=?, ban_until=? WHERE user_id=?',
              (reason, ban_until, target_id))
    conn.commit()
    conn.close()
    for m in MODES:
        for q in lobby_queues[m].values():
            if target_id in q:
                q.remove(target_id)
                break
    await _reply_html(update.message, f"{E_WAITING} <b>{display_name}</b> забанен на <b>{days}</b> дн.\nПричина: {reason}")
    try:
        await _send_html(context.bot, target_id,
            f"{E_WAITING} Вы заблокированы на {days} дн.\nПричина: {reason}"
        )
    except Exception:
        pass


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_mod_or_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /warn <имя> [причина]")
        return
    target = find_user_by_name(context.args[0])
    if not target:
        await _reply_html(update.message, f"{E_CROSS} Игрок «{context.args[0]}» не найден.")
        return
    target_id, display_name = target
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "нарушение правил"
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('UPDATE users SET warns=COALESCE(warns,0)+1 WHERE user_id=?', (target_id,))
    conn.commit()
    c.execute('SELECT warns FROM users WHERE user_id=?', (target_id,))
    warns_count = c.fetchone()[0]
    if warns_count >= 3:
        c.execute('UPDATE users SET is_banned=1, ban_reason=? WHERE user_id=?',
                  ("3 варна", target_id))
        conn.commit()
    conn.close()
    msg = f"<tg-emoji emoji-id='5447644880824181073'>⚠️</tg-emoji> <b>{display_name}</b> получил варн ({warns_count}/3). Причина: {reason}"
    if warns_count >= 3:
        msg += f"\n\n{E_BAN} <b>3 варна — игрок автоматически заблокирован!</b>"
    await _reply_html(update.message, msg)
    try:
        warn_text = (f"<tg-emoji emoji-id='5447644880824181073'>⚠️</tg-emoji> Вы получили варн ({warns_count}/3).\nПричина: {reason}"
                     + (f"\n\n{E_BAN} Вы автоматически заблокированы (3 варна)." if warns_count >= 3 else ""))
        await _send_html(context.bot, target_id, warn_text)
    except Exception:
        pass


async def cmd_unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_mod_or_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /unwarn <имя>")
        return
    target = find_user_by_name(context.args[0])
    if not target:
        await _reply_html(update.message, f"{E_CROSS} Игрок «{context.args[0]}» не найден.")
        return
    target_id, display_name = target
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('UPDATE users SET warns=MAX(0, COALESCE(warns,0)-1) WHERE user_id=?', (target_id,))
    conn.commit()
    c.execute('SELECT warns FROM users WHERE user_id=?', (target_id,))
    warns_count = c.fetchone()[0]
    conn.close()
    await _reply_html(update.message, f"{E_CHECK} Варн снят. <b>{display_name}</b>: {warns_count}/3")


async def cmd_setelo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /setelo <имя> <elo>")
        return
    target = find_user_by_name(context.args[0])
    if not target:
        await _reply_html(update.message, f"{E_CROSS} Игрок «{context.args[0]}» не найден.")
        return
    try:
        new_elo = int(context.args[1])
    except ValueError:
        await _reply_html(update.message, f"{E_CROSS} ELO должен быть числом.")
        return
    target_id, display_name = target
    new_level = elo_to_level(new_elo)
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('UPDATE users SET elo=?, level=? WHERE user_id=?', (new_elo, new_level, target_id))
    conn.commit()
    conn.close()
    await _reply_html(update.message, f"{E_CHECK} ELO игрока <b>{display_name}</b> установлен: <code>{new_elo}</code>")


async def cmd_addelo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /addelo <имя> <+/-кол-во>")
        return
    target = find_user_by_name(context.args[0])
    if not target:
        await _reply_html(update.message, f"{E_CROSS} Игрок «{context.args[0]}» не найден.")
        return
    try:
        delta = int(context.args[1])
    except ValueError:
        await _reply_html(update.message, f"{E_CROSS} Укажи число.")
        return
    target_id, display_name = target
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT elo FROM users WHERE user_id=?', (target_id,))
    old = c.fetchone()[0]
    new_elo   = max(0, old + delta)
    new_level = elo_to_level(new_elo)
    c.execute('UPDATE users SET elo=?, level=? WHERE user_id=?', (new_elo, new_level, target_id))
    conn.commit()
    conn.close()
    sign = "+" if delta >= 0 else ""
    await _reply_html(update.message, f"{E_CHECK} <b>{display_name}</b>: ELO {old} → <code>{new_elo}</code> ({sign}{delta})")


async def cmd_addcoins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /addcoins <юзернейм> <количество>")
        return
    target = find_user_by_name(context.args[0])
    if not target:
        await _reply_html(update.message, f"{E_CROSS} Игрок «{context.args[0]}» не найден.")
        return
    try:
        delta = int(context.args[1])
    except ValueError:
        await _reply_html(update.message, f"{E_CROSS} Укажи число.")
        return
    target_id, display_name = target
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT moon_coins FROM users WHERE user_id=?', (target_id,))
    row = c.fetchone()
    old = (row[0] or 0) if row else 0
    new_coins = max(0, old + delta)
    c.execute('UPDATE users SET moon_coins=? WHERE user_id=?', (new_coins, target_id))
    conn.commit()
    conn.close()
    sign = "+" if delta >= 0 else ""
    await _reply_html(update.message, f"{E_PRICE} <b>{display_name}</b>: Moon Coins {old} → <code>{new_coins}</code> ({sign}{delta})")
    try:
        await _send_html(context.bot, target_id, f"{E_PRICE} Вам начислено <b>{sign}{delta}</b> Moon Coins!\n"
                 f"Баланс: <b>{new_coins}</b>")
    except Exception:
        pass


async def cmd_setwins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /setwins <имя> <победы>")
        return
    target = find_user_by_name(context.args[0])
    if not target:
        await _reply_html(update.message, f"{E_CROSS} Игрок «{context.args[0]}» не найден.")
        return
    try:
        wins = int(context.args[1])
    except ValueError:
        await _reply_html(update.message, f"{E_CROSS} Укажи число.")
        return
    target_id, display_name = target
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('UPDATE users SET wins=? WHERE user_id=?', (wins, target_id))
    conn.commit()
    conn.close()
    await _reply_html(update.message, f"{E_CHECK} Победы <b>{display_name}</b>: <b>{wins}</b>")


async def cmd_setmatches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /setmatches <имя> <матчи>")
        return
    target = find_user_by_name(context.args[0])
    if not target:
        await _reply_html(update.message, f"{E_CROSS} Игрок «{context.args[0]}» не найден.")
        return
    try:
        matches = int(context.args[1])
    except ValueError:
        await _reply_html(update.message, f"{E_CROSS} Укажи число.")
        return
    target_id, display_name = target
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('UPDATE users SET matches_played=? WHERE user_id=?', (matches, target_id))
    conn.commit()
    conn.close()
    await _reply_html(update.message, f"{E_CHECK} Матчи <b>{display_name}</b>: <b>{matches}</b>")


async def cmd_setkd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text("Использование: /setkd <имя> <убийства> <смерти>")
        return
    target = find_user_by_name(context.args[0])
    if not target:
        await _reply_html(update.message, f"{E_CROSS} Игрок «{context.args[0]}» не найден.")
        return
    try:
        kills  = int(context.args[1])
        deaths = int(context.args[2])
    except ValueError:
        await _reply_html(update.message, f"{E_CROSS} Укажи числа.")
        return
    target_id, display_name = target
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('UPDATE users SET kills=?, deaths=? WHERE user_id=?', (kills, deaths, target_id))
    conn.commit()
    conn.close()
    await _reply_html(update.message, f"{E_CHECK} К/Д <b>{display_name}</b>: {kills}/{deaths}")


async def cmd_setnick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /setnick <старый_ник> <новый_ник>")
        return
    target = find_user_by_name(context.args[0])
    if not target:
        await _reply_html(update.message, f"{E_CROSS} Игрок «{context.args[0]}» не найден.")
        return
    target_id, _ = target
    new_nick = context.args[1]
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('UPDATE users SET first_name=? WHERE user_id=?', (new_nick, target_id))
    conn.commit()
    conn.close()
    await _reply_html(update.message, f"{E_CHECK} Никнейм изменён на <b>{new_nick}</b>")


async def cmd_setgameid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /setgameid <имя> <game_id>")
        return
    target = find_user_by_name(context.args[0])
    if not target:
        await _reply_html(update.message, f"{E_CROSS} Игрок «{context.args[0]}» не найден.")
        return
    target_id, display_name = target
    game_id = context.args[1]
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('UPDATE users SET game_id=? WHERE user_id=?', (game_id, target_id))
    conn.commit()
    conn.close()
    await _reply_html(update.message, f"{E_CHECK} Game ID <b>{display_name}</b> → <code>{game_id}</code>")


async def cmd_addpromo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создаёт/обновляет промокод. Использование: /addpromo КОД НАГРАДА АКТИВАЦИИ"""
    if not await _require_admin(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text("Использование: /addpromo <код> <награда_moon_coins> <кол-во_активаций>")
        return
    code = context.args[0].strip().upper()
    try:
        reward      = int(context.args[1])
        activations = int(context.args[2])
    except ValueError:
        await update.message.reply_text("Награда и количество активаций должны быть числами.")
        return
    if reward <= 0 or activations <= 0:
        await update.message.reply_text("Награда и количество активаций должны быть положительными числами.")
        return
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute(
        'INSERT INTO promo_codes (code, reward, activations_left) VALUES (?, ?, ?) '
        'ON CONFLICT(code) DO UPDATE SET reward=excluded.reward, activations_left=excluded.activations_left',
        (code, reward, activations)
    )
    conn.commit()
    conn.close()
    await _reply_html(
        update.message,
        f"{E_CHECK} Промокод <b>{_esc(code)}</b>: {E_PRICE} <b>{reward}</b> Moon Coins, активаций — <b>{activations}</b>."
    )


async def cmd_setcreator(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /setcreator <имя>")
        return
    target = find_user_by_name(context.args[0])
    if not target:
        await _reply_html(update.message, f"{E_CROSS} Игрок «{context.args[0]}» не найден.")
        return
    target_id, display_name = target
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT is_creator FROM users WHERE user_id=?', (target_id,))
    row = c.fetchone()
    new_val = 0 if (row and row[0]) else 1
    c.execute('UPDATE users SET is_creator=? WHERE user_id=?', (new_val, target_id))
    conn.commit()
    conn.close()
    status = "выдан" if new_val else "снят"
    await _reply_html(update.message, f"{E_CHECK} Значок создателя {status} — <b>{display_name}</b>")


async def cmd_addoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    if not context.args:
        await _reply_html(update.message, "Использование: /addoff <юзернейм>")
        return
    target = find_user_by_name(context.args[0])
    if not target:
        await _reply_html(update.message, f"{E_CROSS} Игрок «{context.args[0]}» не найден.")
        return
    target_id, display_name = target
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Значок 1", callback_data=f"addoff_1_{target_id}", api_kwargs={'icon_custom_emoji_id': '5314334546269872179'}),
            InlineKeyboardButton("Значок 2", callback_data=f"addoff_2_{target_id}", api_kwargs={'icon_custom_emoji_id': '5314666276658912691'}),
            InlineKeyboardButton("Значок 3", callback_data=f"addoff_3_{target_id}", api_kwargs={'icon_custom_emoji_id': '5314302200871164289'}),
        ],
        [
            InlineKeyboardButton("❌ Убрать значок", callback_data=f"addoff_0_{target_id}"),
        ],
    ])
    await _reply_html(update.message, f"{EP_USER} <b>{display_name}</b> — выберите значок:", reply_markup=keyboard)


async def addoff_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    conn_a = sqlite3.connect('faceit_bot.db')
    c_a = conn_a.cursor()
    c_a.execute('SELECT is_admin FROM users WHERE user_id=?', (uid,))
    row_a = c_a.fetchone()
    conn_a.close()
    if not (row_a and row_a[0]):
        await query.answer("Только для администраторов.", show_alert=True)
        return
    parts = query.data.split('_')
    badge_val  = int(parts[1])
    target_id  = int(parts[2])
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT COALESCE(first_name, username) FROM users WHERE user_id=?', (target_id,))
    row = c.fetchone()
    display_name = row[0] if row else str(target_id)
    c.execute('UPDATE users SET official_badge=? WHERE user_id=?', (badge_val, target_id))
    conn.commit()
    conn.close()
    BADGE_LABELS = {0: "убран", 1: f"выдан {E_OFF1}", 2: f"выдан {E_OFF2}", 3: f"выдан {E_OFF3}"}
    label = BADGE_LABELS.get(badge_val, "изменён")
    await _edit_html(query, f"{EP_USER} <b>{display_name}</b> — значок {label}")
    try:
        if badge_val > 0:
            badge_icon = E_OFF1 if badge_val == 1 else E_OFF2 if badge_val == 2 else E_OFF3
            await _send_html(context.bot, target_id, f"Вам выдан официальный значок {badge_icon}")
        else:
            await _send_html(context.bot, target_id, f"Ваш официальный значок был убран {E_CROSS}")
    except Exception:
        pass


async def cmd_resetelo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    await _reply_html(update.message, "<tg-emoji emoji-id='5447644880824181073'>⚠️</tg-emoji> <b>Сбросить ELO всем игрокам?</b>\n\nЭто действие необратимо!", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, сбросить", callback_data="resetelo_confirm"),
            InlineKeyboardButton("❌ Отмена",        callback_data="resetelo_cancel"),
        ]]))


async def cmd_newseason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT MAX(season_num) FROM season_archive')
    row = c.fetchone()
    conn.close()
    current_season = (row[0] or 0) + 1
    new_season     = current_season + 1
    await _reply_html(update.message, f"<tg-emoji emoji-id='5411520005386806155'>🏁</tg-emoji> <b>Начать новый сезон {new_season}?</b>\n\n"
        f"Текущий (Сезон {current_season}) будет сохранён в архив, ELO сброшен.", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Начать сезон {new_season}", callback_data=f"newseason_confirm_{new_season}"),
            InlineKeyboardButton("❌ Отмена", callback_data="newseason_cancel"),
        ]]))


async def cmd_resetseason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT MAX(season_num) FROM season_archive')
    row = c.fetchone()
    conn.close()
    season_num = (row[0] or 0) + 1

    old_date = get_setting('season_end_date', '01.09.2026')
    if context.args:
        new_date_raw = context.args[0]
        try:
            datetime.datetime.strptime(new_date_raw, '%d.%m.%Y')
            new_date = new_date_raw
        except ValueError:
            await _reply_html(
                update.message,
                f"{E_CROSS} Неверный формат даты. Используйте: <code>/resetseason 31.12.2026</code>"
            )
            return
    else:
        # Первое продление — 4 месяца (до конца года), далее шаг всегда 3 месяца
        months_step = 4 if get_setting('season_extended_once') != '1' else 3
        try:
            new_date = _add_months(old_date, months_step)
        except ValueError:
            new_date = _add_months(datetime.date.today().strftime('%d.%m.%Y'), months_step)

    await _reply_html(
        update.message,
        f"{E_TROPHY} <b>Сбросить сезон?</b>\n\n"
        f"{E_CROSS} Будет сброшена статистика ВСЕХ игроков (ELO, матчи, победы, K/D по всем режимам) "
        f"и таблицы лидеров.\n"
        f"Текущий сезон сохранится в архив как Сезон {season_num}.\n\n"
        f"{E_CAL} Дата окончания сезона: <b>{old_date}</b> → <b>{new_date}</b>\n\n"
        f"Это действие необратимо!",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да, сбросить сезон", callback_data=f"resetseason_confirm_{season_num}_{new_date}"),
            InlineKeyboardButton("❌ Отмена", callback_data="resetseason_cancel"),
        ]])
    )


async def cmd_addbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    args = context.args
    mode     = args[0] if args and args[0] in MODES else '5v5'
    lobby_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
    if lobby_id not in range(1, 6):
        lobby_id = 1

    global _bot_counter
    _bot_counter -= 1
    bot_uid = _bot_counter

    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users (user_id, username, elo, level) VALUES (?,?,800,1)',
              (bot_uid, f"bot_{abs(bot_uid)}"))
    c.execute('UPDATE users SET first_name=? WHERE user_id=?',
              (f"🤖 Бот {abs(bot_uid)}", bot_uid))
    conn.commit()
    conn.close()

    lobby      = lobby_queues[mode][lobby_id]
    match_size = MODES[mode]['match_size']
    lobby.append(bot_uid)
    _queue_join_time[bot_uid] = datetime.datetime.utcnow()

    await _reply_html(
        update.message,
        f"<tg-emoji emoji-id='5372981976804366741'>🤖</tg-emoji> Бот добавлен в лобби {lobby_id} [{mode}]  ({len(lobby)}/{match_size})"
    )
    if len(lobby) >= match_size:
        players_ids = list(lobby[:match_size])
        lobby_queues[mode][lobby_id].clear()
        for u in players_ids:
            _cleanup_uid(u)
        await start_confirmation(context, players_ids, mode)


async def cmd_addbot9(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /addbot9 [mode] [lobby_id]
    Добавляет 9 ботов в одно лобби одним махом (для быстрого тестирования драфта).
    Если лобби заполняется раньше — прекращает добавление и запускает подтверждение матча.
    """
    if not await _require_admin(update):
        return
    args = context.args
    mode     = args[0] if args and args[0] in MODES else '5v5'
    lobby_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
    if lobby_id not in range(1, 6):
        lobby_id = 1

    global _bot_counter
    match_size = MODES[mode]['match_size']
    lobby      = lobby_queues[mode][lobby_id]

    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()

    added = 0
    filled = False
    for _ in range(9):
        if len(lobby) >= match_size:
            filled = True
            break
        _bot_counter -= 1
        bot_uid = _bot_counter
        c.execute('INSERT OR IGNORE INTO users (user_id, username, elo, level) VALUES (?,?,800,1)',
                  (bot_uid, f"bot_{abs(bot_uid)}"))
        c.execute('UPDATE users SET first_name=? WHERE user_id=?',
                  (f"🤖 Бот {abs(bot_uid)}", bot_uid))
        lobby.append(bot_uid)
        _queue_join_time[bot_uid] = datetime.datetime.utcnow()
        added += 1
        if len(lobby) >= match_size:
            filled = True
            break

    conn.commit()
    conn.close()

    await _reply_html(
        update.message,
        f"<tg-emoji emoji-id='5372981976804366741'>🤖</tg-emoji> Добавлено ботов: {added} в лобби {lobby_id} [{mode}]  ({len(lobby)}/{match_size})"
        + ("\n<tg-emoji emoji-id='5447644880824181073'>⚠️</tg-emoji> Лобби заполнено — оставшиеся боты не добавлены." if filled and added < 9 else "")
    )

    if len(lobby) >= match_size:
        players_ids = list(lobby[:match_size])
        lobby_queues[mode][lobby_id].clear()
        for u in players_ids:
            _cleanup_uid(u)
        await start_confirmation(context, players_ids, mode)


async def cmd_deletebot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    removed = 0
    for mode in MODES:
        for lid in range(1, 6):
            bots = [uid for uid in lobby_queues[mode][lid] if is_bot(uid)]
            for b in bots:
                lobby_queues[mode][lid].remove(b)
                _cleanup_uid(b)
                removed += 1
    await _reply_html(update.message, f"<tg-emoji emoji-id='5445267414562389170'>🗑</tg-emoji> Удалено ботов: {removed}")


async def cmd_resetbotreg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сбрасывает регистрацию всех ботов: удаляет их из БД, лобби, очередей
    и отменяет все активные сессии (подтверждение/драфт/голосование), в которых
    есть боты."""
    if not await _require_admin(update):
        return

    global _bot_counter

    # 1. Убираем ботов из всех лобби и очередей
    removed_queue = 0
    for mode in MODES:
        for lid in range(1, 6):
            bots = [uid for uid in lobby_queues[mode][lid] if is_bot(uid)]
            for b in bots:
                lobby_queues[mode][lid].remove(b)
                _cleanup_uid(b)
                removed_queue += 1

    # 2. Отменяем сессии подтверждения, в которых участвуют боты
    cancelled_confirm = 0
    for sid in list(confirm_state.keys()):
        state = confirm_state[sid]
        if any(is_bot(uid) for uid in state.get('players', [])):
            # Уведомляем реальных игроков об отмене
            for uid in state.get('real_players', []):
                try:
                    await _send_html(context.bot, uid,
                        f"{E_CROSS} <b>Матч отменён</b>\n\nАдминистратор сбросил регистрацию ботов.")
                except Exception:
                    pass
            del confirm_state[sid]
            _confirm_msg_info.pop(sid, None)
            cancelled_confirm += 1

    # 3. Отменяем активные драфты и голосования за карту, где есть боты
    cancelled_draft = 0
    for mid in list(draft_state.keys()):
        draft = draft_state[mid]
        if any(is_bot(uid) for uid in draft.get('all_players', [])):
            del draft_state[mid]
            cancelled_draft += 1
    for mid in list(map_vote_state.keys()):
        mvs = map_vote_state[mid]
        if any(is_bot(uid) for uid in mvs.get('players', [])):
            del map_vote_state[mid]

    # 4. Удаляем все записи ботов из БД (user_id < 0)
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM users WHERE user_id < 0')
    db_count = c.fetchone()[0] or 0
    c.execute('DELETE FROM users WHERE user_id < 0')
    conn.commit()
    conn.close()

    # 5. Сбрасываем счётчик ботов
    _bot_counter = 0

    parts = [f"<tg-emoji emoji-id='5445267414562389170'>🗑</tg-emoji> Удалено из лобби: <b>{removed_queue}</b>",
             f"<tg-emoji emoji-id='5445267414562389170'>🗑</tg-emoji> Удалено из БД: <b>{db_count}</b>",
             f"<tg-emoji emoji-id='5346321684574003384'>🔄</tg-emoji> Счётчик ботов сброшен до 0"]
    if cancelled_confirm:
        parts.append(f"<tg-emoji emoji-id='5447644880824181073'>⚠️</tg-emoji> Отменено подтверждений: <b>{cancelled_confirm}</b>")
    if cancelled_draft:
        parts.append(f"<tg-emoji emoji-id='5447644880824181073'>⚠️</tg-emoji> Отменено драфтов: <b>{cancelled_draft}</b>")

    await _reply_html(
        update.message,
        f"{E_CROSS} <b>Регистрация ботов сброшена</b>\n\n" + "\n".join(parts)
    )


async def cmd_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('''SELECT user_id, COALESCE(first_name, username), elo, matches_played, is_banned
                 FROM users ORDER BY elo DESC LIMIT 30''')
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Игроков нет.")
        return
    msg = f"{E_PEOPLE} <b>Игроки (топ-30 по ELO):</b>\n\n"
    for uid, name, elo, mp, banned in rows:
        ban_tag = f" {E_BAN}" if banned else ""
        msg += f"  <code>{uid}</code> — <b>{name}</b>{ban_tag}  ELO:{elo}  М:{mp}\n"
    await _reply_html(update.message, msg)


async def cmd_matchinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /matchinfo <match_id>")
        return
    try:
        match_id = int(context.args[0])
    except ValueError:
        await _reply_html(update.message, f"{E_CROSS} Укажи числовой ID матча.")
        return
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT match_id, status, winner, timestamp, mode FROM team_matches WHERE match_id=?', (match_id,))
    m = c.fetchone()
    if not m:
        await update.message.reply_text(f"Матч #{match_id} не найден.")
        conn.close()
        return
    c.execute('''SELECT mp.user_id, u.display_name, mp.team, mp.elo_before, mp.elo_change
                 FROM match_players mp
                 JOIN (SELECT user_id, COALESCE(first_name, username) AS display_name FROM users) u
                   ON mp.user_id = u.user_id
                 WHERE mp.match_id=?''', (match_id,))
    players = c.fetchall()
    conn.close()
    status  = m[1]
    winner  = m[2]
    ts      = m[3]
    mode    = m[4] or "5v5"
    msg = (
        f"<tg-emoji emoji-id='5231012545799666522'>🔎</tg-emoji> <b>Матч #{match_id} [{mode}]</b>\n"
        f"Статус: {status}  |  Победитель: {f'Команда {winner}' if winner else '—'}\n"
        f"Дата: {ts}\n\n"
    )
    for uid, name, team, elo_b, elo_c in players:
        elo_b = elo_b or 0
        elo_c = elo_c or 0
        sign = "+" if elo_c >= 0 else ""
        msg += f"  T{team} — <b>{name}</b>: ELO {elo_b} ({sign}{elo_c})\n"
    await _reply_html(update.message, msg)


async def cmd_cancelmatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /cancelmatch <match_id>
    Отменяет матч (драфт или голосование за карту) и возвращает игроков в лобби.
    """
    if not await _require_admin(update):
        return
    if not context.args or not context.args[0].isdigit():
        await _reply_html(update.message,
            f"{E_CROSS} Использование: /cancelmatch &lt;match_id&gt;")
        return

    match_id = int(context.args[0])

    # ── 1. Определяем статус и режим матча ──────────────────────────────────
    conn = sqlite3.connect('faceit_bot.db')
    c    = conn.cursor()
    c.execute('SELECT status, mode FROM team_matches WHERE match_id=?', (match_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        await _reply_html(update.message, f"{E_CROSS} Матч #{match_id} не найден.")
        return

    status, mode = row[0], (row[1] or '5v5')

    if status == 'finished':
        await _reply_html(update.message,
            f"{E_CROSS} Матч #{match_id} уже завершён — отменить нельзя.")
        return

    if status == 'cancelled':
        await _reply_html(update.message,
            f"{E_CROSS} Матч #{match_id} уже отменён.")
        return

    # ── 2. Собираем игроков ────────────────────────────────────────────────
    players: list[int] = []

    # Из драфта (статус 'draft')
    draft = draft_state.get(match_id)
    if draft:
        players = [uid for uid in draft.get('all_players', []) if not is_bot(uid)]

    # Из голосования за карту (статус 'active' на этапе карты)
    mvs = map_vote_state.get(match_id)
    if mvs and not players:
        players = [uid for uid in mvs.get('players', []) if not is_bot(uid)]

    # Если ни в одном из словарей — берём из match_players в БД
    if not players:
        conn2 = sqlite3.connect('faceit_bot.db')
        c2    = conn2.cursor()
        c2.execute('SELECT user_id FROM match_players WHERE match_id=?', (match_id,))
        players = [r[0] for r in c2.fetchall() if not is_bot(r[0])]
        conn2.close()

    # ── 3. Чистим состояние памяти ────────────────────────────────────────
    if match_id in draft_state:
        del draft_state[match_id]
    if match_id in map_vote_state:
        del map_vote_state[match_id]

    # ── 4. Помечаем матч как отменённый в БД ─────────────────────────────
    conn3 = sqlite3.connect('faceit_bot.db')
    c3    = conn3.cursor()
    c3.execute("UPDATE team_matches SET status='cancelled' WHERE match_id=?", (match_id,))
    conn3.commit()
    conn3.close()

    # ── 5. Возвращаем игроков в лобби ─────────────────────────────────────
    returned: list[str] = []
    for uid in players:
        # Находим наименее заполненное лобби для данного режима
        target_lobby = min(range(1, 6), key=lambda i: len(lobby_queues[mode][i]))
        lobby_queues[mode][target_lobby].append(uid)
        _queue_join_time[uid] = datetime.datetime.utcnow()

        # Уведомляем игрока
        try:
            await _send_html(
                context.bot, uid,
                f"{E_RELOAD} <b>Матч #{match_id} [{mode}] отменён администратором.</b>\n\n"
                f"Вы автоматически возвращены в лобби {target_lobby} [{mode}]. "
                f"Ожидайте нового матча!"
            )
        except Exception:
            pass

        # Собираем ники для отчёта
        conn4 = sqlite3.connect('faceit_bot.db')
        c4    = conn4.cursor()
        c4.execute('SELECT COALESCE(first_name, username) FROM users WHERE user_id=?', (uid,))
        nr = c4.fetchone()
        conn4.close()
        name = nr[0] if nr and nr[0] else str(uid)
        returned.append(f"  • {name} → лобби {target_lobby}")

    # ── 6. Обновляем сообщения лобби у всех в них ─────────────────────────
    seen_lobbies: set[tuple[str, int]] = set()
    for uid in players:
        for lid in range(1, 6):
            if uid in lobby_queues[mode].get(lid, []):
                seen_lobbies.add((mode, lid))
    for (m, lid) in seen_lobbies:
        try:
            await _refresh_lobby_messages(context, lid, m)
        except Exception:
            pass

    # ── 7. Ответ администратору ────────────────────────────────────────────
    players_block = "\n".join(returned) if returned else "  (нет игроков)"
    await _reply_html(
        update.message,
        f"{E_CHECK} <b>Матч #{match_id} [{mode}] отменён.</b>\n\n"
        f"{E_RELOAD} Игроки возвращены в очередь:\n{players_block}"
    )


async def cmd_cancelmatched(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /cancelmatched [match_id]
    Отменяет матч, находящийся в стадии драфта.
    Без аргументов — ищет первый активный драфт.
    """
    if not await _require_admin(update):
        return

    # ── 1. Определяем match_id (первичный выбор, без гарантий на гонку) ────
    if context.args and context.args[0].isdigit():
        match_id = int(context.args[0])
        if match_id not in draft_state:
            await _reply_html(update.message,
                f"{E_CROSS} Матч #{match_id} не находится в стадии драфта.")
            return
    else:
        if not draft_state:
            await _reply_html(update.message,
                f"{E_CROSS} Нет активных драфтов.")
            return
        match_id = next(iter(draft_state))

    # ── 2. Лок первым делом: перечитываем состояние ВНУТРИ лока, чтобы не
    #      наступить на гонку с одновременным пиком/таймаутом/финализацией.
    #      Никаких await внутри `async with`, пока лок не отпущен штатно —
    #      иначе конкурентный хендлер может создать новый лок и проскочить. ──
    already_gone = False
    async with _get_draft_lock(match_id):
        draft = draft_state.get(match_id)
        if not draft or draft.get('finalized'):
            already_gone = True
        else:
            mode = draft.get('mode', '5v5')
            all_players = [uid for uid in draft.get('all_players', []) if not is_bot(uid)]
            draft['finalized'] = True
            draft_state.pop(match_id, None)
            _cancel_pick_timer(context, match_id)
    _release_draft_lock(match_id)

    if already_gone:
        await _reply_html(update.message,
            f"{E_CROSS} Матч #{match_id} уже не в стадии драфта (завершён/отменён/финализирован).")
        return

    # ── 3. Обновляем статус в БД ───────────────────────────────────────────
    conn = sqlite3.connect('faceit_bot.db')
    c    = conn.cursor()
    c.execute("UPDATE team_matches SET status='cancelled' WHERE match_id=?", (match_id,))
    conn.commit()
    conn.close()

    # ── 4. Уведомляем игроков и возвращаем в лобби ─────────────────────────
    returned: list[str] = []
    for uid in all_players:
        target_lobby = min(range(1, 6), key=lambda i: len(lobby_queues[mode][i]))
        lobby_queues[mode][target_lobby].append(uid)
        _queue_join_time[uid] = datetime.datetime.utcnow()
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    f"{E_RELOAD} <b>Матч #{match_id} [{mode}] — Драфт отменён администратором.</b>\n\n"
                    f"Вы возвращены в лобби {target_lobby} [{mode}]. Ожидайте нового матча!"
                ),
                parse_mode='HTML',
            )
        except Exception:
            pass
        conn2 = sqlite3.connect('faceit_bot.db')
        c2 = conn2.cursor()
        c2.execute('SELECT COALESCE(first_name, username) FROM users WHERE user_id=?', (uid,))
        nr = c2.fetchone()
        conn2.close()
        name = nr[0] if nr and nr[0] else str(uid)
        returned.append(f"  • {_esc(name)} → лобби {target_lobby}")

    # ── 5. Обновляем сообщения лобби ──────────────────────────────────────
    seen: set[tuple[str, int]] = set()
    for uid in all_players:
        for lid in range(1, 6):
            if uid in lobby_queues[mode].get(lid, []):
                seen.add((mode, lid))
    for (m, lid) in seen:
        try:
            await _refresh_lobby_messages(context, lid, m)
        except Exception:
            pass

    # ── 6. Ответ администратору ────────────────────────────────────────────
    block = "\n".join(returned) if returned else "  (нет игроков)"
    await _reply_html(
        update.message,
        f"{E_CHECK} <b>Драфт матча #{match_id} [{mode}] отменён.</b>\n\n"
        f"{E_RELOAD} Игроки возвращены в очередь:\n{block}",
    )


async def cmd_forcefinish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /forcefinish <match_id> <1|2>")
        return
    try:
        match_id     = int(context.args[0])
        winning_team = int(context.args[1])
    except ValueError:
        await _reply_html(update.message, f"{E_CROSS} Укажи числа.")
        return
    if winning_team not in (1, 2):
        await _reply_html(update.message, f"{E_CROSS} Команда должна быть 1 или 2.")
        return
    conn = sqlite3.connect('faceit_bot.db')
    c = conn.cursor()
    c.execute('SELECT status FROM team_matches WHERE match_id=?', (match_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        await update.message.reply_text(f"Матч #{match_id} не найден.")
        return
    if row[0] == 'finished':
        await update.message.reply_text("Матч уже завершён.")
        return
    conn2 = sqlite3.connect('faceit_bot.db')
    c2 = conn2.cursor()
    c2.execute('UPDATE team_matches SET screenshot_submitted=1 WHERE match_id=?', (match_id,))
    conn2.commit()
    conn2.close()
    conn_m = sqlite3.connect('faceit_bot.db')
    c_m    = conn_m.cursor()
    c_m.execute('SELECT mode FROM team_matches WHERE match_id=?', (match_id,))
    mode_row_f = c_m.fetchone()
    conn_m.close()
    mode_f      = (mode_row_f[0] or '5v5') if mode_row_f else '5v5'
    calib_games = MODES.get(mode_f, MODES['5v5'])['calib_games']
    gain, winners, losers, per_uid = finalize_match(match_id, winning_team, mode_f)
    result_msg = (
        f"{E_ZAP} Матч #{match_id} принудительно завершён!\n"
        f"Победила Команда {winning_team} {E_TROPHY}\n\n"
        f"ELO начислен автоматически (база: ±{gain} ELO)"
    )
    await _reply_html(update.message, result_msg)
    all_uids = [uid for uid, _ in winners + losers]
    for uid in all_uids:
        await _send_match_notification(context.bot, uid, match_id, per_uid.get(uid, {}), calib_games)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await action_history(update, context)


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await action_rules(update, context)


async def cmd_donate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await action_donate(update, context)


# ══════════════════════════════════════════════════════════════════════════════
#  АНТИ-АФК + НАПОМИНАНИЯ
# ══════════════════════════════════════════════════════════════════════════════

async def _job_anti_afk(context: ContextTypes.DEFAULT_TYPE):
    now     = datetime.datetime.utcnow()
    timeout = datetime.timedelta(minutes=AFK_TIMEOUT_MIN)
    kicked  = []
    for mode in MODES:
        for lid in range(1, 6):
            queue = lobby_queues[mode][lid]
            for uid in list(queue):
                joined = _queue_join_time.get(uid)
                if joined and (now - joined) > timeout:
                    queue.remove(uid)
                    kicked.append((uid, lid, mode))
                    _cleanup_uid(uid)
    for uid, lid, mode in kicked:
        try:
            await _send_html(context.bot, uid,
                f"<tg-emoji emoji-id='5413704112220949842'>⏰</tg-emoji> Вы были удалены из лобби {lid} [{mode}] за неактивность ({AFK_TIMEOUT_MIN} мин)."
            )
        except Exception:
            pass
    for uid, lid, mode in kicked:
        await _refresh_lobby_messages(context, lid, mode)


async def _job_inactivity_reminder(context: ContextTypes.DEFAULT_TYPE):
    now        = datetime.datetime.utcnow()
    warn_delta = datetime.timedelta(minutes=AFK_TIMEOUT_MIN - 10)
    for mode in MODES:
        for lid in range(1, 6):
            for uid in list(lobby_queues[mode][lid]):
                joined = _queue_join_time.get(uid)
                if joined and (now - joined) >= warn_delta:
                    try:
                        await _send_html(
                            context.bot, uid,
                            f"<tg-emoji emoji-id='5447644880824181073'>⚠️</tg-emoji> Вы в очереди [{mode}] уже 20 мин. "
                            f"Ещё 10 мин — автовыход."
                        )
                    except Exception:
                        pass


async def cmd_online(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает количество зарегистрированных игроков и количество играющих."""
    if not await _require_admin(update):
        return
    conn = sqlite3.connect('faceit_bot.db')
    c    = conn.cursor()
    # Всего зарегистрированных (game_id — признак полностью завершённой
    # регистрации; first_name теперь сохраняется раньше game_id, поэтому
    # на него как на признак завершения полагаться нельзя)
    c.execute('SELECT COUNT(*) FROM users WHERE game_id IS NOT NULL')
    total_reg = c.fetchone()[0] or 0
    # Игроков в активных матчах (статус active или draft)
    c.execute('''SELECT COUNT(DISTINCT mp.user_id) FROM match_players mp
                 JOIN team_matches tm ON mp.match_id = tm.match_id
                 WHERE tm.status IN ('active', 'draft')''')
    in_match = c.fetchone()[0] or 0
    # В очереди прямо сейчас (in-memory)
    in_queue_real = sum(
        sum(1 for uid in q if not is_bot(uid))
        for m in MODES for q in lobby_queues[m].values()
    )
    conn.close()
    await _reply_html(update.message,
        f"{E_PEOPLE} <b>Онлайн Moon Faceit</b>\n\n"
        f"{EP_USER}  Зарегистрировано: <b>{total_reg}</b>\n"
        f"{E_AIM}  В очереди сейчас: <b>{in_queue_real}</b>\n"
        f"{EP_GAME}  В активных матчах: <b>{in_match}</b>\n"
        f"{E_SWORD}  Всего активных: <b>{in_queue_real + in_match}</b>"
    )


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    if not context.args:
        await _reply_html(update.message, "Использование: /news <сообщение>")
        return
    text = '<tg-emoji emoji-id="5399967660052081305">📢</tg-emoji> <b>Новость от администрации:</b>\n\n' + " ".join(context.args)
    conn = sqlite3.connect('faceit_bot.db')
    c    = conn.cursor()
    c.execute('SELECT user_id FROM users')
    all_uids = [r[0] for r in c.fetchall()]
    conn.close()
    sent = 0
    failed = 0
    for uid in all_uids:
        try:
            await _send_html(context.bot, uid, text)
            sent += 1
        except Exception:
            failed += 1
    await _reply_html(update.message, f"{E_CHECK} Новость отправлена: <b>{sent}</b> игрок(ов).\n"
        f"Не доставлено: <b>{failed}</b> (заблокировали бота).")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик ошибок — выводит в консоль для отладки."""
    import traceback
    err = context.error
    tb = ''.join(traceback.format_exception(type(err), err, err.__traceback__))
    print(f"[ERROR] Update={update}\n{tb}")
    # Пытаемся уведомить пользователя
    if update and hasattr(update, 'effective_message') and update.effective_message:
        try:
            await _reply_html(
                update.effective_message,
                "<tg-emoji emoji-id='5447644880824181073'>⚠️</tg-emoji> Произошла внутренняя ошибка. Администратор уведомлён."
            )
        except Exception:
            pass


def main():
    init_db()
    # ВАЖНО: токен читается ТОЛЬКО из переменных окружения. Раньше здесь был
    # захардкожен реальный токен как fallback по умолчанию — это утечка секрета
    # (токен получает доступ к боту, читая просто исходный код). Он удалён;
    # если ты им пользовался — обязательно отзови его через @BotFather (/revoke)
    # и выпусти новый, затем задай BOT_TOKEN в окружении.
    TOKEN = os.environ.get('BOT_TOKEN') or os.environ.get('TOKEN')
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в переменных окружения!")

    # pella.app: задай переменную окружения WEBHOOK_HOST=myapp.pella.app
    WEBHOOK_HOST = os.environ.get('WEBHOOK_HOST', '')
    PORT = int(os.environ.get('PORT', 8080))

    app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    # Команды
    app.add_handler(CommandHandler("start",        start))
    app.add_handler(CommandHandler("profile",      action_profile))
    app.add_handler(CommandHandler("find",         action_find_match))
    app.add_handler(CommandHandler("queue",        action_queue_status))
    app.add_handler(CommandHandler("leave",        action_leave_queue))
    app.add_handler(CommandHandler("history",      cmd_history))
    app.add_handler(CommandHandler("rules",        cmd_rules))
    app.add_handler(CommandHandler("donate",       cmd_donate))
    app.add_handler(CommandHandler("mystats",      cmd_mystats))
    app.add_handler(CommandHandler("renderstatus", cmd_renderstatus))
    app.add_handler(CommandHandler("admin",        cmd_admin))
    app.add_handler(CommandHandler("setadmin",     cmd_setadmin))
    app.add_handler(CommandHandler("moder",        cmd_moder))
    app.add_handler(CommandHandler("kick",         cmd_kick))
    app.add_handler(CommandHandler("ban",          cmd_ban))
    app.add_handler(CommandHandler("unban",        cmd_unban))
    app.add_handler(CommandHandler("tempban",      cmd_tempban))
    app.add_handler(CommandHandler("warn",         cmd_warn))
    app.add_handler(CommandHandler("unwarn",       cmd_unwarn))
    app.add_handler(CommandHandler("setelo",       cmd_setelo))
    app.add_handler(CommandHandler("addelo",       cmd_addelo))
    app.add_handler(CommandHandler("addcoins",     cmd_addcoins))
    app.add_handler(CommandHandler("setwins",      cmd_setwins))
    app.add_handler(CommandHandler("setmatches",   cmd_setmatches))
    app.add_handler(CommandHandler("setkd",        cmd_setkd))
    app.add_handler(CommandHandler("setnick",      cmd_setnick))
    app.add_handler(CommandHandler("setgameid",    cmd_setgameid))
    app.add_handler(CommandHandler("setcreator",   cmd_setcreator))
    app.add_handler(CommandHandler("addpromo",     cmd_addpromo))
    app.add_handler(CommandHandler("addoff",       cmd_addoff))
    app.add_handler(CommandHandler("resetelo",     cmd_resetelo))
    app.add_handler(CommandHandler("newseason",    cmd_newseason))
    app.add_handler(CommandHandler("resetseason",  cmd_resetseason))
    app.add_handler(CommandHandler("addbot",       cmd_addbot))
    app.add_handler(CommandHandler("addbot9",      cmd_addbot9))
    app.add_handler(CommandHandler("deletebot",    cmd_deletebot))
    app.add_handler(CommandHandler("resetbotreg",  cmd_resetbotreg))
    app.add_handler(CommandHandler("players",      cmd_players))
    app.add_handler(CommandHandler("matchinfo",    cmd_matchinfo))
    app.add_handler(CommandHandler("cancelmatch",   cmd_cancelmatch))
    app.add_handler(CommandHandler("cancelmatched", cmd_cancelmatched))
    app.add_handler(CommandHandler("forcefinish",  cmd_forcefinish))
    app.add_handler(CommandHandler("online",       cmd_online))
    app.add_handler(CommandHandler("news",         cmd_news))
    app.add_handler(CommandHandler("search",       action_search_player))
    app.add_handler(CommandHandler("tasks",        action_tasks))

    # Callback-кнопки
    app.add_handler(CallbackQueryHandler(tasks_callback,            pattern=r"^(task_claim_|tasks_refresh)"))
    app.add_handler(CallbackQueryHandler(search_result_callback,    pattern=r"^search_"))
    app.add_handler(CallbackQueryHandler(menu_callback,             pattern=r"^menu_"))
    app.add_handler(CallbackQueryHandler(mode_select_callback,      pattern=r"^mode_"))
    app.add_handler(CallbackQueryHandler(lobby_join_callback,       pattern=r"^lobby_join_"))
    app.add_handler(CallbackQueryHandler(lobby_leave_inline_callback, pattern=r"^lobby_leave$"))
    app.add_handler(CallbackQueryHandler(confirm_callback,          pattern=r"^confirm_"))
    app.add_handler(CallbackQueryHandler(pick_callback,             pattern=r"^pick_"))
    app.add_handler(CallbackQueryHandler(map_ban_callback,          pattern=r"^mb_"))
    app.add_handler(CallbackQueryHandler(leaderboard_callback,      pattern=r"^lb_"))
    app.add_handler(CallbackQueryHandler(admin_screenshot_callback, pattern=r"^ss_"))
    app.add_handler(CallbackQueryHandler(addoff_callback,           pattern=r"^addoff_"))
    app.add_handler(CallbackQueryHandler(admin_self_badge_callback, pattern=r"^admin_self_badge_"))
    app.add_handler(CallbackQueryHandler(setadmin_badge_callback,   pattern=r"^setadmin_badge_"))
    app.add_handler(CallbackQueryHandler(button_handler,            pattern=r"^(resetelo_|newseason_|resetseason_)"))
    app.add_handler(CallbackQueryHandler(profile_action_callback,   pattern=r"^profile_"))
    app.add_handler(CallbackQueryHandler(shop_category_callback,    pattern=r"^shop_"))
    app.add_handler(CallbackQueryHandler(party_callback,            pattern=r"^party_"))
    app.add_handler(CallbackQueryHandler(check_sub_callback,        pattern=r"^check_sub$"))

    # Регистрация и кнопки меню
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, registration_step_handler
    ), group=0)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, keyboard_router
    ), group=1)

    # Скриншоты
    app.add_handler(MessageHandler(filters.PHOTO, screenshot_handler))
    app.add_error_handler(_error_handler)

    # Jobs
    jq = app.job_queue
    if jq:
        jq.run_repeating(_job_anti_afk,           interval=60, first=60,  name="anti_afk")
        jq.run_repeating(_job_inactivity_reminder, interval=60, first=120, name="inactivity_reminder")

    print("🚀 Moon Faceit Bot запущен!")

    if WEBHOOK_HOST:
        webhook_url = f'https://{WEBHOOK_HOST}/{TOKEN}'
        print(f"🌐 Webhook режим: {webhook_url}")
        app.run_webhook(
            listen='0.0.0.0',
            port=PORT,
            url_path=f'/{TOKEN}',
            webhook_url=webhook_url,
            drop_pending_updates=True,
        )
    else:
        print("📡 Polling режим (для локального запуска)")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
