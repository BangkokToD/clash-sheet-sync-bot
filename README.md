# Clash Sheet Sync Bot

Лёгкий Telegram-бот на Python 3.12 для ручного обновления Google Sheets по данным Clash of Clans API.

Бот обновляет две сущности:

* `Состав` трёх семейных кланов;
* `CWL` / `ЛВК` по трём семейным кланам.

Обновление запускается только вручную через Telegram-кнопки.

Автообновления, БД, web-сервер и тяжёлые Telegram-фреймворки не используются.

---

## Возможности

### Telegram

Бот работает через Telegram Bot API long polling и поддерживает:

* `/start` — показывает две кнопки:

  * `Обновить состав`;
  * `Обновить CWL`;
* `/status` — показывает результаты последних ручных запусков;
* доступ только для Telegram user ID из `.env`;
* защиту от параллельных запусков через общий `asyncio.Lock`.

Если одна операция уже выполняется, повторное нажатие любой кнопки вернёт:

```text
Операция уже выполняется.
```

Если пользователь не разрешён через `.env`, бот отвечает:

```text
Нет доступа.
```

---

## Что обновляет бот

### Лист `Состав`

При нажатии `Обновить состав` бот:

1. Получает составы трёх кланов из Clash of Clans API.
2. Читает старый лист `Состав` в пределах `COMPOSITION_MANAGED_RANGE`.
3. Сохраняет пользовательские поля по `player tag`.
4. Проверяет дубли тегов.
5. Пересобирает активные таблицы кланов.
6. Пересобирает таблицу вышедших.
7. Записывает новое состояние листа.
8. Форматирует управляемые таблицы.
9. Сохраняет статус запуска в `sync_settings.json`.
10. Отправляет отчёт в Telegram.

Активные таблицы располагаются слева друг под другом.

Таблица вышедших располагается справа.

Пользовательские поля сохраняются по колонке `Тег`.

### Лист `CWL`

При нажатии `Обновить CWL` бот:

1. Получает `currentwar/leaguegroup` по трём кланам.
2. Обрабатывает ситуацию `CWL не проводится`.
3. Собирает уникальные `warTag`.
4. Загружает CWL-войны с ограничением конкурентности.
5. Читает старый лист `CWL`.
6. Читает лист `Состав` для актуальных username.
7. Строит новые CWL-таблицы.
8. Переносит пользовательские колонки по стабильному ключу строки.
9. Переносит пользовательские поля со строки `NO_ATTACK` на первую появившуюся атаку.
10. Записывает новый лист.
11. Форматирует управляемые таблицы.
12. Сохраняет статус запуска в `sync_settings.json`.
13. Отправляет отчёт в Telegram.

---

## Ограничения проекта

Проект должен оставаться лёгким:

* Python 3.12;
* без PostgreSQL;
* без SQLite;
* без Redis;
* без Celery;
* без APScheduler;
* без FastAPI;
* без Flask;
* без Django;
* без aiogram;
* без python-telegram-bot;
* без webhook-сервера;
* без автообновления;
* без фонового scheduler.

Telegram Bot API вызывается напрямую через `httpx`.

Google Sheets API вызывается через REST-запросы и access token от `google-auth`.

---

## Структура проекта

```text
clash-sheet-sync-bot/
├── .env
├── .env.example
├── .gitignore
├── README.md
├── bot.py
├── coc_client.py
├── composition_sync.py
├── config.py
├── cwl_sync.py
├── models.py
├── requirements.txt
├── settings_store.py
└── sheets_client.py
```

Назначение основных файлов:

```text
bot.py              Telegram long polling, команды, кнопки, lock, статусы.
config.py           Загрузка и валидация .env.
models.py           Общие модели и нормализация тегов.
settings_store.py   Атомарное хранение sync_settings.json.
coc_client.py       Низкоуровневый клиент Clash of Clans API.
sheets_client.py    Низкоуровневый клиент Google Sheets API.
composition_sync.py Синхронизация листа Состав.
cwl_sync.py         Синхронизация листа CWL.
```

---

## Установка на сервер

Рекомендуемый путь установки:

```text
/opt/clash-sheet-sync-bot
```

### 1. Установить Python 3.12

Проверь версию:

