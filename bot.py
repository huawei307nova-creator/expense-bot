import os
import re
import logging
import psycopg2
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
import numpy as np
from collections import defaultdict

# ─── CONFIG ────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN_HERE")
DATABASE_URL = os.environ.get("DATABASE_URL")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ─── КНОПКИ (порядок как просили) ───────────────────────────────────────────
ITEM_NAMES = {
    "🫐 Морс Клюква":     "Морс Клюква приход",
    "🫐 Морс Смородина":  "Морс Смородина приход",
    "🫐 Морс Кизил":      "Морс Кизил приход",
    "🍮 Эклеры приход":   "Эклеры приход",
    "🍊 Апельсины":       "Апельсины",
    "🍋 Лимон":           "Лимон",
    "🍍 Ананас":          "Ананас",
    "🥝 Киви":            "Киви",
    "🍳 На кухню":        "На кухню",
    "🧁 На кондитерку":   "На кондитерку",
    "🍓 Пюре Малина":     "Пюре Малина",
    "🥭 Пюре Манго":      "Пюре Манго",
    "🍋 Лим фреш":        "Лим фреш",
    "🍊 Апельсин фреш":   "Апельсин фреш",
    "🍺 IPA":             "IPA",
    "🍺 Шпатен":          "Шпатен",
    "🍺 Стаут":           "Стаут",
}

WAITING_AMOUNT = 1

# ─── DATABASE ───────────────────────────────────────────────────────────────
def get_conn():
    url = DATABASE_URL
    if url and url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)

