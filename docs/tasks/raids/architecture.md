# Целевая архитектура учёта рейдов

## 1. Базовое состояние

Документ составлен по репозиторию на HEAD
`bc4febc0f1f5a6cf5e5410c816b52159cbd94425`.

Нормативное целевое поведение и acceptance criteria находятся в
`requirements.md`. Этот документ описывает способ реализации и не создаёт
альтернативных column keys, sorting или error semantics.

Существующий `/sync`:

```text
prepare composition
prepare CWL
write composition
write CWL + _bot_state
commit SQLite
send Telegram report
```

Схема SQLite имеет `SCHEMA_VERSION = 3`. `TableType` содержит
`composition`, `composition_active`, `composition_exited`, `cwl`.
`ColumnValueType` содержит `string`, `integer`, `datetime`.

Существующий `SheetsClient` умеет создавать, дублировать, переименовывать,
перемещать и скрывать листы, но не имеет отдельной операции удаления листа.

## 2. Целевой поток

После реализации `/sync` должен иметь следующий порядок:

```text
создать sync_run и загрузить RuntimeChatConfig
        ↓
prepare composition
        ↓
prepare CWL
        ↓
prepare raids
        ↓
write composition
        ↓
write CWL
        ↓
write raids + обновить _bot_state
        ↓
commit SQLite
        ↓
send Telegram report
```

Ни одна write-операция Google Sheets не начинается, пока не завершились все
три preparation-этапа.

Рейдовая preparation получает planned state состава только для наследования
совпадающих user-колонок. Она не добавляет игроков состава, которых нет в
`members` рейдового API.

## 3. Распределение ответственности

### `clash_sheet_sync_bot/coc/client.py`

Низкоуровневый HTTP-метод:

```python
async def get_capital_raid_seasons(
    self,
    clan_tag: str,
    *,
    limit: int,
) -> list[JsonObject]:
    ...
```

Клиент:

- кодирует тег;
- передаёт `limit` query-параметром;
- проверяет верхнеуровневый `items`;
- преобразует сетевые, HTTP и JSON-ошибки в
  `ClashApiUnavailableError`;
- не рассчитывает коэффициент и не выбирает сезон.

### `clash_sheet_sync_bot/sync/raids.py`

Новый доменный модуль отвечает за:

- строгий разбор используемых сезонов;
- классификацию района по подтверждённому `district.id = 70000000`;
- расчёт `weighted_damage_units`;
- выбор общего `season_key`;
- отсутствие backfill;
- восстановление сохранённого сезона;
- импорт user-values;
- наследование пустых значений из состава;
- построение строк и клановых блоков;
- расчёт diff;
- запись active-листа;
- staging-ротацию;
- registry архивов и pruning;
- форматирование и подсветку;
- подготовку результата для Telegram report.

Доменные dataclass-модели рейдов располагаются в этом модуле по образцу
`sync/cwl.py`. Общие runtime-типы остаются в `models.py`.

Минимальный набор моделей:

- `RaidTechnicalValues`;
- `RaidPlannedRow`;
- `RaidImportedRow`;
- `RaidImportResult`;
- `RaidClanBlock`;
- `BuiltRaidBlock`;
- `PreparedRaidSync`;
- `RaidDiffItem`;
- `RaidSheetSyncResult`.

Публичные точки входа:

```python
async def prepare_public_raid_sync(...) -> PreparedRaidSync:
    ...


async def apply_public_raid_sync(...) -> RaidSheetSyncResult:
    ...
```

Запрещено вызывать публичный `run_*`, который сам начинает запись до завершения
preparation других доменов.

### `clash_sheet_sync_bot/repositories/raid_state.py`

Новый repository-модуль содержит:

- `RaidPlayerState`;
- `RaidSheetArchive`;
- `RaidPlayerStateRepository`;
- `RaidSheetArchiveRepository`.

Repository строк:

- получает последний сохранённый сезон активных кланов;
- читает строки сезона;
- выполняет idempotent upsert по
  `(chat_id, season_key, row_key)`;
- не удаляет старые сезоны при pruning Google Sheets.

Repository архивов:

- регистрирует архив по `season_key` и `sheet_id`;
- перечисляет архивы в каноническом порядке старшинства;
- удаляет registry только после подтверждённого удаления вкладки;
- очищает stale registry через auto-fix, если вкладка уже отсутствует;
- не считает bot-owned листом вкладку только из-за совпавшего названия.

### `clash_sheet_sync_bot/repositories/sheet_blocks.py`

Нужны атомарные SQLite-операции metadata:

- перепривязать blocks старого active-листа к архивному имени при неизменном
  `sheet_id`;
