"""
Бот-ассистент для задач (Telegram, python-telegram-bot v21).

Возможности:
  * приём задач обычным сообщением или командой /add;
  * приоритеты через теги !1 / !2 / !3;
  * напоминания через JobQueue (с восстановлением после перезапуска);
  * списки: /list, /today, /completed;
  * у каждого пользователя свой список задач и своя нумерация.

Лабораторная 2 — интеграция с данными:
  * погода из публичного API Open-Meteo (/city, /weather, строка в /today
    и в напоминаниях) — см. модуль weather.py;
  * запросы к SQLite: поиск по задачам (/search) и задачи за неделю (/week);
  * меню команд Telegram: список появляется при вводе "/".

Лабораторная 3 — запуск для реальных пользователей (Railway):
  * опрос /feedback (оценка и 4 вопроса), отзывы хранятся в SQLite и
    пересылаются администратору;
  * статистика использования /stats и список отзывов /feedbacks (только
    для администратора, ADMIN_ID в переменных окружения);
  * путь к базе берётся из DB_PATH (постоянный том хостинга).

Хранение данных — SQLite (файл tasks.db).
Токен и часовой пояс читаются из .env (BOT_TOKEN, TIMEZONE).
"""

import io
import logging
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import weather  # модуль интеграции с Open-Meteo (лаб. 2)

# ---------------------------------------------------------------------------
# Настройки и логирование
# ---------------------------------------------------------------------------

load_dotenv()  # читаем переменные из файла .env

BOT_TOKEN = os.getenv("BOT_TOKEN")
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Moscow")
# Путь к базе. На хостинге указывает на постоянный том (например /data/tasks.db),
# чтобы задачи не пропадали при обновлении бота.
DB_PATH = os.getenv("DB_PATH", "tasks.db")
# Telegram ID администратора: ему доступны /stats и /feedbacks, ему приходят отзывы
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "") or 0) or None
except ValueError:
    ADMIN_ID = None
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
    # Встроенный LOWER() в SQLite не понимает кириллицу — подключаем свой
    conn.create_function("py_lower", 1, lambda value: value.lower() if value else value)
    return conn


def init_db() -> None:
    """Создаёт таблицы, если их ещё нет."""
    # Папка для базы должна существовать (например, том /data на хостинге)
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
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
        # Настройки пользователя: город для погоды (лаб. 2)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                city    TEXT,   -- красивое название места
                lat     REAL,
                lon     REAL
            )
            """
        )
        # Отзывы пользователей (лаб. 3)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                username   TEXT,
                rating     INTEGER NOT NULL,    -- оценка 1-5
                liked      TEXT,                -- что понравилось
                disliked   TEXT,                -- что не понравилось
                improve    TEXT,                -- что улучшить
                would_use  TEXT,                -- yes / maybe_yes / maybe_no / no
                created_at INTEGER NOT NULL
            )
            """
        )
        # Миграция (лаб. 3): повтор задачи ('daily' — каждый день). Старые базы
        # получают колонку без потери данных.
        columns = [r["name"] for r in conn.execute("PRAGMA table_info(tasks)")]
        if "repeat" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN repeat TEXT")
        # Журнал использования: кто и какую команду вызвал (для /stats)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                command TEXT    NOT NULL,
                ts      INTEGER NOT NULL
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


def db_get_user(user_id: int):
    """Настройки пользователя (город) или None."""
    with closing(db_connect()) as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()


def db_set_city(user_id: int, city: str, lat: float, lon: float) -> None:
    """Сохраняет город пользователя (создаёт или обновляет запись)."""
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO users (user_id, city, lat, lon) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "city = excluded.city, lat = excluded.lat, lon = excluded.lon",
            (user_id, city, lat, lon),
        )


def db_clear_city(user_id: int) -> None:
    """Отключает погоду: забывает город пользователя."""
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "UPDATE users SET city = NULL, lat = NULL, lon = NULL WHERE user_id = ?",
            (user_id,),
        )


def db_progress_rows(user_id: int, days: int = 60):
    """Задачи пользователя, созданные или выполненные за последние days дней."""
    cutoff = now_ts() - days * 24 * 3600
    with closing(db_connect()) as conn:
        return conn.execute(
            "SELECT created_at, completed_at, status FROM tasks WHERE user_id = ? "
            "AND (created_at >= ? OR completed_at >= ?)",
            (user_id, cutoff, cutoff),
        ).fetchall()


def db_calendar_tasks(user_id: int):
    """Активные задачи с напоминанием — для экспорта в календарь."""
    with closing(db_connect()) as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND status = 'active' "
            "AND remind_at IS NOT NULL ORDER BY remind_at",
            (user_id,),
        ).fetchall()


def db_search_tasks(user_id: int, query: str, limit: int = 20):
    """Ищет задачи пользователя (активные и выполненные) по части текста."""
    # Экранируем служебные символы LIKE, чтобы % и _ искались как обычный текст
    escaped = (
        query.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    with closing(db_connect()) as conn:
        return conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? "
            "AND py_lower(text) LIKE ? ESCAPE '\\' "
            "ORDER BY num DESC LIMIT ?",
            (user_id, f"%{escaped}%", limit),
        ).fetchall()


