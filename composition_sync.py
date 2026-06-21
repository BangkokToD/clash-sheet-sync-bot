"""Синхронизация листа состава Clash of Clans."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from coc_client import ClashClient
from config import AppConfig
from models import ClanConfig, normalize_tag
from sheets_client import CellValue, SheetValues, SheetsClient

ACTIVE_HEADERS: Final = (
    "№",
    "Тег",
    "Ратуша",
    "Никнейм",
    "Юзернейм",
    "Имя",
    "Закрепление",
    "Нарушения",
)
EXITED_HEADERS: Final = (*ACTIVE_HEADERS, "Дата выхода")
PLAYER_TAG_RE: Final = re.compile(r"#[0289PYLQGRJCUV]+", re.IGNORECASE)
A1_CELL_RE: Final = re.compile(r"^\$?([A-Za-z]+)\$?([1-9][0-9]*)$")


class CompositionSyncError(RuntimeError):
    """Базовая ошибка синхронизации состава."""


class CompositionDataError(CompositionSyncError):
    """Ошибка данных листа или API, при которой лист нельзя менять."""


@dataclass(frozen=True, slots=True)
class UserFields:
    """Пользовательские поля строки состава.

    Attributes:
        username: Telegram username или другой идентификатор.
        real_name: Реальное имя игрока.
        assignment: Закрепление игрока.
        violations: Нарушения игрока.
    """

    username: str = ""
    real_name: str = ""
    assignment: str = ""
    violations: str = ""


@dataclass(frozen=True, slots=True)
class OldCompositionPlayer:
    """Игрок, найденный на старом листе состава.

    Attributes:
        tag: Нормализованный player tag.
        town_hall: Старое значение ратуши.
        nickname: Старый никнейм.
        user_fields: Пользовательские поля.
        is_exited: Находился ли игрок в таблице вышедших.
        clan_tag: Тег старого активного клана, если удалось определить.
        exited_at: Старая дата выхода.
    """

    tag: str
    town_hall: str
    nickname: str
    user_fields: UserFields
    is_exited: bool
    clan_tag: str | None = None
    exited_at: str = ""


@dataclass(frozen=True, slots=True)
class CurrentClanMember:
    """Текущий участник семейного клана из CoC API.

    Attributes:
        tag: Нормализованный player tag.
        name: Никнейм из CoC API.
        town_hall_level: Уровень ратуши из CoC API.
        clan: Конфигурация текущего клана.
        api_order: Порядок игрока в ответе API.
    """

    tag: str
    name: str
    town_hall_level: int
    clan: ClanConfig
    api_order: int


@dataclass(frozen=True, slots=True)
class CompositionPlayerRow:
    """Строка игрока для нового листа состава.

    Attributes:
        tag: Нормализованный player tag.
        town_hall: Значение ратуши.
        nickname: Никнейм игрока.
        user_fields: Пользовательские поля.
        exited_at: Дата выхода для таблицы вышедших.
        sort_key: Ключ сортировки внутри таблицы.
    """

    tag: str
    town_hall: int | str
    nickname: str
    user_fields: UserFields
    exited_at: str = ""
    sort_key: tuple[Any, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class CompositionChanges:
    """Счётчики изменений состава.

    Attributes:
        added: Количество новых игроков.
        updated: Количество игроков с изменённой ратушей или ником.
        moved: Количество переходов между семейными кланами.
        exited: Количество новых выходов из семьи.
        returned: Количество возвратов из таблицы вышедших.
    """

    added: int = 0
    updated: int = 0
    moved: int = 0
    exited: int = 0
    returned: int = 0

    @property
    def has_changes(self) -> bool:
        """Проверяет наличие изменений.

        Returns:
            `True`, если хотя бы один счётчик больше нуля.
        """

        return any((self.added, self.updated, self.moved, self.exited, self.returned))


@dataclass(frozen=True, slots=True)
class CompositionSyncResult:
    """Результат успешной синхронизации состава.

    Attributes:
        clan_counts: Количество активных игроков по кланам.
        exited_count: Количество игроков в таблице вышедших.
        changes: Счётчики изменений.
    """

    clan_counts: tuple[tuple[str, int], ...]
    exited_count: int
    changes: CompositionChanges

    def to_telegram_message(self) -> str:
        """Формирует Telegram-отчёт об обновлении состава.

        Returns:
            Короткий отчёт для Telegram.
        """

        if not self.changes.has_changes:
            return "Состав обновлён. Изменений нет."

        lines = ["Состав обновлён.", ""]
        lines.extend(f"{name}: {count} игроков" for name, count in self.clan_counts)
        lines.extend(
            [
                f"Вышедшие: {self.exited_count} игроков",
                "",
                f"Добавлено: {self.changes.added}",
                f"Обновлено: {self.changes.updated}",
                f"Перешло между кланами: {self.changes.moved}",
                f"Вышло: {self.changes.exited}",
                f"Вернулось: {self.changes.returned}",
            ],
        )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ParsedComposition:
    """Старое состояние листа состава.

    Attributes:
        players_by_tag: Игроки, найденные на старом листе, по player tag.
    """

    players_by_tag: dict[str, OldCompositionPlayer]


@dataclass(frozen=True, slots=True)
class TableHeader:
    """Найденный заголовок таблицы состава.

    Attributes:
        row_index: Индекс строки заголовка в матрице.
        column_index: Индекс первой колонки таблицы.
        is_exited: Таблица вышедших.
        width: Ширина таблицы в колонках.
        clan_tag: Тег активного клана для активной таблицы.
    """

    row_index: int
    column_index: int
    is_exited: bool
    width: int
    clan_tag: str | None = None


async def run_composition_sync(
    config: AppConfig,
    clash_client: ClashClient,
    sheets_client: SheetsClient,
    detected_at: datetime,
) -> CompositionSyncResult:
    """Выполняет полную синхронизацию листа `Состав`.

    Args:
        config: Конфигурация приложения.
        clash_client: Клиент Clash of Clans API.
        sheets_client: Клиент Google Sheets API.
        detected_at: Дата и время обнаружения изменений.

    Returns:
        Результат успешной синхронизации.

    Raises:
        CompositionDataError: Если данные старого листа или API некорректны.
        ClashApiUnavailableError: Если Clash API недоступен.
        GoogleSheetsError: Если Google Sheets недоступен.
    """

    current_members_by_clan = await _load_current_members(config, clash_client)
    old_values = await sheets_client.read_values(
        config.composition_sheet_name,
        config.composition_managed_range,
    )
    old_composition = parse_old_composition(
        old_values,
        known_clan_tags={clan.tag for clan in config.clans},
    )

    active_rows_by_clan, exited_rows, changes = _build_new_composition(
        config=config,
        old_composition=old_composition,
        current_members_by_clan=current_members_by_clan,
        detected_at=detected_at,
    )

    active_matrix = _build_active_matrix(config.clans, active_rows_by_clan)
    exited_matrix = _build_exited_matrix(exited_rows)

    await sheets_client.rewrite_managed_range(
        sheet_name=config.composition_sheet_name,
        managed_range_a1=config.composition_managed_range,
        updates=[
            SheetValues(
                sheet_name=config.composition_sheet_name,
                range_a1=_range_from_start_cell(
                    config.composition_active_start_cell,
                    rows_count=len(active_matrix),
                    columns_count=len(ACTIVE_HEADERS),
                ),
                values=active_matrix,
            ),
            SheetValues(
                sheet_name=config.composition_sheet_name,
                range_a1=_range_from_start_cell(
                    config.composition_exited_start_cell,
                    rows_count=len(exited_matrix),
                    columns_count=len(EXITED_HEADERS),
                ),
                values=exited_matrix,
            ),
        ],
    )

    return CompositionSyncResult(
        clan_counts=tuple(
            (clan.name, len(active_rows_by_clan.get(clan.tag, [])))
            for clan in config.clans
        ),
        exited_count=len(exited_rows),
        changes=changes,
    )


def parse_old_composition(
    values: Sequence[Sequence[CellValue]],
    *,
    known_clan_tags: set[str],
) -> ParsedComposition:
    """Парсит старый лист состава без привязки к номерам строк.

    Args:
        values: Значения из `COMPOSITION_MANAGED_RANGE`.
        known_clan_tags: Теги семейных кланов из конфигурации.

    Returns:
        Старое состояние состава.

    Raises:
        CompositionDataError: Если найден дубль или повреждённый тег.
    """

    rows = [_normalize_row(row) for row in values]
    headers = _find_table_headers(rows)
    players_by_tag: dict[str, OldCompositionPlayer] = {}

    for header in headers:
        next_header_row = _find_next_header_row(headers, header)
        for row in rows[header.row_index + 1 : next_header_row]:
            player = _parse_player_row(row, header, known_clan_tags)
            if player is None:
                continue
            if player.tag in players_by_tag:
                raise CompositionDataError(
                    f'найден дубль тега {player.tag} на листе "Состав".',
                )
            players_by_tag[player.tag] = player

    return ParsedComposition(players_by_tag=players_by_tag)


async def _load_current_members(
    config: AppConfig,
    clash_client: ClashClient,
) -> dict[str, list[CurrentClanMember]]:
    """Загружает текущий состав трёх кланов из CoC API.

    Args:
        config: Конфигурация приложения.
        clash_client: Клиент Clash of Clans API.

    Returns:
        Участники по тегу клана.

    Raises:
        CompositionDataError: Если один player tag пришёл из двух кланов.
        ClashApiUnavailableError: Если Clash API недоступен.
    """

    members_by_clan: dict[str, list[CurrentClanMember]] = {}
    seen_tags: dict[str, str] = {}

    for clan in config.clans:
        raw_members = await clash_client.get_clan_members(clan.tag)
        clan_members: list[CurrentClanMember] = []

        for api_order, raw_member in enumerate(raw_members, start=1):
            tag = _require_api_member_tag(raw_member)
            name = _require_api_member_name(raw_member, tag)
            town_hall_level = _require_api_member_town_hall(raw_member, tag)

            previous_clan_tag = seen_tags.get(tag)
            if previous_clan_tag is not None:
                raise CompositionDataError(
                    f"player tag {tag} пришёл сразу в двух семейных кланах: "
                    f"{previous_clan_tag} и {clan.tag}.",
                )

            seen_tags[tag] = clan.tag
            clan_members.append(
                CurrentClanMember(
                    tag=tag,
                    name=name,
                    town_hall_level=town_hall_level,
                    clan=clan,
                    api_order=api_order,
                ),
            )

        members_by_clan[clan.tag] = clan_members

    return members_by_clan


def _build_new_composition(
    *,
    config: AppConfig,
    old_composition: ParsedComposition,
    current_members_by_clan: dict[str, list[CurrentClanMember]],
    detected_at: datetime,
) -> tuple[dict[str, list[CompositionPlayerRow]], list[CompositionPlayerRow], CompositionChanges]:
    """Строит новое состояние состава в памяти.

    Args:
        config: Конфигурация приложения.
        old_composition: Старое состояние листа.
        current_members_by_clan: Текущие участники по кланам.
        detected_at: Дата и время обнаружения выхода.

    Returns:
        Активные строки по кланам, строки вышедших и счётчики изменений.
    """

    current_by_tag = {
        member.tag: member
        for members in current_members_by_clan.values()
        for member in members
    }
    active_rows_by_clan: dict[str, list[CompositionPlayerRow]] = {
        clan.tag: [] for clan in config.clans
    }
    exited_rows: list[CompositionPlayerRow] = []

    added = updated = moved = exited = returned = 0

    for clan in config.clans:
        for member in current_members_by_clan.get(clan.tag, []):
            old_player = old_composition.players_by_tag.get(member.tag)
            user_fields = old_player.user_fields if old_player is not None else UserFields()

            if old_player is None:
                added += 1
            elif old_player.is_exited:
                returned += 1
            else:
                if old_player.clan_tag is not None and old_player.clan_tag != member.clan.tag:
                    moved += 1
                elif _has_technical_update(old_player, member):
                    updated += 1

            active_rows_by_clan[clan.tag].append(
                CompositionPlayerRow(
                    tag=member.tag,
                    town_hall=member.town_hall_level,
                    nickname=member.name,
                    user_fields=user_fields,
                    sort_key=(member.api_order, member.tag),
                ),
            )

    detected_at_text = detected_at.replace(microsecond=0).isoformat()
    for old_player in old_composition.players_by_tag.values():
        if old_player.tag in current_by_tag:
            continue

        if old_player.is_exited:
            exited_at = old_player.exited_at
        else:
            exited += 1
            exited_at = detected_at_text

        exited_rows.append(
            CompositionPlayerRow(
                tag=old_player.tag,
                town_hall=old_player.town_hall,
                nickname=old_player.nickname,
                user_fields=old_player.user_fields,
                exited_at=exited_at,
                sort_key=(old_player.tag,),
            ),
        )

    for rows in active_rows_by_clan.values():
        rows.sort(key=lambda row: row.sort_key)
    exited_rows.sort(key=lambda row: row.sort_key)

    return (
        active_rows_by_clan,
        exited_rows,
        CompositionChanges(
            added=added,
            updated=updated,
            moved=moved,
            exited=exited,
            returned=returned,
        ),
    )


def _build_active_matrix(
    clans: tuple[ClanConfig, ClanConfig, ClanConfig],
    rows_by_clan: dict[str, list[CompositionPlayerRow]],
) -> list[list[CellValue]]:
    """Строит матрицу активных таблиц состава.

    Args:
        clans: Три семейных клана.
        rows_by_clan: Активные строки по тегу клана.

    Returns:
        Матрица значений для левой зоны листа.
    """

    matrix: list[list[CellValue]] = []
    for clan_index, clan in enumerate(clans):
        if clan_index > 0:
            matrix.append(_empty_row(len(ACTIVE_HEADERS)))

        matrix.append(_title_row(f"{clan.name} | {clan.tag}", len(ACTIVE_HEADERS)))
        matrix.append(list(ACTIVE_HEADERS))

        for row_number, row in enumerate(rows_by_clan.get(clan.tag, []), start=1):
            matrix.append(_active_player_to_row(row_number, row))

    return matrix


def _build_exited_matrix(rows: Sequence[CompositionPlayerRow]) -> list[list[CellValue]]:
    """Строит матрицу таблицы вышедших.

    Args:
        rows: Строки вышедших игроков.

    Returns:
        Матрица значений для правой зоны листа.
    """

    matrix: list[list[CellValue]] = [
        _title_row("Вышедшие", len(EXITED_HEADERS)),
        list(EXITED_HEADERS),
    ]

    for row_number, row in enumerate(rows, start=1):
        matrix.append(_exited_player_to_row(row_number, row))

    return matrix


def _active_player_to_row(row_number: int, row: CompositionPlayerRow) -> list[CellValue]:
    """Преобразует активного игрока в строку листа.

    Args:
        row_number: Порядковый номер в таблице.
        row: Данные игрока.

    Returns:
        Строка активной таблицы.
    """

    return [
        row_number,
        row.tag,
        row.town_hall,
        row.nickname,
        row.user_fields.username,
        row.user_fields.real_name,
        row.user_fields.assignment,
        row.user_fields.violations,
    ]


def _exited_player_to_row(row_number: int, row: CompositionPlayerRow) -> list[CellValue]:
    """Преобразует вышедшего игрока в строку листа.

    Args:
        row_number: Порядковый номер в таблице.
        row: Данные игрока.

    Returns:
        Строка таблицы вышедших.
    """

    return [
        row_number,
        row.tag,
        row.town_hall,
        row.nickname,
        row.user_fields.username,
        row.user_fields.real_name,
        row.user_fields.assignment,
        row.user_fields.violations,
        row.exited_at,
    ]


def _find_table_headers(rows: Sequence[list[str]]) -> list[TableHeader]:
    """Ищет заголовки активных таблиц и таблицы вышедших.

    Args:
        rows: Нормализованные строки листа.

    Returns:
        Найденные таблицы состава.
    """

    headers: list[TableHeader] = []
    for row_index, row in enumerate(rows):
        max_start = len(row)
        for column_index in range(max_start):
            if _matches_header(row, column_index, EXITED_HEADERS):
                headers.append(
                    TableHeader(
                        row_index=row_index,
                        column_index=column_index,
                        is_exited=True,
                        width=len(EXITED_HEADERS),
                    ),
                )
                continue

            if _matches_header(row, column_index, ACTIVE_HEADERS):
                headers.append(
                    TableHeader(
                        row_index=row_index,
                        column_index=column_index,
                        is_exited=False,
                        width=len(ACTIVE_HEADERS),
                        clan_tag=_find_clan_tag_above(rows, row_index, column_index),
                    ),
                )

    return sorted(headers, key=lambda header: (header.column_index, header.row_index))


def _matches_header(
    row: Sequence[str],
    column_index: int,
    expected_headers: Sequence[str],
) -> bool:
    """Проверяет совпадение заголовков таблицы.

    Args:
        row: Строка листа.
        column_index: Индекс первой колонки.
        expected_headers: Ожидаемые заголовки.

    Returns:
        `True`, если заголовок найден.
    """

    if column_index + len(expected_headers) > len(row):
        return False

    actual_headers = tuple(
        _normalize_header(row[column_index + offset])
        for offset in range(len(expected_headers))
    )
    return actual_headers == tuple(expected_headers)


def _find_clan_tag_above(
    rows: Sequence[list[str]],
    header_row_index: int,
    column_index: int,
) -> str | None:
    """Ищет тег клана в строках над активной таблицей.

    Args:
        rows: Строки листа.
        header_row_index: Индекс строки заголовка.
        column_index: Индекс первой колонки таблицы.

    Returns:
        Нормализованный тег клана или `None`.
    """

    for row_index in range(header_row_index - 1, -1, -1):
        row = rows[row_index]
        segment = row[column_index : column_index + len(ACTIVE_HEADERS)]
        if not any(cell.strip() for cell in segment):
            continue

        for cell in segment:
            match = PLAYER_TAG_RE.search(cell)
            if match is None:
                continue
            try:
                return normalize_tag(match.group(0))
            except ValueError:
                return None
        return None

    return None


def _find_next_header_row(headers: Sequence[TableHeader], current: TableHeader) -> int:
    """Ищет начало следующей таблицы в той же колонке.

    Args:
        headers: Все найденные заголовки.
        current: Текущая таблица.

    Returns:
        Индекс следующего заголовка или большое число для последней таблицы.
    """

    next_rows = [
        header.row_index
        for header in headers
        if header.column_index == current.column_index
        and header.row_index > current.row_index
    ]
    return min(next_rows, default=10**9)


def _parse_player_row(
    row: Sequence[str],
    header: TableHeader,
    known_clan_tags: set[str],
) -> OldCompositionPlayer | None:
    """Парсит одну строку старой таблицы состава.

    Args:
        row: Строка листа.
        header: Метаданные таблицы.
        known_clan_tags: Теги семейных кланов из конфигурации.

    Returns:
        Игрок или `None`, если строка не является игроком.

    Raises:
        CompositionDataError: Если в строке найден повреждённый тег.
    """

    tag_cell_raw = _cell_at(row, header.column_index + 1)
    tag_cell = tag_cell_raw.strip()
    if tag_cell == "":
        return None

    if not tag_cell.startswith("#"):
        if _looks_like_non_player_row(row, header):
            return None
        raise CompositionDataError(f'найден повреждённый тег "{tag_cell}" на листе "Состав".')

    try:
        tag = normalize_tag(tag_cell)
    except ValueError as exc:
        raise CompositionDataError(
            f'найден повреждённый тег "{tag_cell}" на листе "Состав".',
        ) from exc

    if _looks_like_table_title(row, header, tag, known_clan_tags):
        return None

    return OldCompositionPlayer(
        tag=tag,
        town_hall=_cell_at(row, header.column_index + 2),
        nickname=_cell_at(row, header.column_index + 3),
        user_fields=UserFields(
            username=_cell_at(row, header.column_index + 4),
            real_name=_cell_at(row, header.column_index + 5),
            assignment=_cell_at(row, header.column_index + 6),
            violations=_cell_at(row, header.column_index + 7),
        ),
        is_exited=header.is_exited,
        clan_tag=None if header.is_exited else header.clan_tag,
        exited_at=_cell_at(row, header.column_index + 8) if header.is_exited else "",
    )


def _looks_like_non_player_row(row: Sequence[str], header: TableHeader) -> bool:
    """Проверяет, похожа ли строка на служебную, а не на игрока.

    Args:
        row: Строка листа.
        header: Метаданные таблицы.

    Returns:
        `True`, если строку можно безопасно пропустить.
    """

    number_cell = _cell_at(row, header.column_index)
    if number_cell.strip().isdigit():
        return False

    technical_cells = [
        _cell_at(row, header.column_index + 2),
        _cell_at(row, header.column_index + 3),
    ]
    user_cells = [
        _cell_at(row, header.column_index + index)
        for index in range(4, header.width)
    ]
    return all(_is_blank(cell) for cell in [*technical_cells, *user_cells])


def _looks_like_table_title(
    row: Sequence[str],
    header: TableHeader,
    tag: str,
    known_clan_tags: set[str],
) -> bool:
    """Проверяет, является ли строка названием следующей таблицы.

    Args:
        row: Строка листа.
        header: Метаданные таблицы.
        tag: Нормализованный тег из колонки `Тег`.
        known_clan_tags: Теги семейных кланов из конфигурации.

    Returns:
        `True`, если это не строка игрока.
    """

    if tag not in known_clan_tags:
        return False

    town_hall = _cell_at(row, header.column_index + 2)
    nickname = _cell_at(row, header.column_index + 3)
    user_cells = [
        _cell_at(row, header.column_index + index)
        for index in range(4, header.width)
    ]
    return _is_blank(town_hall) and _is_blank(nickname) and all(
        _is_blank(cell) for cell in user_cells
    )


def _has_technical_update(
    old_player: OldCompositionPlayer,
    current_member: CurrentClanMember,
) -> bool:
    """Проверяет изменение технических полей игрока.

    Args:
        old_player: Игрок из старого листа.
        current_member: Игрок из CoC API.

    Returns:
        `True`, если изменились ратуша или ник.
    """

    return (
        old_player.town_hall.strip() != str(current_member.town_hall_level)
        or old_player.nickname != current_member.name
    )


def _require_api_member_tag(raw_member: dict[str, Any]) -> str:
    """Читает player tag участника API.

    Args:
        raw_member: Участник из `ClashClient`.

    Returns:
        Нормализованный player tag.

    Raises:
        CompositionDataError: Если тег некорректен.
    """

    value = raw_member.get("tag")
    if not isinstance(value, str):
        raise CompositionDataError("CoC API вернул участника без tag.")

    try:
        return normalize_tag(value)
    except ValueError as exc:
        raise CompositionDataError(f"CoC API вернул некорректный player tag: {value}.") from exc


def _require_api_member_name(raw_member: dict[str, Any], tag: str) -> str:
    """Читает никнейм участника API.

    Args:
        raw_member: Участник из `ClashClient`.
        tag: Player tag для текста ошибки.

    Returns:
        Никнейм игрока.

    Raises:
        CompositionDataError: Если никнейм отсутствует.
    """

    value = raw_member.get("name")
    if not isinstance(value, str):
        raise CompositionDataError(f"CoC API вернул игрока {tag} без name.")
    return value


def _require_api_member_town_hall(raw_member: dict[str, Any], tag: str) -> int:
    """Читает уровень ратуши участника API.

    Args:
        raw_member: Участник из `ClashClient`.
        tag: Player tag для текста ошибки.

    Returns:
        Уровень ратуши.

    Raises:
        CompositionDataError: Если ратуша отсутствует.
    """

    value = raw_member.get("townHallLevel")
    if not isinstance(value, int) or isinstance(value, bool):
        raise CompositionDataError(f"CoC API вернул игрока {tag} без townHallLevel.")
    return value


def _range_from_start_cell(start_cell: str, rows_count: int, columns_count: int) -> str:
    """Строит закрытый A1-диапазон по стартовой ячейке и размеру.

    Args:
        start_cell: Стартовая ячейка, например `A1`.
        rows_count: Количество строк.
        columns_count: Количество колонок.

    Returns:
        A1-диапазон вида `A1:H10`.

    Raises:
        CompositionDataError: Если стартовая ячейка некорректна.
    """

    match = A1_CELL_RE.fullmatch(start_cell.strip())
    if match is None:
        raise CompositionDataError(f"Некорректная стартовая ячейка: {start_cell}.")

    start_column, start_row_raw = match.groups()
    start_column_number = _column_to_number(start_column)
    start_row = int(start_row_raw)

    end_column = _number_to_column(start_column_number + columns_count - 1)
    end_row = start_row + rows_count - 1
    return f"{start_column.upper()}{start_row}:{end_column}{end_row}"


def _normalize_row(row: Sequence[CellValue]) -> list[str]:
    """Преобразует строку Google Sheets в список строк.

    Args:
        row: Строка значений Google Sheets.

    Returns:
        Строковые значения без `None`.
    """

    return [_cell_to_str(cell) for cell in row]


def _cell_to_str(value: CellValue) -> str:
    """Преобразует значение ячейки в строку без нормализации пользовательских данных.

    Args:
        value: Значение ячейки.

    Returns:
        Строковое представление значения.
    """

    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _cell_at(row: Sequence[str], index: int) -> str:
    """Безопасно читает ячейку строки.

    Args:
        row: Строка листа.
        index: Индекс ячейки.

    Returns:
        Значение ячейки или пустая строка.
    """

    if index < 0 or index >= len(row):
        return ""
    return row[index]


def _is_blank(value: str) -> bool:
    """Проверяет структурную пустоту ячейки.

    Args:
        value: Значение ячейки.

    Returns:
        `True`, если в ячейке нет значимых символов.
    """

    return value.strip() == ""


def _normalize_header(value: str) -> str:
    """Нормализует заголовок таблицы для сравнения.

    Args:
        value: Исходный заголовок.

    Returns:
        Заголовок без пробелов по краям.
    """

    return value.strip()


def _empty_row(width: int) -> list[CellValue]:
    """Создаёт пустую строку заданной ширины.

    Args:
        width: Количество колонок.

    Returns:
        Строка из пустых значений.
    """

    return ["" for _ in range(width)]


def _title_row(title: str, width: int) -> list[CellValue]:
    """Создаёт строку заголовка таблицы.

    Args:
        title: Текст заголовка.
        width: Ширина таблицы.

    Returns:
        Строка заголовка.
    """

    return [title, *("" for _ in range(width - 1))]


def _column_to_number(column: str) -> int:
    """Преобразует буквенное имя колонки в номер.

    Args:
        column: Имя колонки, например `A`.

    Returns:
        Номер колонки, начиная с 1.
    """

    number = 0
    for char in column.upper():
        number = number * 26 + ord(char) - ord("A") + 1
    return number


def _number_to_column(number: int) -> str:
    """Преобразует номер колонки в буквенное имя.

    Args:
        number: Номер колонки, начиная с 1.

    Returns:
        Буквенное имя колонки.
    """

    chars: list[str] = []
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        chars.append(chr(ord("A") + remainder))
    return "".join(reversed(chars))