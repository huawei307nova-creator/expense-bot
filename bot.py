import os
import re
import logging
import psycopg2
import psycopg2.extras
from datetime import datetime, date, timedelta
from io import BytesIO

from telegram import Update, InputFile, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, ConversationHandler
)
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from collections import defaultdict

# ─── CONFIG ────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN_HERE")
DATABASE_URL = os.environ.get("DATABASE_URL")  # Railway задаёт автоматически (формат: postgresql://user:pass@host:port/db)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ─── БЫСТРЫЕ КНОПКИ ─────────────────────────────────────────────────────────
QUICK_ITEMS = [
    "🍊 Апельсины",
    "🍋 Лимон",
    "🍍 Ананас",
    "🍋 Лим фреш",
    "🍊 Апельсин фреш",
    "🍺 IPA",
    "🍺 Шпатен",
    "🍺 Стаут",
]

ITEM_NAMES = {
    "🍊 Апельсины": "Апельсины",
    "🍋 Лимон": "Лимон",
    "🍍 Ананас": "Ананас",
    "🍋 Лим фреш": "Лим фреш",
    "🍊 Апельсин фреш": "Апельсин фреш",
    "🍺 IPA": "IPA",
    "🍺 Шпатен": "Шпатен",
    "🍺 Стаут": "Стаут",
}

WAITING_AMOUNT = 1

# ─── DATABASE ───────────────────────────────────────────────────────────────
def get_conn():
    url = DATABASE_URL
    # Railway иногда даёт postgres://, psycopg2 требует postgresql://
    if url and url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)

def init_db():
    with get_conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id         SERIAL PRIMARY KEY,
                chat_id    BIGINT NOT NULL,
                date       DATE   NOT NULL,
                item       TEXT   NOT NULL,
                amount     REAL   NOT NULL,
                unit       TEXT   DEFAULT '',
                source     TEXT   DEFAULT 'text',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.commit()
    log.info("DB initialized")

def save_expense(chat_id, item, amount, unit="", source="text"):
    today = date.today()
    with get_conn() as con:
        con.execute(
            "INSERT INTO expenses (chat_id, date, item, amount, unit, source) VALUES (%s,%s,%s,%s,%s,%s)",
            (chat_id, today, item.strip(), amount, unit.strip(), source)
        )
        con.commit()

def get_expenses_for_date(chat_id, target_date):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute(
            "SELECT item, amount, unit, source FROM expenses WHERE chat_id=%s AND date=%s ORDER BY id",
            (chat_id, target_date)
        )
        return cur.fetchall()

def get_today_expenses(chat_id):
    return get_expenses_for_date(chat_id, date.today())

def get_daily_totals(chat_id, days=30):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("""
            SELECT date, SUM(amount)
            FROM expenses
            WHERE chat_id=%s
            GROUP BY date
            ORDER BY date DESC
            LIMIT %s
        """, (chat_id, days))
        rows = cur.fetchall()
    return list(reversed(rows))

# ─── PARSER ─────────────────────────────────────────────────────────────────
UNITS = ["кг", "г", "гр", "мл", "л", "литр", "литра", "литров", "штук", "шт", "руб", "р", "тг", "сом"]

def parse_expenses(text: str) -> list[dict]:
    results = []
    parts = re.split(r'[,;\n]|(?<!\w)и(?!\w)', text)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        part = re.sub(r'\b(купили|купил|купила|взяли|взял|за|по|стоит|стоимость|цена)\b', '', part, flags=re.IGNORECASE).strip()
        m = re.search(
            r'^(.+?)\s*[-–—]?\s*(\d+(?:[.,]\d+)?)\s*(' + '|'.join(UNITS) + r')?\s*$',
            part, re.IGNORECASE
        )
        if m:
            item = m.group(1).strip(' -–—').strip()
            amount = float(m.group(2).replace(',', '.'))
            unit = m.group(3) or ''
            if item and amount > 0:
                results.append({"item": item, "amount": amount, "unit": unit})
    return results

# ─── CHART ──────────────────────────────────────────────────────────────────
def build_chart(daily_totals):
    dates = [datetime.combine(d, datetime.min.time()) if isinstance(d, date) else datetime.strptime(str(d), "%Y-%m-%d") for d, _ in daily_totals]
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
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max(amounts)*0.01,
                f"{val:.0f}", ha="center", va="bottom", fontsize=8)
    plt.tight_layout()
    buf = BytesIO()
    plt.savefig(buf, format="png", dpi=130)
    buf.seek(0)
    plt.close(fig)
    return buf

