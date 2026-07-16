# Vision Faceit Bot — Инструкция по установке

## Стек
| Компонент | Было | Стало |
|---|---|---|
| Telegram-библиотека | pyTelegramBotAPI (telebot) | **aiogram 3.x** (async) |
| Профиль-карточка | Pillow (растровый рендер) | **cairosvg** (SVG → PNG, выше качество) |
| HTTP health-check | Flask | **aiohttp** (встроен в aiogram-стек) |

---

## 1. Файлы

```
bot_aiogram.py          ← основной бот (aiogram 3)
card_generator_svg.py   ← новый рендерер карточек (cairosvg)
card_generator.py       ← оригинал Pillow (leaderboard / match / shop)
requirements_aiogram.txt
```

---

## 2. Установка зависимостей

```bash
pip install -r requirements_aiogram.txt
```

**requirements_aiogram.txt:**
```
aiogram==3.10.0
psycopg2-binary==2.9.10
aiohttp==3.9.5
Pillow==10.4.0
cairosvg==2.7.1
requests==2.32.3
```

### cairosvg на Render (free tier)

cairosvg требует системную библиотеку **Cairo**. На Render добавь в `render.yaml`:

```yaml
services:
  - type: web
    name: vision-faceit-bot
    env: python
    buildCommand: |
      apt-get install -y libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 libffi-dev shared-mime-info && pip install -r requirements_aiogram.txt
    startCommand: python bot_aiogram.py
```

Либо в **Dockerfile** (если используешь):
```dockerfile
RUN apt-get update && apt-get install -y \
    libcairo2 libpango-1.0-0 libpangocairo-1.0-0 \
    libgdk-pixbuf2.0-0 libffi-dev shared-mime-info \
    && rm -rf /var/lib/apt/lists/*
RUN pip install -r requirements_aiogram.txt
```

---

## 3. Переменные окружения

Те же что были — ничего не меняется:

```env
BOT_TOKEN=your_token_here
DATABASE_URL=postgresql://...
ADMIN_IDS=123456789,987654321
ADMIN_CHAT_ID=-100...
LOG_CHAT_ID=-100...
CRYPTO_PAY_API_TOKEN=   # опционально
PAYMENT_PROVIDER_TOKEN= # опционально
PORT=5000               # для health-check (aiohttp)
```

---

## 4. Запуск

```bash
python bot_aiogram.py
```

Бот теперь полностью асинхронный. Запускается через `asyncio.run(main())`.

---

## 5. Как работает новый рендер карточки

`card_generator_svg.py` — генерирует SVG строкой, затем конвертирует через cairosvg:

```python
from card_generator_svg import generate_profile_card

buf = generate_profile_card(
    username="Vision",
    game_id="12345678",
    user_id=9876543,
    elo=1250,
    wins=85, losses=40,
    kills=980, deaths=680, assists=320,
    is_premium=True, is_admin=False,
    global_rank=12,
    league="default",
    map_stats=[...],
    recent=[True, False, True, ...],  # список bool (W/L)
    leaderboard=[...],
    mvp_count=5,
)
# buf — io.BytesIO с PNG-данными
```

Возвращает `io.BytesIO` — как и оригинальный `card_generator.py`.

В aiogram отправка:
```python
await bot.send_photo(
    chat_id,
    BufferedInputFile(buf.read(), "profile.png"),
    caption=caption,
    parse_mode="HTML",
)
```

---

## 6. Отличия aiogram 3 от telebot (шпаргалка)

| telebot | aiogram 3 |
|---|---|
| `bot.send_message(...)` | `await bot.send_message(...)` |
| `bot.answer_callback_query(c.id)` | `await callback.answer()` |
| `types.InlineKeyboardMarkup(row_width=2)` | `InlineKeyboardBuilder(); kb.adjust(2)` |
| `kb.add(InlineKeyboardButton(...))` | `kb.button(text=..., callback_data=...)` |
| `reply_markup=kb` | `reply_markup=kb.as_markup()` |
| `@bot.message_handler(commands=["x"])` | `@router.message(Command("x"))` |
| `@bot.callback_query_handler(func=lambda c: c.data=="x")` | `@router.callback_query(F.data == "x")` |
| `bot.infinity_polling()` | `await dp.start_polling(bot)` |

---

## 7. Если cairosvg недоступен

`card_generator_svg.py` автоматически падает в fallback на старый `card_generator.py` (Pillow).
Никаких дополнительных изменений не нужно — бот продолжит работать.
