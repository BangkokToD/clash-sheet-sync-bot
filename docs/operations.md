# Operations runbook

Документ описывает эксплуатацию `clash-sheet-sync-bot` в production: установку, обновление, backup/restore SQLite, systemd-команды и разбор типовых аварий.

README — для первичного запуска. Этот runbook — для владельца проекта, которому нужно быстро понять, что сломалось и как безопасно чинить.

## 1. Базовая модель production

Production-состояние бота держится в нескольких местах:

| Компонент | Что хранит |
|---|---|
| `bot.db` | runtime SQLite: группы, таблицы, кланы, колонки, state, sync history |
| `.env` | секреты и runtime config |
| `credentials.json` | Google service account |
| Google Sheets | пользовательские таблицы и ручные user-values |
| systemd service | автозапуск и рестарт бота |
| journalctl | runtime logs |

Критичные файлы:

```text
.env
credentials.json
bot.db
bot.db-shm
bot.db-wal
```

`bot.db` — главный production state. Потеря `bot.db` означает потерю привязок групп, tracked clans, column profiles, managed blocks и sync history.

## 2. Установка

Пример пути установки:

```text
/opt/clash-sheet-sync-bot
```

Базовая последовательность:

```bash
sudo mkdir -p /opt/clash-sheet-sync-bot
sudo chown -R "$USER":"$USER" /opt/clash-sheet-sync-bot
cd /opt/clash-sheet-sync-bot

git clone https://github.com/BangkokToD/clash-sheet-sync-bot.git .
python3 -m venv .venv
. .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Создать `.env`:

```bash
cp .env.example .env
nano .env
```

Положить Google credentials:

```text
/opt/clash-sheet-sync-bot/credentials.json
```

Проверить проект:

```bash
make check
```

Первый запуск вручную:

```bash
. .venv/bin/activate
python bot.py
```

Если вручную работает, можно подключать systemd.

## 3. Обновление

Безопасный порядок обновления:

```bash
cd /opt/clash-sheet-sync-bot

sudo systemctl stop clash-sheet-sync-bot
cp bot.db "bot.db.backup.$(date +%Y%m%d-%H%M%S)"

git pull --ff-only

. .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt

make check
sudo systemctl start clash-sheet-sync-bot
sudo systemctl status clash-sheet-sync-bot --no-pager
```

Если обновление включает миграции, они применятся при старте бота. Перед обновлением обязательно сделать backup `bot.db`.

## 4. Backup `bot.db`

SQLite может работать в WAL-режиме, поэтому рядом могут быть файлы:

```text
bot.db
bot.db-shm
bot.db-wal
```

### 4.1. Самый безопасный backup при остановленном боте

```bash
cd /opt/clash-sheet-sync-bot

sudo systemctl stop clash-sheet-sync-bot
mkdir -p backups

cp bot.db "backups/bot.db.$(date +%Y%m%d-%H%M%S)"
sudo systemctl start clash-sheet-sync-bot
```

### 4.2. Backup без остановки через sqlite `.backup`

Если установлен `sqlite3`:

```bash
cd /opt/clash-sheet-sync-bot
mkdir -p backups

sqlite3 bot.db ".backup 'backups/bot.db.$(date +%Y%m%d-%H%M%S)'"
```

Это предпочтительнее обычного `cp` на живой базе.

### 4.3. Проверить backup

```bash
sqlite3 backups/bot.db.YYYYMMDD-HHMMSS "PRAGMA integrity_check;"
```

Ожидаемо:

```text
ok
```

## 5. Restore `bot.db`

Порядок восстановления:

```bash
cd /opt/clash-sheet-sync-bot

sudo systemctl stop clash-sheet-sync-bot

cp bot.db "bot.db.before-restore.$(date +%Y%m%d-%H%M%S)"
cp backups/bot.db.YYYYMMDD-HHMMSS bot.db

rm -f bot.db-shm bot.db-wal