- зарегистрировать blocks нового active-листа;
- удалить metadata удалённого архива;
- не затронуть composition/CWL blocks.

Название метода может следовать существующим соглашениям repository, но
операция должна выполняться одним SQL update/delete в текущей транзакции.

### `clash_sheet_sync_bot/sheets/client.py`

Добавить:

```python
async def delete_sheet(self, sheet_id: int) -> None:
    ...
```

Метод отправляет `deleteSheet` через `spreadsheets.batchUpdate`. Он принимает
только конкретный числовой `sheet_id`; удаление по имени запрещено.

Остальные операции переиспользуют существующие:

- `add_sheet`;
- `rename_sheets_atomically`;
- `move_sheet`;
- `write_values` и `batch_update_values`;
- `batch_update_spreadsheet`;
- `hide_dimension`;
- `get_spreadsheet_metadata`.

### `clash_sheet_sync_bot/sheets/admin.py`

Расширить setup и binding:

- обязательный active-лист `Рейды`;
- рейдовые поля в `SheetSetupResult`;
- новая версия `_bot_state`;
- диагностика active-листа и raid staging;
- проверка archive registry;
- auto-fix обязательного листа, `_bot_state`, stale registry и превышенного
  retention;
- размещение `Рейды` перед `CWL`.

Диагностика не получает право удалять произвольные вкладки. Auto-fix удаляет
только архивы, подтверждённые registry и фактическим `sheet_id`.

### `clash_sheet_sync_bot/sync/service.py`

Оркестратор:

- создаёт рейдовые repositories;
- выполняет raid preparation до первой записи;
- добавляет raid write phase;
- передаёт результат в report builder;
- включает `RaidDataError` в ожидаемые доменные ошибки;
- сохраняет существующие locks и semaphore.

Новые write-phase:

```text
prepared
composition_written
cwl_written
raids_written
sqlite_committed
```

`_has_sheet_write_started` должен считать все стадии после `prepared`
потенциально частичной записью.

## 4. Конфигурация

В `AppConfig`, `load_config`, `.env.example` и тестовые фабрики добавить:

```text
RAID_ARCHIVE_SHEETS_LIMIT=4
RAID_ATTACKS_TARGET=6
RAID_NORMAL_DISTRICT_ATTACK_NORM=2
RAID_CAPITAL_DISTRICT_ATTACK_NORM=3
RAID_SEASON_FETCH_LIMIT=5
RAID_API_CONCURRENCY_LIMIT=5
```

Все значения — положительные целые числа.
`RAID_SEASON_FETCH_LIMIT >= 2`.

Бизнес-логика не должна повторять литералы `4`, `6`, `2`, `3`, `5`, если
соответствующее значение доступно через `AppConfig`.

Видимое `x/6`, знаменатель коэффициента и проверка `< 6` используют одно
значение `raid_attacks_target`.

## 5. Модель SQLite

Migration version 4:

1. расширяет `sheet_bindings`:

```text
active_raid_sheet_name TEXT NOT NULL DEFAULT 'Рейды'
active_raid_sheet_id INTEGER
active_raid_season TEXT
```

2. создаёт `raid_player_state`;
3. создаёт `raid_sheet_archives`;
4. создаёт индексы и unique constraints из `requirements.md`;
5. добавляет default `raids` profiles всем существующим чатам;
6. не изменяет пользовательские composition/CWL profiles;
7. допускает повторный безопасный запуск.

`technical_values_json` строки хранит минимум:

```text
player_name
attacks
attack_limit
bonus_attack_limit
capital_resources_looted
weighted_damage_units
normal_points
coefficient
```

`weighted_damage_units` — авторитетное внутреннее целое для повторной проверки
расчёта. `normal_points` и `coefficient` — производные snapshot-поля для
восстановления и диагностики.

## 6. Стабильные ключи

Сезон:

```text
season_key = normalized UTC startTime
```

Строка:

```text
raid_row:<season_key>|<normalized_clan_tag>|<normalized_player_tag>
```

Ключ не зависит от nickname, номера строки, коэффициента и названия листа.

Блоки:

```text
raid:<clan_tag>
raid_message:<clan_tag>
```

Архив идентифицируется registry `sheet_id`, а не отображаемым названием.

## 7. Расчёт

Для каждой разобранной атаки:

```text
weighted_damage_units += destructionPercent * district_multiplier
```

где:

```text
ordinary district multiplier = raid_normal_district_attack_norm
Capital Peak multiplier = raid_capital_district_attack_norm
```

Производные:

```text
normal_points = weighted_damage_units / 100
coefficient = weighted_damage_units / (100 * raid_attacks_target)
```

