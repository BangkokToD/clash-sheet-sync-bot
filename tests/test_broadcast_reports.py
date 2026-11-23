"""Адресаты, durable результаты доставки и приватность админских отчётов."""

from dataclasses import replace

import pytest

from clash_sheet_sync_bot.admin.broadcasts import (
    REPORT_MAX_UNITS,
    audience_pages,
    delivery_error,
    delivery_report_pages,
)
from clash_sheet_sync_bot.admin.flow import SuperadminFlow
from clash_sheet_sync_bot.admin.keyboards import (
    CALLBACK_BROADCAST_RECIPIENTS,
    CALLBACK_BROADCAST_REPORT,
    CALLBACK_BROADCAST_START,
)
from clash_sheet_sync_bot.repositories import (
    BotUserRepository,
    SuperadminRepository,
    TelegramChatRepository,
)
from clash_sheet_sync_bot.repositories.superadmin import BroadcastDelivery, BroadcastTarget
from clash_sheet_sync_bot.telegram.client import TelegramApiError, TelegramChatIdentity
from clash_sheet_sync_bot.telegram.text import utf16_length
from tests.fakes.factories import make_app_config
from tests.fakes.telegram import FakeTelegram, RecordingAccessService

ADMIN = 1001
USER = 2002
GROUP = -7001
NOW = "2026-11-23T21:10:00+00:00"
ANNOUNCEMENT = "Обновление"


def _flow(connection, telegram):
    return SuperadminFlow(
        config=make_app_config(superadmin_user_id=ADMIN),
        telegram=telegram,
        connection=connection,
        access=RecordingAccessService(),
    )


async def _callback(flow, data, *, user=ADMIN):
    return await flow.handle_callback(
        callback_data=data, callback_query_id="test", chat_id=user, message_id=1, user_id=user
    )


async def _prepare(connection, telegram):
    flow = _flow(connection, telegram)
    for user_id in (ADMIN, USER):
        await flow.observe_private_user(
            user_id=user_id, private_chat_id=user_id, display_name=f"Cached {user_id}"
        )
    await TelegramChatRepository(connection).upsert_connected_chat(
        chat_id=GROUP,
        title="Cached Group",
        chat_type="supergroup",
        created_by_user_id=ADMIN,
        now=NOW,
    )
    await SuperadminRepository(connection).set_support_group(
        chat_id=GROUP,
        title="Same support group",
        url="https://t.me/support",
        updated_by_user_id=ADMIN,
        updated_at=NOW,
    )
    await connection.commit()
    await _callback(flow, CALLBACK_BROADCAST_START)
    await flow.handle_private_text(chat_id=ADMIN, user_id=ADMIN, text=ANNOUNCEMENT)
    return flow


@pytest.mark.asyncio
async def test_named_preview_freezes_audience_and_deduplicates_support(migrated_connection):
    telegram = FakeTelegram(
        chats={
            USER: TelegramChatIdentity(USER, "private", "Анна Иванова", "anna"),
            GROUP: TelegramChatIdentity(GROUP, "supergroup", "Клан 🏠", "our_clan"),
        }
    )
    flow = await _prepare(migrated_connection, telegram)
    preview = telegram.sent_messages[-2]["text"]
    assert "Анна Иванова (@anna)" in preview
    assert "Клан 🏠 (@our_clan)" in preview
    assert preview.count(f"ID {GROUP}") == 1

    await BotUserRepository(migrated_connection).observe_private_user(
        user_id=3003, private_chat_id=3003, now=NOW
    )
    await migrated_connection.commit()
    telegram.chats[USER] = replace(telegram.chats[USER], display_name="Новое имя")
    await _callback(flow, "admin:broadcast:confirm:1")

    attempted = [m["chat_id"] for m in telegram.sent_messages if m["text"] == ANNOUNCEMENT]
    assert attempted == [ADMIN, ADMIN, USER, GROUP]  # preview, затем три доставки
    deliveries = await SuperadminRepository(migrated_connection).list_broadcast_deliveries(1)
    assert len(deliveries) == 3
    assert all(d.status == "delivered" for d in deliveries)
    assert (
        next(d.target.display_name for d in deliveries if d.target.chat_id == USER)
        == "Анна Иванова"
    )
    assert all(d.telegram_message_id is not None for d in deliveries)