def init_db():
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("""
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
    with get_conn() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO expenses (chat_id, date, item, amount, unit, source) VALUES (%s,%s,%s,%s,%s,%s)",
            (chat_id, date.today(), item.strip(), amount, unit.strip(), source)
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

def get_daily_totals(chat_id, days=30):
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("""
            SELECT date, SUM(amount) FROM expenses
            WHERE chat_id=%s GROUP BY date ORDER BY date DESC LIMIT %s
        """, (chat_id, days))
        return list(reversed(cur.fetchall()))

def get_week_by_item(chat_id):
    """Возвращает расходы за последние 7 дней: [(date, item, amount)]"""
    week_ago = date.today() - timedelta(days=6)
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("""
            SELECT date, item, SUM(amount) FROM expenses
            WHERE chat_id=%s AND date >= %s
            GROUP BY date, item ORDER BY date
        """, (chat_id, week_ago))
        return cur.fetchall()

def get_day_by_item(chat_id, target_date):
    """Расходы за конкретный день по каждой позиции."""
    with get_conn() as con:
        cur = con.cursor()
        cur.execute("""
            SELECT item, SUM(amount) FROM expenses
            WHERE chat_id=%s AND date=%s
            GROUP BY item ORDER BY SUM(amount) DESC
        """, (chat_id, target_date))
        return cur.fetchall()

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

# ─── CHARTS ─────────────────────────────────────────────────────────────────
def build_day_chart(target_date: date) -> BytesIO:
    """Столбчатая диаграмма по позициям за один день."""
    # placeholder — данные передаются снаружи
    pass

def build_chart_day(rows, target_date: date) -> BytesIO:
    """rows = [(item, amount), ...]"""
    items = [r[0] for r in rows]
    amounts = [r[1] for r in rows]

    fig, ax = plt.subplots(figsize=(max(8, len(items) * 0.9), 5))
    colors = plt.cm.Set3(np.linspace(0, 1, len(items)))
    bars = ax.bar(range(len(items)), amounts, color=colors, zorder=3)
    ax.set_facecolor("#F7F9FC")
    fig.patch.set_facecolor("#F7F9FC")
    ax.grid(axis="y", linestyle="--", alpha=0.5, zorder=0)
    ax.set_xticks(range(len(items)))
    ax.set_xticklabels(items, rotation=35, ha="right", fontsize=9)
    ax.set_title(f"Расходы за {target_date.strftime('%d.%m.%Y')} по позициям", fontsize=13, fontweight="bold", pad=10)
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

def build_chart_week(rows) -> BytesIO:
    """rows = [(date, item, amount)] — стековый график по дням."""
    from collections import defaultdict

    # Собираем структуру: day_data[date][item] = amount
    day_data = defaultdict(lambda: defaultdict(float))
    all_items_set = set()
    for d, item, amount in rows:
        day_data[d][item] += amount
        all_items_set.add(item)

    # Сортируем дни и позиции (позиции по суммарному расходу)
    sorted_days = sorted(day_data.keys())
    item_totals = defaultdict(float)
    for d in sorted_days:
        for item, amt in day_data[d].items():
            item_totals[item] += amt
    sorted_items = sorted(all_items_set, key=lambda x: item_totals[x], reverse=True)

    day_labels = [d.strftime("%d.%m") if hasattr(d, "strftime") else str(d)[-5:] for d in sorted_days]

    COLORS = [
        "#f97b4e","#4e8ef7","#f7c948","#7c5cbf",
        "#3bbfa0","#e05c8a","#6dbf5c","#e8b84b",
        "#5bc4e8","#bf7c5c","#a0bf5c","#bf5c9e",
    ]

    fig, ax = plt.subplots(figsize=(max(8, len(sorted_days) * 1.1), 5))
    bottoms = np.zeros(len(sorted_days))
    for i, item in enumerate(sorted_items):
        values = np.array([day_data[d].get(item, 0) for d in sorted_days])
        ax.bar(range(len(sorted_days)), values, bottom=bottoms,
               label=item, color=COLORS[i % len(COLORS)], zorder=3)
        bottoms += values

    ax.set_facecolor("#F7F9FC")
    fig.patch.set_facecolor("#F7F9FC")
    ax.grid(axis="y", linestyle="--", alpha=0.4, zorder=0)
    ax.set_xticks(range(len(sorted_days)))
    ax.set_xticklabels(day_labels, fontsize=10)
    ax.set_ylabel("Сумма")
    week_start = day_labels[0] if day_labels else ""
    week_end = day_labels[-1] if day_labels else ""
    ax.set_title(f"Расходы за неделю {week_start}–{week_end}", fontsize=13, fontweight="bold", pad=10)

    # Легенда снаружи справа
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=8, frameon=False)

    # Подписи итогов над столбиками
    for j, total in enumerate(bottoms):
        if total > 0:
            ax.text(j, total + max(bottoms)*0.01, f"{total:.0f}",
                    ha="center", va="bottom", fontsize=8, fontweight="bold")

    plt.tight_layout()
    buf = BytesIO()
    plt.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf

# ─── КЛАВИАТУРА ─────────────────────────────────────────────────────────────
def main_keyboard():
    keyboard = [
        ["🫐 Морс Клюква",    "🫐 Морс Смородина", "🫐 Морс Кизил"],
        ["🍮 Эклеры приход"],
        ["🍊 Апельсины",      "🍋 Лимон",          "🍍 Ананас",     "🥝 Киви"],
        ["🍳 На кухню",       "🧁 На кондитерку"],
        ["🍓 Пюре Малина",    "🥭 Пюре Манго"],
        ["🍋 Лим фреш",       "🍊 Апельсин фреш"],
        ["🍺 IPA",            "🍺 Шпатен",         "🍺 Стаут"],
        ["📋 Отчёт",          "📊 График за день",  "📈 График за неделю"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ─── HELPERS ────────────────────────────────────────────────────────────────
def parse_date_arg(arg: str):
    today = date.today()
    if not arg:
        return today, "сегодня"
    arg = arg.lower()
    if arg in ("вчера", "yesterday", "1"):
        d = today - timedelta(days=1)
        return d, d.strftime("%d.%m.%Y")
    if arg == "2":
        d = today - timedelta(days=2); return d, d.strftime("%d.%m.%Y")
    if arg == "3":
        d = today - timedelta(days=3); return d, d.strftime("%d.%m.%Y")
    if re.match(r'^\d{2}\.\d{2}$', arg):
        try: return (d := datetime.strptime(f"{arg}.{today.year}", "%d.%m.%Y").date()), d.strftime("%d.%m.%Y")
        except: pass
    if re.match(r'^\d{2}\.\d{2}\.\d{4}$', arg):
        try: return (d := datetime.strptime(arg, "%d.%m.%Y").date()), d.strftime("%d.%m.%Y")
        except: pass
    return today, "сегодня"

def format_report(rows, date_label):
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
    return f"📋 *Расходы за {date_label}*\n\n" + "\n".join(lines) + f"\n\n💰 *Итого: {total:.0f}*"

# ─── HANDLERS ───────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Слежу за расходами.\n\n"
        "Нажмите кнопку → введите сумму.\n"
        "Или пишите: `апельсины 500, хлеб 45`\n\n"
        "📋 /report — отчёт сегодня\n"
        "📋 /report вчера — вчера\n"
        "📋 /report 25.05 — конкретная дата",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = ctx.args if ctx.args else []
    arg = args[0] if args else ""
    target, label = parse_date_arg(arg)
    rows = get_expenses_for_date(chat_id, target)
    if not rows:
        await update.message.reply_text(f"📭 За {label} расходов нет.", reply_markup=main_keyboard())
        return
    await update.message.reply_text(format_report(rows, label), parse_mode="Markdown", reply_markup=main_keyboard())

async def cmd_chart_day(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    today = date.today()
    rows = get_day_by_item(chat_id, today)
    if not rows:
        await update.message.reply_text("📭 Сегодня расходов нет.", reply_markup=main_keyboard())
        return
    buf = build_chart_day(rows, today)
    total = sum(r[1] for r in rows)
    await update.message.reply_photo(
        photo=InputFile(buf, filename="day.png"),
        caption=f"📊 Расходы за {today.strftime('%d.%m.%Y')} | Итого: {total:.0f}",
        reply_markup=main_keyboard()
    )

async def cmd_chart_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = get_week_by_item(chat_id)
    if not rows:
        await update.message.reply_text("📭 За неделю данных нет.", reply_markup=main_keyboard())
        return
    buf = build_chart_week(rows)
    total = sum(r[1] for r in rows)
    week_ago = (date.today() - timedelta(days=6)).strftime("%d.%m")
    today_str = date.today().strftime("%d.%m")
    await update.message.reply_photo(
        photo=InputFile(buf, filename="week.png"),
        caption=f"📈 Расходы {week_ago}–{today_str} по позициям | Итого: {total:.0f}",
        reply_markup=main_keyboard()
    )

# ─── CONVERSATION ────────────────────────────────────────────────────────────
ALL_BUTTONS = list(ITEM_NAMES.keys()) + ["📋 Отчёт", "📊 График за день", "📈 График за неделю"]

async def button_pressed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📋 Отчёт":
        await cmd_report(update, ctx); return ConversationHandler.END
    if text == "📊 График за день":
        await cmd_chart_day(update, ctx); return ConversationHandler.END
    if text == "📈 График за неделю":
        await cmd_chart_week(update, ctx); return ConversationHandler.END
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
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Введите число, например: `500`", parse_mode="Markdown")
        return WAITING_AMOUNT
    save_expense(update.effective_chat.id, item, amount, source="button")
    await update.message.reply_text(
        f"✅ *{item}*: {amount:.0f} — записано!",
        parse_mode="Markdown", reply_markup=main_keyboard()
    )
    ctx.user_data.pop("pending_item", None)
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("pending_item", None)
    await update.message.reply_text("Отменено.", reply_markup=main_keyboard())
    return ConversationHandler.END

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if not text or text.startswith("/"): return
    chat_id = update.effective_chat.id
    items = parse_expenses(text)
    if not items: return
    saved = []
    for it in items:
        save_expense(chat_id, it["item"], it["amount"], it["unit"], source="text")
        unit_str = f" {it['unit']}" if it["unit"] else ""
        saved.append(f"✅ {it['item']}: {it['amount']:.0f}{unit_str}")
    if saved:
        await update.message.reply_text(
            "Записал:\n" + "\n".join(saved) + "\n\n_/report — итог за сегодня_",
            parse_mode="Markdown", reply_markup=main_keyboard()
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
                await update.message.reply_text("📸 Записал:\n" + "\n".join(saved), parse_mode="Markdown", reply_markup=main_keyboard())
                return
    await update.message.reply_text(
        "📸 Фото получено! Напишите подписью или отдельно:\n`апельсины 150`",
        parse_mode="Markdown", reply_markup=main_keyboard()
    )

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[MessageHandler(
            filters.TEXT & filters.Regex('^(' + '|'.join(re.escape(k) for k in ALL_BUTTONS) + ')$'),
            button_pressed
        )],
        states={WAITING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_amount)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("day", cmd_chart_day))
    app.add_handler(CommandHandler("week", cmd_chart_week))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    log.info("Bot started. Polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
