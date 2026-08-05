"""Администраторский кнопочный flow ручного расписания ЛВК."""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Protocol

import aiosqlite
import httpx

from clash_sheet_sync_bot.coc.client import (
    ClashApiUnavailableError,
    ClashClient,
    ClashCwlNotInProgressError,
    JsonObject,
)
from clash_sheet_sync_bot.common.time import utc_now
from clash_sheet_sync_bot.models import AppConfig, TrackedClan
from clash_sheet_sync_bot.repositories import (
    ClanSettingsRepository,
    CwlForecastRepository,
    CwlForecastRound,
    CwlForecastScheduleKey,
    CwlForecastSession,
    CwlForecastSessionConflictError,
    TelegramChatRepository,
)
from clash_sheet_sync_bot.telegram.access import TelegramAccessService
from clash_sheet_sync_bot.telegram.client import TelegramApiError, TelegramClient

from .domain import (
    CwlForecastDataError,
    extract_known_opponents,
    league_group_fingerprint,
    parse_cwl_war,
    parse_league_group,
)
from .models import CreatedWar, LeagueGroup

CALLBACK_PREFIX: Final = "cf:"
HTTP_TIMEOUT_SECONDS: Final = 45.0
logger = logging.getLogger(__name__)


class ScheduleClashClient(Protocol):
    """Минимальный Clash contract schedule flow и его tests."""

    async def get_current_war_league_group(self, clan_tag: str) -> JsonObject: ...

    async def get_cwl_war(self, war_tag: str) -> JsonObject: ...


@dataclass(frozen=True, slots=True)
class ScheduleContext:
    """Свежая League Group и восстановленные API-known раунды."""

    group: LeagueGroup
    key: CwlForecastScheduleKey
    known: dict[int, str]
    unknown_rounds: tuple[int, ...]


