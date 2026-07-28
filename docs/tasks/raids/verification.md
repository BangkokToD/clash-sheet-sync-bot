# Проверка реализации рейдов

## 1. Принцип

Проверка состоит из четырёх уровней:

1. unit/contract tests;
2. repository и migration tests;
3. integration tests общего `/sync`;
4. ручной smoke на отдельной Google-таблице и копии SQLite.

Успешные unit-тесты формулы не заменяют проверку lifecycle листов. Ручной smoke
не заменяет автоматические failure tests.

Baseline на проверенном HEAD подтверждён вне Codex sandbox:

```text
.venv/bin/python -m pytest -q
94 passed in 0.93s
```

Внутри Codex sandbox SQLite tests могут зависать на инфраструктурном
`aiosqlite` thread wake-up. Это не считается результатом тестов: Codex
сообщает точку остановки, а пользователь повторяет полный SQLite suite вне
sandbox.

## 2. Тестовые данные

До коммита 1 проверить committed реальный ended fixture:

```text
tests/fixtures/capital_raid_seasons.json
```

Fixture содержит пять ended seasons и должен сохранять:

- минимум один ended season;
- обычный район;
- `Capital Peak`;
- несколько атак одного игрока;
- один район с атаками нескольких игроков;
- игрока с бонусной атакой;
- `members` и `attackLog`;
- все обязательные поля parser.

Перед коммитом fixture проверить:

- удалены реальные имена и теги либо заменены согласованными тестовыми;
- связи attacker tag между `members` и attack log сохранены;
- числовые значения не изменены так, чтобы нарушить контракт;
- `district.id` сохранён;
- секретов и API tokens нет.

Подтверждённый `Capital Peak district.id`: `70000000`.

Ongoing tests используют synthetic copy реального season object с заменой
только `state = "ongoing"`. Test helper применяет committed overlay
`tests/fixtures/capital_raid_seasons_ongoing.synthetic.json`; результат не
выдаётся за реальный captured API response.

## 3. Матрица автоматических тестов

| Область | Основной test-файл |
|---|---|
| Config | `tests/test_config.py` |
| CoC API/parser/formula | `tests/test_raid_sync.py` |
| Migration | `tests/test_migrations.py` |
| Repositories | `tests/test_repositories.py` |
| Active sheet/rotation/pruning | `tests/test_raid_sync.py` |
| Setup binding | `tests/test_setup_flow.py` |
| Telegram callbacks | `tests/test_setup_keyboards.py` |
| Diagnose/auto-fix | `tests/test_sheet_admin.py` |
| Reports | `tests/test_report_builder.py` |
| Общий pipeline | `tests/test_sync_service.py` |
| Startup smoke | `tests/test_smoke.py` |
| Регрессия состава | `tests/test_composition_sync.py` |
| Регрессия CWL | `tests/test_cwl_sync.py` |

## 4. Обязательные contract tests

### API

- clan tag URL-encoded;
- `limit` передан;
- `items` обязателен и является list;
- season является object;
- `state`, `startTime`, `endTime`, `members`, `attackLog` проверяются;
- обязательные поля member проверяются;
- обязательные поля district/attack проверяются;
- `bool` не принимается как int;
- destruction находится в `0..100`;
- tags нормализуются;
- число разобранных атак согласуется с member counter;
- ended mismatch является strict contract error;
- ongoing mismatch является retryable domain error до write;
- произвольный корректный non-Capital district ID считается обычным;
- конфликт названия `Capital Peak` и подтверждённого ID отклоняется.

### Формула

| Сценарий | Ожидание |
|---|---|
| Обычный район: `33%`, `67%` | `2.00` нормо-очка |
| Capital Peak: `40%`, `35%`, `25%` | `3.00` нормо-очка |
| Шесть нормативных атак | `K = 1.00` |
| Пять нормативных атак | отображение `K = 0.83` |
| Эффективность выше нормы | `K > 1.00` допустим |
| Разные игроки в одном районе | вклад считается отдельно |
| Нулевой destruction | нулевой вклад без ошибки |

Проверять точное `weighted_damage_units`, а не только округлённую строку.

