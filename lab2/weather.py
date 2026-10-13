"""
Модуль погоды: интеграция с публичным API Open-Meteo (без ключа и регистрации).

Используются два сервиса:
  * геокодинг  — https://geocoding-api.open-meteo.com  (город -> координаты);
  * прогноз    — https://api.open-meteo.com            (текущая погода + сутки).

Модуль не зависит от Telegram: он только ходит в API и форматирует текст.
Любые сетевые и форматные ошибки превращаются в WeatherError с понятным
сообщением, которое бот показывает пользователю.
"""

import logging
import os
import time

import httpx

logger = logging.getLogger("task-bot.weather")

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

REQUEST_TIMEOUT = 15      # секунд на один запрос к API
CACHE_TTL = 600           # прогноз кэшируем на 10 минут
MAX_CITY_LEN = 100        # максимальная длина названия города

# Кэш прогнозов: (широта, долгота) -> (время загрузки, данные)
_cache: dict = {}

# Коды погоды WMO -> (эмодзи, описание на русском)
WEATHER_CODES = {
    0: ("☀️", "ясно"),
    1: ("🌤", "преимущественно ясно"),
    2: ("⛅️", "переменная облачность"),
    3: ("☁️", "пасмурно"),
    45: ("🌫", "туман"),
    48: ("🌫", "изморозь, туман"),
    51: ("🌦", "слабая морось"),
    53: ("🌦", "морось"),
    55: ("🌦", "сильная морось"),
    56: ("🌧", "ледяная морось"),
    57: ("🌧", "сильная ледяная морось"),
    61: ("🌧", "небольшой дождь"),
    63: ("🌧", "дождь"),
    65: ("🌧", "сильный дождь"),
    66: ("🌧", "ледяной дождь"),
    67: ("🌧", "сильный ледяной дождь"),
    71: ("🌨", "небольшой снег"),
    73: ("🌨", "снег"),
    75: ("❄️", "сильный снег"),
    77: ("🌨", "снежные зёрна"),
    80: ("🌦", "небольшой ливень"),
    81: ("🌧", "ливень"),
    82: ("⛈", "сильный ливень"),
    85: ("🌨", "снегопад"),
    86: ("❄️", "сильный снегопад"),
    95: ("⛈", "гроза"),
    96: ("⛈", "гроза с градом"),
    99: ("⛈", "сильная гроза с градом"),
}

# Коды, при которых идут осадки (для подсказки про зонт)
PRECIPITATION_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75,
                       77, 80, 81, 82, 85, 86, 95, 96, 99}


class WeatherError(Exception):
    """Ошибка получения погоды: текст исключения показывается пользователю."""


# ---------------------------------------------------------------------------
# Запросы к API
# ---------------------------------------------------------------------------

async def _get_json(url: str, params: dict) -> dict:
    """GET-запрос к API с таймаутом. Ошибки сети превращаются в WeatherError."""
    # Тот же необязательный прокси, что и для Telegram (см. .env.example).
    # Читаем при каждом запросе, чтобы значение из .env точно было загружено.
    proxy = os.getenv("PROXY_URL") or None
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, proxy=proxy) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException:
        raise WeatherError("⏳ Сервис погоды не ответил вовремя. Попробуйте позже.")
    except httpx.HTTPStatusError as err:
        logger.warning("Open-Meteo вернул код %s", err.response.status_code)
        raise WeatherError("😔 Сервис погоды сейчас недоступен. Попробуйте позже.")
    except (httpx.HTTPError, ValueError) as err:
        logger.warning("Ошибка запроса к Open-Meteo: %r", err)
        raise WeatherError("😔 Не удалось связаться с сервисом погоды.")


async def geocode(city: str) -> dict:
    """
    Ищет город по названию и возвращает словарь:
    {name, region, country, lat, lon}. Если город не найден — WeatherError.
    """
    city = " ".join(city.split())
    if not city:
        raise WeatherError("❌ Укажите город. Пример: /city Москва")
    if len(city) > MAX_CITY_LEN:
        raise WeatherError("❌ Слишком длинное название города.")

    data = await _get_json(
        GEOCODING_URL,
        {"name": city, "count": 1, "language": "ru", "format": "json"},
    )
    results = data.get("results") or []  # если ничего не найдено, ключа нет
    if not results:
        raise WeatherError(
            f"🔎 Город «{city}» не найден. Проверьте название и попробуйте ещё раз."
        )
    try:
        item = results[0]
        return {
            "name": item["name"],
            "region": item.get("admin1") or "",
            "country": item.get("country") or "",
            "lat": float(item["latitude"]),
            "lon": float(item["longitude"]),
        }
    except (KeyError, TypeError, ValueError):
        raise WeatherError("😔 Сервис вернул неожиданный ответ. Попробуйте позже.")


