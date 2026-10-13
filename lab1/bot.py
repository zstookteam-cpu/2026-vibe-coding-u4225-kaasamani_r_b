"""
Бот-ассистент для задач (Telegram, python-telegram-bot v21).

Возможности:
  * приём задач обычным сообщением или командой /add;
  * приоритеты через теги !1 / !2 / !3;
  * напоминания через JobQueue (с восстановлением после перезапуска);
  * списки: /list, /today, /completed;
  * у каждого пользователя свой список задач и своя нумерация.

Хранение данных — SQLite (файл tasks.db).
Токен и часовой пояс читаются из .env (BOT_TOKEN, TIMEZONE).
"""

import logging
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Настройки и логирование
# ---------------------------------------------------------------------------

load_dotenv()  # читаем переменные из файла .env

BOT_TOKEN = os.getenv("BOT_TOKEN")
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Moscow")
DB_PATH = "tasks.db"
# Необязательный прокси для доступа к Telegram (например socks5://127.0.0.1:1080
# или http://127.0.0.1:7890). Нужен, если api.telegram.org недоступен напрямую.
PROXY_URL = os.getenv("PROXY_URL") or None

MAX_TASK_LEN = 500       # максимальная длина текста задачи
TELEGRAM_LIMIT = 4000    # безопасный размер одного сообщения Telegram

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
# Библиотека httpx шумит на уровне INFO — приглушаем
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("task-bot")

try:
    TZ = ZoneInfo(TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    sys.exit(f"❌ Неизвестный часовой пояс в TIMEZONE: {TIMEZONE_NAME!r}")

# Приоритеты: число -> (эмодзи, название)
PRIORITIES = {
    1: ("🔴", "Высокий"),
    2: ("🟡", "Средний"),
    3: ("🟢", "Низкий"),
}
DEFAULT_PRIORITY = 2

# Тег приоритета: !1, !2 или !3, отдельным "словом" (не часть другого текста)
PRIORITY_TAG_RE = re.compile(r"(?<!\S)!([123])(?!\S)")


class UserError(Exception):
    """Ошибка пользователя: текст исключения показывается ему в чате."""


# ---------------------------------------------------------------------------
# Работа с базой данных
# ---------------------------------------------------------------------------

def db_connect() -> sqlite3.Connection:
    """Открывает соединение с базой (строки доступны по имени колонки)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Создаёт таблицы, если их ещё нет."""
    with closing(db_connect()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,  -- глобальный id
                user_id      INTEGER NOT NULL,
                chat_id      INTEGER NOT NULL,
                num          INTEGER NOT NULL,       -- номер задачи у пользователя
                text         TEXT    NOT NULL,
                priority     INTEGER NOT NULL DEFAULT 2,
                status       TEXT    NOT NULL DEFAULT 'active',  -- active / done
                created_at   INTEGER NOT NULL,       -- время создания (UTC, epoch)
                completed_at INTEGER,                -- время выполнения (UTC, epoch)
                remind_at    INTEGER,                -- время напоминания (UTC, epoch)
                reminded     INTEGER NOT NULL DEFAULT 0,  -- напоминание отправлено?
                UNIQUE (user_id, num)
            )
            """
        )
        # Счётчик номеров: номер никогда не переиспользуется после удаления
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS counters (
                user_id  INTEGER PRIMARY KEY,
                last_num INTEGER NOT NULL
            )
            """
        )


def now_ts() -> int:
    """Текущее время в секундах (UTC)."""
    return int(datetime.now(timezone.utc).timestamp())


def db_add_task(user_id: int, chat_id: int, text: str, priority: int) -> int:
    """Создаёт задачу и возвращает её номер (уникальный для пользователя)."""
    with closing(db_connect()) as conn, conn:
        row = conn.execute(
            "SELECT last_num FROM counters WHERE user_id = ?", (user_id,)
        ).fetchone()
        num = (row["last_num"] if row else 0) + 1
        conn.execute(
            "INSERT OR REPLACE INTO counters (user_id, last_num) VALUES (?, ?)",
            (user_id, num),
        )
        conn.execute(
            "INSERT INTO tasks (user_id, chat_id, num, text, priority, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, chat_id, num, text, priority, now_ts()),
        )
    return num