## 5. Migration tests

Нужны две базы:

1. чистая пустая;
2. fixture схемы version 3 с composition/CWL данными и user profiles.

Проверки:

- version становится 4;
- повторный запуск не меняет результат;
- новые binding fields имеют безопасные defaults;
- raid tables и индексы существуют;
- raid profiles добавлены;
- старые user columns сохранены;
- composition/CWL rows сохранены;
- invalid duplicate archive отклоняется constraint;
- repository round-trip не теряет user-values и numeric technical values.

## 6. Preparation tests

- ни один fake Sheets write не вызван;
- разные ongoing seasons дают доменную ошибку;
- API members определяют набор строк;
- состав используется только для user-values;
- отсутствующий member не создаёт нулевую строку;
- manual raid value побеждает composition;
- пустое raid value наследует composition;
- old snapshot применяется между событиями;
- пропущенные промежуточные сезоны отсутствуют;
- tie-break сортировки детерминирован;
- message-block не маскируется под таблицу.

## 7. Sheet tests

- `__bot_key` — первая физическая колонка и скрыта;
- visible columns соответствуют profile order;
- числа отправляются числами;
- `0.00` применяется к двум колонкам;
- `Атаки` использует общий target;
- ongoing не получает красную status fill;
- ended `< target` красит только attack cell;
- ended `>= target` не получает status fill;
- managed range очищается без затрагивания соседних областей;
- пользовательская ширина и alignment сохраняются;
- message-block не проверяется как data block.

## 8. Rotation и failure tests

### Успех

- initial season не создаёт архив;
- новый season создаёт staging;
- staging заполнен до rename;
- rename active/archive выполняется одним batchUpdate;
- новый active имеет canonical title;
- active стоит перед CWL;
- старые blocks указывают на archive title и прежний sheet ID;
- новые blocks указывают на active;
- registry содержит archive;
- остаётся максимум четыре архива.

### Защита удаления

- совпадающее имя без registry не удаляется;
- registry sheet ID, отсутствующий в metadata, не удаляет другой лист;
- active sheet ID не может быть удалён pruning;
- oldest определяется по timestamp, не позиции вкладки;
- удаление archive sheet не удаляет player state.

### Частичные ошибки

- ошибка записи staging;
- ошибка форматирования staging;
- ошибка atomic rename;
- ошибка перемещения active;
- ошибка binding/registry после rename;
- ошибка deleteSheet;
- повторный запуск после каждого сценария;
- pruning cleanup warning сохраняет successful status и registry, не
  использует общий partial-write текст и повторяется;
- stale binding не создаёт второй active;
- staging не становится active автоматически.

## 9. Общий `/sync`

Порядок вызовов:

```text
prepare composition
prepare CWL
prepare raids
apply composition
apply CWL
apply raids
SQLite commit
Telegram report
```

Проверить:

- raid preparation error до write;
- domain errors дают понятный report;
- unexpected error не раскрывает traceback пользователю;
- любая невосстановленная write-stage error добавляет partial warning;
- recoverable pruning остаётся специальным cleanup warning успешного sync без
  общего partial-write текста;
- Telegram failure после commit не меняет success;
- locks и semaphore не обходятся;
- baseline report не разрастается;
- `/status` показывает raid season.

## 10. Целевые команды

После каждого коммита используются команды из `commit-plan.md`.

Перед коммитом 8:

```bash
python -m ruff format --check .
python -m ruff check .
make check
make lint
git diff --check
```

Дополнительно:

```bash
python -m pytest -q \
  tests/test_raid_sync.py \
  tests/test_migrations.py \
  tests/test_repositories.py \
  tests/test_sheet_admin.py \
  tests/test_setup_flow.py \
  tests/test_setup_keyboards.py \
  tests/test_report_builder.py \
  tests/test_sync_service.py
```

## 11. Подготовка ручного smoke

Использовать:

- отдельного тестового Telegram-чата;
- отдельную Google-таблицу;
- отдельную копию SQLite;
- dev-конфигурацию;
- клан, доступный текущему API;
- backup до применения migration.

Не проводить manual migration drill на единственном production-файле.