async def get_weather(lat: float, lon: float) -> dict:
    """
    Возвращает текущую погоду и прогноз на сегодня (с кэшем на 10 минут):
    {temp, feels, wind, precip, code, t_max, t_min, precip_prob}.
    """
    key = (round(lat, 2), round(lon, 2))
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]

    data = await _get_json(
        FORECAST_URL,
        {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,apparent_temperature,precipitation,"
                       "weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max",
            "wind_speed_unit": "ms",
            "timezone": "auto",
            "forecast_days": 1,
        },
    )
    try:
        current = data["current"]
        daily = data["daily"]
        weather = {
            "temp": float(current["temperature_2m"]),
            "feels": float(current["apparent_temperature"]),
            "wind": float(current["wind_speed_10m"]),
            "precip": float(current.get("precipitation") or 0),
            "code": int(current["weather_code"]),
            "t_max": float(daily["temperature_2m_max"][0]),
            "t_min": float(daily["temperature_2m_min"][0]),
            # вероятность осадков может отсутствовать (None) — считаем 0
            "precip_prob": int(daily["precipitation_probability_max"][0] or 0),
        }
    except (KeyError, IndexError, TypeError, ValueError):
        raise WeatherError("😔 Сервис вернул неожиданный ответ. Попробуйте позже.")

    _cache[key] = (time.time(), weather)
    return weather


# ---------------------------------------------------------------------------
# Форматирование
# ---------------------------------------------------------------------------

def fmt_temp(value: float) -> str:
    """Температура со знаком: +5°C, -3°C, 0°C."""
    rounded = round(value)
    return "0°C" if rounded == 0 else f"{rounded:+d}°C"


def describe(code: int) -> tuple:
    """Эмодзи и описание по коду погоды (неизвестный код — нейтральный текст)."""
    return WEATHER_CODES.get(code, ("🌡", "погода без описания"))


def advice(weather: dict) -> list:
    """Короткие подсказки по погоде (зонт, холод, жара, ветер)."""
    tips = []
    if (
        weather["code"] in PRECIPITATION_CODES
        or weather["precip"] > 0
        or weather["precip_prob"] >= 50
    ):
        tips.append("☔ Возьмите зонт")
    if weather["t_min"] <= -15 or weather["temp"] <= -15:
        tips.append("🥶 Очень холодно — оденьтесь теплее")
    if weather["t_max"] >= 30 or weather["temp"] >= 30:
        tips.append("🥵 Жарко — пейте больше воды")
    if weather["wind"] >= 12:
        tips.append("💨 Сильный ветер")
    return tips


def format_full(place: str, weather: dict) -> str:
    """Подробная погода для команды /weather."""
    emoji, text = describe(weather["code"])
    lines = [
        f"{emoji} Погода: {place}",
        f"Сейчас: {fmt_temp(weather['temp'])} "
        f"(ощущается {fmt_temp(weather['feels'])}), {text}",
        f"Ветер: {weather['wind']:.0f} м/с",
        f"Сегодня: {fmt_temp(weather['t_min'])} … {fmt_temp(weather['t_max'])}, "
        f"вероятность осадков до {weather['precip_prob']}%",
    ]
    lines.extend(advice(weather))
    return "\n".join(lines)


def format_short(place: str, weather: dict) -> str:
    """Одна строка погоды для /today и напоминаний."""
    emoji, text = describe(weather["code"])
    line = f"{emoji} {place}: {fmt_temp(weather['temp'])}, {text}"
    if "☔ Возьмите зонт" in advice(weather):
        line += " ☔"
    return line


def place_name(city: dict) -> str:
    """Красивое название места: «Город, регион» (без повтора названия)."""
    parts = [city["name"]]
    region = city.get("region")
    if region and region != city["name"]:
        parts.append(region)
    return ", ".join(parts)