@pytest.mark.asyncio
async def test_failures_and_names_survive_new_flow_and_do_not_resend(migrated_connection, caplog):
    telegram = FakeTelegram(chats={USER: TelegramChatIdentity(USER, "private", "Анна", "anna")})
    flow = await _prepare(migrated_connection, telegram)
    telegram.send_errors_by_chat[USER] = TelegramApiError(
        "Telegram API HTTP 403: Forbidden: bot was blocked by the user. telegram-token"
    )
    telegram.send_errors_by_chat[GROUP] = TelegramApiError("Telegram API HTTP 400: chat not found.")
    await _callback(flow, "admin:broadcast:confirm:1")

    admin = SuperadminRepository(migrated_connection)
    row = await admin.get_broadcast_report(1, ADMIN)
    assert (row["delivered_count"], row["failed_count"], row["status"]) == (1, 2, "completed")
    report = telegram.sent_messages[-1]["text"]
    assert "Анна (@anna)" in report
    assert "Пользователь заблокировал бота" in report
    assert "Чат не найден" in report
    assert "telegram-token" not in report + caplog.text
    assert "broadcast=1" in caplog.text

    new_telegram = FakeTelegram()
    new_flow = _flow(migrated_connection, new_telegram)
    await _callback(new_flow, CALLBACK_BROADCAST_REPORT)
    assert new_telegram.sent_messages[-1]["text"] == report
    assert new_telegram.get_chat_requests == []
    await _callback(new_flow, "admin:broadcast:confirm:1")
    assert all(m["text"] != ANNOUNCEMENT for m in new_telegram.sent_messages)


@pytest.mark.asyncio
async def test_metadata_failure_uses_cached_name_without_blocking_delivery(migrated_connection):
    telegram = FakeTelegram(get_chat_errors={USER: TelegramApiError("getChat недоступен")})
    flow = await _prepare(migrated_connection, telegram)
    assert f"Cached {USER}" in telegram.sent_messages[-2]["text"]
    await _callback(flow, "admin:broadcast:confirm:1")
    row = await SuperadminRepository(migrated_connection).get_broadcast_report(1, ADMIN)
    assert row["delivered_count"] == 3 and row["failed_count"] == 0


@pytest.mark.asyncio
async def test_successful_name_lookup_is_cached_for_later_metadata_errors(migrated_connection):
    telegram = FakeTelegram(chats={USER: TelegramChatIdentity(USER, "private", "Анна", "anna")})
    flow = await _prepare(migrated_connection, telegram)
    telegram.get_chat_errors[USER] = TelegramApiError("getChat недоступен")
    await _callback(flow, CALLBACK_BROADCAST_RECIPIENTS)
    assert "Анна (@anna)" in telegram.sent_messages[-1]["text"]
    user = await (
        await migrated_connection.execute(
            "SELECT is_active, last_seen_at FROM bot_users WHERE user_id = ?", (USER,)
        )
    ).fetchone()
    assert user["is_active"] == 1


@pytest.mark.asyncio
async def test_audience_button_only_reads_names_and_is_superadmin_only(migrated_connection):
    telegram = FakeTelegram()
    flow = await _prepare(migrated_connection, telegram)
    telegram.sent_messages.clear()
    telegram.get_chat_requests.clear()
    for callback in (CALLBACK_BROADCAST_RECIPIENTS, CALLBACK_BROADCAST_REPORT):
        await _callback(flow, callback, user=USER)
    assert telegram.sent_messages == []
    assert telegram.get_chat_requests == []
    assert telegram.answered_callbacks[-1]["text"] == "Нет доступа."

    await _callback(flow, CALLBACK_BROADCAST_RECIPIENTS)
    assert "Адресаты рассылки" in telegram.sent_messages[-1]["text"]
    assert {m["chat_id"] for m in telegram.sent_messages} == {ADMIN}
    assert ANNOUNCEMENT not in [m["text"] for m in telegram.sent_messages]


@pytest.mark.asyncio
async def test_interrupted_delivery_remains_uncertain_and_is_not_retried(migrated_connection):
    telegram = FakeTelegram()
    flow = await _prepare(migrated_connection, telegram)
    telegram.send_errors_by_chat[USER] = RuntimeError("Процесс завершился во время запроса")
    with pytest.raises(RuntimeError):
        await _callback(flow, "admin:broadcast:confirm:1")
    deliveries = await SuperadminRepository(migrated_connection).list_broadcast_deliveries(1)
    assert {d.target.chat_id: d.status for d in deliveries} == {
        ADMIN: "delivered",
        USER: "sending",
        GROUP: "pending",
    }

    read_only_telegram = FakeTelegram()
    await _callback(_flow(migrated_connection, read_only_telegram), CALLBACK_BROADCAST_REPORT)
    text = read_only_telegram.sent_messages[-1]["text"]
    assert "не завершена" in text
    assert "Результат не подтверждён" in text
    assert "Не отправлялось" in text
    await _callback(_flow(migrated_connection, read_only_telegram), "admin:broadcast:confirm:1")
    assert all(m["text"] != ANNOUNCEMENT for m in read_only_telegram.sent_messages)