```bash
python3.12 --version
```

Ожидаемо:

```text
Python 3.12.x
```

### 2. Скопировать проект

Пример:

```bash
sudo mkdir -p /opt/clash-sheet-sync-bot
sudo chown "$USER":"$USER" /opt/clash-sheet-sync-bot
cd /opt/clash-sheet-sync-bot
```

Дальше скопировать файлы проекта в эту папку.

### 3. Создать виртуальное окружение

```bash
cd /opt/clash-sheet-sync-bot
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Если `python3.12 -m venv` недоступен, сначала установи пакет venv для своей ОС.

---

## Зависимости

`requirements.txt`:

```txt
httpx==0.28.1
python-dotenv
google-auth
```

---

## Настройка `.env`

Создать `.env` из примера:

```bash
cp .env.example .env
```

Заполнить:

```env
# Telegram
TELEGRAM_BOT_TOKEN=put_telegram_token_here
TELEGRAM_ALLOWED_USER_IDS=123456789

# Clash of Clans
COC_API_TOKEN=put_coc_api_token_here

# Google Sheets
GOOGLE_SHEET_ID=put_google_sheet_id_here
GOOGLE_SERVICE_ACCOUNT_FILE=credentials.json

# Листы
COMPOSITION_SHEET_NAME=Состав
CWL_SHEET_NAME=CWL

# Кланы
CLAN_1_TAG=#AAAAAAA
CLAN_1_NAME=Clan 1

CLAN_2_TAG=#BBBBBBB
CLAN_2_NAME=Clan 2

CLAN_3_TAG=#CCCCCCC
CLAN_3_NAME=Clan 3

# Размещение на листе "Состав"
COMPOSITION_ACTIVE_START_CELL=A1
COMPOSITION_EXITED_START_CELL=J1
COMPOSITION_MANAGED_RANGE=A1:R1000

# Размещение на листе "CWL"
CWL_START_CELL=A1
CWL_MANAGED_RANGE=A1:Q2000

# Время
TIMEZONE=Europe/Kyiv
```

---

## Описание переменных `.env`

### Telegram

```env
TELEGRAM_BOT_TOKEN=put_telegram_token_here
```

Токен Telegram-бота от `@BotFather`.

```env
TELEGRAM_ALLOWED_USER_IDS=123456789,987654321
```

Список Telegram user ID, которым разрешено управлять ботом.

ID указываются через запятую.

Пробелы допускаются.

Пустое значение не означает доступ для всех. Если оставить переменную пустой, доступ не получит никто.

### Clash of Clans

```env
COC_API_TOKEN=put_coc_api_token_here
```

Токен Clash of Clans API из Clash of Clans Developer Portal.

Важно: токен привязан к IP whitelist. Внешний IP сервера должен быть добавлен в настройках токена.

### Google Sheets

```env
GOOGLE_SHEET_ID=put_google_sheet_id_here
```

ID Google-таблицы.

Пример:

```text
https://docs.google.com/spreadsheets/d/GOOGLE_SHEET_ID/edit
```

```env
GOOGLE_SERVICE_ACCOUNT_FILE=credentials.json
```

Путь к JSON-файлу Google service account.

Обычно файл кладётся в корень проекта под именем:

```text
credentials.json
```

### Листы

```env
COMPOSITION_SHEET_NAME=Состав
CWL_SHEET_NAME=CWL
```

Названия листов, с которыми бот имеет право работать.

Остальные листы бот не должен читать, очищать или изменять.

### Кланы

```env
CLAN_1_TAG=#AAAAAAA
CLAN_1_NAME=Clan 1

CLAN_2_TAG=#BBBBBBB
CLAN_2_NAME=Clan 2

CLAN_3_TAG=#CCCCCCC
CLAN_3_NAME=Clan 3
```

Теги и названия трёх семейных кланов.

Теги должны начинаться с `#`.

Названия используются в Telegram-отчётах и в названиях блоков на листах.

### Размещение листа `Состав`

```env
COMPOSITION_ACTIVE_START_CELL=A1
COMPOSITION_EXITED_START_CELL=J1
COMPOSITION_MANAGED_RANGE=A1:R1000
```

`COMPOSITION_ACTIVE_START_CELL` — где начинается зона активных кланов.

