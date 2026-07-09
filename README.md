# Clash Sheet Sync Bot

Telegram-бот на Python 3.12 для ручной синхронизации Google Sheets с данными Clash of Clans API.

Бот работает через Telegram Bot API long polling, хранит runtime-настройки в SQLite и обновляет Google Sheets для подключённых Telegram-групп.

Основные сценарии:

* администратор подключает Telegram-группу через личный чат с ботом;
* администратор привязывает Google Sheets;
* администратор добавляет отслеживаемые кланы;
* команда `/sync` обновляет листы `Состав` и `CWL`;
* команда `/status` показывает результат последнего обновления.

---

## Архитектура

Проект использует:

* Python 3.12;
* Telegram Bot API через `httpx`;
* Clash of Clans API через `httpx`;
* Google Sheets API через REST и `google-auth`;
* SQLite как runtime-хранилище;
* `aiosqlite` для асинхронной работы с SQLite;
* Google service account для доступа к таблицам;
* long polling без webhook-сервера.

Проект не требует:

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

SQLite-файл задаётся переменной `DB_PATH`. По умолчанию используется:

```text
bot.db
```

В SQLite хранятся:

* подключённые Telegram-группы;
* связи Telegram-администраторов с группами;
* одноразовые setup-токены;
* одноразовые transfer-токены;
* активные привязки Google Sheets;
* отслеживаемые кланы;
* профили колонок;
* состояние игроков состава;
* состояние CWL-строк;
* metadata последних управляемых блоков Google Sheets;
* история запусков `/sync`;
* статус последнего `/sync`.

Runtime-настройки конкретных групп, таблиц, кланов и колонок **не задаются через `.env`**. Они создаются через Telegram setup-flow и сохраняются в SQLite.

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

Создать виртуальное окружение и установить зависимости:

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

Создать `.env` из примера:

```bash
cp .env.example .env
```

Минимальные глобальные секреты и пути:

```env
TELEGRAM_BOT_TOKEN=put_telegram_token_here
COC_API_TOKEN=put_coc_api_token_here
GOOGLE_SERVICE_ACCOUNT_FILE=credentials.json
GOOGLE_SERVICE_ACCOUNT_EMAIL=
DB_PATH=bot.db
```

Глобальные настройки по умолчанию:

```env
DEFAULT_TIMEZONE=Europe/Kyiv
MAX_CLANS_PER_CHAT=20
SYNC_COOLDOWN_SECONDS=60
MAX_CONCURRENT_SYNCS=3
CWL_WAR_CONCURRENCY_LIMIT=5
ADMIN_CACHE_TTL_SECONDS=300
SETUP_TOKEN_TTL_SECONDS=900
TRANSFER_TOKEN_TTL_SECONDS=900
REPORT_MAX_ITEMS=50
```

`.env` содержит только глобальные секреты, пути и лимиты процесса. Данные конкретной Telegram-группы настраиваются через бот и лежат в SQLite.

---

## Telegram bot token

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

## Clash of Clans API token

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

## Google service account

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

## Запуск вручную

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

При первом запуске бот применяет SQLite migrations и создаёт/обновляет `bot.db`.

---

## Публичный setup-flow

Настройка группы выполняется администратором через личный чат с ботом. Бот не может сам показать все группы пользователя без предварительной связи, поэтому используется команда `/connect <token>`.

### 1. Открыть личный чат с ботом

Отправить:

```text
/start
```

Бот покажет главное меню.

Нажать:

```text
Подключить группу
```

Бот выдаст одноразовую команду вида:

```text
/connect <token>
```

### 2. Добавить бота в Telegram-группу

Пользователь, который отправляет `/connect`, должен быть Telegram-администратором группы.

В нужной группе отправить:

```text
/connect <token>
```

После этого бот свяжет Telegram-группу с пользователем-администратором и предложит продолжить настройку в личном чате.

### 3. Открыть настройки

В личном чате отправить:

```text
/settings
```

