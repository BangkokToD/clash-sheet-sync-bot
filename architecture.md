# Архитектура clash-sheet-sync-bot

Документ описывает runtime-архитектуру бота: жизненный цикл приложения, Telegram flow, setup-flow, `/sync` pipeline, источники истины и защитные механизмы записи.

README остаётся документом для запуска и эксплуатации. Этот файл нужен как архитектурная карта проекта.

## 1. Общая модель

`clash-sheet-sync-bot` — Telegram-бот для синхронизации Clash of Clans данных в Google Sheets.

В системе три основных источника данных:

| Источник | Роль |
|---|---|
| SQLite | Runtime source of truth: настройки групп, привязки таблиц, tracked clans, column profiles, state, sync history |
| Google Sheets | Пользовательское рабочее пространство и storage ручных user-values |
| Clash of Clans API | Technical source: текущий состав кланов, CWL league group, CWL wars |

Ключевой принцип: бот не пытается считать Google Sheets главным состоянием системы. Таблица — внешний интерфейс для людей и место, где живут ручные значения. Runtime-решения принимает SQLite.

## 2. BotApp lifecycle

`BotApp` управляет жизненным циклом Telegram polling и обработкой update.

Базовый цикл:

1. Приложение читает конфигурацию из `.env`.
2. Открывает SQLite.
3. Применяет миграции.
4. Запускает Telegram polling.
5. Для каждого Telegram update открывает отдельное SQLite connection.
6. На этом connection создаёт сервисы и repositories.
7. Обрабатывает update.
8. Закрывает connection после завершения обработки update.

Разделение connection per update критично:

- не держится один общий SQLite connection на весь polling;
- параллельные Telegram update не делят один mutable connection;
- транзакции одного update не протекают в другой update;
- проще локализовать rollback/commit;
- меньше риск lock/state конфликтов внутри long-running bot process.

## 3. Telegram update flow

Высокоуровневый flow:

```text
Telegram update
        ↓
BotApp._handle_update
        ↓
определение типа update
        ↓
message / callback_query
        ↓
команда или setup-flow callback/text
        ↓
нужный service/repository
        ↓
Telegram response
```

Основные входы:

| Вход | Обработчик |
|---|---|
| `/start` в личке | главное меню личного чата |
| `/start` в группе | короткая инструкция |
| `/connect <token>` | подключение группы |
| `/accept_transfer <token>` | перенос таблицы в другую группу |
| `/settings` | указатель в личный чат / меню настроек |
| `/cancel` | сброс setup-state пользователя |
| `/sync` | staged sync pipeline |
| `/status` | последний sync summary |
| callback query | setup/settings navigation |
| private text | продолжение setup-flow состояния |

## 4. Setup-flow

Setup-flow отвечает за подключение Telegram-группы, привязку Google Sheets, управление кланами и колонками.

Основные состояния лежат в `telegram_chats.setup_state`.

Типичные setup_state:

```text
awaiting_sheet_link:<user_id>
awaiting_sheet_access:<user_id>:<spreadsheet_id>
awaiting_clan_tag:<user_id>
awaiting_user_column_title:<user_id>:<table_type>
awaiting_column_rename:<user_id>:<table_type>:<column_key>
```

### 4.1. Подключение группы

1. Админ в личке нажимает «Подключить группу».
2. Бот создаёт одноразовый setup token.
3. Админ добавляет бота в группу.
4. Админ отправляет `/connect <token>` в группе.
5. Бот проверяет:
   - token существует;
   - token не использован;
   - token создан этим user_id;
   - user_id является Telegram-администратором группы.
6. Бот создаёт/обновляет:
   - `telegram_chats`;
   - `chat_admin_links`.
7. Настройка продолжается в личке.

### 4.2. Привязка Google Sheets

1. Админ открывает раздел «Таблица».
2. Отправляет ссылку на Google Sheets.
3. Бот извлекает spreadsheet id.
4. Бот проверяет, что таблица не привязана к другой active group.
5. Бот показывает service account email.
6. Админ выдаёт доступ в Google Sheets.
7. Админ нажимает «Проверить доступ».
8. Бот создаёт обязательные листы:
   - `Состав`;
   - `CWL`;
   - `_bot_state`.
9. Бот записывает binding в SQLite.
10. Бот создаёт default column profiles.

### 4.3. Настройки кланов

Tracked clans живут в SQLite в `tracked_clans`.

Кланы:

- добавляются по тегу через CoC API validation;
- мягко удаляются через `is_active = 0`;
- сортируются через `sort_order`;
- при удалении клана active players этого клана переводятся в `untracked`.

### 4.4. Настройки колонок