Сортировка реализует нормативный порядок раздела 10.3 `requirements.md`:
`coefficient DESC`, `attacks DESC`, case-insensitive `player_name ASC`,
`player_tag ASC`. Последний ключ обеспечивает детерминированный результат.

## 8. Выбор сезона

Для каждого активного клана загружается окно последних сезонов.

1. Если есть ongoing, выбирается его `startTime`.
2. Разные ongoing `startTime` в одном sync — preparation error.
3. Если ongoing отсутствует, выбирается самый новый ended.
4. Если API-истории нет, используется последний сохранённый state.
5. Если нет ни API, ни state, строятся message-blocks.

При смене сезона:

- старый active ищется в загруженном API-окне и финализируется;
- при отсутствии используется сохранённый snapshot с warning;
- промежуточные сезоны не создаются.

## 9. Google Sheets

Активный лист:

- каноническое имя `Рейды`;
- располагается перед `CWL`;
- содержит отдельный managed block каждого отслеживаемого клана;
- скрывает первую физическую колонку `__bot_key`;
- не очищает произвольные области за пределами рассчитанного managed range.

Формат:

- `normal_points` и `coefficient`: `0.00`;
- `capital_resources_looted` и `№`: целые;
- `Атаки`: строка `x/<raid_attacks_target>`;
- красная заливка применяется только к ячейке `Атаки`, только для ended и
  только при `< raid_attacks_target`;
- структурное оформление заголовков и полос допустимо, но статусных
  зелёных/жёлтых цветов нет.

## 10. Ротация и retention

Порядок смены сезона:

1. подготовить старый и новый сезоны;
2. финализировать старый active, если доступен API;
3. создать и полностью заполнить raid staging;
4. скрыть `__bot_key`;
5. одним batchUpdate переименовать old active в архив, staging в `Рейды`;
6. переместить новый active перед `CWL`;
7. перепривязать managed-block metadata;
8. обновить binding, `_bot_state` и archive registry;
9. удалить лишние зарегистрированные архивы по одному от самого старого;
10. сохранить SQLite state общей транзакцией `/sync`.

Если удаление лишнего архива не удалось:

- новый active и созданный архив остаются;
- исключение удаления перехватывается внутри raid apply;
- binding и registry нового состояния сохраняются;
- `RaidSheetSyncResult.warnings` получает специальное cleanup warning без
  общего partial-write текста;
- sync может завершиться успешно с предупреждением;
- `sync_runs.status` и `last_sync_status` остаются `success`;
- pruning повторяется при следующем `/sync` и через auto-fix.

Это отдельный recoverable сценарий. Ошибки создания staging, заполнения,
форматирования и атомарного rename остаются ошибками write-фазы.

## 11. Пользовательские значения

Сначала выбирается источник строки user-values:

1. импортированная строка active managed block, включая пустые значения;
2. если импортированной строки нет — сохранённый raid snapshot;
3. если нет обоих источников — пустой словарь.

После этого каждое непустое значение сохраняется. Пустые поля заполняются
одноимёнными непустыми значениями planned composition state.

Намеренно очищенная рейдовая ячейка перекрывает старый raid snapshot, но может
снова получить значение из состава — это соответствует действующему правилу
CWL.

Неактивные или невидимые user-профили сохраняются в snapshot и не должны
случайно стираться.

## 12. Ошибки и восстановление

Preparation error:

- не изменяет Google Sheets;
- откатывает незакоммиченные SQLite-изменения;
- сохраняется как sync error без partial warning.

Attack counter mismatch разделяется по state:

- `ended` — strict contract error;
- `ongoing` — retryable domain error с предложением повторить sync позже;
- оба варианта завершаются до write.

Write error:

- откатывает незакоммиченный SQLite state;
- сообщает partial write warning;
- canonical resolver при следующем запуске предпочитает фактический `Рейды`;
- staging не считается active;
- повторная ротация того же сезона не создаёт второй архив.

Pruning error после завершённой ротации:

- обрабатывается как recoverable warning по разделу 10;
- не теряет новый archive registry;
- не удаляет неподтверждённые листы.

## 13. Документация и SSOT

Коммиты 1–7 реализуют и проверяют поведение, но не описывают рейды в
`SSOT.md` как существующую функцию.

Коммит 8 синхронизирует:

- `SSOT.md`;
- `README.md`;
- `docs/architecture.md`;
- `docs/operations.md`, если эксплуатационные процедуры изменились;
- `CHANGELOG.md`.

SSOT после коммита 8 должен описывать только реально существующие таблицы,
источники, snapshots, fallback, pipeline и инварианты.