def db_week_tasks(user_id: int, limit: int = 30):
    """Задачи за последние 7 дней: (созданные, выполненные)."""
    cutoff = now_ts() - 7 * 24 * 3600
    with closing(db_connect()) as conn:
        created = conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, cutoff, limit),
        ).fetchall()
        done = conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND status = 'done' "
            "AND completed_at >= ? ORDER BY completed_at DESC LIMIT ?",
            (user_id, cutoff, limit),
        ).fetchall()
    return created, done


def log_event(user_id: int, command: str) -> None:
    """Записывает использование команды. Сбой записи не должен ломать бота."""
    try:
        with closing(db_connect()) as conn, conn:
            conn.execute(
                "INSERT INTO events (user_id, command, ts) VALUES (?, ?, ?)",
                (user_id, command, now_ts()),
            )
    except Exception:
        logger.warning("Не удалось записать событие %s", command, exc_info=True)


def db_add_feedback(user_id: int, username: str, data: dict) -> None:
    """Сохраняет отзыв пользователя."""
    with closing(db_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO feedback (user_id, username, rating, liked, disliked, "
            "improve, would_use, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, username, data["rating"], data.get("liked", ""),
                data.get("disliked", ""), data.get("improve", ""),
                data.get("would_use", ""), now_ts(),
            ),
        )


