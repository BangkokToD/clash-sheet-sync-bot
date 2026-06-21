"""Низкоуровневый клиент Google Sheets API."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote, urlencode

import httpx
from google.auth import exceptions as google_auth_exceptions
from google.oauth2 import service_account

SHEETS_API_BASE_URL: Final = "https://sheets.googleapis.com/v4/spreadsheets"
SHEETS_SCOPE: Final = "https://www.googleapis.com/auth/spreadsheets"
GOOGLE_AUTH_TIMEOUT_SECONDS: Final = 30.0

A1_RANGE_RE: Final = re.compile(
    r"^\$?([A-Za-z]+)\$?([1-9][0-9]*):\$?([A-Za-z]+)\$?([1-9][0-9]*)$",
)

CellValue = str | int | float | bool
JsonObject = dict[str, Any]


@dataclass(frozen=True, slots=True)
class SheetValues:
    """Набор значений для записи в диапазон Google Sheets.

    Attributes:
        sheet_name: Название листа.
        range_a1: A1-диапазон без названия листа.
        values: Матрица значений.
    """

    sheet_name: str
    range_a1: str
    values: Sequence[Sequence[CellValue]]


@dataclass(frozen=True, slots=True)
class SheetMetadata:
    """Метаданные одного листа Google Sheets.

    Attributes:
        sheet_id: Числовой ID листа.
        title: Название листа.
    """

    sheet_id: int
    title: str


class GoogleSheetsError(RuntimeError):
    """Базовая ошибка Google Sheets клиента."""


class GoogleSheetsAuthError(GoogleSheetsError):
    """Ошибка авторизации Google Sheets API."""


class GoogleSheetsReadError(GoogleSheetsError):
    """Ошибка чтения Google Sheets."""


class GoogleSheetsWriteError(GoogleSheetsError):
    """Ошибка записи Google Sheets."""


class GoogleAccessTokenProvider:
    """Поставщик access token для Google Sheets API.

    Args:
        service_account_file: Путь к JSON-файлу service account.
        scopes: OAuth scopes для Google API.
    """

    def __init__(
        self,
        service_account_file: str | Path,
        scopes: Sequence[str] = (SHEETS_SCOPE,),
    ) -> None:
        try:
            self._credentials = service_account.Credentials.from_service_account_file(
                str(service_account_file),
                scopes=list(scopes),
            )
        except (OSError, ValueError, google_auth_exceptions.GoogleAuthError) as exc:
            raise GoogleSheetsAuthError("Не удалось загрузить service account.") from exc

        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        """Возвращает актуальный access token.

        Returns:
            OAuth access token.

        Raises:
            GoogleSheetsAuthError: Если токен не удалось получить.
        """

        async with self._lock:
            if not self._credentials.valid or self._credentials.token is None:
                await asyncio.to_thread(self._refresh_credentials)

            token = self._credentials.token
            if not isinstance(token, str) or token == "":
                raise GoogleSheetsAuthError("Google access token пустой.")
            return token

    def _refresh_credentials(self) -> None:
        """Обновляет credentials через google-auth transport на базе httpx.

        Raises:
            GoogleSheetsAuthError: Если refresh завершился ошибкой.
        """

        try:
            self._credentials.refresh(_HttpxGoogleAuthRequest())
        except google_auth_exceptions.GoogleAuthError as exc:
            raise GoogleSheetsAuthError("Не удалось обновить Google access token.") from exc


class SheetsClient:
    """Низкоуровневый клиент Google Sheets Values API.

    Args:
        sheet_id: ID Google Sheets документа.
        token_provider: Поставщик Google access token.
        client: Асинхронный HTTP-клиент.
        base_url: Базовый URL Google Sheets API.
    """

    def __init__(
        self,
        sheet_id: str,
        token_provider: GoogleAccessTokenProvider,
        client: httpx.AsyncClient,
        base_url: str = SHEETS_API_BASE_URL,
    ) -> None:
        self._sheet_id = sheet_id
        self._token_provider = token_provider
        self._client = client
        self._base_url = base_url.rstrip("/")

    async def get_sheet_metadata(self, sheet_name: str) -> SheetMetadata:
        """Читает метаданные листа.

        Args:
            sheet_name: Название листа.

        Returns:
            Метаданные листа.

        Raises:
            GoogleSheetsReadError: Если лист не найден или ответ невалиден.
        """

        query = urlencode(
            {
                "fields": "sheets(properties(sheetId,title))",
            },
        )
        data = await self._request_json(
            method="GET",
            path=f"/{self._sheet_id}?{query}",
            error_cls=GoogleSheetsReadError,
        )

        sheets = data.get("sheets")
        if not isinstance(sheets, list):
            raise GoogleSheetsReadError("Google Sheets metadata не содержит sheets.")

        for raw_sheet in sheets:
            metadata = _parse_sheet_metadata(raw_sheet)
            if metadata.title == sheet_name:
                return metadata

        raise GoogleSheetsReadError(f"Лист {sheet_name} не найден.")

    async def read_values(self, sheet_name: str, range_a1: str) -> list[list[CellValue]]:
        """Читает значения из A1-диапазона.

        Args:
            sheet_name: Название листа.
            range_a1: A1-диапазон без названия листа.

        Returns:
            Матрица значений. Отсутствующие строки Google API не дополняются.

        Raises:
            GoogleSheetsReadError: Если чтение не удалось или ответ невалиден.
        """

        full_range = build_sheet_range(sheet_name, range_a1)
        encoded_range = quote(full_range, safe="")
        data = await self._request_json(
            method="GET",
            path=f"/{self._sheet_id}/values/{encoded_range}",
            error_cls=GoogleSheetsReadError,
        )

        raw_values = data.get("values", [])
        if not isinstance(raw_values, list):
            raise GoogleSheetsReadError("Google Sheets response values должен быть списком.")

        return _normalize_read_values(raw_values)

    async def batch_update_values(self, updates: Sequence[SheetValues]) -> JsonObject:
        """Записывает значения через values.batchUpdate.

        Args:
            updates: Набор диапазонов для записи.

        Returns:
            JSON-ответ Google Sheets API.

        Raises:
            GoogleSheetsWriteError: Если запись не удалась.
        """

        if not updates:
            return {}

        payload = {
            "valueInputOption": "RAW",
            "data": [
                {
                    "range": build_sheet_range(update.sheet_name, update.range_a1),
                    "values": _normalize_write_values(update.values),
                }
                for update in updates
            ],
        }

        return await self._request_json(
            method="POST",
            path=f"/{self._sheet_id}/values:batchUpdate",
            json_payload=payload,
            error_cls=GoogleSheetsWriteError,
        )

    async def batch_update_spreadsheet(
        self,
        requests: Sequence[JsonObject],
    ) -> JsonObject:
        """Выполняет `spreadsheets.batchUpdate`.

        Args:
            requests: Список batchUpdate requests.

        Returns:
            JSON-ответ Google Sheets API.

        Raises:
            GoogleSheetsWriteError: Если batchUpdate не выполнился.
        """

        if not requests:
            return {}

        return await self._request_json(
            method="POST",
            path=f"/{self._sheet_id}:batchUpdate",
            json_payload={"requests": list(requests)},
            error_cls=GoogleSheetsWriteError,
        )

    async def rewrite_managed_range(
        self,
        sheet_name: str,
        managed_range_a1: str,
        updates: Sequence[SheetValues],
    ) -> JsonObject:
        """Очищает managed range и записывает новые значения одним batchUpdate.

        Метод не вызывает отдельный `clear`. Очистка выполняется записью матрицы
        пустых строк `""` в управляемую область, после чего в том же запросе
        записываются актуальные диапазоны.

        Args:
            sheet_name: Название листа, внутри которого разрешена перезапись.
            managed_range_a1: Управляемая область без названия листа.
            updates: Актуальные диапазоны для записи после очистки.

        Returns:
            JSON-ответ Google Sheets API.

        Raises:
            GoogleSheetsWriteError: Если диапазон некорректен или запись не удалась.
        """

        clear_values = _build_empty_values_for_range(managed_range_a1)
        normalized_updates = [
            SheetValues(
                sheet_name=update.sheet_name,
                range_a1=update.range_a1,
                values=update.values,
            )
            for update in updates
        ]

        for update in normalized_updates:
            if update.sheet_name != sheet_name:
                raise GoogleSheetsWriteError(
                    "rewrite_managed_range не должен писать в другой лист.",
                )

        return await self.batch_update_values(
            [
                SheetValues(
                    sheet_name=sheet_name,
                    range_a1=managed_range_a1,
                    values=clear_values,
                ),
                *normalized_updates,
            ],
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        error_cls: type[GoogleSheetsError],
        json_payload: JsonObject | None = None,
    ) -> JsonObject:
        """Выполняет запрос к Google Sheets API.

        Args:
            method: HTTP-метод.
            path: Путь API без базового URL.
            error_cls: Класс ошибки для вызывающего метода.
            json_payload: JSON-тело запроса.

        Returns:
            JSON-объект ответа.

        Raises:
            GoogleSheetsError: Если API недоступно или ответ невалиден.
        """

        token = await self._token_provider.get_token()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }

        try:
            response = await self._client.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                json=json_payload,
            )
        except httpx.HTTPError as exc:
            raise error_cls("Google Sheets API network error.") from exc

        if response.status_code >= 400:
            error_message = _extract_google_error_message(response)
            raise error_cls(
                f"Google Sheets API HTTP {response.status_code}: {error_message}",
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise error_cls("Google Sheets API вернул битый JSON.") from exc

        if not isinstance(data, dict):
            raise error_cls("Google Sheets API вернул не JSON-объект.")

        return data


def build_sheet_range(sheet_name: str, range_a1: str) -> str:
    """Собирает полный A1-диапазон с безопасным названием листа.

    Args:
        sheet_name: Название листа.
        range_a1: A1-диапазон без названия листа.

    Returns:
        Полный диапазон вида `'Состав'!A1:R1000`.

    Raises:
        GoogleSheetsError: Если название листа или диапазон пустые.
    """

    sheet_name = sheet_name.strip()
    range_a1 = range_a1.strip()
    if sheet_name == "":
        raise GoogleSheetsError("Название листа не может быть пустым.")
    if range_a1 == "":
        raise GoogleSheetsError("A1-диапазон не может быть пустым.")

    escaped_sheet_name = sheet_name.replace("'", "''")
    return f"'{escaped_sheet_name}'!{range_a1}"


def _extract_google_error_message(response: httpx.Response) -> str:
    """Извлекает безопасный текст ошибки Google API.

    Args:
        response: HTTP-ответ Google API.

    Returns:
        Короткий текст ошибки без URL и токенов.
    """

    try:
        data = response.json()
    except ValueError:
        text = response.text.strip()
        return _truncate_error_message(text or "нет тела ответа")

    if not isinstance(data, dict):
        return "некорректное тело ошибки"

    error = data.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return _truncate_error_message(message.strip())

    return "нет сообщения ошибки"


def _truncate_error_message(message: str, limit: int = 500) -> str:
    """Обрезает длинное сообщение ошибки.

    Args:
        message: Исходное сообщение.
        limit: Максимальная длина.

    Returns:
        Обрезанное сообщение.
    """

    return message if len(message) <= limit else f"{message[:limit]}..."


def _parse_sheet_metadata(raw_sheet: object) -> SheetMetadata:
    """Парсит метаданные одного листа Google Sheets.

    Args:
        raw_sheet: Объект листа из ответа API.

    Returns:
        Метаданные листа.

    Raises:
        GoogleSheetsReadError: Если структура ответа некорректна.
    """

    if not isinstance(raw_sheet, dict):
        raise GoogleSheetsReadError("Элемент sheets должен быть объектом.")

    properties = raw_sheet.get("properties")
    if not isinstance(properties, dict):
        raise GoogleSheetsReadError("Sheet metadata не содержит properties.")

    sheet_id = properties.get("sheetId")
    title = properties.get("title")
    if not isinstance(sheet_id, int) or isinstance(sheet_id, bool):
        raise GoogleSheetsReadError("Sheet metadata содержит некорректный sheetId.")
    if not isinstance(title, str):
        raise GoogleSheetsReadError("Sheet metadata содержит некорректный title.")

    return SheetMetadata(
        sheet_id=sheet_id,
        title=title,
    )


def _normalize_read_values(raw_values: list[Any]) -> list[list[CellValue]]:
    """Нормализует матрицу значений, прочитанную из Google Sheets.

    Args:
        raw_values: Значение поля `values` из ответа API.

    Returns:
        Матрица примитивных значений.

    Raises:
        GoogleSheetsReadError: Если матрица содержит неподдерживаемые значения.
    """

    values: list[list[CellValue]] = []
    for row_index, raw_row in enumerate(raw_values, start=1):
        if not isinstance(raw_row, list):
            raise GoogleSheetsReadError(f"Строка #{row_index} должна быть списком.")

        row: list[CellValue] = []
        for column_index, raw_cell in enumerate(raw_row, start=1):
            row.append(_normalize_cell_value(raw_cell, row_index, column_index))
        values.append(row)

    return values


def _normalize_write_values(values: Sequence[Sequence[CellValue]]) -> list[list[CellValue]]:
    """Нормализует матрицу значений перед записью.

    Args:
        values: Матрица значений.

    Returns:
        JSON-совместимая матрица значений.

    Raises:
        GoogleSheetsWriteError: Если матрица содержит `None` или неподдерживаемый тип.
    """

    normalized: list[list[CellValue]] = []
    for row_index, row in enumerate(values, start=1):
        if isinstance(row, (str, bytes)) or not isinstance(row, Sequence):
            raise GoogleSheetsWriteError(f"Строка #{row_index} должна быть списком.")

        normalized_row: list[CellValue] = []
        for column_index, cell in enumerate(row, start=1):
            normalized_row.append(_normalize_cell_value(cell, row_index, column_index))
        normalized.append(normalized_row)

    return normalized


def _normalize_cell_value(value: object, row_index: int, column_index: int) -> CellValue:
    """Проверяет одно значение ячейки.

    Args:
        value: Значение ячейки.
        row_index: Номер строки в матрице.
        column_index: Номер колонки в матрице.

    Returns:
        JSON-совместимое значение ячейки.

    Raises:
        GoogleSheetsError: Если значение нельзя безопасно передать в Sheets API.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value

    raise GoogleSheetsError(
        f"Ячейка R{row_index}C{column_index} содержит неподдерживаемое значение.",
    )


