# Clash Sheet Sync Bot

Telegram-бот на Python 3.12 для ручной синхронизации Google Sheets с данными Clash of Clans API.

Бот работает через Telegram Bot API long polling, хранит runtime-настройки в SQLite и обновляет две управляемые зоны Google Sheets:

* `Состав`;
* `CWL`.

Синхронизация запускается вручную командой `/sync` в подключённой Telegram-группе.

---

## Возможности

### Telegram

Бот поддерживает:

* `/start` в личном чате — главное меню администратора;
* `/start` в группе — короткая инструкция по подключению;
* `/help` — справка;
* `/connect <token>` — подключение Telegram-группы;
* `/settings` — настройки группы;
* `/accept_transfer <token>` — перенос таблицы на другую группу;
* `/sync` — ручная синхронизация состава и CWL;
* `/status` — статус последней синхронизации.

Настройка выполняется через личный чат администратора с ботом.

---

## Архитектура

Проект использует:

* Python 3.12;
* Telegram Bot API через `httpx`;
* Clash of Clans API через `httpx`;
* Google Sheets API через REST и `google-auth`;
* SQLite как runtime-хранилище;
* `aiosqlite` для асинхронной работы с SQLite;
* long polling без webhook-сервера.

Проект не использует:

* PostgreSQL;
* Redis;
* Celery;
* APScheduler;
* FastAPI;
* Flask;
* Django;
* aiogram;
* python-telegram-bot.

---

## Что хранится в SQLite

SQLite-файл задаётся переменной `DB_PATH`.

В SQLite хранятся:

* подключённые Telegram-группы;
* связи администраторов с группами;
* setup-токены;
* transfer-токены;
* привязки Google Sheets;
* отслеживаемые кланы;
* профили колонок;
* состояние игроков состава;
* состояние CWL-строк;
* последние управляемые блоки Google Sheets;
* история `/sync`;
* статус последнего `/sync`.

---

## Структура проекта

```text
clash-sheet-sync-bot/
├── .env.example
├── .gitignore
├── Makefile
├── README.md
├── bot.py
├── coc_client.py
├── column_profiles.py
├── composition_sync.py
├── config.py
├── cwl_sync.py
├── migrations.py
├── models.py
├── report_builder.py
├── repositories.py
├── requirements.txt
├── setup_flow.py
├── sheet_admin.py
├── sheets_client.py
├── storage.py
├── sync_service.py
├── telegram_access.py
└── telegram_client.py
```

Назначение основных файлов:

```text
bot.py              Точка входа, Telegram polling, маршрутизация команд.
config.py           Загрузка и валидация глобальной конфигурации.
models.py           Доменные модели и общие типы.
migrations.py       SQLite-схема runtime-хранилища.
storage.py          SQLite-подключение, PRAGMA и транзакции.
repositories.py     Repository-слой для SQLite.
setup_flow.py       Подключение группы и меню настроек.
sync_service.py     Оркестрация /sync, locks, rate limit и отчёты.
report_builder.py   Формирование Telegram HTML-отчётов.
coc_client.py       Низкоуровневый клиент Clash of Clans API.
sheets_client.py    Низкоуровневый клиент Google Sheets API.
sheet_admin.py      Диагностика и подготовка Google Sheets.
composition_sync.py Синхронизация листа состава.
cwl_sync.py         Синхронизация CWL.
column_profiles.py  Дефолтные профили колонок.
telegram_client.py  Минимальный клиент Telegram Bot API.
telegram_access.py  Проверка Telegram-админов.
```

---

## Установка

Рекомендуемый путь установки:

```text
/opt/clash-sheet-sync-bot
```

Создать директорию:

```bash
sudo mkdir -p /opt/clash-sheet-sync-bot
sudo chown "$USER":"$USER" /opt/clash-sheet-sync-bot
cd /opt/clash-sheet-sync-bot
```

Создать виртуальное окружение:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

---

## Зависимости

`requirements.txt`:

```text
httpx==0.28.1
python-dotenv
google-auth
aiosqlite
```

---

## Настройка `.env`

Создать `.env`:

```bash
cp .env.example .env
```

Минимальный набор переменных:

```env
# Telegram
TELEGRAM_BOT_TOKEN=put_telegram_token_here

# Clash of Clans
COC_API_TOKEN=put_coc_api_token_here

# Google Sheets
GOOGLE_SERVICE_ACCOUNT_FILE=credentials.json
GOOGLE_SERVICE_ACCOUNT_EMAIL=

# Storage
DB_PATH=bot.db

# Defaults
DEFAULT_TIMEZONE=Europe/Kyiv

# Limits
MAX_CLANS_PER_CHAT=20
SYNC_COOLDOWN_SECONDS=60
MAX_CONCURRENT_SYNCS=3
CWL_WAR_CONCURRENCY_LIMIT=5
ADMIN_CACHE_TTL_SECONDS=300
SETUP_TOKEN_TTL_SECONDS=900
TRANSFER_TOKEN_TTL_SECONDS=900
REPORT_MAX_ITEMS=50
```

Runtime-настройки конкретных групп, таблиц, кланов и колонок не задаются через `.env`. Они создаются через Telegram setup-flow и сохраняются в SQLite.

---

## Получение Telegram bot token

1. Открыть Telegram.
2. Найти `@BotFather`.
3. Отправить команду:

```text
/newbot
```

4. Создать бота.
5. Скопировать token.
6. Записать token в `.env`:

```env
TELEGRAM_BOT_TOKEN=...
```

Если token попал в чат, логи, скриншот или репозиторий, его нужно перевыпустить.

---

## Получение Clash of Clans API token

1. Открыть Clash of Clans Developer Portal.
2. Создать API token.
3. Добавить внешний IP сервера в whitelist.
4. Записать token в `.env`:

```env
COC_API_TOKEN=...
```

Проверить внешний IP сервера:

```bash
curl -s https://api.ipify.org
echo
```

---

## Создание Google service account

1. Открыть Google Cloud Console.
2. Создать или выбрать проект.
3. Включить Google Sheets API.
4. Создать service account.
5. Создать JSON key.
6. Скачать JSON-файл.
7. Положить файл в корень проекта:

```text
/opt/clash-sheet-sync-bot/credentials.json
```

8. Убедиться, что `.env` указывает на файл:

```env
GOOGLE_SERVICE_ACCOUNT_FILE=credentials.json
```

Если задана переменная `GOOGLE_SERVICE_ACCOUNT_EMAIL`, она должна совпадать с `client_email` внутри `credentials.json`.

---

## Подключение группы

### 1. Запустить бота

```bash
cd /opt/clash-sheet-sync-bot
source .venv/bin/activate
python bot.py
```

### 2. Открыть личный чат с ботом

Отправить:

```text
/start
```

Нажать:

```text
Подключить группу
```

Бот выдаст команду вида:

```text
/connect <token>
```

### 3. Добавить бота в Telegram-группу

Пользователь, который отправляет `/connect`, должен быть администратором группы.

В группе отправить:

```text
/connect <token>
```

После этого настройка продолжится в личном чате.

### 4. Привязать Google Sheets

В личном чате открыть:

```text
/settings
```

Дальше:

```text
Таблица → Привязать таблицу
```

Отправить ссылку на Google Sheets.

Бот покажет email service account. Этот email нужно добавить в Google Sheets с правами редактора.

После выдачи доступа нажать:

```text
Проверить доступ
```

### 5. Добавить кланы

В настройках открыть:

```text
Кланы → Добавить клан
```

Отправить тег клана, например:

```text
#2RVJ0CUR9
```

Бот проверит клан через Clash of Clans API и предложит подтвердить добавление.

Когда таблица привязана и добавлен хотя бы один активный клан, группа становится готовой к `/sync`.

---

## Синхронизация

Команда запускается в подключённой группе:

```text
/sync
```

Бот:

1. Проверяет готовность группы.
2. Проверяет cooldown.
3. Берёт lock на Telegram-группу.
4. Берёт lock на Google Spreadsheet.
5. Загружает runtime-конфиг из SQLite.
6. Загружает данные Clash of Clans API.
7. Импортирует ручные значения из текущих managed-блоков Google Sheets.
8. Готовит новое состояние состава и CWL.
9. Записывает managed-блоки в Google Sheets.
10. Обновляет SQLite state.
11. Отправляет Telegram-отчёт.