def db_last_feedback(limit: int = 10):
    """Последние отзывы (для администратора)."""
    with closing(db_connect()) as conn:
        return conn.execute(
            "SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def db_stats() -> dict:
    """Сводная статистика использования бота (для /stats)."""
    now = now_ts()
    admin = ADMIN_ID or 0
    with closing(db_connect()) as conn:
        def one(sql, params=()):
            return conn.execute(sql, params).fetchone()[0]

        stats = {
            "users_total": one("SELECT COUNT(DISTINCT user_id) FROM events"),
            "users_others": one(
                "SELECT COUNT(DISTINCT user_id) FROM events WHERE user_id != ?",
                (admin,),
            ),
            "users_24h": one(
                "SELECT COUNT(DISTINCT user_id) FROM events WHERE ts >= ?",
                (now - 24 * 3600,),
            ),
            "users_7d": one(
                "SELECT COUNT(DISTINCT user_id) FROM events WHERE ts >= ?",
                (now - 7 * 24 * 3600,),
            ),
            "events_total": one("SELECT COUNT(*) FROM events"),
            "tasks_total": one("SELECT COUNT(*) FROM tasks"),
            "tasks_done": one("SELECT COUNT(*) FROM tasks WHERE status = 'done'"),
            "feedback_count": one("SELECT COUNT(*) FROM feedback"),
            "avg_rating": one("SELECT AVG(rating) FROM feedback"),
            "top_commands": conn.execute(
                "SELECT command, COUNT(*) AS n FROM events "
                "GROUP BY command ORDER BY n DESC, command LIMIT 8"
            ).fetchall(),
            "would_use": conn.execute(
                "SELECT would_use, COUNT(*) AS n FROM feedback "
                "GROUP BY would_use ORDER BY n DESC"
            ).fetchall(),
        }
    return stats


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
    if task["repeat"] == "daily":
        line += "  🔁 каждый день"
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


def next_daily_ts(remind_at: int, now: int | None = None) -> int:
    """Следующее срабатывание ежедневной задачи: прежнее время + N суток, строго после now."""
    now = now_ts() if now is None else now
    if remind_at > now:
        return remind_at
    # Календарные сутки в часовом поясе бота (переход на летнее время учитывается)
    current = datetime.fromtimestamp(remind_at, TZ)
    while int(current.timestamp()) <= now:
        current = (current + timedelta(days=1)).replace(tzinfo=TZ)
    return int(current.timestamp())


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

    # Если у пользователя задан город — добавляем краткую погоду.
    # Сбой погоды не должен мешать напоминанию.
    try:
        info = await user_weather(task["user_id"])
        if info:
            text += "\n\n" + weather.format_short(*info)
    except weather.WeatherError as err:
        logger.info("Погода для напоминания недоступна: %s", err)

    if task["repeat"] == "daily":
        text += "\n🔁 Ежедневная задача. Отметить на сегодня — /done " + str(task["num"])

    try:
        await context.bot.send_message(chat_id=task["chat_id"], text=text)
        if task["repeat"] == "daily":
            # Ежедневная задача: назначаем следующее напоминание (строго в будущем)
            next_ts = next_daily_ts(task["remind_at"])
            db_update(task_id, remind_at=next_ts, reminded=0)
            schedule_job(context.job_queue, db_get_task(task["user_id"], task["num"]))
        else:
            db_update(task_id, reminded=1)
    except Exception:
        logger.exception("Не удалось отправить напоминание по задаче id=%s", task_id)


# Команды, которые Telegram показывает при вводе "/" и в кнопке меню
BOT_COMMANDS = [
    BotCommand("add", "Добавить задачу"),
    BotCommand("list", "Активные задачи по приоритетам"),
    BotCommand("today", "План на сегодня"),
    BotCommand("plan", "План: сегодня, завтра, неделя, потом"),
    BotCommand("progress", "Прогресс за неделю и серия дней"),
    BotCommand("calendar", "Выгрузить напоминания в календарь"),
    BotCommand("weather", "Погода (по желанию)"),
    BotCommand("city", "Задать город для погоды или отключить"),
    BotCommand("remind", "Поставить напоминание"),
    BotCommand("done", "Отметить задачу выполненной"),
    BotCommand("week", "Задачи за последние 7 дней"),
    BotCommand("search", "Поиск по задачам"),
    BotCommand("completed", "Последние выполненные"),
    BotCommand("priority", "Сменить приоритет"),
    BotCommand("unremind", "Отменить напоминание"),
    BotCommand("delete", "Удалить задачу"),
    BotCommand("feedback", "Оставить отзыв о боте"),
    BotCommand("help", "Список команд с примерами"),
    BotCommand("start", "Приветствие"),
]


# Команды только для администратора (показываются в его меню)
ADMIN_COMMANDS = [
    BotCommand("stats", "Статистика использования (админ)"),
    BotCommand("feedbacks", "Последние отзывы (админ)"),
]


async def setup_menu(application: Application) -> None:
    """Регистрирует меню команд в Telegram (подсказки при вводе "/")."""
    try:
        await application.bot.set_my_commands(BOT_COMMANDS)
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info("Меню команд зарегистрировано (%d команд)", len(BOT_COMMANDS))
    except Exception:
        # Меню — удобство, а не необходимость: бот работает и без него
        logger.warning("Не удалось зарегистрировать меню команд", exc_info=True)

    # Дополнительные команды в меню администратора
    if ADMIN_ID:
        try:
            await application.bot.set_my_commands(
                BOT_COMMANDS + ADMIN_COMMANDS, scope=BotCommandScopeChat(ADMIN_ID)
            )
        except Exception:
            logger.warning("Не удалось зарегистрировать меню администратора", exc_info=True)


async def on_startup(application: Application) -> None:
    """Выполняется один раз при запуске бота."""
    await restore_reminders(application)
    await setup_menu(application)


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
    "🔁 /remind <номер> daily <ЧЧ:ММ> — каждый день (/remind 3 daily 20:00)\n"
    "   «/done» для такой задачи отмечает день, задача остаётся\n"
    "🔕 /unremind <номер> — отменить напоминание\n\n"
    "🔎 /search <слово> — поиск по задачам (/search молоко)\n"
    "🗂 /plan — план: сегодня, завтра, на неделе, потом, без срока\n"
    "🗓 /week — задачи за последние 7 дней\n"
    "📈 /progress — график выполненных задач за неделю и серия дней\n"
    "📆 /calendar — файл .ics: задачи с напоминанием в ваш календарь\n\n"
    "🌦 Погода — по желанию (Open-Meteo):\n"
    "📍 /city <город> — включить погоду в /today и напоминаниях\n"
    "📍 /city off — отключить погоду\n"
    "🌤 /weather — погода сейчас; /weather <город> — для другого города\n\n"
    "📝 /feedback — оставить отзыв о боте (короткий опрос)"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — приветствие."""
    await reply(
        update,
        "👋 Привет! Я бот-ассистент для задач.\n\n"
        "Просто напишите задачу — я её запомню. "
        "Добавьте !1, !2 или !3, чтобы задать приоритет.\n"
        "Список команд — /help или введите «/».\n"
        "Напоминания можно выгрузить в календарь (/calendar), "
        "а успехи за неделю посмотреть в /progress.\n"
        "Нужна погода? Это по желанию: /city Москва.\n"
        "Понравился бот? Оставьте отзыв: /feedback",
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


async def user_weather(user_id: int):
    """
    Погода для города пользователя: (название места, данные) или None,
    если город не задан. Ошибки API пробрасываются как WeatherError.
    """
    row = db_get_user(user_id)
    if row is None or row["lat"] is None:
        return None
    return row["city"], await weather.get_weather(row["lat"], row["lon"])


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/today — погода, напоминания на сегодня и все задачи высокого приоритета."""
    user_id = update.effective_user.id
    tasks = db_active_tasks(user_id)
    today = datetime.now(TZ).date()

    # Строка с погодой (если город задан); сбой API не ломает команду
    weather_line = None
    try:
        info = await user_weather(user_id)
        if info:
            weather_line = weather.format_short(*info)
    except weather.WeatherError as err:
        logger.info("Погода для /today недоступна: %s", err)
        weather_line = "🌡 Погода сейчас недоступна."

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

    parts = ["📅 На сегодня:"]
    if weather_line:  # погода — по желанию: без города строки нет
        parts.append(weather_line)
    if not with_reminder and not high:
        parts.append("\n🎉 Ничего срочного нет!")
        await reply(update, "\n".join(parts))
        return

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

    # Ежедневная задача: фиксируем выполнение за сегодня копией и оставляем задачу
    if task["repeat"] == "daily":
        copy_num = db_add_task(task["user_id"], task["chat_id"], task["text"], task["priority"])
        copy = db_get_task(task["user_id"], copy_num)
        db_update(copy["id"], status="done", completed_at=now_ts())
        when = datetime.fromtimestamp(task["remind_at"], TZ).strftime("%H:%M") if task["remind_at"] else ""
        await reply(
            update,
            f"🎉 Готово на сегодня! «{task['text']}»\n"
            f"🔁 Задача останется и повторится завтра{' в ' + when if when else ''}. "
            f"Остановить повтор — /unremind {task['num']}, удалить — /delete {task['num']}.",
        )
        return

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


DAILY_WORDS = ("daily", "ежедневно", "каждыйдень")


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/remind <номер> <ЧЧ:ММ> или /remind <номер> <ДД.ММ ЧЧ:ММ>."""
    if len(context.args) < 2:
        raise UserError(
            "❌ Формат: /remind <номер> <ЧЧ:ММ> или /remind <номер> <ДД.ММ ЧЧ:ММ>\n"
            "Каждый день: /remind <номер> daily <ЧЧ:ММ>\n"
            "Пример: /remind 3 18:30"
        )
    task = get_active_task(update.effective_user.id, parse_number(context.args[0]))

    # Ежедневное напоминание: /remind 3 daily 20:00
    if context.args[1].lower() in DAILY_WORDS:
        if len(context.args) != 3:
            raise UserError("❌ Формат: /remind <номер> daily <ЧЧ:ММ>\nПример: /remind 3 daily 20:00")
        when = parse_remind_time(context.args[2:])
        db_update(task["id"], remind_at=int(when.timestamp()), reminded=0, repeat="daily")
        schedule_job(context.job_queue, db_get_task(task["user_id"], task["num"]))
        await reply(
            update,
            f"🔁 Буду напоминать про задачу #{task['num']} каждый день в "
            f"{when.strftime('%H:%M')}. Первое напоминание — {when.strftime('%d.%m в %H:%M')}.\n"
            f"Отметить на сегодня — /done {task['num']}, остановить повтор — /unremind {task['num']}.",
        )
        return

    when = parse_remind_time(context.args[1:])

    # Сохраняем в базу (чтобы пережить перезапуск) и планируем в JobQueue.
    # Обычное напоминание отключает ежедневный повтор.
    db_update(task["id"], remind_at=int(when.timestamp()), reminded=0, repeat=None)
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

    if not task["remind_at"] or (task["reminded"] and task["repeat"] != "daily"):
        raise UserError(f"ℹ️ У задачи #{task['num']} нет активного напоминания.")
    cancel_job(context.job_queue, task["id"])
    db_update(task["id"], remind_at=None, reminded=0, repeat=None)
    await reply(update, f"🔕 Напоминание для задачи #{task['num']} отменено.")


async def cmd_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/city <город> — запомнить город для погоды (поиск через геокодинг)."""
    user_id = update.effective_user.id
    if not context.args:
        row = db_get_user(user_id)
        if row and row["city"]:
            await reply(update, f"📍 Ваш город: {row['city']}\nСменить: /city <город>")
            return
        raise UserError("❌ Укажите город. Пример: /city Москва")

    if " ".join(context.args).strip().lower() in ("off", "выкл", "откл", "отключить"):
        db_clear_city(user_id)
        await reply(update, "🔕 Погода отключена. Включить снова: /city <город>")
        return

    try:
        city = await weather.geocode(" ".join(context.args))
    except weather.WeatherError as err:
        raise UserError(str(err)) from err

    place = weather.place_name(city)
    db_set_city(user_id, place, city["lat"], city["lon"])
    await reply(update, f"📍 Город сохранён: {place}\nПогода — /weather")


async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/weather — погода в городе пользователя; /weather <город> — в другом."""
    try:
        if context.args:
            # Разовый запрос для другого города (город пользователя не меняется)
            city = await weather.geocode(" ".join(context.args))
            place = weather.place_name(city)
            data = await weather.get_weather(city["lat"], city["lon"])
        else:
            info = await user_weather(update.effective_user.id)
            if info is None:
                raise UserError("📍 Сначала задайте город: /city Москва")
            place, data = info
    except weather.WeatherError as err:
        raise UserError(str(err)) from err

    await reply(update, weather.format_full(place, data))


PLAN_GROUPS = [
    ("today", "📍 Сегодня"),
    ("tomorrow", "🌅 Завтра"),
    ("week", "🗓 На неделе (до 7 дней)"),
    ("later", "📦 Потом (дальше недели)"),
    ("nodate", "🗒 Без срока"),
]


def plan_group(task, today) -> str:
    """В какую группу плана попадает задача — по дате ближайшего напоминания."""
    if not task["remind_at"] or (task["reminded"] and task["repeat"] != "daily"):
        return "nodate"
    day = datetime.fromtimestamp(task["remind_at"], TZ).date()
    if day <= today:  # просроченные напоминания тоже показываем сегодня
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    if day <= today + timedelta(days=7):
        return "week"
    return "later"


def build_plan(tasks, today) -> dict:
    """Раскладывает активные задачи по группам плана (внутри группы: время, приоритет, номер)."""
    groups = {key: [] for key, _ in PLAN_GROUPS}
    for task in tasks:
        groups[plan_group(task, today)].append(task)
    for items in groups.values():
        items.sort(key=lambda t: (t["remind_at"] or 0, t["priority"], t["num"]))
    # Без срока: сначала по приоритету
    groups["nodate"].sort(key=lambda t: (t["priority"], t["num"]))
    return groups


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/plan — задачи по срокам: сегодня, завтра, на неделе, потом, без срока."""
    tasks = db_active_tasks(update.effective_user.id)
    if not tasks:
        await reply(update, "📭 Активных задач нет. Напишите что-нибудь — добавлю!")
        return
    groups = build_plan(tasks, datetime.now(TZ).date())
    parts = ["🗂 План по срокам:"]
    for key, title in PLAN_GROUPS:
        items = groups[key]
        if not items:
            continue
        parts.append(f"\n{title} ({len(items)}):")
        for t in items:
            emoji = PRIORITIES[t["priority"]][0]
            when = ""
            if key != "nodate" and t["remind_at"]:
                fmt = "%H:%M" if key == "today" else "%d.%m %H:%M"
                when = datetime.fromtimestamp(t["remind_at"], TZ).strftime(fmt) + " — "
            repeat = " 🔁" if t["repeat"] == "daily" else ""
            parts.append(f"{when}{emoji} #{t['num']} {t['text']}{repeat}")
    parts.append("\nДобавить срок: /remind <номер> <ЧЧ:ММ>, каждый день: /remind <номер> daily <ЧЧ:ММ>")
    await reply(update, "\n".join(parts))


BAR_MAX = 10  # максимальная длина «столбика» на графике


def bar(value: int) -> str:
    """Текстовый столбик: ███░░ (не длиннее BAR_MAX)."""
    filled = min(value, BAR_MAX)
    return "█" * filled + "░" * (BAR_MAX - filled) if filled else "·"


def progress_stats(rows, today, days: int = 7):
    """
    Считает данные для /progress по строкам задач.
    Возвращает (по_дням, создано, выполнено, серия), где по_дням — список
    (дата, создано, выполнено) от старой даты к новой; серия — число дней подряд
    (до сегодня включительно, а если сегодня ничего — до вчера) с выполненной задачей.
    """
    created_by, done_by = {}, {}
    for row in rows:
        c_day = datetime.fromtimestamp(row["created_at"], TZ).date()
        created_by[c_day] = created_by.get(c_day, 0) + 1
        if row["status"] == "done" and row["completed_at"]:
            d_day = datetime.fromtimestamp(row["completed_at"], TZ).date()
            done_by[d_day] = done_by.get(d_day, 0) + 1

    per_day = []
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        per_day.append((day, created_by.get(day, 0), done_by.get(day, 0)))

    total_created = sum(c for _, c, _ in per_day)
    total_done = sum(d for _, _, d in per_day)

    streak = 0
    day = today if done_by.get(today) else today - timedelta(days=1)
    while done_by.get(day):
        streak += 1
        day -= timedelta(days=1)
    return per_day, total_created, total_done, streak


WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


async def cmd_progress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/progress — выполненные задачи за 7 дней по дням и серия дней подряд."""
    rows = db_progress_rows(update.effective_user.id)
    today = datetime.now(TZ).date()
    per_day, created, done, streak = progress_stats(rows, today)
    if created == 0 and done == 0:
        await reply(
            update,
            "📈 За последние 7 дней пока нет задач. Добавьте задачу — и здесь "
            "появится график выполнения.",
        )
        return
    lines = ["📈 Прогресс за 7 дней (выполнено задач в день):", ""]
    for day, _, d in per_day:
        lines.append(f"{day.strftime('%d.%m')} {WEEKDAYS[day.weekday()]}  {bar(d)} {d}")
    percent = round(done * 100 / created) if created else 0
    lines.append("")
    lines.append(f"✅ Выполнено: {done}  ·  📝 Создано: {created}")
    if created:
        lines.append(f"🎯 Доля выполненных за неделю: {percent}%")
    if streak >= 2:
        lines.append(f"🔥 Серия: {streak} дн. подряд")
    elif streak == 1:
        lines.append("🔥 Серия: 1 день — продолжайте завтра!")
    else:
        lines.append("🔥 Серия: пока нет — выполните задачу сегодня")
    await reply(update, "\n".join(lines))


def ics_escape(text: str) -> str:
    """Экранирует текст для поля iCalendar (RFC 5545)."""
    return (
        text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
        .replace("\r", "").replace("\n", "\\n")
    )


def ics_fold(line: str) -> list[str]:
    """Переносит длинную строку по правилу iCalendar: не более 75 байт в строке."""
    parts, current = [], ""
    for ch in line:
        limit = 75 if not parts else 74  # у продолжений впереди пробел
        if len((current + ch).encode("utf-8")) > limit:
            parts.append(current)
            current = ch
        else:
            current += ch
    parts.append(current)
    return [parts[0]] + [" " + part for part in parts[1:]]


def build_ics(tasks) -> str:
    """Файл календаря .ics: каждая задача с напоминанием — событие на 30 минут."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//task-bot//itmoftmi_krb_bot//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    for task in tasks:
        start = datetime.fromtimestamp(task["remind_at"], timezone.utc)
        end = start + timedelta(minutes=30)
        emoji = PRIORITIES[task["priority"]][0]
        lines += [
            "BEGIN:VEVENT",
            f"UID:task-{task['user_id']}-{task['num']}@task-bot",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}",
            f"SUMMARY:{ics_escape(emoji + ' ' + task['text'])}",
            f"DESCRIPTION:{ics_escape('Задача #' + str(task['num']) + ' из бота')}",
        ]
        if task["repeat"] == "daily":
            lines.append("RRULE:FREQ=DAILY")  # ежедневная задача повторяется в календаре
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    folded = []
    for line in lines:
        folded.extend(ics_fold(line))
    return "\r\n".join(folded) + "\r\n"


