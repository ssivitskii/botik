"""Consistent, fail-closed access to the three Google Sheets tabs."""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
from dataclasses import dataclass
from threading import RLock
from typing import Literal

import gspread
from google.oauth2.service_account import Credentials

import config

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_client = None
_lock = RLock()


@dataclass(frozen=True)
class SheetSnapshot:
    form: frozenset[str]
    blacklist: frozenset[str]
    whitelist: frozenset[str]


@dataclass(frozen=True)
class CleanupResult:
    checked: int
    moved_to_blacklist: int
    kept_whitelisted: int
    deleted_rows: int
    snapshot: SheetSnapshot

    def summary(self) -> dict[str, int]:
        return {
            "checked": self.checked,
            "moved_to_blacklist": self.moved_to_blacklist,
            "kept_whitelisted": self.kept_whitelisted,
            "deleted_rows": self.deleted_rows,
        }


@dataclass(frozen=True)
class AccessDecision:
    state: Literal["allow", "deny", "unknown"]
    reason: str = ""


@dataclass(frozen=True)
class _Tab:
    worksheet: object
    values: list[list[str]]
    columns: dict[str, int]


_cached_snapshot: SheetSnapshot | None = None
_cache_time = 0.0


def normalize(username: str | None) -> str:
    """Normalize a Telegram username for case-insensitive comparison."""
    if not username:
        return ""
    return username.strip().lstrip("@").lower()


def _get_client():
    global _client
    if _client is None:
        credentials = Credentials.from_service_account_file(
            config.GOOGLE_CREDENTIALS_FILE, scopes=_SCOPES
        )
        _client = gspread.authorize(credentials)
    return _client


def _open_spreadsheet():
    return _get_client().open_by_key(config.GOOGLE_SHEET_ID)


def _header_indexes(
    worksheet_name: str, values: list[list[str]], required: tuple[str, ...]
) -> dict[str, int]:
    if not values:
        raise RuntimeError(f"Лист '{worksheet_name}' пуст: отсутствует строка заголовков")
    normalized_headers = [cell.strip().casefold() for cell in values[0]]
    indexes: dict[str, int] = {}
    for column in required:
        target = column.strip().casefold()
        matches = [i for i, header in enumerate(normalized_headers) if header == target]
        if not matches:
            raise RuntimeError(
                f"На листе '{worksheet_name}' отсутствует колонка '{column}'"
            )
        if len(matches) > 1:
            raise RuntimeError(
                f"На листе '{worksheet_name}' колонка '{column}' встречается несколько раз"
            )
        indexes[column] = matches[0]
    return indexes


def _read_tab(spreadsheet, name: str, required: tuple[str, ...]) -> _Tab:
    worksheet = spreadsheet.worksheet(name)
    values = worksheet.get_all_values()
    return _Tab(worksheet, values, _header_indexes(name, values, required))


def _cell(row: list[str], index: int) -> str:
    return row[index] if index < len(row) else ""


def _usernames(tab: _Tab, column: str) -> set[str]:
    index = tab.columns[column]
    return {
        username
        for row in tab.values[1:]
        if (username := normalize(_cell(row, index)))
    }


def _read_all_tabs(spreadsheet) -> tuple[_Tab, _Tab, _Tab]:
    """Read and validate every mandatory header before any mutation."""
    form = _read_tab(
        spreadsheet,
        config.FORM_WORKSHEET_NAME,
        (config.FORM_GROUP_COLUMN, config.FORM_USERNAME_COLUMN),
    )
    blacklist = _read_tab(
        spreadsheet,
        config.BLACKLIST_WORKSHEET_NAME,
        (
            config.BLACKLIST_USERNAME_COLUMN,
            config.BLACKLIST_REASON_COLUMN,
            config.BLACKLIST_DATE_COLUMN,
        ),
    )
    whitelist = _read_tab(
        spreadsheet,
        config.WHITELIST_WORKSHEET_NAME,
        (config.WHITELIST_USERNAME_COLUMN,),
    )
    return form, blacklist, whitelist


def _snapshot_from_tabs(form: _Tab, blacklist: _Tab, whitelist: _Tab) -> SheetSnapshot:
    return SheetSnapshot(
        frozenset(_usernames(form, config.FORM_USERNAME_COLUMN)),
        frozenset(_usernames(blacklist, config.BLACKLIST_USERNAME_COLUMN)),
        frozenset(_usernames(whitelist, config.WHITELIST_USERNAME_COLUMN)),
    )


def _cache(snapshot: SheetSnapshot) -> None:
    global _cached_snapshot, _cache_time
    _cached_snapshot = snapshot
    _cache_time = time.monotonic()


def get_snapshot(force_refresh: bool = False) -> SheetSnapshot:
    """Return one consistent snapshot; refresh errors are always propagated."""
    with _lock:
        age_minutes = (time.monotonic() - _cache_time) / 60
        if (
            not force_refresh
            and _cached_snapshot is not None
            and age_minutes < config.SHEET_CACHE_MINUTES
        ):
            return _cached_snapshot
        spreadsheet = _open_spreadsheet()
        snapshot = _snapshot_from_tabs(*_read_all_tabs(spreadsheet))
        _cache(snapshot)
        return snapshot


def _worksheet_id(worksheet) -> int:
    value = getattr(worksheet, "id", None)
    if value is None:
        value = worksheet._properties["sheetId"]
    return int(value)


