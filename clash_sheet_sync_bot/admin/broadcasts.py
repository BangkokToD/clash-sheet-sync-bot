"""Именованные адресаты и сохранённые результаты рассылок для superadmin."""

from collections.abc import Sequence

from clash_sheet_sync_bot.repositories.superadmin import BroadcastDelivery, BroadcastTarget
from clash_sheet_sync_bot.telegram.client import TelegramApiError, TelegramChatMigratedError
from clash_sheet_sync_bot.telegram.text import utf16_length

REPORT_MAX_UNITS = 3800


def one_line(value: str, limit: int = 160) -> str:
    return " ".join(value.split())[:limit]


def target_label(target: BroadcastTarget) -> str:
    username = f" (@{one_line(target.username, 64)})" if target.username else ""
    kind = "Пользователь" if target.target_type == "private" else "Группа"
    return f"{kind}: {one_line(target.display_name)}{username} · ID {target.chat_id}"


def text_pages(heading: str, entries: Sequence[str]) -> tuple[str, ...]:
    """Разбивает отчёт по целым адресатам, учитывая UTF-16 лимит Telegram."""

    pages = []
    page = heading
    for entry in entries:
        if utf16_length(page + "\n\n" + entry) > REPORT_MAX_UNITS:
            pages.append(page)
            page = heading
        page += "\n\n" + entry
    pages.append(page)
    return tuple(pages)


def audience_pages(targets: Sequence[BroadcastTarget], *, preview: bool = False) -> tuple[str, ...]:
    private_count = sum(t.target_type == "private" for t in targets)
    group_count = len(targets) - private_count
    heading = "Предпросмотр рассылки." if preview else "Адресаты рассылки."
    heading += f"\nПолучатели: {private_count} пользователей и {group_count} групп."
    return text_pages(heading, [target_label(t) for t in targets] or ["Адресатов нет."])


def delivery_error(error: TelegramApiError, token: str) -> str:
    """Сохраняет причину и диагностику Telegram без токена и переводов строки."""

    raw = one_line(str(error).replace(token, "[скрыто]") if token else str(error), 800)
    lowered = raw.lower()
    explanations = (
        ("bot was blocked", "Пользователь заблокировал бота"),
        ("bot was kicked", "Бот удалён из группы"),
        ("bot is not a member", "Бот не состоит в группе"),
        ("bot can't initiate conversation", "Пользователь ещё не открыл личный чат с ботом"),
        ("user is deactivated", "Аккаунт пользователя удалён"),
        ("chat not found", "Чат не найден или недоступен боту"),
        ("not enough rights", "Недостаточно прав для отправки"),
        ("have no rights to send", "Недостаточно прав для отправки"),
        ("http 429", "Превышен лимит запросов Telegram"),
    )
    if isinstance(error, TelegramChatMigratedError):
        return f"Группа преобразована в супергруппу; новый ID {error.new_chat_id}. {raw}"
    for marker, explanation in explanations:
        if marker in lowered:
            return f"{explanation}. {raw}"
    return raw or "Telegram не сообщил причину ошибки."


def delivery_report_pages(
    *,
    broadcast_id: int,
    status: str,
    deliveries: Sequence[BroadcastDelivery],
    delivered_count: int | None = None,
    failed_count: int | None = None,
    has_audience_snapshot: bool = False,
) -> tuple[str, ...]:
    delivered = (
        sum(d.status == "delivered" for d in deliveries) if deliveries else delivered_count or 0
    )
    failed = sum(d.status == "failed" for d in deliveries) if deliveries else failed_count or 0
    heading = "Рассылка завершена." if status == "completed" else "Рассылка не завершена."
    heading += f"\nРассылка №{broadcast_id}.\nДоставлено: {delivered}.\nОшибок: {failed}."
    if not deliveries:
        note = (
            "Адресатов нет."
            if has_audience_snapshot
            else "Подробные результаты для этой рассылки не сохранялись."
        )
        return (heading + "\n\n" + note,)
    heading += f"\nВсего адресатов: {len(deliveries)}."
    for state, label in (("pending", "Не отправлялось"), ("sending", "Результат не подтверждён")):
        count = sum(d.status == state for d in deliveries)
        if count:
            heading += f"\n{label}: {count}."
    labels = {
        "delivered": "✅ Доставлено",
        "failed": "❌ Ошибка",
        "pending": "⏳ Не отправлялось",
        "sending": "❔ Результат не подтверждён",
    }
    entries = []
    for item in deliveries:
        entry = f"{labels[item.status]}\n{target_label(item.target)}"
        if item.error_message:
            entry += "\nПричина: " + one_line(item.error_message, 1000)
        entries.append(entry)
    return text_pages(heading, entries)
