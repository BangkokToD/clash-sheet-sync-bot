# Changelog

Все заметные изменения проекта фиксируются в этом файле.

Формат близок к [Keep a Changelog](https://keepachangelog.com/), но без жёсткой привязки к SemVer: проект пока развивается как учебный/production-ready bot без публичных релизных тегов.

## Unreleased

### Added

- Публичная SQLite runtime-архитектура:
  - Telegram-группы;
  - Google Sheets bindings;
  - tracked clans;
  - column profiles;
  - composition player state;
  - CWL row state;
  - managed sheet blocks;
  - sync run history.
- `/sync` pipeline с staged-подходом:
  - подготовка состава;
  - подготовка CWL;
  - запись состава;
  - запись CWL;
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
  - `docs/architecture.md`;
  - `docs/operations.md`.

### Changed

- README переписан под текущую SQLite public runtime-архитектуру.
- Профили колонок состава разделены:
  - `composition_active`;
  - `composition_exited`.
- CWL write pipeline усилен проверкой season mismatch.
- Composition sheet blocks metadata теперь заменяется согласованно через prefix-based replace.
- Repository-слой разделён на focused package modules.
- Setup keyboard builders вынесены из `setup_flow.py`.
- Общие time/A1 helpers вынесены в отдельные модули.
- Test fakes/factories вынесены в пакет `tests/fakes/`.
- Runtime и dev dependencies закреплены точными версиями.

### Fixed

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