`COMPOSITION_EXITED_START_CELL` — где начинается таблица вышедших.

`COMPOSITION_MANAGED_RANGE` — область, которую бот имеет право очищать и перезаписывать значениями.

### Размещение листа `CWL`

```env
CWL_START_CELL=A1
CWL_MANAGED_RANGE=A1:Q2000
```

`CWL_START_CELL` — где начинается CWL-лист.

`CWL_MANAGED_RANGE` — область, которую бот имеет право очищать и перезаписывать значениями.

### Время

```env
TIMEZONE=Europe/Kyiv
```

IANA-таймзона для дат в статусах и отчётах.

---

## Получение Telegram bot token

1. Открыть Telegram.
2. Найти `@BotFather`.
3. Отправить:

```text
/newbot
```

4. Указать имя бота.
5. Указать username бота.
6. Скопировать token.
7. Записать token в `.env`:

```env
TELEGRAM_BOT_TOKEN=...
```

Если token попал в чат, логи, скриншот или репозиторий — его нужно перевыпустить через `@BotFather`.

---

## Получение Telegram user ID

Варианты:

### Через специального бота

1. Открыть Telegram.
2. Найти бота для показа user ID, например `@userinfobot`.
3. Скопировать свой numeric ID.
4. Добавить его в `.env`:

```env
TELEGRAM_ALLOWED_USER_IDS=123456789
```

### Через временный запуск

Можно временно запустить бота, написать ему `/start` и посмотреть update через Bot API, но проще использовать отдельного бота для user ID.

---

## Получение Clash of Clans API token

1. Открыть Clash of Clans Developer Portal.
2. Войти в аккаунт.
3. Создать API token.
4. В IP whitelist добавить внешний IP сервера.
5. Скопировать token.
6. Записать token в `.env`:

```env
COC_API_TOKEN=...
```

Проверить внешний IP сервера:

```bash
curl -s https://api.ipify.org
echo
```

Проверить CoC API вручную:

```bash
source .venv/bin/activate
python - <<'PY'
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

token = os.environ["COC_API_TOKEN"]
url = "https://api.clashofclans.com/v1/clans/%232RVJ0CUR9/members"

response = httpx.get(
    url,
    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    timeout=20,
)

print(response.status_code)
print(response.text[:500])
PY
```

Если ответ `403`, почти всегда причина в token или IP whitelist.

---

## Создание Google service account

1. Открыть Google Cloud Console.
2. Создать проект или выбрать существующий.
3. Включить Google Sheets API.
4. Создать service account.
5. Создать JSON key для service account.
6. Скачать JSON-файл.
7. Положить файл в корень проекта:

```text
/opt/clash-sheet-sync-bot/credentials.json
```

8. Убедиться, что `.env` указывает на него:

```env
GOOGLE_SERVICE_ACCOUNT_FILE=credentials.json
```

---

## Выдача service account доступа к Google Sheets

1. Открыть `credentials.json`.
2. Найти поле:

```json
"client_email": "..."
```

3. Открыть нужную Google-таблицу.
4. Нажать `Share` / `Поделиться`.
5. Добавить `client_email`.
6. Выдать права `Editor` / `Редактор`.

Без прав редактора бот не сможет писать в таблицу.

---

## Подготовка Google Sheets

В таблице должны существовать листы:

```text
Состав
CWL
```

Их названия должны совпадать с `.env`:

```env
COMPOSITION_SHEET_NAME=Состав
CWL_SHEET_NAME=CWL
```

Бот работает только с configured managed ranges:

```env
COMPOSITION_MANAGED_RANGE=A1:R1000
CWL_MANAGED_RANGE=A1:Q2000
```

Внутри этих диапазонов бот может очищать и перезаписывать значения.

Остальные листы бот не трогает.

---

## Лист `Состав`

### Активные таблицы

Активные кланы идут слева друг под другом, начиная с:

```env
COMPOSITION_ACTIVE_START_CELL=A1
```

Каждый блок содержит колонки:

```text
№
Тег
Ратуша
Никнейм
Юзернейм
Имя
Закрепление
Нарушения
```

Название блока — название клана без тега.

Пример:

```text
NeverNight
Duskborn
Sunfall
```

### Таблица вышедших

Таблица вышедших начинается с:

```env
COMPOSITION_EXITED_START_CELL=J1
```

Колонки:

```text
№
Тег
Ратуша
Никнейм
Юзернейм
Имя
Закрепление
Нарушения
Дата выхода
```

Название блока:

```text
Вышедшие
```

### Что сохраняется

Технические поля обновляются из Clash of Clans API:

```text
Тег
Ратуша
Никнейм
```

Пользовательские поля сохраняются по `Тег`:

```text
Юзернейм
Имя
Закрепление
Нарушения
```

`Дата выхода` сохраняется у уже вышедших игроков.

При новом выходе дата ставится в момент обнаружения выхода ботом.

### Что считается изменением

Бот учитывает:

* новых игроков;
* обновление ратуши;
* обновление ника;
* переход между семейными кланами;
* выход из семьи;
* возвращение из таблицы вышедших.

---

## Лист `CWL`

Лист `CWL` всегда показывает текущий CWL-сезон.

Вверху листа пишется:

```text
CWL season: YYYY-MM
```

Кланы идут друг под другом.

Название блока — название клана без тега.

Колонки CWL:

```text
Раунд
Тег
Ник
Юзернейм
ТХ - номер
ТХ соперника - номер
Звезды
Процент разрушений
Идея1
Реализация1
Сложность цели1
Комментарий1
Идея2
Реализация2
Сложность цели2
Комментарий2
Итоговая оценка
```

### Формат TH

Формат:

```text
1 — TH18
```

Примеры:

```text
1 — TH18
7 — TH16
15 — TH13
```

Такой формат используется для:

```text
ТХ - номер
ТХ соперника - номер
```

### Строки без атаки

Если игрок был в войне раунда, но не атаковал, бот записывает строку с пустыми значениями:

```text
ТХ соперника - номер = ""
Звезды = ""
Процент разрушений = ""
```

Бот не пишет `0`, потому что `0` может означать реальную атаку на 0 звёзд и 0%.

### Пользовательские поля CWL

Пользовательские колонки:

```text
Идея1
Реализация1
Сложность цели1
Комментарий1
Идея2
Реализация2
Сложность цели2
Комментарий2
Итоговая оценка
```

Они переносятся по стабильному ключу строки.

Ключ не виден в таблице и не записывается отдельной колонкой.

Формат ключа для строки без атаки:

```text
season|clan_tag|round|attacker_tag|NO_ATTACK
```

Формат ключа для строки с атакой:

```text
season|clan_tag|round|attacker_tag|DEF_POS_N
```

Если раньше была строка `NO_ATTACK`, а после обновления атака появилась, пользовательские поля переносятся на первую появившуюся атаку этого игрока в этом раунде.

### Совместимость со старыми колонками `Ожидания`

Старый лист может содержать колонки:

```text
Ожидания1
Ожидания2
Ожидания3
```

Бот читает их как алиасы новых колонок:

```text
Сложность цели2
```

В новый лист всегда пишутся новые названия:

```text
Сложность цели2
```

---

## Форматирование листов

Бот форматирует управляемые блоки обычными запросами `spreadsheets.batchUpdate`.

Бот применяет:

* цвет строк названия;
* цвет строк заголовков;
* жирный белый текст для названий и заголовков;
* рамки таблиц;
* чередование фона строк;
* перенос текста;
* вертикальное выравнивание.

Бот не должен сбрасывать вручную выставленную ширину колонок.

Бот не должен намеренно менять горизонтальное выравнивание, если ты выставил его вручную.

Форматирование применяется только к управляемым диапазонам `Состав` и `CWL`.

Остальные листы бот не должен трогать.

---

## Что нельзя делать руками в таблице

### Нельзя ломать колонку `Тег`

`Тег` — главный идентификатор игрока.

Если изменить или удалить тег игрока, бот не обязан восстанавливать игрока по нику, имени или username.

Нельзя писать тег без `#`.

Пример правильно:

```text
#ABC123
```

Пример неправильно:

```text
ABC123
```

Если бот найдёт непустой тег без `#`, обновление будет отменено.

### Нельзя делать дубли тегов