def db_get_task(user_id: int, num: int):
    """Возвращает задачу пользователя по номеру или None."""
    with closing(db_connect()) as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND num = ?", (user_id, num)
        ).fetchone()


def db_active_tasks(user_id: int):
    """Все активные задачи пользователя (по приоритету, затем по номеру)."""
    with closing(db_connect()) as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND status = 'active' "
            "ORDER BY priority, num",
            (user_id,),
        ).fetchall()


def db_completed_tasks(user_id: int, limit: int = 10):
    """Последние выполненные задачи пользователя."""
    with closing(db_connect()) as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND status = 'done' "
            "ORDER BY completed_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def db_update(task_id: int, **fields) -> None:
    """Обновляет поля задачи по глобальному id."""
    columns = ", ".join(f"{name} = ?" for name in fields)
    with closing(db_connect()) as conn, conn:
        conn.execute(
            f"UPDATE tasks SET {columns} WHERE id = ?",
            (*fields.values(), task_id),
        )


def db_delete(task_id: int) -> None:
    """Удаляет задачу."""
    with closing(db_connect()) as conn, conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


def db_pending_reminders():
    """Все неотправленные напоминания активных задач (для восстановления)."""
    with closing(db_connect()) as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE status = 'active' "
            "AND remind_at IS NOT NULL AND reminded = 0"
        ).fetchall()


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def parse_task_text(raw: str) -> tuple[str, int]:
    """
    Разбирает текст задачи: вырезает теги !1/!2/!3 и определяет приоритет.
    Возвращает (чистый текст, приоритет). Приоритет по умолчанию — средний.
    """
    priority = DEFAULT_PRIORITY
    match = PRIORITY_TAG_RE.search(raw)
    if match:
        priority = int(match.group(1))  # берём первый найденный тег
    # Убираем все теги и лишние пробелы
    text = " ".join(PRIORITY_TAG_RE.sub(" ", raw).split())
    return text, priority


def parse_number(value: str) -> int:
    """Преобразует аргумент в номер задачи."""
    if not value.isdigit() or int(value) < 1:
        raise UserError("❌ Номер задачи должен быть положительным числом.")
    return int(value)


def get_user_task(user_id: int, num: int):
    """Находит задачу пользователя или сообщает, что её нет."""
    task = db_get_task(user_id, num)
    if task is None:
        raise UserError(f"❌ Задача №{num} не найдена. Посмотрите номера в /list.")
    return task


def get_active_task(user_id: int, num: int):
    """Находит именно активную (не выполненную) задачу."""
    task = get_user_task(user_id, num)
    if task["status"] != "active":
        raise UserError(f"❌ Задача №{num} уже выполнена.")
    return task


def fmt_dt(ts: int) -> str:
    """Форматирует время (epoch) в ДД.ММ ЧЧ:ММ в часовом поясе бота."""
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")


def fmt_task(task) -> str:
    """Одна строка задачи для списков."""
    line = f"#{task['num']} {task['text']}"
    if task["remind_at"] and not task["reminded"]:
        line += f"  ⏰ {fmt_dt(task['remind_at'])}"
    return line


def parse_remind_time(args: list[str]) -> datetime:
    """
    Разбирает время напоминания из аргументов команды /remind (после номера):
      ЧЧ:ММ        — сегодня (если время прошло — завтра);
      ДД.ММ ЧЧ:ММ  — конкретная дата (в текущем году).
    Возвращает datetime с часовым поясом.
    """
    now = datetime.now(TZ)
    format_error = UserError(
        "❌ Неверный формат времени. Примеры:\n"
        "/remind 3 18:30\n"
        "/remind 3 25.12 09:00"
    )

    if len(args) == 1:
        # Формат ЧЧ:ММ
        try:
            t = datetime.strptime(args[0], "%H:%M")
        except ValueError:
            raise format_error
        when = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if when <= now:
            when += timedelta(days=1)  # время уже прошло — переносим на завтра
        return when

    if len(args) == 2:
        # Формат ДД.ММ ЧЧ:ММ
        try:
            t = datetime.strptime(args[1], "%H:%M")
            day, month = args[0].split(".")
            when = datetime(
                now.year, int(month), int(day), t.hour, t.minute, tzinfo=TZ
            )
        except ValueError:
            raise format_error
        if when <= now:
            raise UserError("❌ Это время уже прошло. Укажите время в будущем.")
        return when

    raise format_error


