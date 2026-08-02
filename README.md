# Clash Sheet Sync Bot

[![CI](https://github.com/BangkokToD/clash-sheet-sync-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/BangkokToD/clash-sheet-sync-bot/actions/workflows/ci.yml)

Telegram-бот для ручной синхронизации Google Sheets с данными Clash of Clans API.

Бот работает через Telegram Bot API long polling, хранит runtime-состояние в SQLite и обновляет Google Sheets для подключённых Telegram-групп.

Текущая версия: `1.0.0`.

## Возможности

- Подключение Telegram-группы через личный чат с ботом.
- Ссылка на группу техподдержки в главном меню.
- Закрытое меню superadmin для настройки техподдержки и общей рассылки.
- Привязка Google Sheets через Google service account.
- Управление отслеживаемыми кланами.
- Настройка колонок состава, CWL и рейдов через inline-меню.
- Перенос ручных значений между одноимёнными колонками состава и «Вышедших».
- Учёт Raid Weekend с рейтингом, межсезонным fallback и архивами.
- Ручной `/sync` для обновления листов `Состав`, `CWL` и `Рейды`.
- `/status` с результатом последней синхронизации.
- Диагностика и auto-fix привязанной таблицы.
- Перенос таблицы и runtime-state в другую Telegram-группу.
- Staged sync pipeline с честным предупреждением о частичной записи.

## Архитектура коротко

Основные источники данных:

| Источник | Роль |
|---|---|
| SQLite | runtime source of truth: группы, таблицы, кланы, колонки, state, sync history |
| Google Sheets | пользовательская таблица и ручные user-values |
| Clash of Clans API | technical source: состав кланов, CWL и Raid Weekend |

Ключевые идеи:

- SQLite хранит настройки и runtime-state.
- Google Sheets не является главным source of truth.
- CoC API является источником technical values.
- `__bot_key` используется как стабильный ключ строк.
- Бот управляет только своими managed blocks в таблице.
- `/sync` сначала готовит данные и только потом начинает запись.
- Если ошибка произошла после начала записи Google Sheets, пользователь получает partial write warning.

Подробно: [docs/architecture.md](docs/architecture.md).

## Структура проекта

```text
clash-sheet-sync-bot/
├── .github/
│   └── workflows/
│       └── ci.yml
├── clash_sheet_sync_bot/
│   ├── coc/
│   ├── admin/
│   ├── common/
│   ├── repositories/
│   ├── setup/
│   ├── sheets/
│   ├── sync/
│   ├── telegram/
│   ├── bot.py
│   ├── config.py
│   ├── migrations.py
│   ├── models.py
│   └── storage.py
├── docs/
│   ├── architecture.md
│   └── operations.md
├── tests/
├── bot.py
├── README.md
├── CHANGELOG.md
├── CONTRIBUTING.md
├── LICENSE
├── Makefile
├── pyproject.toml
├── requirements.txt
└── requirements-dev.txt
```

Назначение основных пакетов:

```text
clash_sheet_sync_bot/bot.py                 application entrypoint
clash_sheet_sync_bot/config.py              глобальная конфигурация
clash_sheet_sync_bot/migrations.py          SQLite migrations
clash_sheet_sync_bot/models.py              доменные модели и типы
clash_sheet_sync_bot/storage.py             SQLite connection/transaction helpers
clash_sheet_sync_bot/coc/                   Clash of Clans API client
clash_sheet_sync_bot/admin/                 superadmin-flow, техподдержка и рассылки
clash_sheet_sync_bot/common/                общие helpers
clash_sheet_sync_bot/repositories/          SQLite repository layer
clash_sheet_sync_bot/setup/                 setup-flow и inline keyboards
clash_sheet_sync_bot/sheets/                Google Sheets client/admin/ranges/columns
clash_sheet_sync_bot/sync/                  sync orchestration, composition, CWL, raids, reports
clash_sheet_sync_bot/telegram/              Telegram client и access checks
```

Корневой `bot.py` оставлен как thin launcher для привычного запуска:

```bash
python bot.py
```

## Установка

Рекомендуемый путь:

```bash
sudo mkdir -p /opt/clash-sheet-sync-bot
sudo chown -R "$USER":"$USER" /opt/clash-sheet-sync-bot
cd /opt/clash-sheet-sync-bot

git clone https://github.com/BangkokToD/clash-sheet-sync-bot.git .
python3.12 -m venv .venv
. .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Настройка `.env`

Создать `.env`:

```bash
cp .env.example .env
nano .env
```

Минимальные переменные:

```env
TELEGRAM_BOT_TOKEN=put_telegram_token_here
SUPERADMIN_USER_ID=put_telegram_user_id_here
COC_API_TOKEN=put_coc_api_token_here
GOOGLE_SERVICE_ACCOUNT_FILE=credentials.json
GOOGLE_SERVICE_ACCOUNT_EMAIL=
DB_PATH=bot.db
```

Лимиты и значения по умолчанию:

```env
DEFAULT_TIMEZONE=Europe/Kyiv
DEV_MODE=False
MAX_CLANS_PER_CHAT=20
SYNC_COOLDOWN_SECONDS=60
MAX_CONCURRENT_SYNCS=3
CWL_WAR_CONCURRENCY_LIMIT=5
RAID_API_CONCURRENCY_LIMIT=5
RAID_SEASON_FETCH_LIMIT=5
RAID_ARCHIVE_SHEETS_LIMIT=4
RAID_ATTACKS_TARGET=6
RAID_NORMAL_DISTRICT_ATTACK_NORM=2
RAID_CAPITAL_DISTRICT_ATTACK_NORM=3
ADMIN_CACHE_TTL_SECONDS=300
SETUP_TOKEN_TTL_SECONDS=900
TRANSFER_TOKEN_TTL_SECONDS=900
REPORT_MAX_ITEMS=50
```

`DEV_MODE=True` отключает cooldown между последовательными `/sync` для разработки.
Если переменная отсутствует, пуста или равна `False`, cooldown работает. Защитные
блокировки одновременных sync остаются включёнными в любом режиме.

`SUPERADMIN_USER_ID` — положительный Telegram user ID единственного владельца
админского меню. Без него бот не запускается. Узнать свой ID можно у Telegram
ботов, показывающих поле `user.id`.

## Google service account

1. Создать service account в Google Cloud.
2. Включить Google Sheets API.
3. Скачать JSON key.
4. Положить файл в корень проекта как `credentials.json`.
5. Убедиться, что `.env` содержит:

```env
GOOGLE_SERVICE_ACCOUNT_FILE=credentials.json
```

Если задан `GOOGLE_SERVICE_ACCOUNT_EMAIL`, он должен совпадать с `client_email` внутри `credentials.json`.

## Запуск вручную

```bash
cd /opt/clash-sheet-sync-bot
. .venv/bin/activate
python bot.py
```

Ожидаемые логи:

```text
bot started, version=1.0.0
telegram polling started
```

## Setup-flow

1. Открыть личный чат с ботом.
2. Отправить `/start`.
3. Открыть «Мои группы» и нажать «Подключить группу».
4. Добавить бота в Telegram-группу.
5. Отправить в группе команду `/connect <token>`.
6. Вернуться в личный чат.
7. Открыть `/settings`.
8. Привязать Google Sheets.
9. Добавить service account email в Google Sheets с правами Editor.
10. Нажать «Проверить доступ».
11. Добавить хотя бы один клан.
12. Запустить `/sync` в группе.

## Администрирование

У пользователя с ID из `SUPERADMIN_USER_ID` в личном главном меню появляется
кнопка «Администрирование».

- «Подключить техподдержку» создаёт одноразовую команду
  `/connect_support <token>`. Для закрытой группы бот должен быть
  администратором с правом создавать ссылки-приглашения.
- После подключения все пользователи видят кнопку «Техподдержка» со ссылкой на
  эту группу.
- «Рассылка всем» принимает одно текстовое сообщение, показывает аудиторию и
  предпросмотр, затем требует явного подтверждения. Получатели — известные
  личные пользователи и все настроенные, не отключённые группы.

Бот начинает учитывать обычного пользователя для рассылки после его первого
взаимодействия с ботом в личном чате. Миграция также переносит в аудиторию уже
известных администраторов групп. Telegram не позволяет восстановить остальных
пользователей, которые писали боту до появления этого реестра.

## Команды Telegram

| Команда | Где | Назначение |
|---|---|---|
| `/start` | личка/группа | главное меню или короткая инструкция |
| `/help` | личка/группа | справка |
| `/connect <token>` | группа | подключение группы |
| `/connect_support <token>` | группа | подключение группы техподдержки superadmin’ом |
| `/settings` | личка/группа | настройки |
| `/accept_transfer <token>` | новая группа | перенос таблицы |
| `/sync` | подключённая группа | синхронизация |
| `/status` | подключённая группа | статус последнего sync |
| `/cancel` | личка | сброс текущего setup-state пользователя |

## Проверки

Установить dev-зависимости:

```bash
python -m pip install -r requirements-dev.txt
```

Быстрая проверка:

```bash
make check
```

Полная локальная проверка перед коммитом:

```bash
python -m ruff format .
python -m ruff check . --fix
make check
make lint
git diff --check
```

`make check` компилирует все tracked Python-файлы через `git ls-files '*.py'` и запускает тесты.

## Production

Основной production-runbook: [docs/operations.md](docs/operations.md).

Короткий минимум:

```bash
sudo systemctl status clash-sheet-sync-bot --no-pager
sudo systemctl restart clash-sheet-sync-bot
journalctl -u clash-sheet-sync-bot -n 100 --no-pager
```

Перед обновлением или ручными изменениями делать backup `bot.db`.

## Безопасность

Не коммитить:

```text
.env
credentials.json
bot.db
bot.db-shm
bot.db-wal
*.log
```

Если в публичный доступ попали `TELEGRAM_BOT_TOKEN`, `COC_API_TOKEN` или `credentials.json`, их нужно перевыпустить.

## Документация

- [SSOT.md](SSOT.md) — нормативные источники данных, приоритеты и инварианты.
- [docs/architecture.md](docs/architecture.md) — устройство и runtime-потоки.
- [docs/operations.md](docs/operations.md) — production runbook.
- [CHANGELOG.md](CHANGELOG.md) — история изменений.
- [CONTRIBUTING.md](CONTRIBUTING.md) — правила разработки.

## License

MIT License. See [LICENSE](LICENSE).