Для согласованной копии SQLite при остановленном боте можно использовать
штатный SQLite backup:

```bash
sqlite3 bot.db ".backup '/path/to/backups/bot-before-raids.db'"
```

Если база работает в WAL и бот не остановлен, копирование только `bot.db`
недостаточно.

## 12. Ручной smoke: новая установка

1. Запустить setup новой группы.
2. Привязать пустую тестовую Google-таблицу.
3. Проверить листы:
   - `Состав`;
   - `Рейды`;
   - `CWL`;
   - скрытый `_bot_state`.
4. Проверить, что `Рейды` расположен перед `CWL`.
5. Открыть `Колонки рейдов`.
6. Скрыть/показать `Золото столицы`.
7. Добавить user-колонку с тем же заголовком, что в составе.
8. Выполнить `/sync`.
9. Проверить одну строку каждого API member.
10. Сверить вручную минимум две атаки обычного района и три атаки Столицы.
11. Проверить числовые форматы и сортировку.
12. Проверить скрытый `__bot_key`.

## 13. Ручной smoke: user-values

1. Заполнить одноимённую user-колонку состава.
2. Оставить рейдовую ячейку пустой.
3. Выполнить `/sync` и проверить наследование.
4. Ввести другое значение вручную в `Рейдах`.
5. Выполнить `/sync` и проверить приоритет ручного значения.
6. Очистить рейдовую ячейку.
7. Выполнить `/sync` и проверить повторное наследование из состава.
8. Переименовать user-колонку так, чтобы совпадение исчезло.
9. Убедиться, что несвязанные значения не переносятся.

## 14. Ручной smoke: ongoing и ended

На ongoing:

- проверить отсутствие красной status fill;
- проверить обновление атак и коэффициента;
- проверить `x/6`.

После ended:

- проверить красную заливку только `Атаки` у `< 6`;
- проверить отсутствие заливки строки и коэффициента;
- проверить сохранение листа между событиями.

Если состояние события невозможно дождаться в разумный срок, оно
подтверждается автоматизированным integration test на реальном обезличенном
fixture, а ручная проверка отмечается как временно недоступная с причиной.

## 15. Ручной smoke: архивы

Пять смен сезонов воспроизводятся на отдельном тестовом harness с fake
CoC/Sheets либо последовательно на тестовых данных. Production timestamps и
registry вручную не подделываются.

Проверить:

1. old active становится архивом;
2. новый active называется `Рейды`;
3. user-values старого сезона остаются в архиве;
4. после пятого архива остаются четыре новых;
5. самый старый archive sheet удалён;
6. его SQLite player state сохранился;
7. похожая пользовательская вкладка сохранилась;
8. delete failure создаёт warning;
9. следующий sync повторяет pruning.

## 16. Диагностика и восстановление

1. Удалить active `Рейды` в тестовой таблице.
2. Запустить диагностику.
3. Проверить fixable issue.
4. Запустить auto-fix.
5. Проверить новый active и `_bot_state`.
6. Оставить raid staging через fake failure.
7. Проверить warning диагностики.
8. Удалить зарегистрированный архив вручную.
9. Проверить stale registry и его безопасную очистку.
10. Создать похожий незарегистрированный лист и проверить, что auto-fix его не
    удаляет.

## 17. Migration drill

На копии production-базы:

1. записать текущий `SCHEMA_VERSION`;
2. посчитать chats, bindings, clans, profiles, composition state и CWL state;
3. применить migration;
4. повторить подсчёты;
5. проверить raid fields/default profiles;
6. запустить приложение повторно;
7. убедиться в идемпотентности;
8. выполнить setup diagnose и один `/sync` на тестовой таблице.

## 18. Итоговый протокол

Перед закрытием задачи сохранить:

```text
HEAD:
Диапазон коммитов:
Migration drill:
Target tests:
Full make check:
Ruff:
Manual new setup:
Manual user-values:
Manual ongoing/ended:
Manual rotation/pruning:
Diagnose/auto-fix:
Известные ограничения:
```

Непройденная обязательная проверка не отмечается как успешная. Указывается
точная причина и риск.
