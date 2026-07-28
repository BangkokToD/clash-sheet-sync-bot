# План коммитов: учёт рейдовых уикендов

## Общие правила

- Базовый HEAD до task-docs: `bc4febc0f1f5a6cf5e5410c816b52159cbd94425`.
- Каждый продуктовый коммит выполняется и проверяется отдельно.
- Перед изменением существующего файла Codex перечитывает его актуальную
  версию.
- Новые параметры проходят через `AppConfig`; бизнес-логика их не хардкодит.
- Новые docstring и комментарии пишутся на русском языке в Google-style.
- Все функции и dataclass-поля получают type hints.
- Несвязанные рефакторинги запрещены.
- `SSOT.md` не меняется до коммита 8.
- Фактический `git commit` выполняется только по прямой команде пользователя.
- Списки изменяемых файлов в пунктах плана являются ожидаемыми, но не
  исчерпывающими. Дополнительный файл можно изменить только при доказанной
  необходимости, с объяснением до изменения и без выхода за scope коммита.
- SQLite pytest внутри Codex sandbox может зависать на `aiosqlite` thread
  wake-up. Codex запускает доступные проверки, а полный SQLite suite
  подтверждается пользователем вне sandbox. Baseline:
  `.venv/bin/python -m pytest -q` → `94 passed in 0.93s`.

## Предварительный docs-коммит

### Сообщение

```text
docs: add raid implementation task
```

### Состав

```text
docs/tasks/raids/README.md
docs/tasks/raids/requirements.md
docs/tasks/raids/decisions.md
docs/tasks/raids/architecture.md
docs/tasks/raids/commit-plan.md
docs/tasks/raids/verification.md
docs/tasks/raids/codex.md
```

### Ограничение

Коммит не изменяет runtime-код, tests, корневой `SSOT.md`, README и changelog.
Он не входит в нумерацию продуктовых коммитов.

---

## Коммит 1. Raid API и доменная формула

### Сообщение

```text
feat: add raid API parsing and scoring
```

### Цель

Получить строго проверенные данные рейдовых сезонов и реализовать чистый
детерминированный расчёт без SQLite и Google Sheets.

### Обязательные входы

- committed реальный ended fixture
  `tests/fixtures/capital_raid_seasons.json`;
- подтверждённый этим fixture `Capital Peak district.id = 70000000`;
- committed synthetic ongoing overlay
  `tests/fixtures/capital_raid_seasons_ongoing.synthetic.json`;
- подтверждение соответствия `members[].attacks` фактическому attack log.

Без валидного fixture Codex останавливается. ID не угадывается и не выводится
из локализованного имени.

### Изменяемые файлы

```text
.env.example
clash_sheet_sync_bot/coc/client.py
clash_sheet_sync_bot/config.py
clash_sheet_sync_bot/models.py
tests/fakes/clash.py
tests/fakes/factories.py
tests/test_config.py
tests/fixtures/capital_raid_seasons.json
tests/fixtures/capital_raid_seasons_ongoing.synthetic.json
```

### Новые файлы

```text
clash_sheet_sync_bot/sync/raids.py
tests/test_raid_sync.py
```

### Реализация

1. Расширить `AppConfig` шестью raid-параметрами из `architecture.md`.
2. Прочитать и валидировать параметры в `load_config`.
3. Добавить значения в `.env.example`.
4. Добавить `ClashClient.get_capital_raid_seasons`.
5. Проверять `limit` до HTTP-вызова.
6. Проверять верхнеуровневый `items`, тип каждого сезона и обязательные поля.
7. В `sync/raids.py` добавить:
   - raid exceptions;
   - протокольную константу Capital Peak ID `70000000`;
   - dataclass технических значений;
   - чистый parser;
   - чистый aggregator;
   - расчёт `weighted_damage_units`;
   - производные `normal_points` и `coefficient`.
8. Нормализовать player/clan tags через `normalize_tag`.
9. Отклонять `bool` в целочисленных полях.
10. Отклонять `destructionPercent` вне `0..100`.
11. Считать любой корректный district ID, кроме подтверждённого Capital Peak
    ID, обычным районом.
