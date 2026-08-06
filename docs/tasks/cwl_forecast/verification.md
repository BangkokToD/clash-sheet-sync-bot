# Проверка и приёмка

## 1. Baseline

На `main@421bf6778c7abdaf054e46a7696500ef10172943` подтверждено:

```text
make check
359 passed in 4.82s

make lint
All checks passed!

python -m ruff format --check .
67 files already formatted

git diff --check
clean
```

В проверочном sandbox первый `make check` падал в 10 HTTP-client tests только
из-за унаследованного SOCKS proxy и отсутствия опционального `socksio`. Это не
дефект репозитория и не основание добавлять `socksio` в runtime dependencies.
Чистый baseline получен командой:

```bash
env -u ALL_PROXY -u all_proxy \
  -u HTTP_PROXY -u http_proxy \
  -u HTTPS_PROXY -u https_proxy \
  -u NO_PROXY -u no_proxy \
  PATH=.venv/bin:$PATH make check
```

Использовать этот обход только если среда действительно инжектирует proxy и
ошибка совпадает. Другие failures не маскировать.

## 2. Проверки каждого этапа

Перед commit:

```bash
python -m ruff format <изменённые Python-файлы и тесты>
python -m ruff check <изменённые Python-файлы и тесты>
python -m pytest -q <целевые тесты этапа>
git diff --check
```

После commit зафиксировать точный вывод, а не писать обобщённое «тесты
прошли». Хотя бы после этапов 1, 3, 5 и 7 запускать полный `make check` и
`make lint`; если runtime небольшой, предпочтительно запускать после каждого.

## 3. Финальный CI quality gate

Команды соответствуют `.github/workflows/ci.yml`:

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m ruff format --check .
python -m ruff check .
git ls-files '*.py' | xargs python -m py_compile
python -m pytest -q
git diff --check
```

Также проверить:

```bash
python -m json.tool resources/telegram_emoji_catalog.json >/dev/null
git status --short
```

Финальный `git status --short` должен быть пустым.

## 4. Migration matrix

Автоматически:

1. новая пустая БД сразу получает целевую schema;
2. повторный migration run ничего не ломает;
3. копия schema v7 обновляется до v8 с сохранением существующих данных;
4. все constraints и foreign keys работают;
5. rollback транзакции при неполной записи schedule не оставляет половину
   раундов;
6. timestamps сохраняются и читаются timezone-aware;
7. expired session заменяется, active session конфликтует.

Перед любым будущим применением к production сделать WAL-consistent backup по
действующему runbook. В рамках этой задачи production migration не запускать.

## 5. Domain test matrix

### League Group

- fixture `current_war_league_group.json` читается;
- 8 кланов и 7 rounds не зависят от names/levels;
- `#0` игнорируется как warTag, но сохраняет номер unknown round;
- перестановка `clans` не меняет fingerprint;
- изменение одного tag меняет fingerprint;
- дубликаты/invalid tags/TH/bool-as-int отклоняются.

### Current war

- `inWar` выбирается при наличии preparation;
- без `inWar` выбирается ближайшая preparation;
- ended wars не выбираются;
- home/away нашего клана не влияет;
- отсутствие ровно одной стороны нашего клана — contract error.

### Rosters

- actual: `townhallLevel DESC`, tag `ASC`;
- future: `townHallLevel DESC`, tag `ASC`;
- ровно `teamSize` строк;
- недостаток — `—`;
- последняя строка содержит суммы TH колонок через два пробела;
- неизвестный TH вне catalog даёт controlled clan error.

### Schedule

- API-known rounds auto-filled;
- полный manual remainder принимается;
- пропуск, дубль, foreign clan, own clan отклоняются;
- появившийся реальный war подтверждает manual entry;
- несовпадение блокирует forecast;
- последний round работает без saved schedule.

## 6. Telegram test matrix

### Entities

- exact text и entity payload;
- одна пустая строка отделяет название клана от матрицы;
- offsets проверены в UTF-16, не Python code points;
- Cyrillic, `🏠`, custom fallback и keycap sequence не сдвигают следующие entity;
- entity length покрывает ровно fallback token;
- parse mode отсутствует.

### Fallback

- custom entities success: один request;
- HTTP 400: второй request plain `fallback_text`;
- network/401/403/429/500: второго request нет;
- HTTP 400 plain retry: третьего request нет.

### Commands

- exact `/cwl_forecast@BotName` routing не ломает group command;
- `/cwl_forecast_schedule@BotName` routing;
- callbacks других flow не перехватываются;
- non-admin не меняет session/schedule;
- fresh admin check на каждом callback.

## 7. Ручной smoke в тестовом Telegram chat

Проводить только после автоматических проверок с тестовым bot/database.

1. Подключить один клан с активной CWL и удалить его schedule.
2. Обычным участником вызвать `/cwl_forecast`.
3. Убедиться: прогноза нет, бот просит администратора выполнить отдельную
   команду.
4. Обычным участником вызвать `/cwl_forecast_schedule` — получить отказ.
5. Администратором открыть schedule flow.
6. Проверить кнопки: `name · ур. N · #TAG`, одна на строку.
7. Выбрать порядок unknown rounds, проверить Back и Cancel без записи.
8. Повторить и Confirm; открыть edit flow и изменить один unknown round.
9. Вызвать `/cwl_forecast`: один message на clan, две пробела в header, `|`
   без пробелов в matrix, все оставшиеся rounds.
10. Вызвать повторно сразу: cooldown с числом секунд.
11. Инициировать долгий запуск и параллельно вызвать ещё раз: точный текст
    `Прогноз ЛВК уже формируется`.
12. Restart процесса и проверить, что cooldown не исчез.
13. Проверить chat с двумя кланами: порядок `sort_order`, отдельные messages.
14. Смоделировать failure одного клана: ready messages первыми, одна summary.
15. Открыть ту же League Group из другого connected chat: schedule уже доступен.
16. Подменить saved opponent так, чтобы он конфликтовал с новым real warTag:
    forecast блокируется и просит админа.
17. Проверить последнюю войну без schedule: две колонки и никакого запроса
    расписания.

## 8. Release checklist

- [ ] Все этапы 0–7 имеют отдельные commits.
- [ ] Нет постороннего refactoring.
- [ ] Полный CI quality gate зелёный.
- [ ] Manual smoke зафиксирован фактическими результатами или явно отмечен как
      не выполненный; его нельзя объявлять пройденным без Telegram среды.
- [ ] `SSOT.md` соответствует фактическому коду.
- [ ] `.env.example` содержит обе настройки.
- [ ] Версия в package и README — `1.1.0`.
- [ ] CHANGELOG содержит `1.1.0`.
- [ ] Рабочее дерево чистое.
- [ ] Не выполнены push/tag/release/deploy/production migration.