class CwlForecastScheduleFlow:
    """Команда и callbacks ввода глобального расписания ЛВК."""

    def __init__(
        self,
        *,
        config: AppConfig,
        telegram: TelegramClient,
        connection: aiosqlite.Connection,
        access: TelegramAccessService,
        clash_client: ScheduleClashClient | None = None,
    ) -> None:
        self._config = config
        self._telegram = telegram
        self._connection = connection
        self._access = access
        self._clash_client = clash_client
        self._repository = CwlForecastRepository(connection)
        self._chats = TelegramChatRepository(connection)
        self._clans = ClanSettingsRepository(connection)

    async def handle_command(self, *, chat_id: int, chat_type: str, user_id: int) -> None:
        """Проверяет доступ и открывает schedule flow для одного/нескольких кланов."""

        if chat_type not in {"group", "supergroup"} or not await self._is_connected(chat_id):
            await self._telegram.send_message(
                chat_id=chat_id,
                text="Команда /cwl_forecast_schedule работает в подключённой группе.",
            )
            return
        if not await self._fresh_admin(chat_id, user_id):
            await self._telegram.send_message(chat_id=chat_id, text="Нет доступа.")
            return
        tracked = await self._clans.list_active_clans(chat_id)
        active: list[tuple[int, TrackedClan]] = []
        for index, clan in enumerate(tracked):
            try:
                await self._load_context(clan.clan_tag)
            except ClashCwlNotInProgressError:
                continue
            except (ClashApiUnavailableError, CwlForecastDataError) as exc:
                logger.warning("schedule clan discovery failed, clan=%s: %s", clan.clan_tag, exc)
                continue
            active.append((index, clan))
        if not active:
            await self._telegram.send_message(chat_id=chat_id, text="Активная ЛВК не найдена.")
            return
        if len(active) > 1:
            markup = {
                "inline_keyboard": [
                    [
                        {
                            "text": f"{clan.clan_name} · {clan.clan_tag}",
                            "callback_data": f"cf:new:{index}",
                        }
                    ]
                    for index, clan in active
                ]
            }
            await self._telegram.send_message(
                chat_id=chat_id,
                text="Выберите клан, для которого нужно настроить расписание ЛВК.",
                reply_markup=markup,
            )
            return
        await self._start_session(
            chat_id=chat_id,
            user_id=user_id,
            clan=active[0][1],
            message_id=None,
        )

    async def handle_callback(
        self,
        *,
        callback_data: str,
        callback_query_id: str,
        chat_id: int,
        message_id: int,
        user_id: int,
    ) -> bool:
        """Обрабатывает только callbacks с prefix `cf:` и всегда fresh-check права."""

        if not callback_data.startswith(CALLBACK_PREFIX):
            return False
        if not await self._is_connected(chat_id) or not await self._fresh_admin(chat_id, user_id):
            await self._telegram.answer_callback_query(
                callback_query_id, "Нет доступа.", show_alert=True
            )
            return True
        parts = callback_data.split(":")
        if len(parts) == 3 and parts[:2] == ["cf", "new"]:
            try:
                index = int(parts[2])
            except ValueError:
                await self._invalid_callback(callback_query_id)
                return True
            clans = await self._clans.list_active_clans(chat_id)
            if index < 0 or index >= len(clans):
                await self._invalid_callback(callback_query_id)
                return True
            await self._telegram.answer_callback_query(callback_query_id, "Принято.")
            await self._start_session(
                chat_id=chat_id,
                user_id=user_id,
                clan=clans[index],
                message_id=message_id,
            )
            return True
        if len(parts) not in {3, 4} or parts[0] != "cf":
            await self._invalid_callback(callback_query_id)
            return True
        session_id, action = parts[1], parts[2]
        session = await self._repository.get_session(session_id)
        now = utc_now()
        if (
            session is None
            or session.source_chat_id != chat_id
            or session.created_by_user_id != user_id
            or session.expires_at <= now
        ):
            if session is not None and session.expires_at <= now:
                await self._repository.delete_session(session.id)
            await self._telegram.answer_callback_query(
                callback_query_id, "Сессия недоступна или истекла.", show_alert=True
            )
            return True
        try:
            context = await self._load_context(session.key.clan_tag)
        except (ClashApiUnavailableError, ClashCwlNotInProgressError, CwlForecastDataError):
            await self._repository.delete_session(session.id)
            await self._telegram.answer_callback_query(
                callback_query_id,
                "League Group изменилась. Запустите команду заново.",
                show_alert=True,
            )
            return True
        if context.key != session.key:
            await self._repository.delete_session(session.id)
            await self._telegram.answer_callback_query(
                callback_query_id,
                "League Group изменилась. Запустите команду заново.",
                show_alert=True,
            )
            return True
        draft = _parse_draft(session.draft, context)
        if action == "cancel":
            await self._repository.delete_session(session.id)
            await self._telegram.answer_callback_query(callback_query_id, "Отменено.")
            await self._telegram.edit_message_text(
                chat_id=chat_id, message_id=message_id, text="Настройка расписания отменена."
            )
            return True
        if action == "back":
            step = max(session.current_step - 1, 0)
            if context.unknown_rounds:
                draft.pop(str(context.unknown_rounds[step]), None)
            await self._update_and_render(session, context, draft, step, message_id)
            await self._telegram.answer_callback_query(callback_query_id, "Принято.")
            return True
        if action == "pick" and len(parts) == 4:
            await self._pick(
                session=session,
                context=context,
                draft=draft,
                raw_index=parts[3],
                message_id=message_id,
                callback_query_id=callback_query_id,
            )
            return True
        if action == "confirm":
            await self._confirm(
                session=session,
                context=context,
                draft=draft,
                message_id=message_id,
                callback_query_id=callback_query_id,
            )
            return True
        await self._invalid_callback(callback_query_id)
        return True

    async def _start_session(
        self, *, chat_id: int, user_id: int, clan: TrackedClan, message_id: int | None
    ) -> None:
        """Создаёт global-key session и показывает первый unknown раунд."""

        try:
            context = await self._load_context(clan.clan_tag)
        except ClashCwlNotInProgressError:
            await self._send_or_edit(
                chat_id, message_id, "Для выбранного клана активная ЛВК не найдена.", None
            )
            return
        except (ClashApiUnavailableError, CwlForecastDataError) as exc:
            logger.warning("schedule start failed, clan=%s: %s", clan.clan_tag, exc)
            await self._send_or_edit(
                chat_id, message_id, "Не удалось загрузить данные ЛВК. Повторите позже.", None
            )
            return
        existing = await self._repository.get_schedule(context.key)
        draft = (
            {
                str(item.round_number): item.opponent_clan_tag
                for item in existing.rounds
                if existing is not None and item.round_number in context.unknown_rounds
            }
            if existing is not None
            else {}
        )
        now = utc_now()
        session = CwlForecastSession(
            id=secrets.token_urlsafe(6),
            key=context.key,
            source_chat_id=chat_id,
            created_by_user_id=user_id,
            message_id=message_id,
            draft={"manual": draft},
            current_step=0,
            expires_at=now + timedelta(seconds=self._config.cwl_forecast_schedule_ttl_seconds),
            created_at=now,
            updated_at=now,
        )
        try:
            await self._repository.acquire_session(session=session, now=now)
        except CwlForecastSessionConflictError:
            await self._send_or_edit(
                chat_id,
                message_id,
                "Расписание уже редактируется другим администратором.",
                None,
            )
            return
        text, markup = _render(context, draft, 0, session.id)
        actual_message_id = await self._send_or_edit(chat_id, message_id, text, markup)
        await self._repository.update_session(
            session_id=session.id,
            draft={"manual": draft},
            current_step=0,
            message_id=actual_message_id or message_id,
            updated_at=now,
        )

    async def _pick(
        self,
        *,
        session: CwlForecastSession,
        context: ScheduleContext,
        draft: dict[str, str],
        raw_index: str,
        message_id: int,
        callback_query_id: str,
    ) -> None:
        try:
            index = int(raw_index)
        except ValueError:
            await self._invalid_callback(callback_query_id)
            return
        if (
            session.current_step >= len(context.unknown_rounds)
            or index < 0
            or index >= len(context.group.clans)
        ):
            await self._invalid_callback(callback_query_id)
            return
        clan = context.group.clans[index]
        round_number = context.unknown_rounds[session.current_step]
        used = set(context.known.values()) | {
            tag for key, tag in draft.items() if key != str(round_number)
        }
        if clan.tag == context.key.clan_tag or clan.tag in used:
            await self._invalid_callback(callback_query_id)
            return
        draft[str(round_number)] = clan.tag
        await self._update_and_render(session, context, draft, session.current_step + 1, message_id)
        await self._telegram.answer_callback_query(callback_query_id, "Принято.")

    async def _confirm(
        self,
        *,
        session: CwlForecastSession,
        context: ScheduleContext,
        draft: dict[str, str],
        message_id: int,
        callback_query_id: str,
    ) -> None:
        if session.current_step < len(context.unknown_rounds) or any(
            str(round_number) not in draft for round_number in context.unknown_rounds
        ):
            await self._invalid_callback(callback_query_id)
            return
        combined = {**context.known, **{int(key): value for key, value in draft.items()}}
        if set(combined) != set(range(1, len(context.group.rounds) + 1)):
            await self._telegram.answer_callback_query(
                callback_query_id, "Расписание неполное.", show_alert=True
            )
            return
        rounds = tuple(
            CwlForecastRound(
                round_number=number,
                opponent_clan_tag=combined[number],
                source="api" if number in context.known else "manual",
            )
            for number in sorted(combined)
        )
        await self._repository.confirm_schedule_from_session(
            session_id=session.id,
            key=context.key,
            rounds=rounds,
            created_by_user_id=session.created_by_user_id,
            source_chat_id=session.source_chat_id,
            now=utc_now(),
        )
        await self._telegram.answer_callback_query(callback_query_id, "Сохранено.")
        await self._telegram.edit_message_text(
            chat_id=session.source_chat_id,
            message_id=message_id,
            text=_schedule_text(context, combined) + "\n\nРасписание сохранено.",
        )

    async def _update_and_render(
        self,
        session: CwlForecastSession,
        context: ScheduleContext,
        draft: dict[str, str],
        step: int,
        message_id: int,
    ) -> None:
        await self._repository.update_session(
            session_id=session.id,
            draft={"manual": draft},
            current_step=step,
            message_id=message_id,
            updated_at=utc_now(),
        )
        text, markup = _render(context, draft, step, session.id)
        await self._telegram.edit_message_text(
            chat_id=session.source_chat_id,
            message_id=message_id,
            text=text,
            reply_markup=markup,
        )

    async def _load_context(self, clan_tag: str) -> ScheduleContext:
        if self._clash_client is not None:
            return await self._load_context_with_client(clan_tag, self._clash_client)
        timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as http_client:
            client = ClashClient(self._config.coc_api_token, http_client)
            return await self._load_context_with_client(clan_tag, client)

    async def _load_context_with_client(
        self, clan_tag: str, client: ScheduleClashClient
    ) -> ScheduleContext:
        group = parse_league_group(await client.get_current_war_league_group(clan_tag))
        tags = [
            (round_.number, tag)
            for round_ in group.rounds
            for tag in round_.war_tags
            if tag != "#0"
        ]
        semaphore = asyncio.Semaphore(self._config.cwl_war_concurrency_limit)

        async def load(round_number: int, war_tag: str) -> CreatedWar:
            async with semaphore:
                return CreatedWar(
                    round_number, war_tag, parse_cwl_war(await client.get_cwl_war(war_tag))
                )

        created = await asyncio.gather(*(load(number, tag) for number, tag in tags))
        known = extract_known_opponents(
            group=group,
            own_clan_tag=clan_tag,
            created_wars=created,
        )
        key = CwlForecastScheduleKey(
            season=group.season,
            group_fingerprint=league_group_fingerprint(group),
            clan_tag=clan_tag,
        )
        unknown = tuple(round_.number for round_ in group.rounds if round_.number not in known)
        return ScheduleContext(group=group, key=key, known=known, unknown_rounds=unknown)

    async def _fresh_admin(self, chat_id: int, user_id: int) -> bool:
        return (
            await self._access.is_admin(chat_id=chat_id, user_id=user_id, force_refresh=True)
        ).is_admin

    async def _is_connected(self, chat_id: int) -> bool:
        return await self._chats.is_connected_group(chat_id)

    async def _send_or_edit(
        self, chat_id: int, message_id: int | None, text: str, markup: dict[str, object] | None
    ) -> int | None:
        if message_id is not None:
            try:
                await self._telegram.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup
                )
                return message_id
            except TelegramApiError:
                pass
        return await self._telegram.send_message(chat_id=chat_id, text=text, reply_markup=markup)

    async def _invalid_callback(self, callback_query_id: str) -> None:
        await self._telegram.answer_callback_query(
            callback_query_id, "Кнопка устарела или повреждена.", show_alert=True
        )