Повторный `/sync` во время активной синхронизации вернёт:

```text
Обновление уже выполняется.
```

Повторный `/sync` во время cooldown вернёт сообщение с количеством секунд до повторного запуска.

---

## Статус

Команда запускается в подключённой группе:

```text
/status
```

Бот показывает:

* дату последней синхронизации;
* статус;
* последнюю ошибку;
* количество активных кланов;
* активный CWL-сезон;
* ссылку на таблицу.

---

## Лист `Состав`

Бот управляет только своими managed-блоками.

Для строк используется скрытая служебная колонка:

```text
__bot_key
```

Она нужна для стабильного переноса ручных пользовательских значений между синхронизациями.

Активные игроки группируются по отслеживаемым кланам.

Игроки, которые были в отслеживаемом клане и вышли из него, попадают в блок:

```text
Вышедшие
```

Сохраняются пользовательские значения видимых user-колонок.

---

## Лист `CWL`

Лист `CWL` показывает активный CWL-сезон.

Для строк используется скрытая служебная колонка:

```text
__bot_key
```

CWL-строки привязаны к стабильному ключу:

```text
season|clan_tag|round|attacker_tag|marker
```

Для игрока без атаки используется marker:

```text
NO_ATTACK
```

Для атак используется marker:

```text
ATTACK_1
ATTACK_2
```

Если у игрока была строка `NO_ATTACK`, а после следующей синхронизации появилась атака, пользовательские значения переносятся на первую появившуюся атаку.

---

## Диагностика таблицы

В настройках группы доступен раздел:

```text
Таблица → Проверить таблицу
```

Диагностика проверяет:

* наличие листа состава;
* наличие активного CWL-листа;
* наличие служебного листа `_bot_state`;
* совпадение Google Sheet ID между SQLite и `_bot_state`;
* доступ на запись values API;
* доступ на `spreadsheets.batchUpdate`;
* наличие `__bot_key` в известных managed-блоках;
* наличие незавершённых staging-листов CWL.

Если проблема исправима автоматически, бот покажет кнопку:

```text
Исправить
```

---

## Перенос таблицы на другую группу

В старой группе открыть:

```text
/settings → Таблица → Перенести в другую группу
```

Бот выдаст команду:

```text
/accept_transfer <token>
```

Добавить бота в новую группу и отправить там эту команду.

Пользователь должен быть администратором старой и новой группы.

После переноса:

* активная привязка таблицы переносится на новую группу;
* runtime state переносится на новый `chat_id`;
* старая группа отключается.

---

## Что нельзя делать руками в таблице

Нельзя:

* удалять или ломать скрытую колонку `__bot_key`;
* сортировать один столбец отдельно от остальных;
* переносить CWL-строки между блоками разных кланов;
* удалять служебный лист `_bot_state`;
* переименовывать managed-листы без последующей диагностики;
* вручную менять технические колонки и ожидать, что бот сохранит эти изменения.

Ручные заметки нужно вносить только в пользовательские колонки.

---

## Безопасность

Нельзя коммитить:

```text
.env
credentials.json
*.log
bot.db
bot.db-shm
bot.db-wal
```

Если в публичный доступ попали:

```text
TELEGRAM_BOT_TOKEN
COC_API_TOKEN
credentials.json
```

их нужно перевыпустить.

---

## Ручной запуск

```bash
cd /opt/clash-sheet-sync-bot
source .venv/bin/activate
python bot.py
```

Ожидаемые логи:

```text
bot started
telegram polling started
```

Остановить:

```text
Ctrl+C
```

---

## Проверка проекта

```bash
make check
```

Сейчас `make check` выполняет:

```bash
python -m py_compile *.py
```

---

## Очистка локального Python-кэша

Если после удаления файлов `grep` продолжает находить старые строки в `__pycache__`, удалить кэш:

```bash
find . -type d -name __pycache__ -prune -exec rm -rf {} +
```

---

## Backup SQLite

Перед обновлением проекта или ручными изменениями на сервере сделать backup:

```bash
cp bot.db "bot.db.backup.$(date -u +%Y%m%dT%H%M%SZ)"
```

Если включён WAL и рядом есть файлы `bot.db-shm` / `bot.db-wal`, перед backup лучше остановить сервис:

```bash
sudo systemctl stop clash-sheet-sync-bot
cp bot.db "bot.db.backup.$(date -u +%Y%m%dT%H%M%SZ)"
sudo systemctl start clash-sheet-sync-bot
```

---

## Запуск через systemd

Создать unit-файл:

```bash
sudo nano /etc/systemd/system/clash-sheet-sync-bot.service
```

Содержимое:

```ini
[Unit]
Description=Clash Sheet Sync Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/clash-sheet-sync-bot
ExecStart=/opt/clash-sheet-sync-bot/.venv/bin/python /opt/clash-sheet-sync-bot/bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
PrivateTmp=true
MemoryMax=300M
CPUQuota=70%

[Install]
WantedBy=multi-user.target
```

Применить:

```bash
sudo systemctl daemon-reload
sudo systemctl enable clash-sheet-sync-bot
sudo systemctl start clash-sheet-sync-bot
```

Проверить:

```bash
sudo systemctl status clash-sheet-sync-bot
journalctl -u clash-sheet-sync-bot -f
```

Перезапустить:

```bash
sudo systemctl restart clash-sheet-sync-bot
```

Остановить:

```bash
sudo systemctl stop clash-sheet-sync-bot
```

---

## Troubleshooting

### Бот запускается, но не отвечает

Проверить:

1. Правильный ли `TELEGRAM_BOT_TOKEN`.
2. Не запущен ли второй экземпляр бота с тем же token.
3. Есть ли интернет с сервера.
4. Логи systemd.

```bash
journalctl -u clash-sheet-sync-bot -f
```

### `CoC API HTTP 403`

Обычно причина:

* неверный `COC_API_TOKEN`;
* токен истёк;
* внешний IP сервера не добавлен в whitelist;
* бот запущен с другого IP.

Проверить IP:

```bash
curl -s https://api.ipify.org
echo
```

### Ошибка доступа к Google Sheets

Проверить:

1. Существует ли `credentials.json`.
2. Правильно ли задан `GOOGLE_SERVICE_ACCOUNT_FILE`.
3. Совпадает ли `GOOGLE_SERVICE_ACCOUNT_EMAIL`, если он задан.
4. Добавлен ли `client_email` service account в таблицу.
5. Выданы ли права редактора.

### Группа не настроена

Открыть личный чат с ботом:

```text
/settings
```

Проверить:

* таблица привязана;
* добавлен хотя бы один активный клан;
* группа не отключена.

### Таблица повреждена

Открыть:

```text
/settings → Таблица → Проверить таблицу
```

Если бот показывает исправимые проблемы, нажать:

```text
Исправить
```

---

## Обновление проекта на сервере

1. Остановить сервис:

```bash
sudo systemctl stop clash-sheet-sync-bot
```

2. Сделать backup SQLite:

```bash
cp bot.db "bot.db.backup.$(date -u +%Y%m%dT%H%M%SZ)"
```

3. Обновить файлы проекта.

4. Обновить зависимости, если изменился `requirements.txt`:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

5. Проверить проект:

```bash
make check
```

6. Запустить сервис:

```bash
sudo systemctl start clash-sheet-sync-bot
```

7. Проверить логи:

```bash
journalctl -u clash-sheet-sync-bot -f
```

---

## Быстрый чеклист production

Перед передачей проверить:

* `.env` заполнен;
* `credentials.json` лежит на сервере;
* service account имеет доступ `Editor` к Google Sheets;
* CoC API token создан;
* внешний IP сервера добавлен в CoC API whitelist;
* `make check` проходит;
* бот запускается вручную;
* systemd unit установлен;
* `/start` в личке работает;
* `/connect` в группе работает;
* `/settings` открывает настройки;
* таблица привязана;
* клан добавлен;
* `/sync` работает;
* `/status` работает;
* `bot.db` не попадает в git;
* `.env` и `credentials.json` не попадают в git.

---

## Git-проверки

```bash
git status --short
git diff --check
git check-ignore .env credentials.json bot.db bot.db-shm bot.db-wal
```

Ожидаемо:

* `git diff --check` без ошибок;
* секреты и runtime DB игнорируются;
* реальные токены не попали в diff.