def _build_empty_values_for_range(range_a1: str) -> list[list[str]]:
    """Создаёт матрицу пустых строк для очистки A1-диапазона.

    Args:
        range_a1: Закрытый A1-диапазон вида `A1:R1000`.

    Returns:
        Матрица `""` размером с диапазон.

    Raises:
        GoogleSheetsWriteError: Если диапазон некорректен.
    """

    rows_count, columns_count = _get_range_size(range_a1)
    return [["" for _ in range(columns_count)] for _ in range(rows_count)]


def _get_range_size(range_a1: str) -> tuple[int, int]:
    """Вычисляет размер закрытого A1-диапазона.

    Args:
        range_a1: Закрытый A1-диапазон вида `A1:R1000`.

    Returns:
        Кортеж `(количество строк, количество колонок)`.

    Raises:
        GoogleSheetsWriteError: Если диапазон некорректен.
    """

    match = A1_RANGE_RE.fullmatch(range_a1.strip())
    if match is None:
        raise GoogleSheetsWriteError(
            f"Managed range должен быть закрытым A1-диапазоном: {range_a1}.",
        )

    start_column, start_row_raw, end_column, end_row_raw = match.groups()
    start_column_number = _column_to_number(start_column)
    end_column_number = _column_to_number(end_column)
    start_row = int(start_row_raw)
    end_row = int(end_row_raw)

    if end_column_number < start_column_number or end_row < start_row:
        raise GoogleSheetsWriteError(f"Managed range задан в обратном порядке: {range_a1}.")

    return end_row - start_row + 1, end_column_number - start_column_number + 1


