#!/bin/bash
# Запуск бота одной командой: окружение -> зависимости -> токен -> старт.
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ Python 3 не найден. Установите его с https://www.python.org/downloads/ и запустите скрипт снова."
  exit 1
fi

# Пересоздаём окружение, если его нет или оно сломано
if [ ! -x venv/bin/python ]; then
  rm -rf venv
  echo "▶ Создаю виртуальное окружение..."
  python3 -m venv venv || { echo "❌ Не удалось создать окружение."; exit 1; }
fi
source venv/bin/activate

echo "▶ Устанавливаю зависимости (1-2 минуты при первом запуске)..."
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt || { echo "❌ Не удалось установить зависимости."; exit 1; }

# Токен: спрашиваем, если файла .env нет или в нём заглушка
if [ ! -f .env ] || ! grep -Eq '^BOT_TOKEN=[0-9]+:[A-Za-z0-9_-]{30,}' .env; then
  echo
  echo "Вставьте токен от @BotFather и нажмите Enter (символы не отображаются):"
  read -rs TOKEN
  echo
  TOKEN=$(printf '%s' "$TOKEN" | tr -d '[:space:]"')
  printf 'BOT_TOKEN=%s\nTIMEZONE=Europe/Moscow\n' "$TOKEN" > .env
  echo "✅ Токен сохранён в файл .env"
fi

echo "▶ Запускаю бота. Остановить — Ctrl+C."
python bot.py