async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/calendar — отправляет файл .ics с задачами, у которых есть напоминание."""
    tasks = db_calendar_tasks(update.effective_user.id)
    if not tasks:
        raise UserError(
            "📆 Нет задач с напоминанием. Поставьте напоминание, например "
            "/remind 1 18:30, и повторите /calendar."
        )
    data = build_ics(tasks).encode("utf-8")
    document = io.BytesIO(data)
    document.name = "tasks.ics"
    await update.message.reply_document(
        document,
        caption=(
            f"📆 Задач в календаре: {len(tasks)}. Откройте файл — события добавятся "
            "в ваш календарь (Google, Apple, Outlook)."
        ),
    )


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/search <слово> — поиск по задачам (активным и выполненным)."""
    query = " ".join(context.args).strip()
    if not query:
        raise UserError("❌ Что искать? Пример: /search молоко")
    if len(query) > 100:
        raise UserError("❌ Слишком длинный запрос (максимум 100 символов).")

    limit = 20
    tasks = db_search_tasks(update.effective_user.id, query, limit)
    if not tasks:
        await reply(update, f"🔎 По запросу «{query}» ничего не найдено.")
        return

    lines = [f"🔎 Найдено по запросу «{query}»: {len(tasks)}"]
    for t in tasks:
        icon = "✅" if t["status"] == "done" else PRIORITIES[t["priority"]][0]
        lines.append(f"{icon} #{t['num']} {t['text']}")
    if len(tasks) == limit:
        lines.append(f"(показаны первые {limit}, уточните запрос)")
    await reply(update, "\n".join(lines))


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/week — задачи, созданные и выполненные за последние 7 дней."""
    created, done = db_week_tasks(update.effective_user.id)
    if not created and not done:
        await reply(update, "🗓 За последние 7 дней задач не было.")
        return

    lines = ["🗓 За последние 7 дней:"]
    if created:
        lines.append(f"\n🆕 Создано ({len(created)}):")
        lines.extend(
            f"{PRIORITIES[t['priority']][0]} #{t['num']} {t['text']}  "
            f"({fmt_dt(t['created_at'])})"
            for t in created
        )
    if done:
        lines.append(f"\n✅ Выполнено ({len(done)}):")
        lines.extend(
            f"#{t['num']} {t['text']}  ({fmt_dt(t['completed_at'])})" for t in done
        )
    await reply(update, "\n".join(lines))


# ---------------------------------------------------------------------------
# Обратная связь и статистика (лаб. 3)
# ---------------------------------------------------------------------------

# Шаги опроса /feedback
FB_RATING, FB_LIKED, FB_DISLIKED, FB_IMPROVE, FB_WOULD_USE = range(5)

WOULD_USE_LABELS = {
    "yes": "да, точно",
    "maybe_yes": "скорее да",
    "maybe_no": "скорее нет",
    "no": "нет",
}
MAX_FEEDBACK_LEN = 1000  # максимальная длина одного ответа в опросе


def is_admin(user_id: int) -> bool:
    """Администратор задаётся переменной ADMIN_ID."""
    return ADMIN_ID is not None and user_id == ADMIN_ID


def clean_feedback_text(raw: str) -> str:
    """Чистит ответ: лишние пробелы, ограничение длины, «-» означает «пропустить»."""
    text = " ".join(raw.split())[:MAX_FEEDBACK_LEN]
    return "" if text in ("-", "—") else text


async def cmd_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/feedback — короткий опрос: оценка и 4 вопроса о боте."""
    log_event(update.effective_user.id, "feedback")
    context.user_data["fb"] = {}
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"{i}⭐", callback_data=f"fb_rate:{i}") for i in range(1, 6)]]
    )
    await update.message.reply_text(
        "📝 Короткий опрос — он поможет улучшить бота.\n"
        "Отмена в любой момент — /cancel.\n\n"
        "Как вы оцениваете бота от 1 до 5?",
        reply_markup=keyboard,
    )
    return FB_RATING


