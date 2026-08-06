# Поэтапный план реализации

## Общие правила

- Ветка: `feat/cwl-forecast-v1.1.0` от актуального `main`.
- До первого product commit зафиксировать этот каталог отдельным docs commit.
- Один этап — один тематический commit.
- Следующий этап начинается только после зелёных проверок текущего.
- При обнаружении новой продуктовой развилки работа останавливается.
- Запрещены push, PR, tag, release, deploy и production migration.
- После каждого этапа сохранить SHA, scope и точные результаты проверок в
  итоговом рабочем отчёте.

## Этап 0. Зафиксировать task-docs

Commit:

```text
docs: add CWL forecast implementation task
```

Scope:

- только `docs/tasks/cwl_forecast/*`;
- проверить, что распакованные файлы совпадают с этим пакетом;
- не менять корневой `SSOT.md` и код.

Проверки:

```text
git diff --check
git status --short
```

Условие завершения: task-docs находятся в feature branch одним commit.

## Этап 1. Persistence и config

Commit:

```text
feat: add CWL forecast persistence
```

Scope:

- новая migration следующей свободной версии;
- forecast tables и indexes;
- typed repository для cooldown, schedules, rounds и sessions;
- config cooldown/TTL;
- dataclasses persistence layer;
- unit/repository/migration/config tests.

Не входит:

- Telegram commands;
- Clash API orchestration;
- emoji formatting.

Обязательные тесты:

- пустая БД;
- повторный запуск migrations;
- upgrade от schema v7;
- уникальность schedule key и round/opponent;
- atomic replace schedule;
- expired session replacement;
- активная session conflict;
- cooldown read/write и UTC parsing;
- config defaults/errors.

Условие завершения: repository contract работает независимо от bot flow.

## Этап 2. Telegram custom emoji

Commit:

```text
feat: support Telegram custom emoji messages
```

Scope:

- loader и validator существующего JSON-каталога;
- immutable models каталога;
- Telegram message entity model;
- `send_message`/при необходимости `edit_message_text` с entities;
- взаимное исключение `parse_mode` и `entities`;
- различимый HTTP 400 exception;
- обновление Telegram fake;
- UTF-16 utility/builder foundation и tests.

Не входит:

- forecast message layout целиком;
- fallback policy orchestration команды;
- изменение самого JSON без доказанного дефекта.

Обязательные тесты:

- полный валидный catalog 1–18;
- неизвестная schema, пропавший ключ, дубли ID, invalid ID;
- UTF-16 offsets рядом с surrogate-pair/keycap/Cyrillic;
- exact Bot API payload;
- `parse_mode + entities` rejection;
- HTTP 400 отличается от 401/403/429/5xx/network.

## Этап 3. Forecast domain и форматирование

Commit:

```text
feat: add CWL forecast domain
```

Scope:

- strict League Group/CWL War parsing или безопасное переиспользование
  существующего parser;
- fingerprint;
- извлечение созданных пар по round;
- выбор current war;
- actual/predicted roster rules;
- schedule validation;
- построение custom и fallback message models;
- pure unit tests на fixture и synthetic wars.

Не входит:

- DB flow кнопочной команды;
- bot routing;
- отправка сообщений.

Обязательные сценарии:

- `inWar` приоритетнее `preparation`;
- ближайшая preparation стабильна;
- сторона нашего клана определяется независимо от home/away;
- actual TH DESC + tag ASC;
- prediction TH DESC + tag ASC;
- cut до teamSize и padding `—`;
- все будущие раунды;
- последний раунд без schedule;
- incomplete/duplicate/foreign/conflicting schedule;
- header без `|`, ровно два пробела;
- одна пустая строка между clan title и header;
- body только `|`, без пробелов;
- суммы TH колонок через два пробела;
- Unicode keycap rounds;
- exact UTF-16 entities и plain fallback.

## Этап 4. Администраторский ввод расписания

Commit:

```text
feat: add CWL forecast schedule flow
```

Scope:

- `/cwl_forecast_schedule` routing;
- fresh admin check на command и callback;
- выбор нашего клана при нескольких active clans;
- session lifecycle и TTL;
- последовательные opponent buttons с name/level/tag;
- API-known locked rounds;
- back/cancel/confirm/edit;
- сохранение полного schedule и audit metadata;
- callback data codec;
- flow/integration tests.

Не входит:

- `/cwl_forecast` output;
- OCR, photos, image downloads;
- выдача прав обычным участникам.

Обязательные сценарии:

- non-admin отказ без записи;
- права отозваны между command и callback;
- один/несколько tracked clans;
- одна кнопка в строке, exact label;
- выбранный clan исчезает из вариантов;
- другой user/chat не управляет session;
- active conflict и expired replacement;
- callback <= 64 UTF-8 bytes;
- stale League Group/fingerprint;
- confirmation transaction;
- cancel не изменяет существующий schedule;
- edit меняет только всё ещё `#0` rounds;
- global schedule виден из другого chat.

## Этап 5. Пользовательская команда прогноза

Commit:

```text
feat: add CWL forecast command
```

Scope:

- `/cwl_forecast` routing;
- per-chat singleflight;
- persisted cooldown до API;
- обработка runtime clans в `sort_order`;
- Clash API orchestration и per-run war cache;
- ready/inactive/schedule-required/failed outcomes;
- one message per ready clan;
- custom emoji HTTP 400 plain fallback;
- schedule instructions и technical error summary;
- flow/integration tests.

Обязательные сценарии:

- любой group member может запустить;
- disconnected/private chat не выполняет работу;
- cooldown начинается до API и переживает restart;
- `DEV_MODE` отключает cooldown, но не lock;
- точный concurrent текст;
- отсутствие CWL молча;
- missing schedule не публикует partial forecast;
- last round schedule не требует;
- successful clans отправлены до summary;
- partial API error не отменяет другие;
- один clan — одно сообщение;
- custom entities success;
- только 400 даёт ровно один fallback retry;
- fallback error не запускает третий вызов.

## Этап 6. Документация и эксплуатация

Commit:

```text
docs: document CWL forecast operations
```

Scope:

- `.env.example`;
- README команды и пример;
- `docs/architecture.md`;
- `docs/operations.md` с admin flow, cooldown, recovery;
- `SSOT.md` только по фактически реализованному состоянию;
- help/command descriptions, если они живут в коде, могут быть частью этапа 5
  вместо дублирования.

Документировать:

- почему будущие пары вводятся вручную;
- global key/fingerprint и audit;
- как исправить conflict;
- custom emoji fallback;
- migration/backup/runbook;
- отсутствие OCR и неофициальных API;
- метрики/логи, доступные фактически.

Проверки: ссылки, примеры env, команды запуска и `git diff --check`.

## Этап 7. Подготовить релиз 1.1.0

Commit:

```text
chore: prepare 1.1.0 release
```

Scope:

- `clash_sheet_sync_bot/__init__.py` version `1.1.0`;
- README version, если текущая конвенция её показывает;
- `CHANGELOG.md`: перенести относящиеся изменения в `[1.1.0]` с актуальной
  датой и сохранить стиль compare links проекта;
- только необходимые release metadata corrections.

До commit выполнить полный quality gate из `verification.md`. Не создавать tag,
GitHub Release и не делать push.

## Финальное состояние

- ветка содержит этапы 0–7 в указанном порядке;
- рабочее дерево чистое;
- версия `1.1.0`;
- все проверки зелёные;
- итоговый отчёт содержит commit SHA каждого этапа;
- публикация ожидает отдельного решения пользователя.
