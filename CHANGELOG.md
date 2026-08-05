# Changelog

Все заметные изменения проекта фиксируются в этом файле.

Формат близок к [Keep a Changelog](https://keepachangelog.com/), а версии
следуют [Semantic Versioning](https://semver.org/).

## Unreleased

Пока нет невыпущенных изменений.

## [1.1.0] - 2026-08-05

### Added

- `/cwl_forecast` для матрицы фактических составов текущей CWL war и
  потенциальной силы всех оставшихся соперников по раундам.
- `/cwl_forecast_schedule` с fresh admin checks, кнопочным вводом неизвестных
  пар, back/cancel/confirm/edit и глобальным переиспользованием расписания.
- SQLite migration 8 с отдельными forecast cooldown, schedules, rounds и
  временными sessions; upgrade с schema 7 и идемпотентный migration runner.
- Persisted cooldown `/cwl_forecast`, per-chat singleflight и общий war cache
  одного запуска с существующим `CWL_WAR_CONCURRENCY_LIMIT`.
- Строгий League Group/CWL war domain: fingerprint, выбор current war,
  actual/predicted rosters и блокировка неполного или конфликтного schedule.
- Startup-валидация Telegram emoji catalog, UTF-16 custom emoji entities и
  ровно один plain fallback только после HTTP 400.
- Конфигурация `CWL_FORECAST_COOLDOWN_SECONDS` и
  `CWL_FORECAST_SCHEDULE_TTL_SECONDS`.

### Changed

- Все колонки forecast-матрицы сортируются по ратушам по убыванию, а итоговая
  строка показывает сумму уровней ратуш для каждой колонки.
- README, SSOT, архитектурная документация и production runbook описывают
  forecast data ownership, ручное расписание, conflict recovery и migration 8.

## [1.0.0] - 2026-08-02

### Added

- GitHub Actions CI для format-check, Ruff, `py_compile` и полного pytest suite.
- Память одноимённых пользовательских колонок между активным составом и
  «Вышедшими» без автоматического создания новых колонок.
- Учёт Raid Weekend в общем `/sync`: рейтинг, user-колонки,
  межсезонный fallback, staging-ротация и четыре bot-owned архива.
- SQLite-таблицы `raid_player_state` и `raid_sheet_archives`, raid binding,
  настройка raid-колонок, диагностика и auto-fix.
- Обязательный `SUPERADMIN_USER_ID` и закрытое админское меню.
- Подключение публичной или закрытой группы техподдержки через одноразовый токен.
- Кнопка «Техподдержка» в главном меню после настройки группы поддержки.
- Подтверждаемая текстовая рассылка известным пользователям и активным группам
  с журналом и итоговыми счётчиками доставки.
- SQLite-реестр пользователей бота с переносом существующих администраторов.
- Межсезонный CWL fallback на последний сохранённый сезон из SQLite.
- Безопасный `DEV_MODE`, отключающий cooldown последовательных `/sync` без снятия
  блокировок конкурентной записи.
- Публичная SQLite runtime-архитектура:
  - Telegram-группы;
  - Google Sheets bindings;
  - tracked clans;
  - column profiles;
  - composition player state;
  - CWL row state;
  - raid player state и archive registry;
  - managed sheet blocks;
  - sync run history.
- `/sync` pipeline с staged-подходом:
  - подготовка состава;
  - подготовка CWL;
  - подготовка рейдов;
  - запись состава;
  - запись CWL;
  - запись рейдов;
  - сохранение SQLite state;
  - Telegram report.
- `/status` с summary последнего sync.
- Setup-flow для подключения группы через одноразовый `/connect <token>`.
- Transfer-flow для переноса таблицы и runtime state в другую Telegram-группу.
- Диагностика Google Sheets binding.
- Auto-fix обязательных листов и служебного `_bot_state`.
- Managed blocks metadata в SQLite.
- Service-колонка `__bot_key` для стабильного сопоставления строк.
- `_bot_state` sheet для служебного состояния привязанной таблицы.
- CWL season consistency check.
- Partial write warning после начала записи Google Sheets.
- Connection per Telegram update.
- Fresh Telegram admin checks для чувствительных действий.
- Runtime/dev test tooling:
  - `pytest`;
  - `pytest-asyncio`;
  - `ruff`;
  - `make check`;
  - `make lint`;
  - `make format`.
- Архитектурная документация:
  - `SSOT.md`;
  - `docs/architecture.md`;
  - `docs/operations.md`.

### Changed

- В рейдовом рейтинге `Ник` снова расположен перед `Тегом`, коэффициент
  отображается как `Выполнение нормы` целым процентом, а золото столицы —
  целым числом с разделителем тысяч.
- Успешный Telegram-отчёт `/sync` сокращён до обновлённых разделов, списка
  кланов, строки `Разработчик: BangkokToD` и `Чата Леши`; таблица открывается
  отдельной inline-кнопкой, а recoverable warnings остаются в
  `sync_runs.report_json` для диагностики.
- Ратуши в составе отображаются как `TH14`, а CWL-показатели — монохромными
  звёздами `★★★` и числом с `%`; пропущенная атака выделяется мягким розовым.
- README переписан под текущую SQLite public runtime-архитектуру.
- Профили колонок состава разделены:
  - `composition_active`;
  - `composition_exited`.
- CWL write pipeline усилен проверкой season mismatch.
- Composition sheet blocks metadata теперь заменяется согласованно через prefix-based replace.
- Repository-слой разделён на focused package modules.
- Runtime-код перенесён из корня проекта в пакет `clash_sheet_sync_bot/`; корневой `bot.py` оставлен как compatibility launcher.
- Setup keyboard builders вынесены из `setup_flow.py`.
- Общие time/A1 helpers вынесены в отдельные модули.
- Test fakes/factories вынесены в пакет `tests/fakes/`.
- Runtime и dev dependencies закреплены точными версиями.

### Fixed

- Migration 7 восстанавливает raid presentation только для прежних стандартных
  профилей, не перезаписывая пользовательские заголовки и ручной порядок колонок.
- Superadmin migration повторяется идемпотентным repair-шагом version 6 для баз,
  где version 5 уже была занята более ранней локальной миграцией.
- Пользовательское горизонтальное выравнивание колонок состава сохраняется после `/sync`.
- `/cancel` сбрасывает только setup-state текущего пользователя.
- `/cancel` не сбрасывает чужой setup-state.
- Text completion создания user-колонки требует fresh admin check.
- Text completion rename колонки требует fresh admin check.
- `TelegramMessageNotModifiedError` не создаёт дубль сообщения.
- Sensitive callbacks используют `force_refresh=True`.
- Ошибка до записи Google Sheets не добавляет partial write warning.
- Ошибка после начала записи Google Sheets добавляет partial write warning.
- `sync_runs.error_stage` сохраняется при ошибке.
- Unexpected sync exception логируется, но пользователю не отправляется raw exception.
- Ошибка доставки Telegram report после успешного sync не откатывает сохранённый success.
- CWL war loading использует настраиваемый concurrency limit.
- CWL season mismatch запрещает запись до повреждения таблицы.
- Composition managed blocks не оставляют устаревшие block records.

### Removed

- Legacy `sync_settings.json` runtime state.
- Legacy `settings_store.py`.
- Legacy sync settings models.
- Мёртвые boundary helpers для `/sync` и `/status`.
- Неиспользуемые report helper-функции.
- Монолитный `repositories.py` в пользу пакета `repositories/`.

[1.1.0]: https://github.com/BangkokToD/clash-sheet-sync-bot/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/BangkokToD/clash-sheet-sync-bot/releases/tag/v1.0.0