# ─── КЛАВИАТУРЫ ─────────────────────────────────────────────────────────────
def main_keyboard():
    keyboard = [
        ["🍊 Апельсины", "🍋 Лимон", "🍍 Ананас"],
        ["🍋 Лим фреш", "🍊 Апельсин фреш"],
        ["🍺 IPA", "🍺 Шпатен", "🍺 Стаут"],
        ["📋 Отчёт", "📊 Диаграмма"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ─── HANDLERS ───────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я слежу за расходами.\n\n"
        "Нажмите кнопку товара → введите сумму.\n"
        "Или пишите вручную: `апельсины 500, хлеб 45`\n\n"
        "📋 /report — отчёт за сегодня\n"
        "📊 /chart — диаграмма по дням",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = ctx.args if ctx.args else []
    arg = args[0].lower() if args else ""
    today = date.today()

    if arg in ("вчера", "yesterday", "1"):
        target = today - timedelta(days=1)
    elif arg == "2":
        target = today - timedelta(days=2)
    elif arg == "3":
        target = today - timedelta(days=3)
    elif re.match(r'^\d{2}\.\d{2}$', arg):
        try:
            target = datetime.strptime(f"{arg}.{today.year}", "%d.%m.%Y").date()
        except:
            target = today
    elif re.match(r'^\d{2}\.\d{2}\.\d{4}$', arg):
        try:
            target = datetime.strptime(arg, "%d.%m.%Y").date()
        except:
            target = today
    else:
        target = today

    rows = get_expenses_for_date(chat_id, target)
    date_label = "сегодня" if target == today else target.strftime("%d.%m.%Y")

    if not rows:
        await update.message.reply_text(f"📭 За {date_label} расходов нет.", reply_markup=main_keyboard())
        return

    grouped = defaultdict(list)
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

    text = f"📋 *Расходы за {date_label}*\n\n" + "\n".join(lines) + f"\n\n💰 *Итого: {total:.0f}*"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_keyboard())

async def cmd_chart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = get_daily_totals(chat_id, days=30)
    if not rows:
        await update.message.reply_text("📭 Данных пока нет.", reply_markup=main_keyboard())
        return
    if len(rows) < 2:
        await update.message.reply_text("📊 Нужно хотя бы 2 дня данных.", reply_markup=main_keyboard())
        return
    buf = build_chart(rows)
    await update.message.reply_photo(photo=InputFile(buf, filename="chart.png"), caption="📊 Расходы за последние дни")

# ─── CONVERSATION ────────────────────────────────────────────────────────────
async def button_pressed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "📋 Отчёт":
        await cmd_report(update, ctx)
        return ConversationHandler.END
    if text == "📊 Диаграмма":
        await cmd_chart(update, ctx)
        return ConversationHandler.END

    if text in ITEM_NAMES:
        ctx.user_data["pending_item"] = ITEM_NAMES[text]
        await update.message.reply_text(
            f"Введите сумму для *{ITEM_NAMES[text]}*:",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        return WAITING_AMOUNT

    return ConversationHandler.END

async def receive_amount(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(',', '.')
    item = ctx.user_data.get("pending_item", "Товар")

    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Введите число, например: `500`", parse_mode="Markdown")
        return WAITING_AMOUNT

    save_expense(update.effective_chat.id, item, amount, source="button")
    await update.message.reply_text(
        f"✅ *{item}*: {amount:.0f} — записано!",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )
    ctx.user_data.pop("pending_item", None)
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("pending_item", None)
    await update.message.reply_text("Отменено.", reply_markup=main_keyboard())
    return ConversationHandler.END

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text or text.startswith("/"):
        return
    chat_id = update.effective_chat.id
    items = parse_expenses(text)
    if not items:
        return
    saved = []
    for it in items:
        save_expense(chat_id, it["item"], it["amount"], it["unit"], source="text")
        unit_str = f" {it['unit']}" if it["unit"] else ""
        saved.append(f"✅ {it['item']}: {it['amount']:.0f}{unit_str}")
    if saved:
        await update.message.reply_text(
            "Записал:\n" + "\n".join(saved) + "\n\n_/report — итог за сегодня_",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    if caption:
        items = parse_expenses(caption)
        if items:
            chat_id = update.effective_chat.id
            saved = []
            for it in items:
                save_expense(chat_id, it["item"], it["amount"], it["unit"], source="photo")
                unit_str = f" {it['unit']}" if it["unit"] else ""
                saved.append(f"✅ {it['item']}: {it['amount']:.0f}{unit_str}")
            if saved:
                await update.message.reply_text(
                    "📸 Записал:\n" + "\n".join(saved),
                    parse_mode="Markdown",
                    reply_markup=main_keyboard()
                )
                return
    await update.message.reply_text(
        "📸 Фото получено! Напишите подписью к фото или отдельно:\n`апельсины 150`",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(
            filters.TEXT & filters.Regex('^(' + '|'.join(re.escape(k) for k in list(ITEM_NAMES.keys()) + ["📋 Отчёт", "📊 Диаграмма"]) + ')$'),
            button_pressed
        )],
        states={
            WAITING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    log.info("Bot started. Polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