async def reply(update: Update, text: str) -> None:
    """Отправляет ответ, разбивая длинный текст на части (лимит Telegram)."""
    while len(text) > TELEGRAM_LIMIT:
        cut = text.rfind("\n", 0, TELEGRAM_LIMIT)
        cut = cut if cut > 0 else TELEGRAM_LIMIT
        await update.message.reply_text(text[:cut])
        text = text[cut:].lstrip("\n")
    await update.message.reply_text(text)


# ---------------------------------------------------------------------------
# Напоминания (JobQueue)
# ---------------------------------------------------------------------------

def job_name(task_id: int) -> str:
    """Уникальное имя задания в JobQueue для напоминания."""
    return f"remind_{task_id}"


def cancel_job(job_queue, task_id: int) -> None:
    """Отменяет запланированное напоминание, если оно есть."""
    for job in job_queue.get_jobs_by_name(job_name(task_id)):
        job.schedule_removal()


def schedule_job(job_queue, task) -> None:
    """Планирует отправку напоминания по задаче (старое напоминание заменяется)."""
    cancel_job(job_queue, task["id"])
    when = datetime.fromtimestamp(task["remind_at"], timezone.utc)
    job_queue.run_once(
        send_reminder,
        when=when,
        chat_id=task["chat_id"],
        user_id=task["user_id"],
        name=job_name(task["id"]),
        data=task["id"],
    )


