# Contributing

Документ описывает правила работы с проектом `clash-sheet-sync-bot`.

Проект учебный, но целится в production-ready качество. Поэтому изменения должны быть маленькими, проверяемыми и согласованными с текущей архитектурой.

## 1. Базовый workflow

Перед изменениями:

```bash
git status
```

После изменений:

```bash
python -m ruff format .
python -m ruff check . --fix
make check
make lint
git diff --check
```

Минимальная проверка для каждого коммита:

```bash
make check
```

Если менялись style/imports:

```bash
make lint
```

## 2. Коммиты

Коммиты должны быть тематическими.

Хорошо:

```text
fix: keep composition sheet blocks metadata consistent
test: add sync service write failure tests
refactor: extract setup keyboards
docs: add operations runbook
```

Плохо:

```text
fix all
update
misc
final changes
```

Один коммит должен решать одну задачу:

- fix;
- test;
- refactor;
- docs;
- chore.

Не смешивать в одном коммите:

- бизнес-логику и форматирование;
- миграции и README;
- tests и unrelated cleanup;
- refactor и изменение поведения.

## 3. Архитектурные инварианты

Ключевые правила проекта:

- SQLite — runtime source of truth.
- Google Sheets — user workspace и storage ручных user-values.
- CoC API — technical source.
- `__bot_key` — основной стабильный ключ строки.
- Managed blocks — единственные области таблицы, которыми управляет бот.
- `/sync` не должен писать Google Sheets до завершения preparation-фазы.
- Ошибка после начала записи Google Sheets должна давать partial write warning.
- Одна Telegram-группа не должна иметь два параллельных sync.
- Одна Google-таблица не должна получать две параллельные записи.
- CWL-лист не должен смешивать разные seasons.
- Telegram delivery failure после успешного SQLite commit не откатывает success.

Если изменение нарушает один из этих пунктов, сначала нужно менять архитектурный документ и тесты.

## 4. SQLite и миграции

Правила:

- Не менять схему молча.
- Поднимать `SCHEMA_VERSION`, если нужна миграция.
- Новая миграция должна быть идемпотентной.
- Существующие production-базы должны обновляться без ручного SQL.
- Не удалять legacy data без отдельного cleanup-коммита и теста.
- Перед опасными изменениями делать backup `bot.db`.

Проверки:

```bash
make check
```

Желательно иметь тест, который:

- применяет миграции на пустую БД;
- применяет миграции повторно;
- проверяет результат на legacy data.

## 5. Google Sheets

Правила:

- Не считать Google Sheets главным runtime state.
- Не писать в пользовательские области вне managed blocks.
- Перед перезаписью managed blocks импортировать user-values.
- Не удалять ручные user-values без необходимости.
- Не ломать `__bot_key`.
- Любая ошибка после начала записи должна быть явно отражена как partial write warning.

Google Sheets write не является общей транзакцией. Поэтому код должен честно различать:

```text
ошибка до записи
ошибка после начала записи
```

## 6. Telegram access

Чувствительные действия требуют fresh admin check:

- привязка таблицы;
- смена таблицы;
- отвязка таблицы;
- диагностика/auto-fix;
- создание transfer token;
- добавление/удаление/перемещение кланов;
- создание/переименование/удаление/перемещение колонок.

Обычное открытие меню может использовать admin cache.

## 7. `/sync` pipeline

Не смешивать preparation и write.

Правильная структура:

```text
prepare composition
prepare CWL
write composition
write CWL
commit SQLite
send Telegram report
```

Если нужно добавить новый этап:

1. Добавить подготовку до write-фазы.
2. Добавить явный write phase.
3. Обновить error handling.
4. Добавить tests на partial write behavior.

## 8. Tests

Тесты должны фиксировать контракты, а не реализацию ради реализации.

Обязательные зоны покрытия:

- migrations;
- repositories;
- setup-flow access;
- sync-service error handling;
- composition planning;
- CWL season/row key logic;
- report builder;
- config validation.

Fake-объекты лежат в:

```text
tests/fakes/
```

Не плодить локальные fake-классы в тестах, если они переиспользуются.

## 9. Ruff и форматирование

Перед коммитом:

```bash
python -m ruff format .
python -m ruff check . --fix
make lint
```

Не делать отдельный большой style-only diff внутри смыслового коммита.

Если нужен массовый формат — отдельный chore/refactor-коммит.

## 10. Документация

README — для запуска и базовой эксплуатации.

Архитектура:

```text
docs/architecture.md
```

Production runbook:

```text
docs/operations.md
```

Если изменение затрагивает runtime contract, обновить документацию в том же коммите или отдельным соседним docs-коммитом.

## 11. Dependencies

Runtime dependencies фиксируются в `requirements.txt`.

Dev dependencies фиксируются в `requirements-dev.txt`.

При обновлении зависимостей:

```bash
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
make check
make lint
```

Не добавлять dependency, если задача решается стандартной библиотекой без заметного ущерба читаемости.

## 12. Что нельзя делать без отдельного обсуждения

- Менять формат `callback_data`.
- Менять meaning существующих `setup_state`.
- Удалять legacy table types без миграции.
- Удалять `__bot_key`.
- Делать Google Sheets главным source of truth.
- Запускать несколько bot process с одним Telegram token.
- Менять transfer-flow без тестов.
- Менять partial write warning без тестов.
- Добавлять license без явного выбора лицензии владельцем проекта.