Бот покажет список известных групп. Выбрать нужную группу.

### 4. Привязать Google Sheets

Открыть раздел:

```text
Таблица → Привязать таблицу
```

Отправить ссылку на Google Sheets.

Бот покажет email service account. Этот email нужно добавить в Google Sheets с правами редактора.

После выдачи доступа нажать:

```text
Проверить доступ
```

Если доступ корректный, бот подготовит служебные листы и сохранит привязку в SQLite.

### 5. Добавить кланы

Открыть раздел:

```text
Кланы → Добавить клан
```

Отправить тег клана, например:

```text
#2RVJ0CUR9
```

Бот проверит клан через Clash of Clans API и предложит подтвердить добавление.

Когда таблица привязана и добавлен хотя бы один активный клан, группа готова к `/sync`.

---

## Команды Telegram

### `/start`

В личном чате открывает главное меню администратора.

В группе показывает короткую инструкцию: настройка выполняется через личный чат с ботом.

### `/help`

Показывает краткую справку по подключению.

### `/connect <token>`

Используется в Telegram-группе для подключения группы к боту. Token создаётся в личном чате через кнопку `Подключить группу`.

### `/settings`

В личном чате показывает список известных групп и меню настройки.

В группе отправляет кнопку перехода в личный чат, если пользователь является Telegram-администратором.

### `/accept_transfer <token>`

Используется в новой Telegram-группе для переноса активной таблицы и runtime state со старой группы.

### `/sync`

Запускает ручную синхронизацию в подключённой группе.

### `/status`

Показывает статус последнего `/sync` в подключённой группе.

### `/cancel`

В личном чате сбрасывает активное состояние настройки пользователя: ожидание ссылки на таблицу, тега клана, названия колонки или переименования колонки.

---

## Синхронизация `/sync`

Команда запускается в подключённой Telegram-группе:

```text
/sync
```

Бот выполняет:

1. Проверку готовности группы.
2. Проверку cooldown.
3. Lock на Telegram-группу.
4. Lock на Google Spreadsheet.
5. Глобальный лимит одновременных sync.
6. Загрузку runtime-конфига из SQLite.
7. Загрузку данных Clash of Clans API.
8. Импорт ручных значений из текущих managed-блоков Google Sheets.
9. Подготовку нового состояния состава и CWL.
10. Запись managed-блоков в Google Sheets.
11. Обновление SQLite state.
12. Отправку Telegram-отчёта.

Повторный `/sync` во время активной синхронизации вернёт:

```text
Обновление уже выполняется.
```

Повторный `/sync` во время cooldown вернёт сообщение с количеством секунд до повторного запуска.

Если Google Sheets частично обновился, а затем sync упал, бот сохраняет честную ошибку в SQLite и показывает предупреждение:

```text
Таблица могла быть частично обновлена. Запустите диагностику и повторите /sync.
```

---

## Статус `/status`

Команда запускается в подключённой группе:

```text
/status
```

Бот показывает:

* дату последней синхронизации;
* статус последнего запуска;
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

Сохраняются ручные значения видимых user-колонок.

Если клан удалён из отслеживания, metadata старых блоков состава заменяется актуальным набором блоков, чтобы бот не держал ссылки на удалённые `composition_active:*` блоки.

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

Для атак используются markers:

```text
ATTACK_1
ATTACK_2
```

Если у игрока была строка `NO_ATTACK`, а после следующей синхронизации появилась атака, пользовательские значения переносятся на первую появившуюся атаку.

Если активные кланы возвращают разные CWL-сезоны, бот отменяет sync и показывает, какой сезон вернул каждый участвующий клан. Бот не выбирает максимальный сезон автоматически.

Количество одновременных запросов CWL wars задаётся переменной:

```env
CWL_WAR_CONCURRENCY_LIMIT=5
```

---

## Служебный лист `_bot_state`

При привязке таблицы бот создаёт или использует служебный лист:

```text
_bot_state
```

На нём хранится техническая информация о текущей привязке:

* маркер владельца managed state;
* версия служебной схемы;
* Telegram `chat_id`;
* Google Spreadsheet ID;
* название и ID листа состава;
* название и ID активного CWL-листа;
* активный CWL-сезон;
* название и ID самого `_bot_state`;
* timezone;
* дата последнего обновления служебного состояния.

Этот лист нужен для диагностики и защиты от случайной привязки не той таблицы.

Удалять или редактировать `_bot_state` вручную не нужно.

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
* наличие незавершённых staging-листов CWL;
* конфликт активной таблицы с другой Telegram-группой.

Если проблема исправима автоматически, бот покажет кнопку:

```text
Исправить
```

После ошибок частичной записи рекомендуется выполнить диагностику, исправить найденные проблемы и повторить `/sync`.

---

## Перенос таблицы на другую группу

Сценарий нужен, если таблицу и runtime state нужно перенести на другой Telegram `chat_id`.

В старой группе открыть:

```text
/settings → Таблица → Перенести в другую группу
```

Бот выдаст команду:

```text
/accept_transfer <token>
```

Добавить бота в новую группу и отправить там эту команду.

Пользователь, который принимает перенос, должен быть Telegram-администратором старой и новой группы.

После переноса:

* активная привязка таблицы переносится на новую группу;
* отслеживаемые кланы переносятся на новый `chat_id`;
* профили колонок переносятся на новый `chat_id`;
* state состава и CWL переносится на новый `chat_id`;
* metadata managed-блоков переносится на новый `chat_id`;
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

## Backup SQLite

Перед обновлением проекта или ручными изменениями на сервере нужно сделать backup `bot.db`.

Если бот остановлен:

```bash
cp bot.db "bot.db.backup.$(date -u +%Y%m%dT%H%M%SZ)"
```

Если бот запущен и рядом есть WAL-файлы `bot.db-shm` / `bot.db-wal`, сначала остановить сервис:

```bash
sudo systemctl stop clash-sheet-sync-bot
cp bot.db "bot.db.backup.$(date -u +%Y%m%dT%H%M%SZ)"
sudo systemctl start clash-sheet-sync-bot
```

Проверить backup:

```bash
ls -lh bot.db.backup.*
```

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

## Проверка проекта

```bash
make check
```

Сейчас `make check` выполняет:

```bash
python -m py_compile *.py
```

Дополнительные проверки перед коммитом:

```bash
git diff --check
git status --short
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

Проверить статус:

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

## Troubleshooting

### Бот запускается, но не отвечает

Проверить:

1. Правильный ли `TELEGRAM_BOT_TOKEN`.
2. Не запущен ли второй экземпляр бота с тем же token.
3. Есть ли интернет с сервера.
4. Не падает ли процесс по логам systemd.

```bash
journalctl -u clash-sheet-sync-bot -f
```

### `CoC API HTTP 403`

Обычно причина:

* неверный `COC_API_TOKEN`;
* token истёк;
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

### `/sync` пишет, что обновление уже выполняется

Это означает, что активен lock на Telegram-группу или Google Spreadsheet. Нужно дождаться завершения текущего sync.

Если процесс был аварийно остановлен, process-local lock исчезнет после перезапуска сервиса.

---

## Быстрый production checklist

Перед передачей владельцу бота проверить:

* `.env` заполнен;
* `credentials.json` лежит на сервере;
* service account имеет доступ `Editor` к Google Sheets;
* CoC API token создан;
* внешний IP сервера добавлен в CoC API whitelist;
* `make check` проходит;
* `bot.db` создаётся;
* бот запускается вручную;
* systemd unit установлен;
* `/start` в личке работает;
* `/connect` в группе работает;
* `/settings` открывает настройки;
* таблица привязана;
* клан добавлен;
* `/sync` работает;
* `/status` работает;
* backup `bot.db` понятен владельцу;
* `.env`, `credentials.json` и `bot.db` не попадают в git.

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

---

## License

MIT License. See [LICENSE](LICENSE).