def _column_to_number(column: str) -> int:
    """Преобразует буквенное имя колонки в номер.

    Args:
        column: Имя колонки, например `A`, `R`, `AA`.

    Returns:
        Номер колонки, начиная с 1.
    """

    number = 0
    for char in column.upper():
        number = number * 26 + ord(char) - ord("A") + 1
    return number


class _HttpxGoogleAuthResponse:
    """Ответ google-auth transport, построенный из httpx.Response.

    Args:
        response: Ответ синхронного httpx-клиента.
    """

    def __init__(self, response: httpx.Response) -> None:
        self.status = response.status_code
        self.headers = response.headers
        self.data = response.content


class _HttpxGoogleAuthRequest:
    """Синхронный google-auth transport на базе httpx."""

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        **_: object,
    ) -> _HttpxGoogleAuthResponse:
        """Выполняет HTTP-запрос для google-auth.

        Args:
            url: URL запроса.
            method: HTTP-метод.
            body: Тело запроса.
            headers: HTTP-заголовки.
            timeout: Таймаут запроса.

        Returns:
            Ответ в формате, ожидаемом google-auth.

        Raises:
            google_auth_exceptions.TransportError: Если транспорт недоступен.
        """

        request_timeout = timeout if timeout is not None else GOOGLE_AUTH_TIMEOUT_SECONDS

        try:
            with httpx.Client(timeout=request_timeout) as client:
                response = client.request(
                    method=method,
                    url=url,
                    content=body,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise google_auth_exceptions.TransportError(
                "Google auth transport network error.",
            ) from exc

        return _HttpxGoogleAuthResponse(response)