Column profiles живут в SQLite в `column_profiles`.

Поддерживаются профили:

```text
composition_active
composition_exited
cwl
```

Колонки бывают:

| kind | Смысл |
|---|---|
| service | служебные колонки бота, например `__bot_key` |
| system | технические видимые колонки |
| user | пользовательские ручные колонки |

Service-колонки нельзя скрывать/переименовывать/удалять через UI. Они нужны для стабильного сопоставления строк.

## 5. SQLite как runtime source of truth

SQLite — главный runtime source of truth.

В SQLite живут:

- Telegram chats;
- admin links;
- setup tokens;
- transfer tokens;
- sheet bindings;
- tracked clans;
- column profiles;
- composition player state;
- CWL row state;
- managed sheet blocks;
- sync runs;
- последний sync status/error.

Google Sheets не используется как источник настроек. Если пользователь поменяет порядок/названия/видимость колонок через Telegram UI, истина сохраняется в SQLite, а следующая синхронизация применяет её к таблице.

## 6. Google Sheets как user-values storage

Google Sheets хранит пользовательские ручные значения в user-колонках.

Перед записью новых managed blocks бот импортирует текущие значения из таблицы:

- читает предыдущие managed blocks;
- находит строки по `__bot_key`;
- переносит user-values в planned state;
- после этого перезаписывает managed ranges.

Если `__bot_key` повреждён, бот пытается fallback по техническим колонкам там, где это безопасно. Если fallback невозможен, строка пропускается и добавляется warning.

Google Sheets — это не свободная таблица целиком. Бот управляет только своими managed blocks.

## 7. CoC API как technical source

Clash of Clans API — источник технических данных.

Для состава:

- список участников tracked clans;
- player tag;
- nickname;
- town hall;
- clan tag.

Для CWL:

- current war league group;
- season;
- rounds;
- warTags;
- wars;
- attacks;
- stars;
- destruction percentage;
- town hall участников.

Technical fields не берутся из Google Sheets как истина. Если пользователь руками поменяет техническую колонку, следующая синхронизация восстановит значение из CoC API.

## 8. `/sync` pipeline

`/sync` выполняется staged-подходом: сначала подготовка всех данных, потом запись.

Упрощённый pipeline:

```text
/start sync run
        ↓
load RuntimeChatConfig from SQLite
        ↓
prepare composition
        ↓
prepare CWL
        ↓
write composition to Google Sheets
        ↓
write CWL to Google Sheets
        ↓
write SQLite state
        ↓
commit SQLite
        ↓
send Telegram report
```

Смысл staged-подхода:

- до начала записи в Google Sheets собрать максимум данных;
- если CoC API или импорт таблицы падает на этапе подготовки, таблица ещё не тронута;
- если ошибка случилась после начала записи в Google Sheets, пользователь получает partial write warning.

## 9. Locks и concurrency control

В `/sync` используются три уровня ограничения конкурентности.

### 9.1. Chat lock

Chat lock защищает от двух одновременных `/sync` в одной Telegram-группе.

```text
CHAT_SYNC_LOCKS[chat_id]
```

Если sync уже идёт для группы, новый `/sync` получает сообщение:

```text
Обновление уже выполняется.
```

### 9.2. Sheet lock

Sheet lock защищает одну Google-таблицу от одновременной записи из разных Telegram-групп.

```text
SHEET_SYNC_LOCKS[google_sheet_id]
```

Это важно из-за transfer-сценариев и защиты от двойной привязки. Даже если разные чаты теоретически указывают на один spreadsheet, одновременно писать нельзя.

### 9.3. Global semaphore

Global semaphore ограничивает общую параллельность sync по процессу.

```text
GLOBAL_SEMAPHORE
```

Лимит берётся из `.env` / `AppConfig`:

```text
MAX_CONCURRENT_SYNCS
```

Это защищает:

- CoC API от всплеска запросов;
- Google Sheets API от лишней нагрузки;
- SQLite и event loop от перегруза.

## 10. Managed blocks

Managed block — прямоугольная область Google Sheets, которой управляет бот.

Metadata managed blocks хранится в SQLite в `sheet_blocks`.

Каждый block описывает:

```text
chat_id
sheet_name
sheet_id
block_key
start_cell
rows_count
columns_count
```

Примеры block_key:

```text
composition_active:#AAA111
composition_exited
cwl:#AAA111
cwl_message:#AAA111
```

Бот использует managed blocks, чтобы:

- знать, какие области таблицы были записаны прошлым sync;
- импортировать user-values из правильных диапазонов;
- очищать старые области перед записью новых;
- форматировать только управляемые диапазоны;
- скрывать служебные колонки.

## 11. `__bot_key`