async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Срабатывает в назначенное время и присылает напоминание."""
    task_id = context.job.data
    with closing(db_connect()) as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    # Задачу могли удалить/выполнить, или напоминание уже отправлено
    if task is None or task["status"] != "active" or task["reminded"]:
        return

    emoji = PRIORITIES[task["priority"]][0]
    text = f"⏰ Напоминание!\n{emoji} #{task['num']} {task['text']}"
    # Если бот был выключен и напоминание опоздало — честно говорим об этом
    if now_ts() - task["remind_at"] > 60:
        text += f"\n(запланировано на {fmt_dt(task['remind_at'])}, бот был офлайн)"

    try:
        await context.bot.send_message(chat_id=task["chat_id"], text=text)
        db_update(task_id, reminded=1)
    except Exception:
        logger.exception("Не удалось отправить напоминание по задаче id=%s", task_id)


async def restore_reminders(application: Application) -> None:
    """При запуске бота заново планирует все неотправленные напоминания."""
    tasks = db_pending_reminders()
    for task in tasks:
        schedule_job(application.job_queue, task)
    logger.info("Восстановлено напоминаний: %d", len(tasks))


# ---------------------------------------------------------------------------
# Обработчики команд
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "📖 Команды:\n\n"
    "➕ Добавить задачу — просто напишите текст или /add <текст>\n"
    "   Пример: Купить молоко !1\n"
    "   Приоритет: !1 🔴 высокий, !2 🟡 средний, !3 🟢 низкий\n"
    "   (без тега — средний)\n\n"
    "📋 /list — активные задачи по приоритетам\n"
    "📅 /today — напоминания на сегодня и важные задачи\n"
    "✅ /done <номер> — задача выполнена (/done 3)\n"
    "🏁 /completed — последние 10 выполненных\n"
    "🗑 /delete <номер> — удалить задачу (/delete 3)\n"
    "🔁 /priority <номер> <1|2|3> — сменить приоритет (/priority 3 1)\n\n"
    "⏰ /remind <номер> <ЧЧ:ММ> — напомнить сегодня/завтра (/remind 3 18:30)\n"
    "⏰ /remind <номер> <ДД.ММ ЧЧ:ММ> — на дату (/remind 3 25.12 09:00)\n"
    "🔕 /unremind <номер> — отменить напоминание"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — приветствие."""
    await reply(
        update,
        "👋 Привет! Я бот-ассистент для задач.\n\n"
        "Просто напишите задачу — я её запомню. "
        "Добавьте !1, !2 или !3, чтобы задать приоритет.\n"
        "Список команд — /help",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — список команд."""
    await reply(update, HELP_TEXT)


async def add_task(update: Update, raw_text: str) -> None:
    """Общая логика создания задачи (для /add и для обычных сообщений)."""
    text, priority = parse_task_text(raw_text)
    if not text:
        raise UserError(
            "❌ Текст задачи пустой. Пример: /add Купить молоко !1"
        )
    if len(text) > MAX_TASK_LEN:
        raise UserError(f"❌ Слишком длинная задача (максимум {MAX_TASK_LEN} символов).")

    num = db_add_task(
        update.effective_user.id, update.effective_chat.id, text, priority
    )
    emoji, name = PRIORITIES[priority]
    await reply(update, f"✅ Задача #{num} добавлена\n{emoji} {name}: {text}")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/add <текст> — добавить задачу."""
    await add_task(update, " ".join(context.args))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Любое обычное сообщение (не команда) — новая задача."""
    await add_task(update, update.message.text)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/list — активные задачи, сгруппированные по приоритету."""
    tasks = db_active_tasks(update.effective_user.id)
    if not tasks:
        await reply(update, "📭 Активных задач нет. Напишите что-нибудь — добавлю!")
        return

    parts = ["📋 Ваши задачи:"]
    for priority, (emoji, name) in PRIORITIES.items():
        group = [t for t in tasks if t["priority"] == priority]
        if group:
            parts.append(f"\n{emoji} {name}:")
            parts.extend(fmt_task(t) for t in group)
    await reply(update, "\n".join(parts))


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/today — напоминания на сегодня + все задачи высокого приоритета."""
    tasks = db_active_tasks(update.effective_user.id)
    today = datetime.now(TZ).date()

    # Задачи с напоминанием на сегодня (по времени)
    with_reminder = sorted(
        (
            t for t in tasks
            if t["remind_at"]
            and datetime.fromtimestamp(t["remind_at"], TZ).date() == today
        ),
        key=lambda t: t["remind_at"],
    )
    reminder_ids = {t["id"] for t in with_reminder}
    # Важные задачи (без дублей тех, что уже в списке напоминаний)
    high = [t for t in tasks if t["priority"] == 1 and t["id"] not in reminder_ids]

    if not with_reminder and not high:
        await reply(update, "🎉 На сегодня ничего срочного нет!")
        return

    parts = ["📅 На сегодня:"]
    if with_reminder:
        parts.append("\n⏰ С напоминанием:")
        for t in with_reminder:
            time_str = datetime.fromtimestamp(t["remind_at"], TZ).strftime("%H:%M")
            emoji = PRIORITIES[t["priority"]][0]
            parts.append(f"{time_str} — {emoji} #{t['num']} {t['text']}")
    if high:
        parts.append("\n🔴 Высокий приоритет:")
        parts.extend(fmt_task(t) for t in high)
    await reply(update, "\n".join(parts))


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/done <номер> — отметить задачу выполненной."""
    if len(context.args) != 1:
        raise UserError("❌ Укажите номер задачи. Пример: /done 3")
    task = get_active_task(update.effective_user.id, parse_number(context.args[0]))

    # Выполненной задаче напоминание больше не нужно
    cancel_job(context.job_queue, task["id"])
    db_update(task["id"], status="done", completed_at=now_ts(), remind_at=None)
    await reply(update, f"🎉 Готово! Задача #{task['num']} выполнена:\n{task['text']}")


async def cmd_completed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/completed — последние 10 выполненных задач."""
    tasks = db_completed_tasks(update.effective_user.id)
    if not tasks:
        await reply(update, "📭 Выполненных задач пока нет.")
        return
    lines = ["🏁 Последние выполненные:"]
    lines.extend(
        f"✅ #{t['num']} {t['text']}  ({fmt_dt(t['completed_at'])})" for t in tasks
    )
    await reply(update, "\n".join(lines))


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/delete <номер> — удалить задачу."""
    if len(context.args) != 1:
        raise UserError("❌ Укажите номер задачи. Пример: /delete 3")
    task = get_user_task(update.effective_user.id, parse_number(context.args[0]))

    cancel_job(context.job_queue, task["id"])
    db_delete(task["id"])
    await reply(update, f"🗑 Задача #{task['num']} удалена.")


async def cmd_priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/priority <номер> <1|2|3> — изменить приоритет."""
    if len(context.args) != 2:
        raise UserError("❌ Формат: /priority <номер> <1|2|3>. Пример: /priority 3 1")
    task = get_active_task(update.effective_user.id, parse_number(context.args[0]))

    if context.args[1] not in ("1", "2", "3"):
        raise UserError("❌ Приоритет — это 1 (🔴), 2 (🟡) или 3 (🟢).")
    priority = int(context.args[1])

    db_update(task["id"], priority=priority)
    emoji, name = PRIORITIES[priority]
    await reply(update, f"🔁 Задача #{task['num']} теперь {emoji} {name.lower()}.")


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/remind <номер> <ЧЧ:ММ> или /remind <номер> <ДД.ММ ЧЧ:ММ>."""
    if len(context.args) < 2:
        raise UserError(
            "❌ Формат: /remind <номер> <ЧЧ:ММ> или /remind <номер> <ДД.ММ ЧЧ:ММ>\n"
            "Пример: /remind 3 18:30"
        )
    task = get_active_task(update.effective_user.id, parse_number(context.args[0]))
    when = parse_remind_time(context.args[1:])

    # Сохраняем в базу (чтобы пережить перезапуск) и планируем в JobQueue
    db_update(task["id"], remind_at=int(when.timestamp()), reminded=0)
    schedule_job(context.job_queue, db_get_task(task["user_id"], task["num"]))
    await reply(
        update,
        f"⏰ Напомню про задачу #{task['num']} — {when.strftime('%d.%m в %H:%M')}.",
    )


async def cmd_unremind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unremind <номер> — отменить напоминание."""
    if len(context.args) != 1:
        raise UserError("❌ Укажите номер задачи. Пример: /unremind 3")
    task = get_active_task(update.effective_user.id, parse_number(context.args[0]))

    if not task["remind_at"] or task["reminded"]:
        raise UserError(f"ℹ️ У задачи #{task['num']} нет активного напоминания.")
    cancel_job(context.job_queue, task["id"])
    db_update(task["id"], remind_at=None, reminded=0)
    await reply(update, f"🔕 Напоминание для задачи #{task['num']} отменено.")


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Неизвестная команда."""
    await reply(update, "🤔 Не знаю такой команды. Список команд — /help")


# ---------------------------------------------------------------------------
# Обработка ошибок
# ---------------------------------------------------------------------------

def safe(handler):
    """
    Обёртка для обработчиков: ошибки пользователя (UserError) показываются
    понятным сообщением, а неожиданные ошибки логируются — бот не падает.
    """
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await handler(update, context)
        except UserError as err:
            await update.message.reply_text(str(err))
        except Exception:
            logger.exception("Ошибка в обработчике %s", handler.__name__)
            await update.message.reply_text(
                "😔 Что-то пошло не так. Попробуйте ещё раз."
            )
    wrapper.__name__ = handler.__name__
    return wrapper


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик ошибок (сетевые сбои и т.п.) — только логируем."""
    logger.error("Необработанное исключение", exc_info=context.error)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN:
        sys.exit("❌ Не задан BOT_TOKEN. Скопируйте .env.example в .env и впишите токен.")

    init_db()

    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(restore_reminders)
        # Увеличенные таймауты: при медленном канале запросы не обрываются
        .connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(30)
        .get_updates_connect_timeout(30).get_updates_read_timeout(40)
        .get_updates_write_timeout(30).get_updates_pool_timeout(30)
    )
    if PROXY_URL:
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)
        logger.info("Используется прокси для Telegram")
    app = builder.build()

    # Команды
    commands = {
        "start": cmd_start,
        "help": cmd_help,
        "add": cmd_add,
        "list": cmd_list,
        "today": cmd_today,
        "done": cmd_done,
        "completed": cmd_completed,
        "delete": cmd_delete,
        "priority": cmd_priority,
        "remind": cmd_remind,
        "unremind": cmd_unremind,
    }
    for name, handler in commands.items():
        app.add_handler(CommandHandler(name, safe(handler)))

    # Обычный текст (не команда) — новая задача
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, safe(on_text)))
    # Любая другая команда — подсказка (должен идти последним)
    app.add_handler(MessageHandler(filters.COMMAND, safe(cmd_unknown)))

    app.add_error_handler(on_error)

    logger.info("Бот запущен. Часовой пояс: %s", TIMEZONE_NAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