Если один и тот же player tag найден на листе `Состав` больше одного раза, обновление состава отменится.

CWL тоже может быть отменён, потому что он читает username с листа `Состав`.

### Нельзя сортировать один столбец отдельно

Можно сортировать строки таблицы целиком.

Нельзя сортировать только одну колонку, например только `Юзернейм` или только `Никнейм`.

Если разрушить соответствие ячеек внутри строки, бот не обязан это восстанавливать.

### Нельзя переносить CWL-строки между таблицами кланов

CWL-ключ зависит от семейного клана, раунда, игрока и цели атаки.

Перенос строк между блоками разных кланов не поддерживается.

### Не писать заметки вне пользовательских колонок

Пользовательские поля состава:

```text
Юзернейм
Имя
Закрепление
Нарушения
```

Пользовательские поля CWL:

```text
Идея1
Реализация1
Сложность цели1
Комментарий1
Идея2
Реализация2
Сложность цели2
Комментарий2
Итоговая оценка
```

Заметки вне этих колонок внутри managed ranges не гарантируются к сохранению.

---

## Безопасность

Нельзя коммитить:

```text
.env
credentials.json
sync_settings.json
*.log
```

Эти файлы должны быть в `.gitignore`.

Текущий `.gitignore`:

```gitignore
.env
credentials.json
sync_settings.json
*.log
__pycache__/
.venv/
.pytest_cache/
.ruff_cache/
```

Если токены попали в чат, логи, скриншот или репозиторий — их нужно перевыпустить.

Особенно важно перевыпустить:

```text
TELEGRAM_BOT_TOKEN
COC_API_TOKEN
credentials.json
```

---

## Ручной запуск

Из папки проекта:

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

## Проверка синтаксиса

```bash
cd /opt/clash-sheet-sync-bot
source .venv/bin/activate
python -m py_compile bot.py config.py models.py settings_store.py coc_client.py sheets_client.py composition_sync.py cwl_sync.py
```

---

## Проверка `.gitignore`

```bash
git check-ignore .env credentials.json sync_settings.json
```

Ожидаемо все три файла должны быть проигнорированы.

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
MemoryMax=200M
CPUQuota=50%

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
```

Смотреть логи:

```bash
journalctl -u clash-sheet-sync-bot -f
```

Перезапустить после изменения `.env`:

```bash
sudo systemctl restart clash-sheet-sync-bot
```

Остановить:

```bash
sudo systemctl stop clash-sheet-sync-bot
```

---

## Проверка Telegram

1. Запустить бота.
2. Открыть Telegram.
3. Написать боту:

```text
/start
```

Ожидаемо бот покажет кнопки:

```text
Обновить состав
Обновить CWL
```

Проверить статус:

```text
/status
```

Ожидаемый формат:

```text
Последнее обновление состава: дата / ещё не запускалось
Статус состава: успешно / ошибка / -
Ошибка состава: текст ошибки / -

Последнее обновление CWL: дата / ещё не запускалось
Статус CWL: успешно / ошибка / -
Ошибка CWL: текст ошибки / -
```

---

## Финальная проверка на тестовой таблице

Перед запуском на основной таблице желательно сделать копию Google Sheets и проверить всё на ней.

### 1. Подготовить тестовую таблицу

1. Создать копию основной Google-таблицы.
2. Убедиться, что есть листы:

   * `Состав`;
   * `CWL`.
3. Выдать service account права `Editor`.
4. В `.env` поставить ID тестовой таблицы:

```env
GOOGLE_SHEET_ID=test_sheet_id
```

5. Перезапустить бота.

### 2. Проверить `Состав`

1. Нажать в Telegram `Обновить состав`.
2. Ожидать в логах:

```text
composition sync started
composition sync finished
```

3. Проверить на листе:

   * активные кланы слева;
   * `Вышедшие` справа;
   * названия блоков без тегов;
   * пользовательские колонки пустые или сохранены;
   * цвета и рамки применены.

4. Вписать вручную значения в пользовательские поля:

```text
Юзернейм
Имя
Закрепление
Нарушения
```

5. Снова нажать `Обновить состав`.

Ожидаемо пользовательские поля сохраняются по `Тег`.

### 3. Проверить защиту от дублей

1. В тестовой таблице вручную продублировать один player tag на листе `Состав`.
2. Нажать `Обновить состав`.

Ожидаемо:

```text
Обновление состава отменено.

