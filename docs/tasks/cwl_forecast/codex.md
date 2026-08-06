# Инструкция Codex

## Мандат

Пользователь явно разрешил в рамках этой задачи:

- создать новую ветку от `main`;
- реализовывать этапы `commit-plan.md` по порядку;
- после зелёной проверки каждого этапа самостоятельно создавать тематический
  commit;
- продолжать следующий этап без промежуточного запроса подтверждения;
- подготовить код и metadata версии `1.1.0`.

Не разрешены: push, Pull Request, tag, GitHub Release, deploy, удаление данных,
reset чужих изменений и migration production-базы.

## 1. Обязательная ориентация

До любых изменений:

1. прочитать все файлы `docs/tasks/cwl_forecast/` полностью;
2. прочитать `README.md`, `SSOT.md`, `CONTRIBUTING.md`, `CHANGELOG.md`,
   `docs/architecture.md`, `docs/operations.md`, `pyproject.toml`, `Makefile`,
   `.github/workflows/ci.yml`, `.env.example`;
3. перечислить весь tracked tree через `rg --files` и открыть все исходники и
   тесты, относящиеся к bot routing, Telegram, Clash API, migrations,
   repositories, runtime config, CWL и fakes;
4. проверить существующие:
   `resources/telegram_emoji_catalog.json` и
   `tests/fixtures/current_war_league_group.json`;
5. найти и полностью прочитать применимый `AGENTS.md`, если он появился;
6. сообщить пользователю факты, понимание задачи, затрагиваемые части и риски.

Нельзя начинать с генерации кода по одним task-docs без проверки фактического
репозитория.

## 2. Проверка git и создание ветки

Проверенный baseline этого пакета:

```text
main
421bf6778c7abdaf054e46a7696500ef10172943
```

Порядок:

1. выполнить `git status --short --branch`, `git branch --show-current`,
   `git rev-parse HEAD`;
2. распакованные `docs/tasks/cwl_forecast/*` могут быть единственными
   ожидаемыми untracked changes;
3. любые другие изменения сохранить и не перезаписывать; если они затрагивают
   scope задачи — остановиться и спросить пользователя;
4. перейти на локальный `main` без destructive reset;
5. если доступен remote, допускается только безопасное обновление
   `git pull --ff-only`; при divergence остановиться;
6. если актуальный main новее указанного HEAD, перечитать изменившиеся файлы,
   проверить свободный schema version и скорректировать только технический
   план без изменения продуктовых контрактов;
7. создать `git switch -c feat/cwl-forecast-v1.1.0`;
8. выполнить этап 0 и commit task-docs.

Нельзя откатывать более новый main к указанному baseline.

## 3. Baseline tests

После создания ветки и до product code:

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m ruff format --check .
python -m ruff check .
git ls-files '*.py' | xargs python -m py_compile
python -m pytest -q
git diff --check
```

Ожидаемый baseline на проверенном HEAD: `359 passed`. Если среда инжектирует
SOCKS proxy и failures точно указывают на отсутствующий `socksio`, повторить
pytest с unset proxy variables по `verification.md`. Не добавлять зависимость
и не маскировать иные failures.

При настоящем baseline failure остановиться до продуктовых изменений.

## 4. Рабочий цикл каждого этапа

Перед этапом вывести:

```text
Этап и будущий commit:
Текущие ветка и HEAD:
Git status:
Прочитанные файлы:
Известные контракты:
Затрагиваемые части:
Не входит в этап:
Риски и неизвестные:
План тестов:
```

Затем:

1. реализовать только текущий этап;
2. использовать `apply_patch` для ручных правок;
3. соблюдать Python 3.12, type hints, русские Google-style docstrings;
4. не исправлять unrelated code;
5. запускать целевые тесты и проверки этапа;
6. просмотреть полный `git diff` и `git diff --check`;
7. staged scope должен соответствовать только этапу;
8. создать commit с точным сообщением из плана;
9. сообщить SHA, файлы, проверки и ограничения;
10. автоматически перейти к следующему этапу.

Если autoformatter изменил файл вне scope, не включать его молча.

## 5. Условия немедленной остановки

Codex останавливается и задаёт минимальный вопрос, если:

- есть чужие изменения в затрагиваемых файлах;
- branch/main divergent или нельзя безопасно определить базу;
- catalog/fixture отсутствуют или их содержимое противоречит описанному
  контракту;
- следующий schema version уже занят другой migration;
- фактический Telegram/Clash contract делает требование невыполнимым;
- для продолжения нужно новое продуктовое решение;
- baseline или stage tests падают по причине, которую нельзя безопасно
  исправить в scope этапа;
- нужны push, секреты, production access или destructive operation.

Не являются причиной спрашивать подтверждение:

- выбор внутреннего имени private function;
- разбиение нового package на файлы при сохранении архитектурных границ;
- добавление недостающего unit test;
- автоматический commit успешно завершённого этапа, потому что он уже явно
  разрешён пользователем.

## 6. Особые запреты реализации

- Не использовать OCR, изображения и распознавание названий.
- Не выводить schedule из позиции `clans` или неизвестного алгоритма игры.
- Не отправлять partial forecast при missing/conflicting schedule.
- Не ослаблять fresh admin check.
- Не хранить display name/level как источник истины расписания.
- Не объединять `parse_mode` и entities.
- Не считать Python string offsets Telegram UTF-16 offsets.
- Не retry custom fallback на любых ошибках, кроме HTTP 400.
- Не переиспользовать setup state для schedule session.
- Не добавлять новую dependency без фактической необходимости и объяснения.
- Не менять существующие Sheet/CWL sync contracts.
- Не обновлять `SSOT.md` описанием функции до её фактической реализации.

## 7. Commit hygiene

Перед каждым commit:

```bash
git status --short
git diff --check
git diff --cached --check
git diff --cached --stat
```

Проверить diff содержательно. Не использовать `git add -A`, если в рабочем
дереве есть посторонние файлы; добавлять явные paths. Не использовать
`--no-verify`, amend чужих commits, force и reset.

После commit:

```bash
git show --stat --oneline --decorate HEAD
git status --short
```

## 8. Финальный отчёт

После этапа 7 предоставить:

1. ветку и финальный HEAD;
2. таблицу этап → SHA → commit message;
3. перечень реализованных контрактов;
4. миграцию и schema version;
5. точные команды и результаты полного quality gate;
6. фактический статус manual smoke, без выдуманного успеха;
7. известные риски и ограничения;
8. подтверждение версии `1.1.0`;
9. подтверждение чистого working tree;
10. явное указание, что push/tag/release/deploy не выполнялись.

Остановиться на локально подготовленной ветке и ждать дальнейшего решения
пользователя о review и публикации.
