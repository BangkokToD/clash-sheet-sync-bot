# Целевая архитектура

## 1. Принцип разделения

Forecast — отдельный use case. Его нельзя добавлять в большой
`sync/cwl.py`, потому что этот модуль формирует Sheet-модели завершённых и
текущих войн, а новая функция управляет Telegram-командами, черновыми
сессиями и прогнозным доменом.

Рекомендуемый новый package:

```text
clash_sheet_sync_bot/cwl_forecast/
├── __init__.py
├── models.py
├── domain.py
├── formatting.py
├── service.py
└── flow.py
```

Codex может уточнить разбиение, если сохранит границы ответственности и не
создаст циклические зависимости.

## 2. Ответственность компонентов

### Domain

- нормализация тегов и fingerprint League Group;
- strict parsing League Group и CWL War;
- поиск войны нашего клана;
- выбор `inWar`/ближайшей `preparation`;
- определение фактического соперника;
- сортировка actual rosters по TH/tag;
- сортировка predicted roster по TH/tag;
- объединение API-известных и ручных раундов;
- выявление неполного или конфликтного расписания.

Domain не знает о Telegram client, SQLite connection, callback data и
message IDs. Времена API парсятся в timezone-aware UTC.

### Formatting

- построение логической матрицы;
- расчёт итоговой суммы уровней ратуш для каждой колонки;
- Unicode keycap labels;
- сборка текста и custom emoji entities;
- расчёт UTF-16 offsets;
- сборка plain fallback текста.

Полезная модель результата:

```python
@dataclass(frozen=True, slots=True)
class TelegramText:
    text: str
    entities: tuple[TelegramMessageEntity, ...]
    fallback_text: str
```

Не требуется следовать имени дословно; важен единый builder, исключающий
расхождение текста и offsets.

### Service

- orchestration одного клана;
- вызовы существующего `ClashClient`;
- ограниченная конкурентность загрузки созданных CWL wars;
- чтение/валидация расписания;
- выдача typed result: ready, inactive, schedule-required, failed.

`CWL_WAR_CONCURRENCY_LIMIT` переиспользуется. Один и тот же warTag в рамках
одного запуска загружается один раз и кэшируется в памяти запуска.

### Telegram flow

- `/cwl_forecast`: доступ, persisted cooldown, per-chat singleflight,
  обработка всех runtime clans, порядок сообщений и error summary;
- `/cwl_forecast_schedule`: fresh admin checks, выбор нашего клана, пошаговые
  callbacks, подтверждение и отмена;
- callback data encode/decode;
- безопасное редактирование одного session message.

## 3. Интеграция с текущим кодом

### `bot.py`

- зарегистрировать две команды;
- forecast callbacks маршрутизировать отдельно от существующего setup flow;
- не менять семантику `/sync`, setup и transfer;
- разрешённые `getUpdates` уже включают `message` и `callback_query`, photo не
  требуется.

### `coc/client.py`

Переиспользовать существующие:

- `get_current_war_league_group()`;
- `get_cwl_war()`.

Добавлять публичную ручку только если фактический client contract не позволяет
передать требуемые данные. Не дублировать HTTP layer.

### `telegram/client.py`

- расширить `send_message`/`edit_message_text` поддержкой `entities`;
- запретить одновременные `parse_mode` и `entities`;
- ввести различимый exception для Telegram HTTP 400;
- сохранить существующую классификацию retryable/permission ошибок;
- plain fallback выполняет caller forecast, потому что только он знает, что
  ошибка относится к custom emoji сообщению.

### `telegram/emoji_catalog.py`

Новый loader существующего JSON:

- читает UTF-8;
- возвращает immutable typed catalog;
- валидирует schema и полный набор ключей;
- сообщает конкретный path/key в ошибке;
- не подменяет ошибочный каталог молчаливым default.

Загружать при composition root старта, а не на каждую команду.

### `repositories/cwl_forecast.py`

Новый repository владеет forecast tables. Все multi-step операции записи
расписания и захвата сессии транзакционны. SQL не должен проникать во flow.

### `config.py`

- `cwl_forecast_cooldown_seconds: int = 60`;
- `cwl_forecast_schedule_ttl_seconds: int = 600`;
- строгая положительная валидация с учётом действующего стиля config;
- `DEV_MODE` обрабатывается в flow/service, а не меняет распарсенное значение.

## 4. SQLite schema

Имена могут быть уточнены в соответствии со стилем репозитория. Семантика
обязательна.

### `cwl_forecast_chat_state`

```text
chat_id              INTEGER PRIMARY KEY REFERENCES telegram_chats(chat_id)
last_started_at      TEXT NOT NULL
```

Запись timestamp должна происходить атомарно при принятии команды до API.
Cooldown нельзя хранить только в памяти.

### `cwl_forecast_schedules`

```text
id                    INTEGER PRIMARY KEY
season                TEXT NOT NULL
group_fingerprint     TEXT NOT NULL
clan_tag              TEXT NOT NULL
created_by_user_id    INTEGER NOT NULL
source_chat_id        INTEGER NOT NULL
created_at            TEXT NOT NULL
updated_at            TEXT NOT NULL
UNIQUE(season, group_fingerprint, clan_tag)
```

### `cwl_forecast_rounds`

