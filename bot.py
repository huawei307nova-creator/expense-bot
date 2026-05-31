import os
import re
import sqlite3
import base64
import json
import logging
from datetime import datetime, date
from io import BytesIO
import httpx

from telegram import Update, InputFile
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters
)
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from collections import defaultdict

# ─── CONFIG ────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN_HERE")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY_HERE")
DB_PATH = "expenses.db"

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-exp:generateContent"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ─── DATABASE ───────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id   INTEGER NOT NULL,
            date      TEXT    NOT NULL,
            item      TEXT    NOT NULL,
            amount    REAL    NOT NULL,
            unit      TEXT    DEFAULT '',
            source    TEXT    DEFAULT 'text',
            created_at TEXT   DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.commit()
    con.close()

def save_expense(chat_id: int, item: str, amount: float, unit: str = "", source: str = "text"):
    today = date.today().isoformat()
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO expenses (chat_id, date, item, amount, unit, source) VALUES (?,?,?,?,?,?)",
        (chat_id, today, item.strip(), amount, unit.strip(), source)
    )
    con.commit()
    con.close()

def get_today_expenses(chat_id: int):
    today = date.today().isoformat()
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT item, amount, unit, source FROM expenses WHERE chat_id=? AND date=? ORDER BY id",
        (chat_id, today)
    ).fetchall()
    con.close()
    return rows

def get_daily_totals(chat_id: int, days: int = 30):
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        """SELECT date, SUM(amount) FROM expenses
           WHERE chat_id=?
           GROUP BY date ORDER BY date DESC LIMIT ?""",
        (chat_id, days)
    ).fetchall()
    con.close()
    return list(reversed(rows))

# ─── GEMINI HELPERS ─────────────────────────────────────────────────────────
async def gemini_request(parts: list) -> str:
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            json={"contents": [{"parts": parts}]}
        )
    data = r.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()

async def parse_text_with_gemini(text: str) -> list[dict]:
    prompt = f"""Из этого сообщения извлеки все расходы (покупки).
Верни ТОЛЬКО валидный JSON-массив объектов без пояснений и без markdown.
Каждый объект: {{"item": "название", "amount": число, "unit": "единица или пустая строка"}}
unit — г/кг/мл/л/штука/пустая строка если не указано.
amount — числовое значение (цена ИЛИ количество — то, что указано рядом с товаром).

Сообщение: {text}

Пример ответа: [{{"item":"апельсин","amount":500,"unit":""}},{{"item":"эклеры","amount":13,"unit":""}}]"""

    raw = await gemini_request([{"text": prompt}])
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)

async def parse_image_with_gemini(image_bytes: bytes, caption: str = "") -> list[dict]:
    b64 = base64.standard_b64encode(image_bytes).decode()
    prompt = (
        "На этой фотографии может быть чек, упаковка продукта или список покупок. "
        "Извлеки все товары и их стоимость/количество. "
        + (f"Дополнительный контекст: {caption}. " if caption else "")
        + "Верни ТОЛЬКО валидный JSON-массив объектов без пояснений и без markdown. "
        "Каждый объект: {\"item\": \"название\", \"amount\": число, \"unit\": \"г/кг/мл/л/руб/пустая\"}. "
        "Если это чек — amount это цена. Если упаковка — amount это граммовка/объём. "
        "Если ничего не нашёл — верни []."
    )
    raw = await gemini_request([
        {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
        {"text": prompt}
    ])
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)

# ─── CHART ──────────────────────────────────────────────────────────────────
def build_chart(daily_totals: list[tuple]) -> BytesIO:
    dates = [datetime.strptime(d, "%Y-%m-%d") for d, _ in daily_totals]
    amounts = [a for _, a in daily_totals]

    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.bar(dates, amounts, color="#4F8EF7", width=0.6, zorder=3)
    ax.set_facecolor("#F7F9FC")
    fig.patch.set_facecolor("#F7F9FC")
    ax.grid(axis="y", linestyle="--", alpha=0.5, zorder=0)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.xaxis.set_major_locator(mdates.DayLocator())
    plt.xticks(rotation=45, ha="right", fontsize=9)
    ax.set_title("Расходы по дням", fontsize=14, fontweight="bold", pad=12)
    ax.set_ylabel("Сумма")

    for bar, val in zip(bars, amounts):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + max(amounts) * 0.01,
            f"{val:.0f}",
            ha="center", va="bottom", fontsize=8
        )

    plt.tight_layout()
    buf = BytesIO()
    plt.savefig(buf, format="png", dpi=130)
    buf.seek(0)
    plt.close(fig)
    return buf