sudo systemctl start clash-sheet-sync-bot
sudo systemctl status clash-sheet-sync-bot --no-pager
```

После restore проверить логи:

```bash
journalctl -u clash-sheet-sync-bot -n 100 --no-pager
```

Если база восстановлена на старую версию, бот при старте применит недостающие миграции.

## 6. systemd

Пример unit-файла:

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

[Install]
WantedBy=multi-user.target
```

Путь:

```text
/etc/systemd/system/clash-sheet-sync-bot.service
```

Применить:

```bash
sudo systemctl daemon-reload
sudo systemctl enable clash-sheet-sync-bot
sudo systemctl start clash-sheet-sync-bot
```

Основные команды:

```bash
sudo systemctl status clash-sheet-sync-bot --no-pager
sudo systemctl stop clash-sheet-sync-bot
sudo systemctl start clash-sheet-sync-bot
sudo systemctl restart clash-sheet-sync-bot
sudo systemctl disable clash-sheet-sync-bot
```

## 7. journalctl

Последние логи:

```bash
journalctl -u clash-sheet-sync-bot -n 100 --no-pager
```

Следить в реальном времени:

```bash
journalctl -u clash-sheet-sync-bot -f
```

Логи за текущий день:

```bash
journalctl -u clash-sheet-sync-bot --since today --no-pager
```

Логи после конкретного времени:

```bash
journalctl -u clash-sheet-sync-bot --since "2026-07-09 12:00:00" --no-pager
```

Фильтр ошибок:

```bash
journalctl -u clash-sheet-sync-bot --since today --no-pager | grep -i "error\|exception\|failed"
```

## 8. CoC API 403

Симптомы:

```text
CoC API HTTP 403
Forbidden
access denied
invalid authorization
```

Возможные причины:

- неверный `COC_API_TOKEN`;
- token отозван;
- token создан не для текущего IP сервера;
- сервер сменил IP;
- CoC API временно недоступен;
- запрос идёт с IP, не добавленного в developer portal.

Что делать:

1. Узнать внешний IP сервера:

```bash
curl -4 ifconfig.me
```

2. Проверить, что этот IP указан в Clash of Clans Developer Portal для API token.
3. Если IP изменился — создать новый token или обновить allowed IP.
4. Обновить `.env`:

```text
COC_API_TOKEN=...
```

5. Перезапустить бота:

```bash
sudo systemctl restart clash-sheet-sync-bot
```

6. Проверить логи:

```bash
journalctl -u clash-sheet-sync-bot -n 100 --no-pager
```

## 9. Google Sheets auth

Симптомы:

```text
Не удалось загрузить service account.
Не удалось обновить Google access token.
Service account не содержит client_email.
GOOGLE_SERVICE_ACCOUNT_EMAIL не совпадает с client_email credentials.json.
Google Sheets API HTTP 403
```

Проверить файлы:

```bash
cd /opt/clash-sheet-sync-bot
ls -la credentials.json .env
```

Проверить `client_email`:

```bash
python - <<'PY'
import json
from pathlib import Path

data = json.loads(Path("credentials.json").read_text())
print(data.get("client_email"))
PY
```

Проверить `.env`:

```bash
grep -E "GOOGLE_SERVICE_ACCOUNT_FILE|GOOGLE_SERVICE_ACCOUNT_EMAIL" .env
```

Типовые причины:

- `credentials.json` отсутствует;
- путь в `GOOGLE_SERVICE_ACCOUNT_FILE` неправильный;
- JSON повреждён;
- service account email не совпадает с `.env`;
- service account не добавлен в Google Sheets как Editor;
- таблица удалена или доступ отозван;
- Google Sheets API не включён для проекта.

Что делать:

1. Убедиться, что `credentials.json` лежит в ожидаемом месте.
2. Убедиться, что `GOOGLE_SERVICE_ACCOUNT_FILE` указывает на него.
3. Сравнить `client_email` с `GOOGLE_SERVICE_ACCOUNT_EMAIL`.
4. Добавить service account email в Google Sheets с правами Editor.
5. Перезапустить бота.
6. В настройках группы запустить диагностику таблицы.

## 10. Partial write warning

Текст:

```text
Таблица могла быть частично обновлена. Запустите диагностику и повторите /sync.
```

Что означает:

- бот уже начал запись в Google Sheets;
- после этого произошла ошибка;
- Google Sheets не даёт общей транзакции на весь pipeline;
- часть таблицы могла обновиться, а часть остаться старой.

Что делать:

1. Не править таблицу вручную хаотично.
2. Открыть настройки группы.
3. Запустить диагностику таблицы.
4. Если есть исправимые проблемы — нажать auto-fix.
5. Повторить `/sync`.
6. Если ошибка повторяется — смотреть `sync_runs.error_stage` и journal logs.

Полезный SQLite-запрос:

```bash
sqlite3 bot.db "
SELECT id, chat_id, status, started_at, finished_at, error_stage, error_message
FROM sync_runs
ORDER BY id DESC
LIMIT 5;
"
```

Интерпретация `error_stage`:

| stage | Смысл |
|---|---|
| `prepared` | ошибка до записи Google Sheets |
| `composition_written` | ошибка после начала записи состава |
| `cwl_written` | ошибка после начала записи CWL |
| `sqlite_committed` | SQLite уже сохранил success |

## 11. CWL season mismatch

Симптом:

```text
CWL-сезоны активных кланов не совпадают
```

Что означает:

- CoC API вернул разные `season` для активных tracked clans;
- бот запрещает смешивать разные CWL seasons в одном active CWL-листе;
- запись должна быть остановлена до изменения таблицы.

Что делать:

1. Проверить список active clans в настройках.
2. Убедиться, что все кланы реально участвуют в одном CWL-сезоне.
3. Временно отключить/удалить из tracked clans клан с другим season.
4. Повторить `/sync`.

Не рекомендуется вручную объединять такие данные в одном CWL-листе: это ломает перенос user-values и архивирование сезонов.

## 12. Повреждённый `__bot_key`

Симптомы в отчёте/warnings:

```text
повреждён __bot_key
fallback невозможен
использован fallback по Тегу
использован fallback по техническим колонкам
```

Что такое `__bot_key`:

- скрытая service-колонка;
- основной стабильный ключ строки;
- нужна для связи строки Google Sheets с SQLite state.

Причины повреждения:

- пользователь раскрыл скрытую колонку и изменил значение;
- пользователь удалил первую колонку managed block;
- пользователь скопировал/переставил строки вместе с повреждением ключа;
- таблица была вручную сильно отредактирована.

Что делать:

1. Запустить диагностику таблицы.
2. Если диагностика предлагает auto-fix — выполнить.
3. Повторить `/sync`.
4. Если проблема только в одной строке и user-values важны:
   - вручную перенести user-values в корректную строку после sync;
   - не редактировать `__bot_key`.
5. Если таблица сильно повреждена:
   - сделать копию таблицы;
   - отвязать/привязать новую таблицу;
   - запустить sync;
   - перенести ручные значения вручную.

## 13. Перенос таблицы

Transfer flow нужен, когда одна Google-таблица и runtime-настройки должны переехать в другую Telegram-группу.

Порядок:

1. В старой группе открыть настройки.
2. В разделе таблицы выбрать перенос.
3. Бот выдаст команду:

```text
/accept_transfer <token>
```

4. Добавить бота в новую группу.
5. В новой группе отправить эту команду.
6. Команду должен отправить пользователь, который является админом старой и новой группы.

Что переносится:

- active sheet binding;
- tracked clans;
- column profiles;
- composition player state;
- CWL row state;
- sheet blocks.

Что происходит со старой группой:

```text
status = disabled
setup_state = NULL
```

Если перенос не проходит:

- проверить, что token не истёк;
- проверить, что token не использован;
- проверить, что пользователь админ обеих групп;
- проверить, что у новой группы нет active binding;
- смотреть journalctl.

## 14. Бот не отвечает

Порядок диагностики.

### 14.1. Проверить systemd

```bash
sudo systemctl status clash-sheet-sync-bot --no-pager
```

Если service упал:

```bash
journalctl -u clash-sheet-sync-bot -n 100 --no-pager
```

После исправления:

```bash
sudo systemctl restart clash-sheet-sync-bot
```

### 14.2. Проверить `.env`

```bash
cd /opt/clash-sheet-sync-bot
grep -E "TELEGRAM_BOT_TOKEN|COC_API_TOKEN|GOOGLE_SERVICE_ACCOUNT_FILE" .env
```

Не выводить секреты в публичные чаты. Для проверки достаточно увидеть, что переменные есть и не пустые.

### 14.3. Проверить сеть

```bash
curl -I https://api.telegram.org
curl -I https://api.clashofclans.com
curl -I https://sheets.googleapis.com
```

### 14.4. Проверить Python-зависимости

```bash
cd /opt/clash-sheet-sync-bot
. .venv/bin/activate
python -m py_compile *.py
```

### 14.5. Проверить Telegram token

Симптомы неправильного token:

```text
Unauthorized
401
Not Found
```

Что делать:

1. Проверить `TELEGRAM_BOT_TOKEN` в `.env`.
2. Сверить token с BotFather.
3. Перезапустить service.
4. Проверить journalctl.

### 14.6. Проверить polling conflict

Если бот запущен дважды, Telegram может отдавать конфликт polling.

Симптомы:

```text
Conflict: terminated by other getUpdates request
```

Что делать:

```bash
ps aux | grep bot.py
sudo systemctl stop clash-sheet-sync-bot
pkill -f "python.*bot.py"
sudo systemctl start clash-sheet-sync-bot
```

## 15. Диагностика SQLite

Проверить целостность:

```bash
sqlite3 bot.db "PRAGMA integrity_check;"
```

Проверить последние группы:

```bash
sqlite3 bot.db "
SELECT chat_id, title, status, last_sync_status, last_sync_error
FROM telegram_chats
ORDER BY updated_at DESC
LIMIT 10;
"
```

Проверить активные bindings:

```bash
sqlite3 bot.db "
SELECT chat_id, google_sheet_id, composition_sheet_name, active_cwl_sheet_name, active_cwl_season
FROM sheet_bindings
WHERE is_active = 1;
"
```

Проверить active clans:

```bash
sqlite3 bot.db "
SELECT chat_id, clan_tag, clan_name, sort_order
FROM tracked_clans
WHERE is_active = 1
ORDER BY chat_id, sort_order;
"
```

Проверить managed blocks:

```bash
sqlite3 bot.db "
SELECT chat_id, sheet_name, block_key, start_cell, rows_count, columns_count
FROM sheet_blocks
ORDER BY chat_id, sheet_name, block_key;
"
```

## 16. Чего не делать

Не делать в production без backup:

- удалять `bot.db`;
- вручную чистить `sheet_blocks`;
- вручную менять `column_profiles`;
- вручную менять `chat_id` в runtime tables;
- редактировать скрытую колонку `__bot_key`;
- запускать два экземпляра бота с одним token;
- менять Google Sheet structure во время `/sync`;
- делать restore базы без остановки service.

## 17. Быстрый emergency checklist

Если production сломан:

1. Остановить бота:

```bash
sudo systemctl stop clash-sheet-sync-bot
```

2. Сделать backup текущей базы:

```bash
cp bot.db "bot.db.emergency.$(date +%Y%m%d-%H%M%S)"
```

3. Посмотреть последние логи:

```bash
journalctl -u clash-sheet-sync-bot -n 200 --no-pager
```

4. Проверить базу:

```bash
sqlite3 bot.db "PRAGMA integrity_check;"
```

5. Исправить причину:
   - `.env`;
   - `credentials.json`;
   - CoC API token/IP;
   - Google Sheets access;
   - systemd unit;
   - restore DB.

6. Запустить:

```bash
sudo systemctl start clash-sheet-sync-bot
sudo systemctl status clash-sheet-sync-bot --no-pager
```

7. Проверить `/status` в группе.
8. Запустить диагностику таблицы, если были Google Sheets ошибки.
9. Повторить `/sync`.