Причина: найден дубль тега #... на листе "Состав".
```

Лист не должен быть перезаписан.

После проверки дубль нужно удалить.

### 4. Проверить `CWL`

1. Нажать `Обновить CWL`.
2. Если CWL проводится, ожидаемые логи:

```text
cwl sync started
cwl sync finished
```

3. Проверить лист:

   * сверху `CWL season: YYYY-MM`;
   * блоки кланов идут друг под другом;
   * названия блоков без тегов;
   * строки без атаки присутствуют;
   * для строк без атаки пустые:

     * `ТХ соперника - номер`;
     * `Звезды`;
     * `Процент разрушений`;
   * формат TH вида `1 — TH18`;
   * пользовательские колонки на месте.

4. Вписать вручную значения в пользовательские поля CWL:

```text
Идея1
Реализация1
Сложность цели1
Комментарий1
Идея2
Реализация2
Сложность цели2
Комментарий2
Итоговая оценка
```

5. Снова нажать `Обновить CWL`.

Ожидаемо пользовательские поля сохраняются по CWL-ключу.

### 5. Проверить `/status`

После успешных запусков:

```text
/status
```

Должно показать `успешно` для состава и CWL.

---

## Troubleshooting

### Бот пишет `Нет доступа.`

Причина: Telegram user ID не добавлен в `.env`.

Проверить:

```env
TELEGRAM_ALLOWED_USER_IDS=123456789
```

После изменения `.env` перезапустить бота.

Для systemd:

```bash
sudo systemctl restart clash-sheet-sync-bot
```

### Бот запускается, но не отвечает

Проверить:

1. Правильный ли `TELEGRAM_BOT_TOKEN`.
2. Не запущен ли второй экземпляр бота с тем же token.
3. Есть ли интернет с сервера.
4. Логи:

```bash
journalctl -u clash-sheet-sync-bot -f
```

### `CoC API HTTP 403`

Обычно причина:

* неверный `COC_API_TOKEN`;
* токен истёк или перевыпущен;
* внешний IP сервера не добавлен в whitelist;
* бот запущен с другого IP.

Проверить внешний IP:

```bash
curl -s https://api.ipify.org
echo
```

Добавить этот IP в Clash of Clans Developer Portal.

После изменения токена обновить `.env` и перезапустить бота.

### `API недоступно.`

Бот возвращает это сообщение при ошибках Clash of Clans API:

```text
timeout
network error
HTTP 5xx
HTTP 403
HTTP 429
битый JSON
невозможность распарсить обязательные поля
```

Если ошибка временная, можно повторить позже.

Если повторяется постоянно — проверить token, IP whitelist и доступность API.

### `Не удалось прочитать Google Sheets.`

Проверить:

1. `GOOGLE_SHEET_ID`.
2. Существуют ли листы `Состав` и `CWL`.
3. Правильно ли указан `GOOGLE_SERVICE_ACCOUNT_FILE`.
4. Есть ли файл `credentials.json`.
5. Выдан ли service account доступ `Editor`.

### `Не удалось записать Google Sheets.`

Проверить:

1. Права service account.
2. Не защищены ли нужные диапазоны от редактирования.
3. Не удалены ли листы.
4. Корректны ли managed ranges.
5. Есть ли место в диапазонах `COMPOSITION_MANAGED_RANGE` и `CWL_MANAGED_RANGE`.

### `CWL не проводится.`

Это нормальная ситуация, если CWL сейчас не активна у всех трёх кланов.

В этом случае лист `CWL` не меняется.

### Часть кланов не участвует в CWL

Если часть кланов участвует, а часть нет:

* участвующие кланы обновляются;
* под неучаствующими кланами пишется `CWL не проводится`;
* в Telegram-отчёте появляется список таких кланов.

### Пользовательские поля не сохранились

Проверить:

1. Не был ли изменён `Тег`.
2. Не был ли тег удалён.
3. Не был ли тег продублирован.
4. Не сортировалась ли одна колонка отдельно от остальных.
5. Не переносились ли CWL-строки между блоками кланов.

### Ширина колонок или выравнивание изменились

Бот не должен намеренно сбрасывать ручную ширину столбцов и горизонтальное выравнивание.

Если это произошло:

1. Проверить, не включён ли вручную auto resize в самой таблице.
2. Проверить, не применялось ли форматирование ко всему диапазону вручную.
3. Проверить актуальность кода `composition_sync.py` и `cwl_sync.py`.

### Битый `sync_settings.json`

Если файл повреждён, бот может не стартовать или не читать статус.

Можно удалить файл:

```bash
rm sync_settings.json
```

При следующем запуске бот создаст новое состояние статусов.

### В логи попали секреты

Если в логи, чат или скриншот попали:

```text
TELEGRAM_BOT_TOKEN
COC_API_TOKEN
credentials.json
```

их нужно перевыпустить.

---

## Логи

Логи пишутся в stdout.

Минимальные события:

```text
bot started
telegram polling started
composition sync started
composition sync finished
composition sync failed: reason
cwl sync started
cwl sync finished
cwl sync failed: reason
api unavailable: reason
google sheets read failed: reason
google sheets write failed: reason
bot stopped
```

Пустые Telegram polling-ответы не логируются.

Полные API-ответы не логируются.

Секреты логировать нельзя.

---

## Обновление проекта на сервере

1. Остановить сервис:

```bash
sudo systemctl stop clash-sheet-sync-bot
```

2. Обновить файлы проекта.

3. Обновить зависимости, если изменился `requirements.txt`:

```bash
cd /opt/clash-sheet-sync-bot
source .venv/bin/activate
python -m pip install -r requirements.txt
```

4. Проверить синтаксис:

```bash
python -m py_compile bot.py config.py models.py settings_store.py coc_client.py sheets_client.py composition_sync.py cwl_sync.py
```

5. Запустить сервис:

```bash
sudo systemctl start clash-sheet-sync-bot
```

6. Проверить логи:

```bash
journalctl -u clash-sheet-sync-bot -f
```

---

## Быстрый чеклист запуска

```bash
cd /opt/clash-sheet-sync-bot
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