@pytest.mark.asyncio
async def test_old_broadcast_keeps_totals_without_inventing_recipient_outcomes(migrated_connection):
    admin = SuperadminRepository(migrated_connection)
    broadcast_id = await admin.create_broadcast(
        created_by_user_id=ADMIN, text="Old", entities_json="[]", created_at=NOW
    )
    await admin.claim_broadcast(broadcast_id=broadcast_id, created_by_user_id=ADMIN, started_at=NOW)
    await admin.finish_broadcast(
        broadcast_id=broadcast_id,
        finished_at=NOW,
        user_targets_count=7,
        group_targets_count=8,
        delivered_count=11,
        failed_count=4,
    )
    await migrated_connection.commit()
    telegram = FakeTelegram()
    await _callback(_flow(migrated_connection, telegram), CALLBACK_BROADCAST_REPORT)
    text = telegram.sent_messages[-1]["text"]
    assert "Доставлено: 11" in text and "Ошибок: 4" in text
    assert "Подробные результаты для этой рассылки не сохранялись" in text
    assert telegram.get_chat_requests == []


@pytest.mark.asyncio
async def test_empty_preview_does_not_add_new_recipients_at_confirmation(migrated_connection):
    admin = SuperadminRepository(migrated_connection)
    bid = await admin.create_broadcast(
        created_by_user_id=ADMIN, text=ANNOUNCEMENT, entities_json="[]", created_at=NOW
    )
    await admin.snapshot_broadcast_targets(bid, ())
    await BotUserRepository(migrated_connection).observe_private_user(
        user_id=USER, private_chat_id=USER, now=NOW
    )
    await migrated_connection.commit()
    telegram = FakeTelegram()
    await _callback(_flow(migrated_connection, telegram), f"admin:broadcast:confirm:{bid}")
    assert all(m["text"] != ANNOUNCEMENT for m in telegram.sent_messages)
    assert "Адресатов нет" in telegram.sent_messages[-1]["text"]


@pytest.mark.asyncio
async def test_legacy_draft_gets_audience_when_confirmed(migrated_connection):
    admin = SuperadminRepository(migrated_connection)
    bid = await admin.create_broadcast(
        created_by_user_id=ADMIN, text=ANNOUNCEMENT, entities_json="[]", created_at=NOW
    )
    await BotUserRepository(migrated_connection).observe_private_user(
        user_id=USER, private_chat_id=USER, now=NOW
    )
    await migrated_connection.commit()
    telegram = FakeTelegram()
    await _callback(_flow(migrated_connection, telegram), f"admin:broadcast:confirm:{bid}")
    delivered = await admin.list_broadcast_deliveries(bid)
    assert len(delivered) == 1 and delivered[0].status == "delivered"
    assert delivered[0].target.chat_id == USER


@pytest.mark.asyncio
async def test_report_delivery_failure_does_not_undo_broadcast_completion(migrated_connection):
    telegram = FakeTelegram()
    flow = await _prepare(migrated_connection, telegram)
    telegram.send_errors_by_chat[ADMIN] = TelegramApiError("Сетевой сбой")
    await _callback(flow, "admin:broadcast:confirm:1")
    report = await SuperadminRepository(migrated_connection).get_broadcast_report(1, ADMIN)
    assert report["status"] == "completed"
    assert (report["delivered_count"], report["failed_count"]) == (2, 1)


def test_long_reports_preserve_every_recipient_within_telegram_utf16_limit():
    targets = tuple(BroadcastTarget(i, "private", "🏠" * 160, f"user{i}") for i in range(80))
    deliveries = tuple(BroadcastDelivery(t, "failed", "🏠" * 1000, None, NOW) for t in targets)
    for pages in (
        audience_pages(targets),
        delivery_report_pages(broadcast_id=1, status="completed", deliveries=deliveries),
    ):
        assert len(pages) > 1
        assert all(utf16_length(page) <= REPORT_MAX_UNITS for page in pages)
        text = "\n".join(pages)
        for target in targets:
            assert text.count(f"(@user{target.chat_id})") == 1


@pytest.mark.parametrize(
    "error",
    [
        "Forbidden: bot was blocked by the user",
        "Bad Request: chat not found",
        "http 429: retry later",
    ],
)
def test_error_details_redact_token_and_normalize_newlines(error):
    result = delivery_error(TelegramApiError(error + "\nsecret-token"), "secret-token")
    assert "secret-token" not in result and "\n" not in result
    assert error in result