async def fb_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 1: оценка кнопкой."""
    query = update.callback_query
    await query.answer()
    rating = int(query.data.split(":")[1])
    context.user_data.setdefault("fb", {})["rating"] = rating
    await query.edit_message_text(f"Оценка: {rating}⭐")
    await context.bot.send_message(
        query.message.chat_id,
        "👍 Что вам понравилось? (если нечего сказать — отправьте «-»)",
    )
    return FB_LIKED


async def fb_liked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 2: что понравилось."""
    context.user_data.setdefault("fb", {})["liked"] = clean_feedback_text(update.message.text)
    await update.message.reply_text(
        "👎 Что не понравилось или было неудобно? (или «-»)"
    )
    return FB_DISLIKED


async def fb_disliked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 3: что не понравилось."""
    context.user_data.setdefault("fb", {})["disliked"] = clean_feedback_text(update.message.text)
    await update.message.reply_text("💡 Что бы вы улучшили или добавили? (или «-»)")
    return FB_IMPROVE


async def fb_improve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 4: что улучшить, затем вопрос про будущее использование."""
    context.user_data.setdefault("fb", {})["improve"] = clean_feedback_text(update.message.text)
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Да, точно", callback_data="fb_use:yes"),
             InlineKeyboardButton("Скорее да", callback_data="fb_use:maybe_yes")],
            [InlineKeyboardButton("Скорее нет", callback_data="fb_use:maybe_no"),
             InlineKeyboardButton("Нет", callback_data="fb_use:no")],
        ]
    )
    await update.message.reply_text(
        "🔁 Хотели бы вы пользоваться таким ботом дальше?", reply_markup=keyboard
    )
    return FB_WOULD_USE