Заполнить `.env`.

Положить:

```text
credentials.json
```

Проверить синтаксис:

```bash
python -m py_compile bot.py config.py models.py settings_store.py coc_client.py sheets_client.py composition_sync.py cwl_sync.py
```

Запустить:

```bash
python bot.py
```

Открыть Telegram:

```text
/start
```

Нажать:

```text
Обновить состав
Обновить CWL
```

Проверить:

```text
/status
```

---

## Production checklist

Перед передачей проекта проверить:

* `.env` заполнен;
* `credentials.json` лежит на сервере;
* service account имеет доступ `Editor` к Google Sheets;
* CoC API token создан;
* внешний IP сервера добавлен в CoC API whitelist;
* Telegram user ID добавлены в `TELEGRAM_ALLOWED_USER_IDS`;
* бот запускается вручную;
* `/start` показывает две кнопки;
* `/status` работает;
* `Обновить состав` работает на тестовой таблице;
* пользовательские поля состава сохраняются;
* дубли тегов блокируют обновление;
* `Обновить CWL` работает во время CWL;
* пользовательские поля CWL сохраняются;
* `sync_settings.json` создаётся и обновляется;
* `.env`, `credentials.json`, `sync_settings.json` не попадают в git;
* systemd unit установлен;
* service стартует после перезагрузки сервера.

---

## Команды для git-проверки

```bash
git status --short
git diff --check
git check-ignore .env credentials.json sync_settings.json
```

Ожидаемо:

* `git diff --check` без ошибок;
* секретные файлы игнорируются;
* реальные токены не попали в diff.

---

## Лицензия и передача

Проект предназначен для внутреннего использования клановой семьи Clash of Clans.

Перед передачей другому администратору нужно отдельно передать:

* путь к серверу;
* способ входа на сервер;
* где лежит проект;
* где лежит `.env`;
* где лежит `credentials.json`;
* какой Google Sheet используется;
* какие Telegram user ID имеют доступ;
* как перевыпускать Telegram token;
* как перевыпускать CoC API token;
* как менять IP whitelist CoC API;
* как смотреть логи systemd.
