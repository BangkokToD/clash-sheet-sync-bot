# Clash Sheet Sync Bot

Лёгкий Telegram-бот для ручного обновления листов Google Sheets по данным Clash of Clans API.

На текущем этапе реализован только первый коммит: скелет проекта, конфигурация и файловое хранилище статусов. Telegram long polling, кнопки, Clash API, Google Sheets API, синхронизация состава и CWL добавляются следующими коммитами.

## Ограничения проекта

Проект должен оставаться лёгким:

- Python 3.12;
- без БД;
- без автообновления;
- без webhook-сервера;
- без FastAPI, Flask, Django;
- без aiogram и python-telegram-bot;
- Telegram Bot API вызывается напрямую через `httpx`;
- Google Sheets API вызывается через REST и service account.

## Структура

```text
clash-sheet-sync-bot/
├── .env
├── .env.example
├── .gitignore
├── README.md
├── bot.py
├── config.py
├── models.py
├── requirements.txt
└── settings_store.py