`__bot_key` — скрытая service-колонка в managed blocks.

Она нужна для стабильной идентификации строк независимо от:

- сортировки;
- изменения nickname;
- изменения town hall;
- перемещения игрока;
- ручных правок видимых колонок.

Примеры значений:

```text
composition_player:#PLAYER
cwl_row:<season>|<clan_tag>|<round>|<attacker_tag>|<marker>
```

Колонка физически записывается в таблицу, но скрывается через Google Sheets API. Пользователь не должен её редактировать.

Если `__bot_key` повреждён, бот может попытаться восстановить связь по видимым technical columns. Но это fallback, а не основной контракт.

## 12. `_bot_state`

`_bot_state` — служебный лист Google Sheets.

Он хранит минимальный state о binding:

```text
managed_by
schema_version
chat_id
google_sheet_id
composition_sheet_name
composition_sheet_id
active_cwl_sheet_name
active_cwl_sheet_id
active_cwl_season
bot_state_sheet_name
bot_state_sheet_id
timezone
updated_at
```

Назначение `_bot_state`:

- диагностика привязанной таблицы;
- проверка, что SQLite binding соответствует таблице;
- восстановление sheet IDs через auto-fix;
- безопасная служебная write-проверка;
- хранение активного CWL-листа и сезона рядом с таблицей.

Лист `_bot_state` скрывается от пользователя.

## 13. CWL season mismatch

CWL season mismatch запрещён.

Причина: публичный CWL-лист строится как единый лист активного сезона для всех tracked clans. Если CoC API возвращает разные `season` для разных активных кланов, нельзя безопасно смешивать строки в один active CWL.

В такой ситуации sync должен остановиться до записи таблицы.

Это защищает от:

- смешивания разных CWL-сезонов;
- порчи активного CWL-листа;
- некорректного архивирования;
- неправильного переноса user-values между сезонами.

Пользователь получает ошибку с перечислением кланов и сезонов.

## 14. Partial write warning

Partial write warning означает, что ошибка произошла после начала записи в Google Sheets.

Текст:

```text
Таблица могла быть частично обновлена. Запустите диагностику и повторите /sync.
```

Когда warning не добавляется:

- ошибка во время подготовки данных;
- CoC API упал до записи;
- импорт Google Sheets упал до записи;
- RuntimeChatConfig не найден;
- любая ошибка до `composition_written`.

Когда warning добавляется:

- ошибка после начала записи состава;
- ошибка после начала записи CWL;
- unexpected exception после старта write-фазы.

Зачем это нужно:

- не обещать пользователю атомарность Google Sheets;
- явно сказать, что таблица могла остаться в промежуточном состоянии;
- направить пользователя в диагностику;
- оставить `error_stage` в `sync_runs`.

SQLite commit при успешном sync происходит после Google Sheets write. Если Telegram report не доставлен уже после успешного commit, сохранённый success не откатывается.

## 15. Sync history и status

Каждый `/sync` создаёт запись в `sync_runs`.

В sync history фиксируются:

```text
chat_id
started_by_user_id
status
started_at
finished_at
error_stage
error_clan_tag
error_war_tag
error_message
report_json
```

`/status` берёт summary из SQLite:

- последний старт sync;
- последнее завершение sync;
- последний статус;
- последнюю ошибку;
- количество active clans;
- активный CWL season;
- spreadsheet URL.

## 16. Transfer flow

Transfer flow переносит активную таблицу и runtime state на другой Telegram chat.

Основная идея:

1. В старой группе создаётся transfer token.
2. В новой группе админ отправляет `/accept_transfer <token>`.
3. Бот проверяет, что user_id админ старой и новой группы.
4. Активная sheet binding переносится на новый chat_id.
5. Runtime state переносится:
   - tracked clans;
   - column profiles;
   - composition player state;
   - CWL row state;
   - sheet blocks.
6. Старый chat получает status `disabled`.
7. Новый chat получает status `ready`.

## 17. Инварианты

Ключевые инварианты проекта:

- SQLite — runtime source of truth.
- Google Sheets — внешний user workspace и storage ручных user-values.
- CoC API — источник technical values.
- `__bot_key` — основной ключ строки.
- Managed blocks нельзя рассматривать как произвольные пользовательские области.
- `/sync` не должен писать Google Sheets до завершения preparation-фазы.
- После начала записи Google Sheets любая ошибка должна давать partial write warning.
- Одна Telegram-группа не должна иметь два параллельных sync.
- Одна Google-таблица не должна получать две параллельные записи.
- CWL-лист не должен смешивать разные seasons.
- Telegram delivery failure после успешного SQLite commit не откатывает success.