def _append_row_request(tab: _Tab, username: str, reason: str, date: str) -> dict:
    values = [
        {"userEnteredValue": {"stringValue": ""}}
        for _ in range(len(tab.values[0]))
    ]
    for column, value in (
        (config.BLACKLIST_USERNAME_COLUMN, username),
        (config.BLACKLIST_REASON_COLUMN, reason),
        (config.BLACKLIST_DATE_COLUMN, date),
    ):
        values[tab.columns[column]] = {"userEnteredValue": {"stringValue": value}}
    return {
        "appendCells": {
            "sheetId": _worksheet_id(tab.worksheet),
            "rows": [{"values": values}],
            "fields": "userEnteredValue",
        }
    }


def _delete_row_request(tab: _Tab, row_number: int) -> dict:
    return {
        "deleteDimension": {
            "range": {
                "sheetId": _worksheet_id(tab.worksheet),
                "dimension": "ROWS",
                "startIndex": row_number - 1,
                "endIndex": row_number,
            }
        }
    }


def clean_and_get_snapshot(dry_run: bool = False) -> CleanupResult:
    """Project or atomically apply cleanup, returning the cleaned snapshot."""
    with _lock:
        spreadsheet = _open_spreadsheet()
        form, blacklist, whitelist = _read_all_tabs(spreadsheet)
        whitelist_names = _usernames(whitelist, config.WHITELIST_USERNAME_COLUMN)
        existing_blacklist = _usernames(blacklist, config.BLACKLIST_USERNAME_COLUMN)
        projected_blacklist = set(existing_blacklist)
        projected_form: set[str] = set()
        pattern = re.compile(config.GROUP_PATTERN)
        group_index = form.columns[config.FORM_GROUP_COLUMN]
        username_index = form.columns[config.FORM_USERNAME_COLUMN]
        rows_to_delete: list[int] = []
        additions: list[tuple[str, str]] = []
        kept_whitelisted = 0

        for row_number, row in enumerate(form.values[1:], start=2):
            group = _cell(row, group_index).strip()
            username = normalize(_cell(row, username_index))
            if pattern.fullmatch(group):
                if username:
                    projected_form.add(username)
                continue
            if username and username in whitelist_names:
                kept_whitelisted += 1
                projected_form.add(username)
                continue
            rows_to_delete.append(row_number)
            if username and username not in projected_blacklist:
                projected_blacklist.add(username)
                additions.append((username, f"Некорректная группа: '{group}'"))

        snapshot = SheetSnapshot(
            frozenset(projected_form),
            frozenset(projected_blacklist),
            frozenset(whitelist_names),
        )
        if not dry_run:
            today = dt.date.today().isoformat()
            requests = [
                _append_row_request(blacklist, username, reason, today)
                for username, reason in additions
            ]
            requests.extend(
                _delete_row_request(form, row_number)
                for row_number in sorted(rows_to_delete, reverse=True)
            )
            if requests:
                latest = _read_all_tabs(spreadsheet)
                if any(
                    current.values != original.values
                    for current, original in zip(latest, (form, blacklist, whitelist))
                ):
                    raise RuntimeError(
                        "Таблица изменилась во время проверки; очистка отменена"
                    )
                spreadsheet.batch_update({"requests": requests})
            _cache(snapshot)

        result = CleanupResult(
            checked=max(len(form.values) - 1, 0),
            moved_to_blacklist=len(additions),
            kept_whitelisted=kept_whitelisted,
            deleted_rows=len(rows_to_delete),
            snapshot=snapshot,
        )
        logger.info(
            "Валидация формы: проверено=%d, добавлено в черный список=%d, "
            "удалено строк=%d, оставлено по белому списку=%d%s",
            result.checked,
            result.moved_to_blacklist,
            result.deleted_rows,
            result.kept_whitelisted,
            " (dry-run)" if dry_run else "",
        )
        return result


def run_group_validation(dry_run: bool = False) -> dict[str, int]:
    return clean_and_get_snapshot(dry_run=dry_run).summary()


def decide_access(
    snapshot: SheetSnapshot,
    username: str | None,
    second_factor_member: bool | None = True,
) -> AccessDecision:
    """Apply the shared username and optional second-group policy."""
    normalized = normalize(username)
    if not normalized:
        if config.KICK_USERS_WITHOUT_USERNAME:
            return AccessDecision("deny", "нет username")
    else:
        if normalized in snapshot.blacklist:
            return AccessDecision("deny", "в черном списке")
        if normalized not in snapshot.form:
            return AccessDecision("deny", "не найден в очищенной форме")
    if config.REQUIRE_SECOND_FACTOR_GROUP and normalized not in snapshot.whitelist:
        if second_factor_member is None:
            return AccessDecision("unknown", "не удалось проверить вторую группу")
        if not second_factor_member:
            return AccessDecision("deny", "не состоит во второй группе")
    return AccessDecision("allow")


def get_allowed_usernames(force_refresh: bool = False) -> set[str]:
    return set(get_snapshot(force_refresh).form)


def get_blacklist_usernames(force_refresh: bool = False) -> set[str]:
    return set(get_snapshot(force_refresh).blacklist)


def get_whitelist_usernames(force_refresh: bool = False) -> set[str]:
    return set(get_snapshot(force_refresh).whitelist)


def is_allowed(username: str | None) -> bool:
    snapshot = clean_and_get_snapshot().snapshot
    return decide_access(snapshot, username).state == "allow"


def is_whitelisted(username: str | None) -> bool:
    return normalize(username) in get_snapshot().whitelist


def _reset_state_for_tests() -> None:
    global _client, _cached_snapshot, _cache_time
    with _lock:
        _client = None
        _cached_snapshot = None
        _cache_time = 0.0
