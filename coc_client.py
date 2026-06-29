"""Низкоуровневый клиент Clash of Clans API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote

import httpx

from models import normalize_tag

COC_API_BASE_URL: Final = "https://api.clashofclans.com/v1"

JsonObject = dict[str, Any]


class ClashApiUnavailableError(RuntimeError):
    """Ошибка недоступности или некорректности Clash of Clans API."""


class ClashCwlNotInProgressError(RuntimeError):
    """CWL не проводится для запрошенного клана."""


class ClashClanNotFoundError(RuntimeError):
    """Клан не найден в Clash of Clans API."""


@dataclass(frozen=True, slots=True)
class ClanLookupResult:
    """Результат проверки клана через CoC API.

    Attributes:
        tag: Нормализованный тег клана.
        name: Название клана из CoC API.
    """

    tag: str
    name: str


class ClashClient:
    """Низкоуровневый клиент Clash of Clans API.

    Клиент не содержит бизнес-логики синхронизации листов. Он только выполняет
    HTTP-запросы, кодирует теги и проверяет минимальный контракт ответа API.

    Args:
        api_token: Токен Clash of Clans API.
        client: Асинхронный HTTP-клиент.
        base_url: Базовый URL Clash of Clans API.
    """

    def __init__(
        self,
        api_token: str,
        client: httpx.AsyncClient,
        base_url: str = COC_API_BASE_URL,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {api_token}",
        }


    async def get_clan(self, clan_tag: str) -> ClanLookupResult:
        """Получает краткую информацию о клане по тегу.

        Args:
            clan_tag: Тег клана вида `#ABC123`.

        Returns:
            Нормализованный тег и название клана.

        Raises:
            ClashClanNotFoundError: Если клан не найден.
            ClashApiUnavailableError: Если API недоступно или ответ невалиден.
        """

        encoded_tag = encode_coc_tag(clan_tag)
        data = await self._get_json(
            f"/clans/{encoded_tag}",
            clan_404_as_not_found=True,
        )
        raw_tag = _require_str(data, "tag", "clan")
        name = _require_str(data, "name", "clan")
        try:
            normalized_tag = normalize_tag(raw_tag)
        except ValueError as exc:
            raise ClashApiUnavailableError("CoC clan response содержит некорректный tag.") from exc
        return ClanLookupResult(tag=normalized_tag, name=name)

    async def get_clan_members(self, clan_tag: str) -> list[JsonObject]:
        """Получает список участников клана.

        Args:
            clan_tag: Тег клана вида `#ABC123`.

        Returns:
            Список участников с полями `tag`, `name`, `townHallLevel`.

        Raises:
            ClashApiUnavailableError: Если API недоступно или ответ невалиден.
        """

        encoded_tag = encode_coc_tag(clan_tag)
        data = await self._get_json(f"/clans/{encoded_tag}/members")
        items = data.get("items")
        if not isinstance(items, list):
            raise ClashApiUnavailableError("CoC members response не содержит items.")

        members: list[JsonObject] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise ClashApiUnavailableError(
                    f"CoC members response содержит некорректного участника #{index}.",
                )

            tag = _require_str(item, "tag", f"member #{index}")
            name = _require_str(item, "name", f"member #{index}")
            town_hall_level = _require_int(item, "townHallLevel", f"member #{index}")

            try:
                normalized_tag = normalize_tag(tag)
            except ValueError as exc:
                raise ClashApiUnavailableError(
                    f"CoC members response содержит некорректный tag у member #{index}.",
                ) from exc

            members.append(
                {
                    "tag": normalized_tag,
                    "name": name,
                    "townHallLevel": town_hall_level,
                },
            )

        return members

    async def get_current_war_league_group(self, clan_tag: str) -> JsonObject:
        """Получает текущую CWL league group клана.

        Args:
            clan_tag: Тег клана вида `#ABC123`.

        Returns:
            JSON-объект league group.

        Raises:
            ClashCwlNotInProgressError: Если API вернул 404 для league group.
            ClashApiUnavailableError: Если API недоступно или ответ невалиден.
        """

        encoded_tag = encode_coc_tag(clan_tag)
        data = await self._get_json(
            f"/clans/{encoded_tag}/currentwar/leaguegroup",
            league_group_404_as_not_in_progress=True,
        )

        _require_str(data, "season", "leaguegroup")
        _require_str(data, "state", "leaguegroup")
        _require_list(data, "clans", "leaguegroup")
        _require_list(data, "rounds", "leaguegroup")
        return data

    async def get_cwl_war(self, war_tag: str) -> JsonObject:
        """Получает конкретную войну CWL по warTag.

        Args:
            war_tag: Тег войны CWL вида `#ABC123`.

        Returns:
            JSON-объект CWL war.

        Raises:
            ClashApiUnavailableError: Если API недоступно или ответ невалиден.
        """

        encoded_tag = encode_coc_tag(war_tag)
        data = await self._get_json(f"/clanwarleagues/wars/{encoded_tag}")

        _require_str(data, "state", "cwl war")
        _require_dict(data, "clan", "cwl war")
        _require_dict(data, "opponent", "cwl war")
        return data

    async def _get_json(
        self,
        path: str,
        *,
        league_group_404_as_not_in_progress: bool = False,
        clan_404_as_not_found: bool = False,
    ) -> JsonObject:
        """Выполняет GET-запрос к Clash of Clans API.

        Args:
            path: Путь API без базового URL.
            league_group_404_as_not_in_progress: Нужно ли трактовать HTTP 404
                как отсутствие CWL, а не как недоступность API.
            clan_404_as_not_found: Нужно ли трактовать HTTP 404 как отсутствие клана.

        Returns:
            JSON-объект ответа.

        Raises:
            ClashCwlNotInProgressError: Если CWL не проводится.
            ClashApiUnavailableError: Если API недоступно или ответ невалиден.
        """

        try:
            response = await self._client.get(
                f"{self._base_url}{path}",
                headers=self._headers,
            )
        except httpx.HTTPError as exc:
            raise ClashApiUnavailableError("CoC API network error.") from exc

        if response.status_code == 404 and league_group_404_as_not_in_progress:
            raise ClashCwlNotInProgressError("CWL не проводится.")
        if response.status_code == 404 and clan_404_as_not_found:
            raise ClashClanNotFoundError("Клан не найден.")

        if response.status_code >= 400:
            raise ClashApiUnavailableError(f"CoC API HTTP {response.status_code}.")

        try:
            data = response.json()
        except ValueError as exc:
            raise ClashApiUnavailableError("CoC API вернул битый JSON.") from exc

        if not isinstance(data, dict):
            raise ClashApiUnavailableError("CoC API вернул не JSON-объект.")

        return data


def encode_coc_tag(tag: str) -> str:
    """Кодирует CoC tag для URL path segment.

    Args:
        tag: Тег Clash of Clans вида `#ABC123`.

    Returns:
        URL-encoded тег, например `%23ABC123`.

    Raises:
        ValueError: Если тег некорректен.
    """

    return quote(normalize_tag(tag), safe="")


def _require_str(data: JsonObject, key: str, context: str) -> str:
    """Читает обязательное строковое поле API-ответа.

    Args:
        data: JSON-объект.
        key: Имя поля.
        context: Контекст для текста ошибки.

    Returns:
        Строковое значение.

    Raises:
        ClashApiUnavailableError: Если поле отсутствует или имеет неверный тип.
    """

    value = data.get(key)
    if not isinstance(value, str):
        raise ClashApiUnavailableError(f"{context}: поле {key} должно быть строкой.")
    return value


def _require_int(data: JsonObject, key: str, context: str) -> int:
    """Читает обязательное целочисленное поле API-ответа.

    Args:
        data: JSON-объект.
        key: Имя поля.
        context: Контекст для текста ошибки.

    Returns:
        Целочисленное значение.

    Raises:
        ClashApiUnavailableError: Если поле отсутствует или имеет неверный тип.
    """

    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ClashApiUnavailableError(f"{context}: поле {key} должно быть числом.")
    return value


def _require_list(data: JsonObject, key: str, context: str) -> list[Any]:
    """Читает обязательное списковое поле API-ответа.

    Args:
        data: JSON-объект.
        key: Имя поля.
        context: Контекст для текста ошибки.

    Returns:
        Список из API-ответа.

    Raises:
        ClashApiUnavailableError: Если поле отсутствует или имеет неверный тип.
    """

    value = data.get(key)
    if not isinstance(value, list):
        raise ClashApiUnavailableError(f"{context}: поле {key} должно быть списком.")
    return value


def _require_dict(data: JsonObject, key: str, context: str) -> JsonObject:
    """Читает обязательное объектное поле API-ответа.

    Args:
        data: JSON-объект.
        key: Имя поля.
        context: Контекст для текста ошибки.

    Returns:
        Вложенный JSON-объект.

    Raises:
        ClashApiUnavailableError: Если поле отсутствует или имеет неверный тип.
    """

    value = data.get(key)
    if not isinstance(value, dict):
        raise ClashApiUnavailableError(f"{context}: поле {key} должно быть объектом.")
    return value