"""Тесты Telegram entities, UTF-16 offsets и HTTP-классификации."""

from __future__ import annotations

import json

import httpx
import pytest

from clash_sheet_sync_bot.telegram.client import (
    TelegramApiError,
    TelegramBadRequestError,
    TelegramChatMigratedError,
    TelegramClient,
    TelegramMessageEntity,
)
from clash_sheet_sync_bot.telegram.text import TelegramTextBuilder, utf16_length


def test_utf16_builder_handles_surrogate_keycap_and_cyrillic() -> None:
    builder = TelegramTextBuilder()
    builder.append("Клан🏠 4️⃣ ")
    builder.append_custom_emoji("🏠", "123")
    builder.append("|")
    builder.append_custom_emoji("🔰", "456")

    text, entities = builder.build()

    assert text == "Клан🏠 4️⃣ 🏠|🔰"
    first_offset = utf16_length("Клан🏠 4️⃣ ")
    assert entities == (
        TelegramMessageEntity("custom_emoji", first_offset, 2, "123"),
        TelegramMessageEntity("custom_emoji", first_offset + 3, 2, "456"),
    )
    assert utf16_length("4️⃣") == 3


@pytest.mark.asyncio
async def test_send_message_uses_exact_entities_payload() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramClient("secret", http_client)
        message_id = await client.send_message(
            chat_id=-1001,
            text="🏠",
            entities=(TelegramMessageEntity("custom_emoji", 0, 2, "123"),),
        )

    assert message_id == 77
    assert len(requests) == 1
    assert json.loads(requests[0].content) == {
        "chat_id": -1001,
        "text": "🏠",
        "entities": [{"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "123"}],
    }


@pytest.mark.asyncio
async def test_parse_mode_and_entities_are_mutually_exclusive() -> None:
    async with httpx.AsyncClient() as http_client:
        client = TelegramClient("secret", http_client)
        with pytest.raises(ValueError, match="parse_mode"):
            await client.send_message(
                1,
                "x",
                parse_mode="HTML",
                entities=(TelegramMessageEntity("bold", 0, 1),),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", (401, 403, 429, 500))
async def test_only_http_400_has_bad_request_exception(status_code: int) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status_code,
            json={"ok": False, "description": "failure"},
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = TelegramClient("secret", http_client)
        with pytest.raises(TelegramApiError) as captured:
            await client.send_message(1, "x")

    assert not isinstance(captured.value, TelegramBadRequestError)


@pytest.mark.asyncio
async def test_http_400_has_bad_request_exception() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(400, json={"ok": False, "description": "bad entity"})
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        with pytest.raises(TelegramBadRequestError, match="bad entity"):
            await TelegramClient("secret", http_client).send_message(1, "x")


@pytest.mark.asyncio
async def test_http_400_exposes_migrated_supergroup_chat_id() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            400,
            json={
                "ok": False,
                "description": "Bad Request: group chat was upgraded to a supergroup chat",
                "parameters": {"migrate_to_chat_id": -1004441868861},
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as http_client:
        with pytest.raises(TelegramChatMigratedError) as captured:
            await TelegramClient("secret", http_client).get_chat_member(-5367551907, 1001)

    assert captured.value.new_chat_id == -1004441868861


@pytest.mark.asyncio
async def test_network_error_is_not_bad_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(TelegramApiError) as captured:
            await TelegramClient("secret", http_client).send_message(1, "x")

    assert not isinstance(captured.value, TelegramBadRequestError)