12. Отклонять противоречие, если API обозначает Capital Peak с другим ID.
13. Проверять согласованность разобранного количества атак и
    `members[].attacks`.
14. Для ended mismatch возвращать strict contract error.
15. Для ongoing mismatch возвращать retryable domain error до write.
16. Не использовать float для промежуточного суммирования.

### Тесты

```bash
python -m pytest -q tests/test_config.py tests/test_raid_sync.py
python -m ruff check clash_sheet_sync_bot/coc/client.py \
  clash_sheet_sync_bot/config.py \
  clash_sheet_sync_bot/models.py \
  clash_sheet_sync_bot/sync/raids.py \
  tests/test_config.py \
  tests/test_raid_sync.py
git diff --check
```

Покрыть:

- query `limit`;
- URL encoding clan tag;
- сетевую, HTTP и JSON-ошибку;
- отсутствие/неверный тип обязательных полей;
- `bool` вместо int;
- произвольный non-Capital district ID как обычный район;
- конфликт названия Capital Peak и подтверждённого ID;
- ended attack counter mismatch как strict error;
- ongoing attack counter mismatch как retryable error;
- обычный район `33% + 67% = 2,00`;
- Capital Peak `40% + 35% + 25% = 3,00`;
- шесть нормативных атак `K = 1,00`;
- пять нормативных атак `K = 0,83` при отображении;
- несколько игроков в одном районе;
- несколько районов одного игрока;
- коэффициент больше `1,00`;
- fixture целиком.

### Критерий завершения

На выходе существует проверенный API/parser/scoring слой, который ничего не
знает о binding, Sheets и SQLite.

### За пределами коммита

- миграция;
- repository;
- выбор сезона нескольких кланов;
- лист `Рейды`;
- setup;
- общий `/sync`;
- документация SSOT.

---

## Коммит 2. SQLite foundation рейдов

### Сообщение

```text
feat: add raid runtime storage
```

### Цель

Добавить версионированную схему, runtime binding, профили колонок и repositories
без включения рейдов в общий `/sync`.

### Изменяемые файлы

```text
clash_sheet_sync_bot/migrations.py
clash_sheet_sync_bot/models.py
clash_sheet_sync_bot/repositories/__init__.py
clash_sheet_sync_bot/repositories/base.py
clash_sheet_sync_bot/repositories/bindings.py
clash_sheet_sync_bot/repositories/columns.py
clash_sheet_sync_bot/repositories/sheet_blocks.py
clash_sheet_sync_bot/sheets/column_profiles.py
tests/conftest.py
tests/fakes/factories.py
tests/test_migrations.py
tests/test_repositories.py
tests/test_sheet_admin.py
```

### Новый файл

```text
clash_sheet_sync_bot/repositories/raid_state.py
```

### Реализация

1. Повысить `SCHEMA_VERSION` с `3` до `4`.
2. Добавить migration 4 без изменения уже применённых migration 1–3.
3. Расширить `sheet_bindings` raid-полями.
4. Создать `raid_player_state`.
5. Создать `raid_sheet_archives`.
6. Создать constraints и индексы из `requirements.md`.
7. Добавить `raids` в `TableType`.
8. Добавить `number` в `ColumnValueType` и strict repository parser.
9. Расширить `SheetBinding`, `RuntimeChatConfig` и `ChatSyncConfig`.
10. Расширить `RuntimeConfigRepository` и `SheetBindingRepository`.
11. Добавить отдельный метод обновления active raid binding.
12. Добавить методы чтения/upsert raid rows.
13. Добавить методы archive registry:
    - list ordered;
    - get by season/sheet ID;
    - upsert;
    - delete;
    - remove stale.
14. Добавить в `SheetBlockRepository` операции rebind/delete metadata.
15. Добавить default raid profiles и Telegram title профиля.
16. Заполнить defaults для всех существующих чатов migration-скриптом.
17. Не модифицировать существующие пользовательские profiles.

### Тесты

```bash
python -m pytest -q \
  tests/test_migrations.py \
  tests/test_repositories.py \
  tests/test_sheet_admin.py \
  tests/test_config.py
python -m ruff check clash_sheet_sync_bot tests
git diff --check
```

