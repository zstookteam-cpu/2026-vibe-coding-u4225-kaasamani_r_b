"""
Автотесты логики бота (лаб. 2–3). Запуск:  python -m unittest -v test_logic

Сеть не нужна: ответы Open-Meteo и Telegram подменяются заглушками,
база данных создаётся во временном файле.
"""

import os
import re
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

os.environ.setdefault("BOT_TOKEN", "test-token")

import bot  # noqa: E402
import weather  # noqa: E402

GEO_OK = {
    "results": [
        {
            "name": "Санкт-Петербург",
            "latitude": 59.93863,
            "longitude": 30.31413,
            "country": "Россия",
            "admin1": "Санкт-Петербург",
        }
    ]
}

FORECAST_OK = {
    "current": {
        "temperature_2m": 8.4,
        "apparent_temperature": 5.1,
        "precipitation": 0.0,
        "weather_code": 3,
        "wind_speed_10m": 4.2,
    },
    "daily": {
        "temperature_2m_max": [9.6],
        "temperature_2m_min": [3.2],
        "precipitation_probability_max": [20],
    },
}


def make_update(user_id=1, chat_id=10):
    """Поддельный Update: запоминаем всё, что бот отвечает в чат."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    return update


def make_context(*args):
    context = MagicMock()
    context.args = list(args)
    context.job_queue = MagicMock()
    context.job_queue.get_jobs_by_name.return_value = []
    return context


def replies(update):
    """Все тексты, которые бот отправил в ответ."""
    return [call.args[0] for call in update.message.reply_text.call_args_list]


class WeatherModuleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        weather._cache.clear()

    # --- форматирование
    def test_fmt_temp(self):
        self.assertEqual(weather.fmt_temp(5.4), "+5°C")
        self.assertEqual(weather.fmt_temp(-3.6), "-4°C")
        self.assertEqual(weather.fmt_temp(0.2), "0°C")

    def test_describe_unknown_code(self):
        emoji, text = weather.describe(12345)
        self.assertTrue(emoji and text)

    def test_advice(self):
        base = dict(temp=10, t_min=5, t_max=12, wind=3, precip=0, code=0, precip_prob=0)
        self.assertEqual(weather.advice(base), [])
        self.assertIn("☔ Возьмите зонт", weather.advice({**base, "precip_prob": 60}))
        self.assertIn("☔ Возьмите зонт", weather.advice({**base, "code": 63}))
        self.assertTrue(any("холодно" in t for t in weather.advice({**base, "t_min": -20})))
        self.assertTrue(any("Жарко" in t for t in weather.advice({**base, "t_max": 33})))
        self.assertTrue(any("ветер" in t for t in weather.advice({**base, "wind": 15})))

    def test_place_name(self):
        self.assertEqual(
            weather.place_name({"name": "Санкт-Петербург", "region": "Санкт-Петербург"}),
            "Санкт-Петербург",
        )
        self.assertEqual(
            weather.place_name({"name": "Казань", "region": "Татарстан"}),
            "Казань, Татарстан",
        )

    # --- геокодинг
    async def test_geocode_found(self):
        with patch.object(weather, "_get_json", AsyncMock(return_value=GEO_OK)) as m:
            city = await weather.geocode("  Санкт-Петербург ")
        self.assertEqual(city["name"], "Санкт-Петербург")
        self.assertAlmostEqual(city["lat"], 59.93863)
        self.assertEqual(m.call_args.args[1]["name"], "Санкт-Петербург")

    async def test_geocode_not_found(self):
        # когда город не найден, Open-Meteo вообще не присылает ключ "results"
        with patch.object(weather, "_get_json", AsyncMock(return_value={})):
            with self.assertRaises(weather.WeatherError) as ctx:
                await weather.geocode("Нигденск")
        self.assertIn("не найден", str(ctx.exception))

    async def test_geocode_empty_and_too_long(self):
        with self.assertRaises(weather.WeatherError):
            await weather.geocode("   ")
        with self.assertRaises(weather.WeatherError):
            await weather.geocode("а" * 101)

    async def test_geocode_broken_answer(self):
        with patch.object(weather, "_get_json", AsyncMock(return_value={"results": [{}]})):
            with self.assertRaises(weather.WeatherError):
                await weather.geocode("Москва")

    # --- прогноз
    async def test_get_weather_parse_and_cache(self):
        mock = AsyncMock(return_value=FORECAST_OK)
        with patch.object(weather, "_get_json", mock):
            first = await weather.get_weather(59.94, 30.31)
            second = await weather.get_weather(59.94, 30.31)
        self.assertEqual(mock.call_count, 1, "второй вызов должен браться из кэша")
        self.assertEqual(first, second)
        self.assertEqual(first["code"], 3)
        self.assertEqual(first["precip_prob"], 20)

    async def test_get_weather_expired_cache(self):
        mock = AsyncMock(return_value=FORECAST_OK)
        with patch.object(weather, "_get_json", mock):
            await weather.get_weather(1.0, 2.0)
            key = (1.0, 2.0)
            weather._cache[key] = (time.time() - weather.CACHE_TTL - 1, weather._cache[key][1])
            await weather.get_weather(1.0, 2.0)
        self.assertEqual(mock.call_count, 2)

    async def test_get_weather_null_probability(self):
        data = {**FORECAST_OK, "daily": {**FORECAST_OK["daily"],
                                         "precipitation_probability_max": [None]}}
        with patch.object(weather, "_get_json", AsyncMock(return_value=data)):
            result = await weather.get_weather(5.0, 5.0)
        self.assertEqual(result["precip_prob"], 0)

    async def test_get_weather_unexpected_answer(self):
        with patch.object(weather, "_get_json", AsyncMock(return_value={"foo": 1})):
            with self.assertRaises(weather.WeatherError):
                await weather.get_weather(7.0, 7.0)

    def test_format_full_and_short(self):
        data = {"temp": 8.4, "feels": 5.1, "wind": 4.2, "precip": 0.0, "code": 3,
                "t_max": 9.6, "t_min": 3.2, "precip_prob": 20}
        full = weather.format_full("Санкт-Петербург", data)
        self.assertIn("+8°C", full)
        self.assertIn("ощущается +5°C", full)
        self.assertIn("+3°C … +10°C", full)
        self.assertIn("до 20%", full)
        short = weather.format_short("Санкт-Петербург", data)
        self.assertIn("Санкт-Петербург: +8°C", short)
        self.assertEqual(len(short.splitlines()), 1)

    # --- сетевые ошибки (через подмену транспорта httpx)
    async def _call_with_transport(self, handler):
        original = httpx.AsyncClient

        def factory(**kwargs):
            kwargs.pop("proxy", None)
            return original(transport=httpx.MockTransport(handler), **kwargs)

        with patch.object(weather.httpx, "AsyncClient", factory):
            return await weather._get_json("https://example.test/api", {"a": 1})

    async def test_http_ok(self):
        result = await self._call_with_transport(lambda r: httpx.Response(200, json={"ok": 1}))
        self.assertEqual(result, {"ok": 1})

    async def test_http_timeout(self):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)
        with self.assertRaises(weather.WeatherError) as ctx:
            await self._call_with_transport(handler)
        self.assertIn("не ответил", str(ctx.exception))

    async def test_http_server_error(self):
        with self.assertRaises(weather.WeatherError):
            await self._call_with_transport(lambda r: httpx.Response(500))

    async def test_http_connection_error(self):
        def handler(request):
            raise httpx.ConnectError("down", request=request)
        with self.assertRaises(weather.WeatherError):
            await self._call_with_transport(handler)

    async def test_http_invalid_json(self):
        with self.assertRaises(weather.WeatherError):
            await self._call_with_transport(lambda r: httpx.Response(200, content=b"<html>"))


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_path = bot.DB_PATH
        bot.DB_PATH = os.path.join(self.tmp.name, "test.db")
        bot.init_db()
        weather._cache.clear()

    def tearDown(self):
        bot.DB_PATH = self._old_path
        self.tmp.cleanup()

    async def run_cmd(self, handler, *args, user_id=1):
        """Запускает команду через обёртку safe (как в настоящем боте)."""
        update, context = make_update(user_id), make_context(*args)
        await bot.safe(handler)(update, context)
        return update, context

    # --- меню команд
    def test_menu_commands_valid(self):
        names = [c.command for c in bot.BOT_COMMANDS]
        self.assertEqual(len(names), len(set(names)), "команды не должны повторяться")
        for command in bot.BOT_COMMANDS:
            self.assertRegex(command.command, r"^[a-z0-9_]{1,32}$")
            self.assertTrue(3 <= len(command.description) <= 256)
            self.assertTrue(hasattr(bot, f"cmd_{command.command}"),
                            f"нет обработчика для /{command.command}")
        for needed in ("city", "weather", "search", "week", "today", "help", "feedback"):
            self.assertIn(needed, names)
        # служебные команды администратора не показываются всем
        admin_names = [c.command for c in bot.ADMIN_COMMANDS]
        for name in admin_names:
            self.assertNotIn(name, names)
            self.assertTrue(hasattr(bot, f"cmd_{name}"))

    def test_help_mentions_new_commands(self):
        for text in ("/city", "/weather", "/search", "/week"):
            self.assertIn(text, bot.HELP_TEXT)

    async def test_setup_menu_registers_commands(self):
        app = MagicMock()
        app.bot.set_my_commands = AsyncMock()
        app.bot.set_chat_menu_button = AsyncMock()
        with patch.object(bot, "ADMIN_ID", None):
            await bot.setup_menu(app)
        app.bot.set_my_commands.assert_awaited_once_with(bot.BOT_COMMANDS)
        app.bot.set_chat_menu_button.assert_awaited_once()

    async def test_setup_menu_admin_gets_extra_commands(self):
        app = MagicMock()
        app.bot.set_my_commands = AsyncMock()
        app.bot.set_chat_menu_button = AsyncMock()
        with patch.object(bot, "ADMIN_ID", 777):
            await bot.setup_menu(app)
        self.assertEqual(app.bot.set_my_commands.await_count, 2)
        second = app.bot.set_my_commands.await_args_list[1]
        self.assertEqual(second.args[0], bot.BOT_COMMANDS + bot.ADMIN_COMMANDS)

    async def test_setup_menu_failure_does_not_crash(self):
        app = MagicMock()
        app.bot.set_my_commands = AsyncMock(side_effect=RuntimeError("network"))
        await bot.setup_menu(app)  # не должно бросить исключение

    # --- /city
    async def test_city_saved(self):
        with patch.object(weather, "geocode", AsyncMock(return_value={
                "name": "Казань", "region": "Татарстан", "country": "Россия",
                "lat": 55.79, "lon": 49.12})):
            update, _ = await self.run_cmd(bot.cmd_city, "Казань")
        self.assertIn("Казань, Татарстан", replies(update)[0])
        row = bot.db_get_user(1)
        self.assertEqual(row["city"], "Казань, Татарстан")
        self.assertAlmostEqual(row["lat"], 55.79)

    async def test_city_updated_not_duplicated(self):
        bot.db_set_city(1, "Москва", 55.75, 37.62)
        bot.db_set_city(1, "Казань", 55.79, 49.12)
        self.assertEqual(bot.db_get_user(1)["city"], "Казань")

    async def test_city_not_found_message(self):
        with patch.object(weather, "_get_json", AsyncMock(return_value={})):
            update, _ = await self.run_cmd(bot.cmd_city, "Нигденск")
        self.assertIn("не найден", replies(update)[0])
        self.assertIsNone(bot.db_get_user(1))

    async def test_city_without_args(self):
        update, _ = await self.run_cmd(bot.cmd_city)
        self.assertIn("Укажите город", replies(update)[0])
        bot.db_set_city(1, "Москва", 55.75, 37.62)
        update, _ = await self.run_cmd(bot.cmd_city)
        self.assertIn("Ваш город: Москва", replies(update)[0])

    # --- /weather
    async def test_weather_requires_city(self):
        update, _ = await self.run_cmd(bot.cmd_weather)
        self.assertIn("/city", replies(update)[0])

    async def test_weather_for_saved_city(self):
        bot.db_set_city(1, "Санкт-Петербург", 59.94, 30.31)
        with patch.object(weather, "_get_json", AsyncMock(return_value=FORECAST_OK)):
            update, _ = await self.run_cmd(bot.cmd_weather)
        text = replies(update)[0]
        self.assertIn("Погода: Санкт-Петербург", text)
        self.assertIn("+8°C", text)

    async def test_weather_other_city_does_not_change_saved(self):
        bot.db_set_city(1, "Москва", 55.75, 37.62)
        async def fake(url, params):
            return GEO_OK if "geocoding" in url else FORECAST_OK
        with patch.object(weather, "_get_json", fake):
            update, _ = await self.run_cmd(bot.cmd_weather, "Санкт-Петербург")
        self.assertIn("Санкт-Петербург", replies(update)[0])
        self.assertEqual(bot.db_get_user(1)["city"], "Москва")

    async def test_weather_api_down(self):
        bot.db_set_city(1, "Москва", 55.75, 37.62)
        err = weather.WeatherError("😔 Сервис погоды сейчас недоступен. Попробуйте позже.")
        with patch.object(weather, "_get_json", AsyncMock(side_effect=err)):
            update, _ = await self.run_cmd(bot.cmd_weather)
        self.assertIn("недоступен", replies(update)[0])

    # --- /search
    async def test_search_cyrillic_case_insensitive(self):
        bot.db_add_task(1, 10, "Купить МОЛОКО", 1)
        bot.db_add_task(1, 10, "Позвонить клиенту", 2)
        bot.db_add_task(2, 20, "Молоко для другого пользователя", 2)
        update, _ = await self.run_cmd(bot.cmd_search, "молоко")
        text = replies(update)[0]
        self.assertIn("Купить МОЛОКО", text)
        self.assertNotIn("клиенту", text)
        self.assertNotIn("другого пользователя", text, "чужие задачи видеть нельзя")

    async def test_search_includes_done_tasks(self):
        bot.db_add_task(1, 10, "Сделать платёж", 2)
        task = bot.db_get_task(1, 1)
        bot.db_update(task["id"], status="done", completed_at=bot.now_ts())
        update, _ = await self.run_cmd(bot.cmd_search, "платёж")
        self.assertIn("✅ #1 Сделать платёж", replies(update)[0])

    async def test_search_special_characters_are_literal(self):
        bot.db_add_task(1, 10, "Скидка 50% на всё", 2)
        bot.db_add_task(1, 10, "Другая задача", 2)
        update, _ = await self.run_cmd(bot.cmd_search, "50%")
        text = replies(update)[0]
        self.assertIn("Скидка 50%", text)
        self.assertNotIn("Другая", text)
        update, _ = await self.run_cmd(bot.cmd_search, "_")
        self.assertIn("ничего не найдено", replies(update)[0])

    async def test_search_empty_and_not_found(self):
        update, _ = await self.run_cmd(bot.cmd_search)
        self.assertIn("Что искать", replies(update)[0])
        update, _ = await self.run_cmd(bot.cmd_search, "а" * 101)
        self.assertIn("длинный", replies(update)[0])
        update, _ = await self.run_cmd(bot.cmd_search, "несуществующее")
        self.assertIn("ничего не найдено", replies(update)[0])

    # --- /week
    async def test_week_filters_by_date(self):
        bot.db_add_task(1, 10, "Свежая задача", 2)
        old = bot.db_add_task(1, 10, "Старая задача", 2)
        task = bot.db_get_task(1, old)
        bot.db_update(task["id"], created_at=bot.now_ts() - 10 * 24 * 3600)
        done = bot.db_add_task(1, 10, "Недавно выполнена", 3)
        task = bot.db_get_task(1, done)
        bot.db_update(task["id"], status="done", completed_at=bot.now_ts())
        update, _ = await self.run_cmd(bot.cmd_week)
        text = replies(update)[0]
        self.assertIn("Свежая задача", text)
        self.assertNotIn("Старая задача", text)
        self.assertIn("Выполнено (1)", text)

    async def test_week_empty(self):
        update, _ = await self.run_cmd(bot.cmd_week)
        self.assertIn("задач не было", replies(update)[0])

    # --- /today
    async def test_today_with_weather(self):
        bot.db_set_city(1, "Санкт-Петербург", 59.94, 30.31)
        bot.db_add_task(1, 10, "Важное дело", 1)
        with patch.object(weather, "_get_json", AsyncMock(return_value=FORECAST_OK)):
            update, _ = await self.run_cmd(bot.cmd_today)
        text = replies(update)[0]
        self.assertIn("Санкт-Петербург: +8°C", text)
        self.assertIn("Важное дело", text)

    async def test_today_weather_failure_does_not_break(self):
        bot.db_set_city(1, "Москва", 55.75, 37.62)
        bot.db_add_task(1, 10, "Важное дело", 1)
        err = weather.WeatherError("down")
        with patch.object(weather, "_get_json", AsyncMock(side_effect=err)):
            update, _ = await self.run_cmd(bot.cmd_today)
        text = replies(update)[0]
        self.assertIn("Погода сейчас недоступна", text)
        self.assertIn("Важное дело", text, "задачи должны показываться и без погоды")

    async def test_today_without_city_has_no_weather_nag(self):
        # Лаб. 3: погода по желанию — без города /today не упоминает погоду
        update, _ = await self.run_cmd(bot.cmd_today)
        text = replies(update)[0]
        self.assertNotIn("/city", text)
        self.assertNotIn("погод", text.lower())

    # --- напоминание
    async def _reminder(self):
        num = bot.db_add_task(1, 10, "Позвонить клиенту", 1)
        task = bot.db_get_task(1, num)
        bot.db_update(task["id"], remind_at=bot.now_ts())
        context = MagicMock()
        context.job.data = task["id"]
        context.bot.send_message = AsyncMock()
        return task, context

    async def test_reminder_includes_weather(self):
        bot.db_set_city(1, "Санкт-Петербург", 59.94, 30.31)
        task, context = await self._reminder()
        with patch.object(weather, "_get_json", AsyncMock(return_value=FORECAST_OK)):
            await bot.send_reminder(context)
        text = context.bot.send_message.call_args.kwargs["text"]
        self.assertIn("Позвонить клиенту", text)
        self.assertIn("Санкт-Петербург: +8°C", text)
        self.assertEqual(bot.db_get_task(1, 1)["reminded"], 1)

    async def test_reminder_sent_even_if_weather_fails(self):
        bot.db_set_city(1, "Москва", 55.75, 37.62)
        task, context = await self._reminder()
        with patch.object(weather, "_get_json",
                          AsyncMock(side_effect=weather.WeatherError("down"))):
            await bot.send_reminder(context)
        text = context.bot.send_message.call_args.kwargs["text"]
        self.assertIn("Позвонить клиенту", text)
        self.assertEqual(bot.db_get_task(1, 1)["reminded"], 1)

    async def test_reminder_without_city_unchanged(self):
        task, context = await self._reminder()
        await bot.send_reminder(context)
        text = context.bot.send_message.call_args.kwargs["text"]
        self.assertIn("Напоминание", text)
        self.assertNotIn("°C", text)

    # --- старая функциональность (лаб. 1) не сломана
    def test_lab1_parsing_still_works(self):
        self.assertEqual(bot.parse_task_text("Купить молоко !1"), ("Купить молоко", 1))
        self.assertEqual(bot.parse_task_text("Сдать отчёт"), ("Сдать отчёт", 2))
        with self.assertRaises(bot.UserError):
            bot.parse_number("0")

    async def test_lab1_add_list_done_flow(self):
        update, context = make_update(), make_context("Купить", "хлеб", "!1")
        await bot.safe(bot.cmd_add)(update, context)
        update, _ = await self.run_cmd(bot.cmd_list)
        self.assertIn("Купить хлеб", replies(update)[0])
        update, _ = await self.run_cmd(bot.cmd_done, "1")
        self.assertIn("выполнена", replies(update)[0])
        update, _ = await self.run_cmd(bot.cmd_done, "99")
        self.assertIn("не найдена", replies(update)[0])


# ---------------------------------------------------------------------------
# Лаб. 3: обратная связь, статистика, события
# ---------------------------------------------------------------------------

def make_callback_update(data, user_id=5, username="tester"):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = username
    update.effective_user.full_name = "Test User"
    update.effective_chat.id = 50
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message.chat_id = 50
    return update


def make_text_update(text, user_id=5, username="tester"):
    update = make_update(user_id)
    update.effective_user.username = username
    update.effective_user.full_name = "Test User"
    update.message.text = text
    return update


def make_async_context(*args):
    context = make_context(*args)
    context.user_data = {}
    context.bot.send_message = AsyncMock()
    return context


class FeedbackAndStatsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_path = bot.DB_PATH
        bot.DB_PATH = os.path.join(self.tmp.name, "nested", "test.db")
        bot.init_db()
        self.admin_patch = patch.object(bot, "ADMIN_ID", 777)
        self.admin_patch.start()

    def tearDown(self):
        self.admin_patch.stop()
        bot.DB_PATH = self._old_path
        self.tmp.cleanup()

    def test_init_db_creates_nested_directory(self):
        self.assertTrue(os.path.exists(bot.DB_PATH))

    def test_clean_feedback_text(self):
        self.assertEqual(bot.clean_feedback_text("  много   пробелов "), "много пробелов")
        self.assertEqual(bot.clean_feedback_text("-"), "")
        self.assertEqual(bot.clean_feedback_text("—"), "")
        self.assertEqual(len(bot.clean_feedback_text("а" * 5000)), bot.MAX_FEEDBACK_LEN)

    def test_is_admin(self):
        self.assertTrue(bot.is_admin(777))
        self.assertFalse(bot.is_admin(1))
        with patch.object(bot, "ADMIN_ID", None):
            self.assertFalse(bot.is_admin(777))

    async def _full_survey(self, user_id=5):
        ctx = make_async_context()
        upd = make_text_update("/feedback", user_id)
        self.assertEqual(await bot.cmd_feedback(upd, ctx), bot.FB_RATING)
        self.assertIn("1 до 5", replies(upd)[0])
        self.assertEqual(await bot.fb_rating(make_callback_update("fb_rate:4", user_id), ctx), bot.FB_LIKED)
        self.assertEqual(await bot.fb_liked(make_text_update("Удобно", user_id), ctx), bot.FB_DISLIKED)
        self.assertEqual(await bot.fb_disliked(make_text_update("-", user_id), ctx), bot.FB_IMPROVE)
        self.assertEqual(await bot.fb_improve(make_text_update("Повторяющиеся задачи", user_id), ctx), bot.FB_WOULD_USE)
        end = await bot.fb_would_use(make_callback_update("fb_use:maybe_yes", user_id), ctx)
        return end, ctx

    async def test_full_survey_saved_and_admin_notified(self):
        end, ctx = await self._full_survey()
        self.assertEqual(end, bot.ConversationHandler.END)
        rows = bot.db_last_feedback()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["rating"], row["liked"], row["disliked"]), (4, "Удобно", ""))
        self.assertEqual((row["improve"], row["would_use"]), ("Повторяющиеся задачи", "maybe_yes"))
        self.assertEqual(row["username"], "tester")
        sent = [c.args for c in ctx.bot.send_message.await_args_list]
        self.assertTrue(any(a[0] == 777 and "Новый отзыв" in a[1] for a in sent))
        self.assertTrue(any(a[0] == 50 and "Спасибо" in a[1] for a in sent))

    async def test_admin_own_feedback_not_forwarded(self):
        _, ctx = await self._full_survey(user_id=777)
        sent = [c.args for c in ctx.bot.send_message.await_args_list]
        self.assertFalse(any(a[0] == 777 and "Новый отзыв" in a[1] for a in sent))

    async def test_stale_would_use_button(self):
        ctx = make_async_context()
        upd = make_callback_update("fb_use:yes")
        end = await bot.fb_would_use(upd, ctx)
        self.assertEqual(end, bot.ConversationHandler.END)
        self.assertEqual(bot.db_last_feedback(), [])
        self.assertIn("устарел", upd.callback_query.edit_message_text.await_args.args[0])

    async def test_cancel_during_survey(self):
        ctx = make_async_context()
        await bot.cmd_feedback(make_text_update("/feedback"), ctx)
        upd = make_text_update("/cancel")
        self.assertEqual(await bot.fb_cancel(upd, ctx), bot.ConversationHandler.END)
        self.assertNotIn("fb", ctx.user_data)
        self.assertIn("отменён", replies(upd)[0])

    async def test_text_instead_of_buttons(self):
        ctx = make_async_context()
        upd = make_text_update("привет")
        self.assertEqual(await bot.fb_need_rating(upd, ctx), bot.FB_RATING)
        self.assertEqual(await bot.fb_need_would_use(upd, ctx), bot.FB_WOULD_USE)

    async def test_guard_handles_unexpected_error(self):
        async def broken(update, context):
            raise RuntimeError("boom")
        ctx = make_async_context()
        ctx.user_data["fb"] = {"rating": 3}
        upd = make_text_update("x")
        result = await bot.guard(broken)(upd, ctx)
        self.assertEqual(result, bot.ConversationHandler.END)
        self.assertNotIn("fb", ctx.user_data)
        ctx.bot.send_message.assert_awaited()

    async def test_stale_button_alert(self):
        upd = make_callback_update("fb_rate:3")
        await bot.stale_button(upd, make_async_context())
        upd.callback_query.answer.assert_awaited_once()

    async def test_stats_admin_only(self):
        upd = make_update(user_id=1)
        await bot.safe(bot.cmd_stats)(upd, make_context())
        self.assertIn("только администратору", replies(upd)[0])
        upd = make_update(user_id=777)
        await bot.safe(bot.cmd_stats)(upd, make_context())
        self.assertIn("Статистика", replies(upd)[0])

    async def test_feedbacks_admin_only_and_listing(self):
        upd = make_update(user_id=1)
        await bot.safe(bot.cmd_feedbacks)(upd, make_context())
        self.assertIn("только администратору", replies(upd)[0])
        upd = make_update(user_id=777)
        await bot.safe(bot.cmd_feedbacks)(upd, make_context())
        self.assertIn("пока нет", replies(upd)[0])
        await self._full_survey()
        upd = make_update(user_id=777)
        await bot.safe(bot.cmd_feedbacks)(upd, make_context())
        self.assertIn("Повторяющиеся задачи", replies(upd)[0])

    async def test_safe_logs_events_and_stats_numbers(self):
        for uid, cmd in [(1, bot.cmd_help), (1, bot.cmd_help), (2, bot.cmd_list), (777, bot.cmd_help)]:
            await bot.safe(cmd)(make_update(user_id=uid), make_context())
        await self._full_survey(user_id=2)
        st = bot.db_stats()
        self.assertEqual(st["users_total"], 3)
        self.assertEqual(st["users_others"], 2)
        self.assertEqual(st["users_24h"], 3)
        self.assertEqual(st["feedback_count"], 1)
        self.assertEqual(st["avg_rating"], 4)
        top = {row["command"]: row["n"] for row in st["top_commands"]}
        self.assertEqual(top["help"], 3)
        self.assertEqual(top["list"], 1)

    def test_log_event_failure_does_not_raise(self):
        with patch.object(bot, "db_connect", side_effect=RuntimeError("db down")):
            bot.log_event(1, "help")  # не должно бросить исключение

    async def test_safe_works_when_logging_fails(self):
        with patch.object(bot, "db_connect", side_effect=RuntimeError("db down")):
            upd = make_update()
            await bot.safe(bot.cmd_help)(upd, make_context())
        self.assertTrue(replies(upd))

    def test_weather_cache_is_capped(self):
        weather._cache.clear()
        for i in range(weather.MAX_CACHE_SIZE + 50):
            weather._cache[(i, i)] = (time.time(), {})
        async def run():
            with patch.object(weather, "_get_json", AsyncMock(return_value=FORECAST_OK)):
                await weather.get_weather(1.5, 2.5)
        import asyncio
        asyncio.run(run())
        self.assertLessEqual(len(weather._cache), weather.MAX_CACHE_SIZE + 1)
        weather._cache.clear()


# ---------------------------------------------------------------------------
# Лаб. 3: улучшения по отзывам (погода по желанию, /progress, /calendar)
# ---------------------------------------------------------------------------

class FeedbackImprovementsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_path = bot.DB_PATH
        bot.DB_PATH = os.path.join(self.tmp.name, "t.db")
        bot.init_db()

    def tearDown(self):
        bot.DB_PATH = self._old_path
        self.tmp.cleanup()

    async def run_cmd(self, handler, *args, user_id=1):
        update, context = make_update(user_id), make_context(*args)
        await bot.safe(handler)(update, context)
        return update, context

    # --- погода по желанию
    async def test_city_off(self):
        bot.db_set_city(1, "Москва", 55.75, 37.62)
        update, _ = await self.run_cmd(bot.cmd_city, "off")
        self.assertIn("отключена", replies(update)[0])
        self.assertIsNone(bot.db_get_user(1)["lat"])
        # после отключения /today не вызывает погоду
        with patch.object(weather, "_get_json", AsyncMock(side_effect=AssertionError)):
            update, _ = await self.run_cmd(bot.cmd_today)
        self.assertNotIn("погод", replies(update)[0].lower())

    async def test_city_off_without_city_is_safe(self):
        update, _ = await self.run_cmd(bot.cmd_city, "off")
        self.assertIn("отключена", replies(update)[0])

    def test_start_does_not_push_weather(self):
        update = make_update()
        import asyncio
        asyncio.run(bot.cmd_start(update, make_context()))
        text = replies(update)[0]
        self.assertIn("/progress", text)
        self.assertIn("/calendar", text)
        self.assertIn("по желанию", text)

    def test_help_mentions_new_commands(self):
        for text in ("/progress", "/calendar", "/city off"):
            self.assertIn(text, bot.HELP_TEXT)

    # --- /progress
    def _task(self, user_id, days_ago_created, days_ago_done=None, hour=12):
        now = datetime.now(bot.TZ).replace(hour=hour, minute=0, second=0, microsecond=0)
        num = bot.db_add_task(user_id, 10, f"t{days_ago_created}-{days_ago_done}", 2)
        task = bot.db_get_task(user_id, num)
        fields = {"created_at": int((now - timedelta(days=days_ago_created)).timestamp())}
        if days_ago_done is not None:
            fields["status"] = "done"
            fields["completed_at"] = int((now - timedelta(days=days_ago_done)).timestamp())
        bot.db_update(task["id"], **fields)

    def test_progress_stats_and_streak(self):
        for _ in range(2):
            self._task(1, 3, 0)          # выполнено сегодня
        self._task(1, 2, 1)              # вчера
        self._task(1, 5, 2)              # позавчера
        self._task(1, 0)                 # создана сегодня, не выполнена
        self._task(2, 0, 0)              # чужая задача не учитывается
        rows = bot.db_progress_rows(1)
        today = datetime.now(bot.TZ).date()
        per_day, created, done, streak = bot.progress_stats(rows, today)
        self.assertEqual(len(per_day), 7)
        self.assertEqual((created, done), (5, 4))
        self.assertEqual(per_day[-1][2], 2)   # сегодня выполнено 2
        self.assertEqual(streak, 3)

    def test_progress_streak_counts_from_yesterday(self):
        self._task(1, 2, 1)
        self._task(1, 3, 2)
        today = datetime.now(bot.TZ).date()
        *_, streak = bot.progress_stats(bot.db_progress_rows(1), today)
        self.assertEqual(streak, 2)

    def test_progress_streak_broken(self):
        self._task(1, 5, 4)
        today = datetime.now(bot.TZ).date()
        *_, streak = bot.progress_stats(bot.db_progress_rows(1), today)
        self.assertEqual(streak, 0)

    async def test_progress_empty(self):
        update, _ = await self.run_cmd(bot.cmd_progress)
        self.assertIn("пока нет задач", replies(update)[0])

    async def test_progress_output(self):
        self._task(1, 1, 0)
        self._task(1, 1, 0)
        update, _ = await self.run_cmd(bot.cmd_progress)
        text = replies(update)[0]
        self.assertIn("Прогресс за 7 дней", text)
        self.assertIn("██", text)
        self.assertIn("Выполнено: 2", text)
        self.assertIn("100%", text)

    def test_bar_capped(self):
        self.assertEqual(len(bot.bar(50)), bot.BAR_MAX)
        self.assertEqual(bot.bar(0), "·")

    # --- /calendar
    def test_ics_escape_and_fold(self):
        self.assertEqual(bot.ics_escape("a,b;c\\d\nz"), "a\\,b\\;c\\\\d\\nz")
        folded = bot.ics_fold("SUMMARY:" + "Я" * 100)
        for line in folded:
            self.assertLessEqual(len(line.encode("utf-8")), 75)
        self.assertEqual("".join(l.lstrip(" ") if i else l for i, l in enumerate(folded)),
                         "SUMMARY:" + "Я" * 100)

    def test_build_ics_structure(self):
        num = bot.db_add_task(1, 10, "Сдать отчёт, срочно", 1)
        task = bot.db_get_task(1, num)
        bot.db_update(task["id"], remind_at=1900000000)
        ics = bot.build_ics(bot.db_calendar_tasks(1))
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(ics.endswith("END:VCALENDAR\r\n"))
        self.assertEqual(ics.count("BEGIN:VEVENT"), 1)
        self.assertIn("DTSTART:20300317T174640Z", ics)
        self.assertIn("\\,", ics)  # запятая экранирована

    async def test_calendar_without_reminders(self):
        bot.db_add_task(1, 10, "Без напоминания", 2)
        update, _ = await self.run_cmd(bot.cmd_calendar)
        self.assertIn("Нет задач с напоминанием", replies(update)[0])

    async def test_calendar_sends_file_only_own_active_tasks(self):
        for uid, text in ((1, "Моя"), (2, "Чужая")):
            num = bot.db_add_task(uid, 10, text, 2)
            bot.db_update(bot.db_get_task(uid, num)["id"], remind_at=bot.now_ts() + 3600)
        done = bot.db_add_task(1, 10, "Готовая", 2)
        bot.db_update(bot.db_get_task(1, done)["id"], remind_at=bot.now_ts() + 3600, status="done")
        update = make_update(1)
        update.message.reply_document = AsyncMock()
        await bot.safe(bot.cmd_calendar)(update, make_context())
        update.message.reply_document.assert_awaited_once()
        sent = update.message.reply_document.await_args
        content = sent.args[0].getvalue().decode("utf-8")
        self.assertEqual(sent.args[0].name, "tasks.ics")
        self.assertIn("Моя", content)
        self.assertNotIn("Чужая", content)
        self.assertNotIn("Готовая", content)


# ---------------------------------------------------------------------------
# Лаб. 3: /plan и ежедневные задачи (по отзыву №4)
# ---------------------------------------------------------------------------

class PlanAndDailyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_path = bot.DB_PATH
        bot.DB_PATH = os.path.join(self.tmp.name, "p.db")
        bot.init_db()

    def tearDown(self):
        bot.DB_PATH = self._old_path
        self.tmp.cleanup()

    async def run_cmd(self, handler, *args, user_id=1):
        update, context = make_update(user_id), make_context(*args)
        await bot.safe(handler)(update, context)
        return update, context

    def _task(self, text, priority=2, days=None, hour=15, **extra):
        num = bot.db_add_task(1, 10, text, priority)
        task = bot.db_get_task(1, num)
        fields = dict(extra)
        if days is not None:
            when = datetime.now(bot.TZ).replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=days)
            fields["remind_at"] = int(when.timestamp())
        if fields:
            bot.db_update(task["id"], **fields)
        return bot.db_get_task(1, num)

    # --- миграция
    def test_migration_adds_repeat_column_to_old_db(self):
        import sqlite3
        old = os.path.join(self.tmp.name, "old.db")
        conn = sqlite3.connect(old)
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, "
                     "chat_id INTEGER NOT NULL, num INTEGER NOT NULL, text TEXT NOT NULL, "
                     "priority INTEGER NOT NULL DEFAULT 2, status TEXT NOT NULL DEFAULT 'active', "
                     "created_at INTEGER NOT NULL, completed_at INTEGER, remind_at INTEGER, "
                     "reminded INTEGER NOT NULL DEFAULT 0, UNIQUE (user_id, num))")
        conn.execute("INSERT INTO tasks (user_id, chat_id, num, text, created_at) VALUES (1, 1, 1, 'старая', 1)")
        conn.commit()
        conn.close()
        bot.DB_PATH = old
        bot.init_db()
        bot.init_db()  # повторный запуск не ломается
        row = bot.db_get_task(1, 1)
        self.assertEqual(row["text"], "старая")
        self.assertIsNone(row["repeat"])

    # --- /plan
    def test_plan_groups(self):
        today = datetime.now(bot.TZ).date()
        tasks = [
            self._task("сегодня", days=0, hour=23),
            self._task("просрочено", days=-1),
            self._task("завтра", days=1),
            self._task("через 4 дня", days=4),
            self._task("через 20 дней", days=20),
            self._task("без срока", priority=1),
            self._task("уже напомнили", days=-2, reminded=1),
        ]
        groups = bot.build_plan(tasks, today)
        names = {k: [t["text"] for t in v] for k, v in groups.items()}
        self.assertEqual(sorted(names["today"]), ["просрочено", "сегодня"])
        self.assertEqual(names["tomorrow"], ["завтра"])
        self.assertEqual(names["week"], ["через 4 дня"])
        self.assertEqual(names["later"], ["через 20 дней"])
        self.assertEqual(sorted(names["nodate"]), ["без срока", "уже напомнили"])
        self.assertEqual(names["nodate"][0], "без срока")  # приоритет 1 выше

    def test_plan_daily_task_stays_in_schedule(self):
        today = datetime.now(bot.TZ).date()
        task = self._task("полить цветы", days=1, repeat="daily", reminded=1)
        self.assertEqual(bot.plan_group(task, today), "tomorrow")

    async def test_plan_command(self):
        self._task("Позвонить", priority=1, days=0, hour=23)
        self._task("Купить хлеб")
        update, _ = await self.run_cmd(bot.cmd_plan)
        text = replies(update)[0]
        self.assertIn("Сегодня (1)", text)
        self.assertIn("Без срока (1)", text)
        self.assertNotIn("Завтра", text)
        self.assertIn("Позвонить", text)

    async def test_plan_empty(self):
        update, _ = await self.run_cmd(bot.cmd_plan)
        self.assertIn("Активных задач нет", replies(update)[0])

    async def test_plan_only_own_tasks(self):
        num = bot.db_add_task(2, 20, "Чужая", 2)
        update, _ = await self.run_cmd(bot.cmd_plan)
        self.assertNotIn("Чужая", replies(update)[0])

    # --- ежедневные задачи
    async def test_remind_daily_sets_repeat(self):
        self._task("Полить цветы")
        update, context = await self.run_cmd(bot.cmd_remind, "1", "daily", "20:00")
        self.assertIn("каждый день", replies(update)[0])
        row = bot.db_get_task(1, 1)
        self.assertEqual(row["repeat"], "daily")
        self.assertGreater(row["remind_at"], bot.now_ts())
        context.job_queue.run_once.assert_called_once()

    async def test_remind_daily_bad_format(self):
        self._task("Полить цветы")
        update, _ = await self.run_cmd(bot.cmd_remind, "1", "daily")
        self.assertIn("daily", replies(update)[0])
        update, _ = await self.run_cmd(bot.cmd_remind, "1", "daily", "25:99")
        self.assertIn("Неверный формат", replies(update)[0])
        self.assertIsNone(bot.db_get_task(1, 1)["repeat"])

    async def test_plain_remind_clears_repeat(self):
        self._task("Полить цветы", repeat="daily")
        await self.run_cmd(bot.cmd_remind, "1", "23:59")
        self.assertIsNone(bot.db_get_task(1, 1)["repeat"])

    async def test_unremind_stops_daily(self):
        task = self._task("Полить цветы", days=1, repeat="daily")
        update, _ = await self.run_cmd(bot.cmd_unremind, "1")
        self.assertIn("отменено", replies(update)[0])
        row = bot.db_get_task(1, 1)
        self.assertIsNone(row["repeat"])
        self.assertIsNone(row["remind_at"])

    async def test_done_on_daily_keeps_task_and_records_completion(self):
        self._task("Полить цветы", days=1, repeat="daily")
        update, _ = await self.run_cmd(bot.cmd_done, "1")
        self.assertIn("Готово на сегодня", replies(update)[0])
        original = bot.db_get_task(1, 1)
        self.assertEqual(original["status"], "active")
        self.assertEqual(original["repeat"], "daily")
        done = bot.db_completed_tasks(1)
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["text"], "Полить цветы")
        # выполнение попадает в статистику /progress
        today = datetime.now(bot.TZ).date()
        _, _, done_count, streak = bot.progress_stats(bot.db_progress_rows(1), today)
        self.assertEqual((done_count, streak), (1, 1))

    def test_next_daily_ts(self):
        now = bot.now_ts()
        self.assertEqual(bot.next_daily_ts(now + 100, now), now + 100)
        past = now - 3 * 24 * 3600 - 60
        nxt = bot.next_daily_ts(past, now)
        self.assertGreater(nxt, now)
        self.assertLessEqual(nxt - now, 24 * 3600 + 3600)
        self.assertEqual(
            datetime.fromtimestamp(nxt, bot.TZ).strftime("%H:%M"),
            datetime.fromtimestamp(past, bot.TZ).strftime("%H:%M"),
        )

    async def test_send_reminder_reschedules_daily(self):
        task = self._task("Полить цветы", repeat="daily", remind_at=bot.now_ts())
        context = make_context()
        context.job.data = task["id"]
        context.bot.send_message = AsyncMock()
        await bot.send_reminder(context)
        text = context.bot.send_message.await_args.kwargs["text"]
        self.assertIn("Ежедневная задача", text)
        row = bot.db_get_task(1, 1)
        self.assertGreater(row["remind_at"], bot.now_ts())
        self.assertEqual(row["reminded"], 0)
        context.job_queue.run_once.assert_called_once()

    async def test_send_reminder_one_time_marks_reminded(self):
        task = self._task("Разово", remind_at=bot.now_ts())
        context = make_context()
        context.job.data = task["id"]
        context.bot.send_message = AsyncMock()
        await bot.send_reminder(context)
        self.assertEqual(bot.db_get_task(1, 1)["reminded"], 1)
        context.job_queue.run_once.assert_not_called()

    def test_list_marks_daily(self):
        task = self._task("Полить цветы", days=1, repeat="daily")
        self.assertIn("🔁", bot.fmt_task(task))

    def test_ics_daily_has_rrule(self):
        self._task("Полить цветы", days=1, repeat="daily")
        self._task("Разово", days=1)
        ics = bot.build_ics(bot.db_calendar_tasks(1))
        self.assertEqual(ics.count("RRULE:FREQ=DAILY"), 1)


if __name__ == "__main__":
    unittest.main()