# ─── HANDLERS ───────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я слежу за расходами группы.\n\n"
        "📝 Просто пишите: *апельсин 500, эклеры 13* — или отправьте фото чека/упаковки.\n\n"
        "📋 /report — отчёт за сегодня\n"
        "📊 /chart — диаграмма расходов по дням\n"
        "❓ /help — помощь",
        parse_mode="Markdown",
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Как пользоваться:*\n\n"
        "• Пишите расходы в любом формате:\n"
        "  `апельсины 150, хлеб 45`\n"
        "  `молоко - 89 руб`\n"
        "  `купили яблоки за 200 и сок 60`\n\n"
        "• Отправьте фото чека или упаковки (можно с подписью)\n\n"
        "*/report* — все расходы за сегодня + итог\n"
        "*/chart* — столбчатая диаграмма по дням (последние 30 дней)",
        parse_mode="Markdown",
    )

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = get_today_expenses(chat_id)
    if not rows:
        await update.message.reply_text("📭 Сегодня расходов ещё нет.")
        return

    grouped: dict = defaultdict(list)
    for item, amount, unit, source in rows:
        grouped[item].append((amount, unit, source))

    lines = []
    total = 0.0
    for item, entries in grouped.items():
        s = sum(a for a, _, _ in entries)
        unit = entries[-1][1]
        icon = "📸" if any(src == "photo" for _, _, src in entries) else "💬"
        unit_str = f" {unit}" if unit else ""
        lines.append(f"{icon} *{item}*: {s:.0f}{unit_str}")
        total += s

    today_str = date.today().strftime("%d.%m.%Y")
    text = f"📋 *Расходы за {today_str}*\n\n" + "\n".join(lines) + f"\n\n💰 *Итого: {total:.0f}*"
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_chart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = get_daily_totals(chat_id, days=30)
    if not rows:
        await update.message.reply_text("📭 Данных для диаграммы пока нет.")
        return
    if len(rows) < 2:
        await update.message.reply_text("📊 Нужно хотя бы 2 дня данных для диаграммы.")
        return

    buf = build_chart(rows)
    await update.message.reply_photo(
        photo=InputFile(buf, filename="chart.png"),
        caption="📊 Расходы за последние дни"
    )

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text or text.startswith("/"):
        return

    chat_id = update.effective_chat.id
    try:
        items = await parse_text_with_gemini(text)
    except Exception as e:
        log.warning(f"Gemini text parse error: {e}")
        await update.message.reply_text("⚠️ Не удалось разобрать сообщение. Попробуйте формат: *товар сумма*", parse_mode="Markdown")
        return

    if not items:
        return

    saved = []
    for it in items:
        item = it.get("item", "").strip()
        amount = float(it.get("amount", 0))
        unit = it.get("unit", "")
        if item and amount > 0:
            save_expense(chat_id, item, amount, unit, source="text")
            unit_str = f" {unit}" if unit else ""
            saved.append(f"✅ {item}: {amount:.0f}{unit_str}")

    if saved:
        await update.message.reply_text(
            "Записал:\n" + "\n".join(saved) + "\n\n_/report — посмотреть итог за сегодня_",
            parse_mode="Markdown"
        )

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    caption = update.message.caption or ""

    photo = update.message.photo[-1]
    file = await ctx.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()

    processing_msg = await update.message.reply_text("🔍 Анализирую фото...")

    try:
        items = await parse_image_with_gemini(bytes(image_bytes), caption)
    except Exception as e:
        log.warning(f"Gemini image parse error: {e}")
        await processing_msg.edit_text("⚠️ Не удалось распознать фото. Попробуйте написать расходы текстом.")
        return

    if not items:
        await processing_msg.edit_text("🤷 На фото не нашёл расходов. Можно написать их текстом!")
        return

    saved = []
    for it in items:
        item = it.get("item", "").strip()
        amount = float(it.get("amount", 0))
        unit = it.get("unit", "")
        if item and amount > 0:
            save_expense(chat_id, item, amount, unit, source="photo")
            unit_str = f" {unit}" if unit else ""
            saved.append(f"✅ {item}: {amount:.0f}{unit_str}")

    if saved:
        await processing_msg.edit_text(
            "📸 С фото записал:\n" + "\n".join(saved) + "\n\n_/report — посмотреть итог за сегодня_",
            parse_mode="Markdown"
        )
    else:
        await processing_msg.edit_text("🤷 Не удалось извлечь данные из фото.")

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    log.info("Bot started. Polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