Покрыть:

- чистую БД до version 4;
- upgrade базы version 3;
- повторный `apply_migrations`;
- сохранение старых composition/CWL данных;
- создание raid profiles существующим чатам;
- `number` parser;
- round-trip raid row;
- round-trip archive registry;
- уникальность `season_key` и `sheet_id`;
- порядок старшинства архивов;
- rebind и удаление только выбранных raid blocks;
- transfer binding со всеми raid-полями.

### Критерий завершения

Runtime может хранить и читать raid binding/state, но никакая команда ещё не
создаёт и не записывает лист `Рейды`.

### За пределами коммита

- preparation нескольких кланов;
- Google Sheets;
- pruning;
- setup UI;
- общий `/sync`;
- SSOT.

---

## Коммит 3. Preparation и агрегированный state

### Сообщение

```text
feat: prepare raid weekend state
```

### Цель

Полностью подготовить рейдовый planned state до Google write.

### Изменяемые файлы

```text
clash_sheet_sync_bot/sync/raids.py
tests/fakes/clash.py
tests/fakes/factories.py
tests/fakes/sheets.py
tests/test_raid_sync.py
```

При необходимости только для переиспользования существующих публичных типов:

```text
clash_sheet_sync_bot/sync/composition.py
```

Изменение composition допускается только без изменения его поведения.

### Реализация

1. Реализовать `prepare_public_raid_sync`.
2. Загружать окно сезонов активных кланов с ограниченной конкуренцией.
3. Выбрать единый `season_key` по правилам ТЗ.
4. Остановить preparation при разных ongoing `startTime`.
5. Не создавать промежуточные пропущенные сезоны.
6. Найти и финализировать старый active-сезон, если он доступен.
7. Восстановить старый active из SQLite с warning, если API-окно его потеряло.
8. Строить строки только для `members`.
9. Создавать message-block клана без выбранного сезона.
10. Создать стабильный raid row key.
11. Импортировать raid user-values из зарегистрированных active blocks.
12. Использовать strict fallback идентификации только по контракту
    `requirements.md`.
13. Объединить imported values, SQLite snapshot и composition values.
14. Сопоставлять user-колонки по `column_title_identity`.
15. Не перезаписывать непустое ручное raid-значение составом.
16. Построить детерминированный рейтинг.
17. Построить diff без поатаковых элементов.
18. На preparation не выполнять ни одной write-операции.

### Тесты

```bash
python -m pytest -q tests/test_raid_sync.py tests/test_composition_sync.py
python -m ruff check clash_sheet_sync_bot/sync/raids.py tests/test_raid_sync.py
git diff --check
```

Покрыть:

- один ongoing;
- одинаковый ongoing нескольких кланов;
- конфликт ongoing start time;
- выбор newest ended;
- пустую API-историю и SQLite fallback;
- отсутствие любых данных;
- только API members;
- игрока API, ушедшего из текущего состава;
- отсутствие нулевой строки состава;
- deterministic tie-break;
- новый row key;
- imported value;
- SQLite snapshot;
- пустой raid + значение состава;
- manual raid value побеждает состав;
- невидимые snapshot-поля не стираются;
- старый active найден/не найден в API-окне;
- отсутствие backfill;
- ноль Sheet write calls.

### Критерий завершения

Preparation возвращает полностью готовые блоки, строки, warnings и diff.
Google Sheets и binding ещё не изменяются.

### За пределами коммита

- запись/форматирование;
- staging;
- pruning;
- setup;
- orchestration;
- SSOT.

---

## Коммит 4. Лист `Рейды`

### Сообщение

```text
feat: render active raid sheet
```

### Цель

Создавать или безопасно перезаписывать active-лист одного сезона без
архивирования.

### Изменяемые файлы

```text
clash_sheet_sync_bot/sync/raids.py
tests/fakes/sheets.py
tests/test_raid_sync.py
```

При необходимости низкоуровневой форматировки:

```text
clash_sheet_sync_bot/sheets/client.py
```

### Реализация