```text
schedule_id           INTEGER NOT NULL REFERENCES cwl_forecast_schedules(id)
round_number          INTEGER NOT NULL
opponent_clan_tag     TEXT NOT NULL
source                TEXT NOT NULL CHECK(source IN ('api', 'manual'))
PRIMARY KEY(schedule_id, round_number)
UNIQUE(schedule_id, opponent_clan_tag)
```

Сохранение полного расписания заменяет строки только внутри одной транзакции.

### `cwl_forecast_schedule_sessions`

```text
id                    TEXT PRIMARY KEY
season                TEXT NOT NULL
group_fingerprint     TEXT NOT NULL
clan_tag              TEXT NOT NULL
source_chat_id        INTEGER NOT NULL
created_by_user_id    INTEGER NOT NULL
message_id            INTEGER
draft_json            TEXT NOT NULL
current_step          INTEGER NOT NULL
expires_at            TEXT NOT NULL
created_at            TEXT NOT NULL
updated_at            TEXT NOT NULL
UNIQUE(season, group_fingerprint, clan_tag)
```

SQLite partial uniqueness по "неистёкшим" строкам не нужна: repository в
транзакции удаляет истёкшую сессию, затем пытается создать новую под UNIQUE.
При конфликте сообщает, что расписание уже редактируется.

JSON черновика — внутренний validated формат, не доверенный Telegram payload.
После чтения он повторно проверяется typed parser.

### Migration

- на проверенном HEAD schema version 7, целевая — 8;
- проверять актуальную версию перед реализацией;
- создать таблицы/indexes и поднять version в одной существующей migration
  модели;
- не применять к production в рамках задачи.

## 5. Идентичность расписания

```text
normalized_tags = sorted(normalize(tag) for tag in league_group.clans)
canonical = "\n".join(normalized_tags).encode("ascii")
group_fingerprint = sha256(canonical).hexdigest()
```

Если проект уже имеет общий нормализатор тегов, использовать его. Prefix `#`
должен быть представлен единообразно. Hash algorithm и canonical encoding
фиксируются тестом. Season берётся из League Group и валидируется как строка
API-сезона, а не из локальной даты сервера.

## 6. Восстановление пары из созданной войны

Для каждого реального warTag:

1. загрузить war;
2. проверить, что наш clan tag совпадает ровно с одной стороной;
3. другой side tag является соперником;
4. связать с `round_number` позиции объекта `rounds` (нумерация с 1);
5. проверить принадлежность обеих сторон League Group.

Порядок warTags внутри раунда не связан с порядком массива clans и не должен
использоваться для прогнозирования.

## 7. Singleflight и порядок побочных эффектов

Для `/cwl_forecast`:

1. проверить connected group и наличие active clans;
2. атомарно захватить process-local lock chat;
3. проверить persisted cooldown;
4. записать `last_started_at`;
5. отпустить DB transaction, но удерживать lock;
6. загрузить и сформировать результаты;
7. отправить ready forecasts по `sort_order`;
8. отправить schedule-required инструкции;
9. отправить одну technical error summary;
10. освободить lock в `finally`.

Lock предотвращает параллельный запуск в одном процессе; persisted timestamp
защищает restart. Если deployment когда-либо станет multi-process, нужна будет
отдельная distributed lease — это вне scope `1.1.0`.

## 8. Schedule callbacks

Callback payload не является источником данных. Он только адресует session и
action. Список кланов, уровни, допустимый следующий выбор и права каждый раз
восстанавливаются из SQLite + свежего API.

Пример формы, не обязательный literal:

```text
cf:<short_session_id>:pick:<opponent_index>
cf:<short_session_id>:back
cf:<short_session_id>:confirm
cf:<short_session_id>:cancel
```

Encoder обязан тестом гарантировать `len(payload.encode("utf-8")) <= 64`.

## 9. Ошибки и observability

- Логи содержат chat ID, clan tag, season/fingerprint prefix, round и category,
  но не dump полного API payload.
- Пользовательские сообщения краткие; stack trace только в log.
- Ошибка одного клана не отменяет другие кланы.
- Ошибка Telegram при отправке одного ready результата считается ошибкой
  доставки конкретного клана и не должна приводить к повторной отправке уже
  доставленных сообщений.
- Custom emoji HTTP 400 допускает ровно один plain retry.

## 10. Карта файлов

Ожидаемые изменения:

```text
clash_sheet_sync_bot/__init__.py
clash_sheet_sync_bot/bot.py
clash_sheet_sync_bot/config.py
clash_sheet_sync_bot/migrations.py
clash_sheet_sync_bot/cwl_forecast/*
clash_sheet_sync_bot/repositories/cwl_forecast.py
clash_sheet_sync_bot/telegram/client.py
clash_sheet_sync_bot/telegram/emoji_catalog.py
tests/fakes/telegram.py
tests/test_config.py
tests/test_migrations.py
tests/test_repositories.py
tests/test_cwl_forecast_*.py
tests/test_emoji_catalog.py
.env.example
README.md
SSOT.md
docs/architecture.md
docs/operations.md
CHANGELOG.md
```

Список ориентировочный. Любой дополнительный файл должен быть объяснён.
`resources/telegram_emoji_catalog.json` и существующая fixture меняются только
при доказанном дефекте их контракта.