def _parse_draft(raw: dict[str, object], context: ScheduleContext) -> dict[str, str]:
    manual = raw.get("manual")
    if not isinstance(manual, dict):
        raise CwlForecastDataError("Черновик расписания повреждён.")
    result: dict[str, str] = {}
    allowed = {str(number) for number in context.unknown_rounds}
    for key, value in manual.items():
        if not isinstance(key, str) or key not in allowed or not isinstance(value, str):
            raise CwlForecastDataError("Черновик расписания повреждён.")
        if value not in {clan.tag for clan in context.group.clans}:
            raise CwlForecastDataError("Черновик содержит чужой клан.")
        result[key] = value
    return result


def _render(
    context: ScheduleContext, draft: dict[str, str], step: int, session_id: str
) -> tuple[str, dict[str, object]]:
    combined = {**context.known, **{int(key): value for key, value in draft.items()}}
    text = _schedule_text(context, combined)
    rows: list[list[dict[str, str]]] = []
    if step < len(context.unknown_rounds):
        round_number = context.unknown_rounds[step]
        text += f"\n\nВыберите соперника для раунда {round_number}."
        used = set(context.known.values()) | {
            tag for key, tag in draft.items() if key != str(round_number)
        }
        current = draft.get(str(round_number))
        for index, clan in enumerate(context.group.clans):
            if clan.tag == context.key.clan_tag or (clan.tag in used and clan.tag != current):
                continue
            rows.append(
                [
                    {
                        "text": f"{clan.name} · ур. {clan.clan_level} · {clan.tag}",
                        "callback_data": f"cf:{session_id}:pick:{index}",
                    }
                ]
            )
    else:
        text += "\n\nПроверьте полное расписание перед записью."
        rows.append([{"text": "Подтвердить", "callback_data": f"cf:{session_id}:confirm"}])
    if step > 0:
        rows.append([{"text": "Назад", "callback_data": f"cf:{session_id}:back"}])
    rows.append([{"text": "Отмена", "callback_data": f"cf:{session_id}:cancel"}])
    return text, {"inline_keyboard": rows}


def _schedule_text(context: ScheduleContext, values: dict[int, str]) -> str:
    clans = {clan.tag: clan for clan in context.group.clans}
    lines = [f"Расписание ЛВК: {clans[context.key.clan_tag].name} | {context.key.clan_tag}"]
    for round_number in range(1, len(context.group.rounds) + 1):
        tag = values.get(round_number)
        if tag is None:
            value = "—"
        else:
            clan = clans[tag]
            value = f"{clan.name} | {clan.tag}"
            if round_number in context.known:
                value += " 🔒"
        lines.append(f"{round_number}. {value}")
    return "\n".join(lines)


def callback_data_fits_limit(payload: str) -> bool:
    """Проверяет официальный лимит callback data в 64 UTF-8 bytes."""

    return len(payload.encode("utf-8")) <= 64