async def fb_would_use(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Шаг 5: ответ кнопкой — сохраняем отзыв и благодарим."""
    query = update.callback_query
    await query.answer()
    key = query.data.split(":")[1]
    data = context.user_data.pop("fb", {})
    if "rating" not in data:  # кнопка от старого опроса
        await query.edit_message_text("Опрос устарел. Начните заново: /feedback")
        return ConversationHandler.END
    data["would_use"] = key

    user = update.effective_user
    name = user.username or user.full_name or str(user.id)
    db_add_feedback(user.id, name, data)
    await query.edit_message_text(f"Будете пользоваться: {WOULD_USE_LABELS[key]}")
    await context.bot.send_message(
        query.message.chat_id, "🙏 Спасибо за отзыв! Он поможет сделать бота лучше."
    )
    await notify_admin_feedback(context, user, name, data)
    return ConversationHandler.END


async def notify_admin_feedback(context, user, name: str, data: dict) -> None:
    """Пересылает новый отзыв администратору (если это не он сам)."""
    if ADMIN_ID is None or user.id == ADMIN_ID:
        return
    text = (
        f"📝 Новый отзыв от {name} (id {user.id})\n"
        f"Оценка: {data['rating']}⭐\n"
        f"👍 {data.get('liked') or '—'}\n"
        f"👎 {data.get('disliked') or '—'}\n"
        f"💡 {data.get('improve') or '—'}\n"
        f"🔁 Будет пользоваться: {WOULD_USE_LABELS.get(data.get('would_use'), '—')}"
    )
    try:
        await context.bot.send_message(ADMIN_ID, text)
    except Exception:
        logger.warning("Не удалось отправить отзыв администратору", exc_info=True)


async def fb_need_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Текст вместо кнопки оценки."""
    await update.message.reply_text("Нажмите кнопку с оценкой выше или отправьте /cancel.")
    return FB_RATING


async def fb_need_would_use(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Текст вместо кнопки последнего вопроса."""
    await update.message.reply_text("Выберите вариант кнопкой выше или отправьте /cancel.")
    return FB_WOULD_USE


async def fb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel во время опроса."""
    context.user_data.pop("fb", None)
    await update.message.reply_text("Опрос отменён. Вернуться к нему можно командой /feedback.")
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel вне опроса."""
    await reply(update, "Сейчас нечего отменять.")


async def stale_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка опроса, который уже не активен (например, после перезапуска бота)."""
    await update.callback_query.answer(
        "Опрос устарел. Начните заново: /feedback", show_alert=True
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats — статистика использования (только администратор)."""
    if not is_admin(update.effective_user.id):
        raise UserError("⛔ Эта команда доступна только администратору.")
    st = db_stats()
    lines = [
        "📊 Статистика бота",
        f"👥 Пользователей: {st['users_total']} (кроме вас: {st['users_others']})",
        f"🕒 Активных за 24 часа: {st['users_24h']}, за 7 дней: {st['users_7d']}",
        f"⌨️ Обращений к боту: {st['events_total']}",
        f"📝 Задач создано: {st['tasks_total']}, выполнено: {st['tasks_done']}",
    ]
    if st["top_commands"]:
        lines.append("\n🔝 Популярные команды:")
        lines.extend(f"/{row['command']} — {row['n']}" for row in st["top_commands"])
    lines.append(f"\n💬 Отзывов: {st['feedback_count']}")
    if st["feedback_count"]:
        lines.append(f"⭐ Средняя оценка: {st['avg_rating']:.1f}")
        lines.append(
            "🔁 Будут пользоваться: "
            + ", ".join(
                f"{WOULD_USE_LABELS.get(row['would_use'], '—')} — {row['n']}"
                for row in st["would_use"]
            )
        )
    await reply(update, "\n".join(lines))


async def cmd_feedbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/feedbacks — последние отзывы (только администратор)."""
    if not is_admin(update.effective_user.id):
        raise UserError("⛔ Эта команда доступна только администратору.")
    rows = db_last_feedback()
    if not rows:
        await reply(update, "💬 Отзывов пока нет.")
        return
    lines = [f"💬 Последние отзывы ({len(rows)}):"]
    for row in rows:
        lines.append(
            f"\n#{row['id']} {fmt_dt(row['created_at'])} — {row['username']}, "
            f"{row['rating']}⭐\n"
            f"👍 {row['liked'] or '—'}\n"
            f"👎 {row['disliked'] or '—'}\n"
            f"💡 {row['improve'] or '—'}\n"
            f"🔁 {WOULD_USE_LABELS.get(row['would_use'], '—')}"
        )
    await reply(update, "\n".join(lines))


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
    Заодно записывает использование команды для статистики (/stats).
    """
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        name = handler.__name__
        name = name[4:] if name.startswith("cmd_") else {"on_text": "text"}.get(name, name)
        if update.effective_user:
            log_event(update.effective_user.id, name)
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


def guard(handler):
    """
    Обёртка для шагов опроса (кнопки и диалог): неожиданная ошибка логируется,
    пользователь получает сообщение, диалог завершается — бот не падает.
    """
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await handler(update, context)
        except Exception:
            logger.exception("Ошибка в обработчике %s", handler.__name__)
            context.user_data.pop("fb", None)
            if update.effective_chat:
                try:
                    await context.bot.send_message(
                        update.effective_chat.id,
                        "😔 Что-то пошло не так. Попробуйте ещё раз: /feedback",
                    )
                except Exception:
                    logger.warning("Не удалось отправить сообщение об ошибке")
            return ConversationHandler.END
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
        .post_init(on_startup)
        # Увеличенные таймауты: при медленном канале запросы не обрываются
        .connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(30)
        .get_updates_connect_timeout(30).get_updates_read_timeout(40)
        .get_updates_write_timeout(30).get_updates_pool_timeout(30)
    )
    if PROXY_URL:
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)
        logger.info("Используется прокси для Telegram")
    app = builder.build()

    # Опрос /feedback (диалог из нескольких шагов). Регистрируется первым,
    # чтобы ответы на вопросы не превращались в новые задачи.
    text_only = filters.TEXT & ~filters.COMMAND
    feedback_conv = ConversationHandler(
        entry_points=[CommandHandler("feedback", guard(cmd_feedback))],
        states={
            FB_RATING: [
                CallbackQueryHandler(guard(fb_rating), pattern=r"^fb_rate:[1-5]$"),
                MessageHandler(text_only, guard(fb_need_rating)),
            ],
            FB_LIKED: [MessageHandler(text_only, guard(fb_liked))],
            FB_DISLIKED: [MessageHandler(text_only, guard(fb_disliked))],
            FB_IMPROVE: [MessageHandler(text_only, guard(fb_improve))],
            FB_WOULD_USE: [
                CallbackQueryHandler(
                    guard(fb_would_use), pattern=r"^fb_use:(yes|maybe_yes|maybe_no|no)$"
                ),
                MessageHandler(text_only, guard(fb_need_would_use)),
            ],
        },
        fallbacks=[CommandHandler("cancel", guard(fb_cancel))],
        allow_reentry=True,
        conversation_timeout=1800,  # забытый опрос закрывается через 30 минут
    )
    app.add_handler(feedback_conv)
    # Кнопки устаревшего опроса (после перезапуска бота) — понятный ответ
    app.add_handler(CallbackQueryHandler(stale_button, pattern=r"^fb_"))

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
        "city": cmd_city,
        "weather": cmd_weather,
        "search": cmd_search,
        "week": cmd_week,
        "cancel": cmd_cancel,
        "stats": cmd_stats,
        "feedbacks": cmd_feedbacks,
        "plan": cmd_plan,
        "progress": cmd_progress,
        "calendar": cmd_calendar,
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
