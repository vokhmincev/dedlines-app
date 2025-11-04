from __future__ import annotations
import os
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes
)

# импорт из твоего Flask-приложения
from app import db, app, User, Deadline, SHEETS, gsheet_to_csv_url, fetch_csv_rows, find_score_by_surname

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

TZ = ZoneInfo("Europe/Moscow")

# ================= helpers =================
def _fmt_deadline(d: Deadline) -> str:
    when = d.due_at.strftime("%d.%m.%Y") if d.all_day else d.due_at.strftime("%d.%m.%Y %H:%M")
    tag = f"[{d.kind}]" if d.kind else ""
    subj = f"{d.subject}: " if d.subject else ""
    return f"• {when} — {tag} {subj}{d.title}".strip()

async def _require_linked(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> User | None:
    chat_id = update.effective_chat.id
    with app.app_context():
        u = User.query.filter_by(tg_id=chat_id).first()
        if not u:
            await update.effective_message.reply_text(
                "Этот чат ещё не привязан к аккаунту.\n"
                "Отправь команду: /bind <твой_логин_на_сайте>\n\n"
                "Пример: /bind ivanov"
            )
            return None
        return u

# ========= on-demand commands =========
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот дедлайнов.\n"
        "Команды:\n"
        "• /bind <логин> — привязать аккаунт сайта\n"
        "• /next — ближайшие дедлайны (10 дней)\n"
        "• /scores — твои баллы по предметам\n"
        "• /help — справка"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)

async def cmd_bind(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) != 1:
        await update.message.reply_text("Использование: /bind <логин>\nНапример: /bind ivanov")
        return
    login = ctx.args[0].strip().lower()
    chat = update.effective_chat

    with app.app_context():
        u = User.query.filter_by(username=login).first()
        if not u:
            await update.message.reply_text("Пользователь с таким логином не найден.")
            return
        u.tg_id = chat.id
        u.tg_username = chat.username or None
        db.session.commit()

    await update.message.reply_text("Готово! Аккаунт привязан ✅")

async def cmd_next(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = await _require_linked(update, ctx)
    if not u:
        return
    now = datetime.now(TZ)
    horizon = now + timedelta(days=10)
    with app.app_context():
        items = (
            Deadline.query
            .filter(Deadline.due_at >= now, Deadline.due_at <= horizon)
            .order_by(Deadline.due_at.asc())
            .all()
        )
    if not items:
        await update.message.reply_text("На ближайшие 10 дней дедлайнов нет 🎉")
        return
    text_lines = ["Ближайшие дедлайны:\n"] + [_fmt_deadline(d) for d in items[:30]]
    await update.message.reply_text("\n".join(text_lines))

async def cmd_scores(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = await _require_linked(update, ctx)
    if not u:
        return
    surname = u.surname

    results = []
    errors = []
    for sheet in SHEETS:
        try:
            csv_url = gsheet_to_csv_url(sheet["url"])
            rows = fetch_csv_rows(csv_url)
            found = find_score_by_surname(
                rows,
                surname,
                prefer_total=sheet.get("prefer_total", False),
                sum_until_total=sheet.get("sum_until_total", False),
                take_last_total=sheet.get("take_last_total", False),
            )
            if found:
                results.append(f"• {sheet['name']}: {round(found['sum'], 3)}")
            else:
                results.append(f"• {sheet['name']}: —")
        except Exception as e:
            errors.append(f"{sheet['name']}: {e}")

    if results:
        await update.message.reply_text("Твои баллы:\n" + "\n".join(results))
    else:
        await update.message.reply_text("Не удалось получить баллы.")
    if errors:
        await update.message.reply_text("⚠️ Ошибки:\n" + "\n".join(errors[:5]))

# ========= scheduled jobs (через JobQueue) =========
async def job_daily_digest(ctx: ContextTypes.DEFAULT_TYPE):
    """Ежедневная сводка: дедлайны на сегодня и на завтра для всех привязанных пользователей."""
    now = datetime.now(TZ)
    today_start = datetime.combine(now.date(), time(0, 0), tzinfo=TZ)
    tomorrow_start = today_start + timedelta(days=1)
    after_tomorrow_start = tomorrow_start + timedelta(days=1)

    with app.app_context():
        users = User.query.filter(User.tg_id.isnot(None)).all()

        # забираем дедлайны на сегодня/завтра разом, чтобы не гонять БД в цикле
        todays = (
            Deadline.query
            .filter(Deadline.due_at >= today_start, Deadline.due_at < tomorrow_start)
            .order_by(Deadline.due_at.asc())
            .all()
        )
        tomorrows = (
            Deadline.query
            .filter(Deadline.due_at >= tomorrow_start, Deadline.due_at < after_tomorrow_start)
            .order_by(Deadline.due_at.asc())
            .all()
        )

    txt_today = "На сегодня дедлайны:\n" + "\n".join(_fmt_deadline(d) for d in todays) if todays else "Сегодня дедлайнов нет 🎉"
    txt_tomorrow = "На завтра дедлайны:\n" + "\n".join(_fmt_deadline(d) for d in tomorrows) if tomorrows else "На завтра дедлайнов нет 🎉"

    for u in users:
        # отправляем раздельными сообщениями, чтобы было читабельно
        try:
            await ctx.bot.send_message(chat_id=u.tg_id, text=txt_today)
            await ctx.bot.send_message(chat_id=u.tg_id, text=txt_tomorrow)
        except Exception:
            # молча пропускаем (например, если юзер закрыл личку боту)
            pass

async def job_hourly_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    """Каждый час напоминаем о дедлайнах, которые начнутся в ближайшие 24 часа."""
    now = datetime.now(TZ)
    soon = now + timedelta(hours=24)
    with app.app_context():
        users = User.query.filter(User.tg_id.isnot(None)).all()
        upcoming = (
            Deadline.query
            .filter(Deadline.due_at >= now, Deadline.due_at <= soon)
            .order_by(Deadline.due_at.asc())
            .all()
        )

    if not upcoming:
        return

    text_lines = ["Напоминание: дедлайны в ближайшие 24 часа:\n"] + [_fmt_deadline(d) for d in upcoming[:50]]
    msg = "\n".join(text_lines)
    for u in users:
        try:
            await ctx.bot.send_message(chat_id=u.tg_id, text=msg)
        except Exception:
            pass

# ========= app entry =========
def main():
    app_ = Application.builder().token(TOKEN).build()

    # команды
    app_.add_handler(CommandHandler("start", cmd_start))
    app_.add_handler(CommandHandler("help", cmd_help))
    app_.add_handler(CommandHandler("bind", cmd_bind))
    app_.add_handler(CommandHandler("next", cmd_next))
    app_.add_handler(CommandHandler("scores", cmd_scores))
    app_.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_help))  # простая помощь

    # планировщик
    # Ежедневная сводка в 09:00 по МСК
    app_.job_queue.run_daily(
        job_daily_digest,
        time=time(9, 0, tzinfo=TZ),
        name="daily_digest_msk"
    )
    # Почасовое напоминание на 24 часа вперёд
    app_.job_queue.run_repeating(
        job_hourly_reminders,
        interval=3600,  # секунд
        first=10,       # через 10 секунд после старта
        name="hourly_reminders"
    )

    app_.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