1. Реализовать `apply_public_raid_sync` для первого/неизменившегося сезона.
2. Разрешать active-лист по `sheet_id`, binding title и canonical title.
3. Строить отдельные blocks кланов.
4. Перезаписывать только рассчитанный managed range.
5. Записывать numeric значения как `int/float`, не как локализованные строки.
6. Применять формат `0.00`.
7. Скрывать физическую колонку `__bot_key`.
8. Сохранять пользовательские ширины и горизонтальное выравнивание.
9. Показывать `Атаки` как `x/<target>`.
10. Не использовать статусную заливку ongoing.
11. Для ended `< target` красить только ячейку `Атаки`.
12. Не красить коэффициент и строку.
13. Обновлять raid row state и managed block metadata.
14. Между событиями отображать сохранённый ended state.

### Тесты

```bash
python -m pytest -q \
  tests/test_raid_sync.py \
  tests/test_sheet_admin.py
python -m ruff check clash_sheet_sync_bot/sync/raids.py tests/test_raid_sync.py
git diff --check
```

Покрыть:

- матрицу и порядок колонок;
- отдельные blocks нескольких кланов;
- message-block;
- numeric cell values;
- number format;
- скрытый bot key;
- ongoing без красной заливки;
- ended `5/6` с заливкой только `Атаки`;
- ended `6/6` без статусной заливки;
- target из конфигурации;
- очистку старого managed range;
- сохранение ширины/выравнивания;
- повторную запись того же сезона;
- восстановление active по canonical title.

### Критерий завершения

Изолированный raid apply корректно поддерживает один active-сезон и SQLite
state. Смена сезона ещё не архивируется.

### За пределами коммита

- staging и архивы;
- delete sheet;
- retention;
- setup;
- общий `/sync`;
- SSOT.

---

## Коммит 5. Ротация и четыре архива

### Сообщение

```text
feat: rotate and prune raid archives
```

### Цель

Безопасно сменять active-сезон, хранить четыре архива и восстанавливаться после
частичных ошибок.

### Изменяемые файлы

```text
clash_sheet_sync_bot/repositories/raid_state.py
clash_sheet_sync_bot/repositories/sheet_blocks.py
clash_sheet_sync_bot/sheets/client.py
clash_sheet_sync_bot/sync/raids.py
tests/fakes/sheets.py
tests/test_raid_sync.py
tests/test_repositories.py
```

### Реализация

1. Добавить `SheetsClient.delete_sheet(sheet_id)`.
2. Реализовать staging title с `sync_run_id`.
3. Полностью записывать и форматировать staging до rename.
4. Генерировать уникальное архивное имя.
5. Атомарно переименовывать old active и staging.
6. Перемещать новый `Рейды` перед `CWL`.
7. Регистрировать архив по фактическому `sheet_id`.
8. Перепривязывать old managed blocks к архивному имени.
9. Регистрировать blocks нового active.
10. Обновлять active raid binding.
11. Prune по `season_start_at`, затем `archived_at`.
12. Не учитывать active в лимите.
13. Удалять только registry archive, найденный в metadata по тому же
    `sheet_id`.
14. После подтверждённого удаления очищать registry и blocks.
15. Не удалять `raid_player_state`.
16. При pruning failure сохранять binding/registry, завершать sync со status
    `success` и возвращать специальный cleanup warning без общего
    partial-write текста.
17. Повторять pruning на каждом следующем raid apply.
18. Реализовать canonical resolver незавершённой ротации.
19. Не архивировать initial message-only sheet без active season.

### Тесты

```bash
python -m pytest -q \
  tests/test_raid_sync.py \
  tests/test_repositories.py
python -m ruff check \
  clash_sheet_sync_bot/repositories/raid_state.py \
  clash_sheet_sync_bot/repositories/sheet_blocks.py \
  clash_sheet_sync_bot/sheets/client.py \
  clash_sheet_sync_bot/sync/raids.py \
  tests/test_raid_sync.py
git diff --check
```

Покрыть:

- первый сезон без архива;
- смену сезона;
- один atomic rename request;
- уникальный суффикс имени;
- перенос active перед CWL;
- четыре архива сохраняются;
- пятый удаляет один самый старый;
- несколько лишних удаляются до лимита;
- active не входит в лимит;
- пользовательский похожий лист не удаляется;
- registry с отсутствующим sheet ID не удаляет другой лист;
- raid state сохраняется;
- metadata blocks перепривязывается;
- pruning failure возвращает cleanup warning, сохраняет success и registry;
- следующий sync повторяет pruning;
- ошибка до rename;
- ошибка после rename;
- stale binding указывает на архив, но canonical active найден;
- повторный sync не архивирует сезон второй раз.

### Критерий завершения

Изолированный raid apply поддерживает полный lifecycle и recoverable pruning,
но setup и общий `/sync` ещё не подключены.

### За пределами коммита

- Telegram UI;
- `_bot_state` raid-поля;
- общий pipeline;
- отчёты;
- SSOT.

---

## Коммит 6. Setup, `_bot_state` и диагностика

### Сообщение

```text
feat: integrate raids into sheet setup
```

### Цель

Сделать raid binding обязательной частью создания, переноса, диагностики и
настроек таблицы.

### Изменяемые файлы

```text
clash_sheet_sync_bot/repositories/bindings.py
clash_sheet_sync_bot/setup/flow.py
clash_sheet_sync_bot/setup/keyboards.py
clash_sheet_sync_bot/sheets/admin.py
clash_sheet_sync_bot/sheets/column_profiles.py
tests/fakes/factories.py
tests/test_setup_flow.py
tests/test_setup_keyboards.py
tests/test_sheet_admin.py
tests/test_repositories.py
```

### Реализация

1. Добавить `DEFAULT_RAID_SHEET_NAME = "Рейды"`.
2. Расширить `SheetSetupResult`.
3. Создавать/находить обязательный active raid sheet.
4. Размещать `Рейды` перед `CWL`.
5. Повысить `_bot_state` schema version.
6. Записывать raid name, ID и season.
7. Читать legacy `_bot_state` без raid-полей как fixable state.
8. Расширить binding upsert/update/transfer.
9. Добавить раздел `Колонки рейдов`.
10. Использовать короткий callback payload, например `r`.
11. Расширить strict parsers разрешённых `TableType`.
12. Диагностировать:
    - active raid sheet;
    - raid binding fields;
    - raid bot key blocks;
    - raid staging;
    - stale registry;
    - retention overflow.
13. Auto-fix:
    - создаёт отсутствующий active;
    - переписывает `_bot_state`;
    - скрывает bot key;
    - очищает stale registry;
    - повторяет безопасный pruning.
14. Не удалять лист только по префиксу имени.

### Тесты

```bash
python -m pytest -q \
  tests/test_setup_flow.py \
  tests/test_setup_keyboards.py \
  tests/test_sheet_admin.py \
  tests/test_repositories.py
python -m ruff check \
  clash_sheet_sync_bot/setup \
  clash_sheet_sync_bot/sheets/admin.py \
  tests/test_setup_flow.py \
  tests/test_setup_keyboards.py \
  tests/test_sheet_admin.py
git diff --check
```

Покрыть:

- новую binding;
- существующий лист `Рейды`;
- legacy `_bot_state`;
- новый `_bot_state`;
- diagnose missing active;
- auto-fix missing active;
- raid staging warning;
- archive overflow;
- stale registry;
- короткий callback round-trip;
- add/rename/toggle/move/restore raid columns;
- transfer binding;
- сохранение composition/CWL полей.

### Критерий завершения

Новые и старые установки имеют корректный raid binding и UI, но `/sync` ещё
не вызывает raid preparation/apply.

### За пределами коммита

- общий orchestration;
- Telegram raid summary;
- корневая документация и SSOT.

---

## Коммит 7. Общий `/sync` и отчёты

### Сообщение

```text
feat: integrate raids into sync pipeline
```

### Цель

Включить готовый рейдовый домен в production pipeline, status и Telegram
reports без нарушения preparation/write границы.

### Изменяемые файлы

```text
clash_sheet_sync_bot/repositories/chats.py
clash_sheet_sync_bot/sync/reports.py
clash_sheet_sync_bot/sync/service.py
tests/fakes/factories.py
tests/test_report_builder.py
tests/test_sync_service.py
tests/test_smoke.py
```

Возможно:

```text
clash_sheet_sync_bot/repositories/__init__.py
```

если exports не были завершены в коммите 2.

### Реализация

1. Создать raid repositories в `SyncService`.
2. Выполнить raid preparation после CWL preparation и до первой записи.
3. Передать composition planned state для наследования.
4. Выполнить raid apply после CWL apply.
5. Добавить `WRITE_PHASE_RAIDS_WRITTEN`.
6. Обновить `_has_sheet_write_started`.
7. Обработать `RaidDataError`.
8. Не перехватывать unexpected ошибки как доменные.
9. Добавить `RaidSheetSyncResult` в success report.
10. Добавить сводку:
    - сезон и state;
    - количество участников;
    - `6/6`;
    - `< 6/6` только ended;
    - архивирование;
    - удалённый архив;
    - warning сохранённого старого state;
    - специальный cleanup warning при recoverable pruning.
11. В baseline report не перечислять большой diff.
12. Расширить `/status` active raid season.
13. Не менять гарантию Telegram delivery failure после SQLite commit.
14. Сохранить chat lock, sheet lock и global semaphore.

### Тесты

```bash
python -m pytest -q \
  tests/test_sync_service.py \
  tests/test_report_builder.py \
  tests/test_smoke.py \
  tests/test_raid_sync.py
python -m ruff check \
  clash_sheet_sync_bot/sync/service.py \
  clash_sheet_sync_bot/sync/reports.py \
  tests/test_sync_service.py \
  tests/test_report_builder.py
git diff --check
```

Покрыть:

- порядок всех prepare до write;
- raid preparation error без Sheet writes;
- composition write error;
- CWL write error;
- raid write error;
- partial warning после каждой write phase;
- recoverable pruning cleanup warning с successful commit и без общего
  partial-write текста;
- общий SQLite commit;
- Telegram delivery failure после commit;
- baseline report;
- ongoing/ended/interseason raid reports;
- `/status` с raid season;
- отсутствие raw unexpected exception у пользователя;
- старые composition/CWL pipeline tests.

### Полная проверка перед завершением

```bash
make check
make lint
git diff --check
```

### Критерий завершения

Функция полностью работает в runtime и проходит автоматические проверки.
Корневая документация всё ещё описывает старое состояние до коммита 8.

### За пределами коммита

- изменение продуктовых правил;
- дополнительный рефакторинг;
- SSOT и финальный manual smoke.

---

## Коммит 8. Документация и финальная проверка

### Сообщение

```text
docs: document raid weekend sync
```

### Предусловие

Коммиты 1–7 завершены, полный test suite зелёный, поведение вручную проверено на
тестовой таблице. SSOT нельзя обновлять раньше этого момента.

### Изменяемые файлы

```text
SSOT.md
README.md
CHANGELOG.md
docs/architecture.md
docs/operations.md
docs/tasks/raids/README.md
docs/tasks/raids/requirements.md
```

`docs/operations.md` изменяется только при появлении реальной новой
эксплуатационной процедуры.

### Реализация

1. Обновить `SSOT.md` по фактически существующему коду:
   - технический источник рейдов;
   - runtime tables и archive registry;
   - Sheets user-values;
   - managed active/staging/archive areas;
   - raid keys;
   - конфликт источников;
   - interseason fallback;
   - общий pipeline;
   - raid invariants.
2. Не переносить в SSOT нереализованные идеи и будущий средний рейтинг.
3. Обновить архитектурный поток.
4. Добавить пользовательское описание листа и конфигурации в README.
5. Добавить migration/config/backup сведения в operations только при
   необходимости.
6. Добавить запись changelog.
7. Изменить статус task docs на `реализовано`.
8. Зафиксировать фактический диапазон коммитов и результаты проверки.

### Автоматические проверки

```bash
python -m ruff format --check .
python -m ruff check .
make check
make lint
git diff --check
```

### Ручные проверки

Выполнить весь раздел `verification.md`.

### Критерий завершения

- Код, тесты, task docs, архитектура и SSOT описывают одно поведение.
- Все критерии `requirements.md` выполнены.
- Никакая будущая функция не представлена как реализованная.
- Старые composition/CWL сценарии проходят без регрессий.
