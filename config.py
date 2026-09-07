"""Strict configuration loaded from ``.env`` and the environment."""

from __future__ import annotations

import os
import re

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when configuration is missing or malformed."""


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(
        f"{name} должен быть одним из: true/false, yes/no, on/off, 1/0"
    )


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} должен быть целым числом") from exc


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MANAGED_CHAT_ID = _int("MANAGED_CHAT_ID", 0)
SECOND_FACTOR_CHAT_ID = _int("SECOND_FACTOR_CHAT_ID", 0)
LOG_CHAT_ID = _int("LOG_CHAT_ID", 0)

GOOGLE_CREDENTIALS_FILE = os.getenv(
    "GOOGLE_CREDENTIALS_FILE", "credentials.json"
).strip()
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "").strip()
FORM_WORKSHEET_NAME = os.getenv("FORM_WORKSHEET_NAME", "Форма").strip()
FORM_GROUP_COLUMN = os.getenv("FORM_GROUP_COLUMN", "группа").strip()
FORM_USERNAME_COLUMN = os.getenv("FORM_USERNAME_COLUMN", "username").strip()
BLACKLIST_WORKSHEET_NAME = os.getenv(
    "BLACKLIST_WORKSHEET_NAME", "Черный список"
).strip()
BLACKLIST_USERNAME_COLUMN = os.getenv(
    "BLACKLIST_USERNAME_COLUMN", "username"
).strip()
BLACKLIST_REASON_COLUMN = os.getenv("BLACKLIST_REASON_COLUMN", "причина").strip()
BLACKLIST_DATE_COLUMN = os.getenv("BLACKLIST_DATE_COLUMN", "дата").strip()
WHITELIST_WORKSHEET_NAME = os.getenv(
    "WHITELIST_WORKSHEET_NAME", "Белый список"
).strip()
WHITELIST_USERNAME_COLUMN = os.getenv(
    "WHITELIST_USERNAME_COLUMN", "username"
).strip()

# Exact default: uppercase ASCII M31 followed by two ASCII digits.
GROUP_PATTERN = os.getenv("GROUP_PATTERN", r"^M31[0-9]{2}$")

KICK_ON_JOIN = _bool("KICK_ON_JOIN", True)
KICK_USERS_WITHOUT_USERNAME = _bool("KICK_USERS_WITHOUT_USERNAME", True)
RECHECK_INTERVAL_MINUTES = _int("RECHECK_INTERVAL_MINUTES", 60)
SHEET_CACHE_MINUTES = _int("SHEET_CACHE_MINUTES", 5)
REQUIRE_SECOND_FACTOR_GROUP = _bool("REQUIRE_SECOND_FACTOR_GROUP", False)

TELEGRAM_API_ID = _int("TELEGRAM_API_ID", 0)
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
TELETHON_SESSION_NAME = os.getenv("TELETHON_SESSION_NAME", "audit_session").strip()
AUDIT_GROUP_ID = _int("AUDIT_GROUP_ID", 0)
AUDIT_DRY_RUN = _bool("AUDIT_DRY_RUN", True)

DB_PATH = os.getenv("DB_PATH", "members.db").strip()


def _require_nonempty(names: list[tuple[str, str]]) -> None:
    missing = [name for name, value in names if not value]
    if missing:
        raise ConfigError(
            "Не заданы обязательные переменные окружения: " + ", ".join(missing)
        )


def _validate_chat_id(name: str, value: int) -> None:
    if value >= 0:
        raise ConfigError(f"{name} должен быть отрицательным ID Telegram-группы")


def _validate_sheet_settings() -> None:
    _require_nonempty(
        [
            ("GOOGLE_CREDENTIALS_FILE", GOOGLE_CREDENTIALS_FILE),
            ("GOOGLE_SHEET_ID", GOOGLE_SHEET_ID),
            ("FORM_WORKSHEET_NAME", FORM_WORKSHEET_NAME),
            ("FORM_GROUP_COLUMN", FORM_GROUP_COLUMN),
            ("FORM_USERNAME_COLUMN", FORM_USERNAME_COLUMN),
            ("BLACKLIST_WORKSHEET_NAME", BLACKLIST_WORKSHEET_NAME),
            ("BLACKLIST_USERNAME_COLUMN", BLACKLIST_USERNAME_COLUMN),
            ("BLACKLIST_REASON_COLUMN", BLACKLIST_REASON_COLUMN),
            ("BLACKLIST_DATE_COLUMN", BLACKLIST_DATE_COLUMN),
            ("WHITELIST_WORKSHEET_NAME", WHITELIST_WORKSHEET_NAME),
            ("WHITELIST_USERNAME_COLUMN", WHITELIST_USERNAME_COLUMN),
        ]
    )
    if not GROUP_PATTERN.strip():
        raise ConfigError("GROUP_PATTERN не должен быть пустым")
    if SHEET_CACHE_MINUTES <= 0:
        raise ConfigError("SHEET_CACHE_MINUTES должен быть положительным")
    worksheet_names = [
        FORM_WORKSHEET_NAME.casefold(),
        BLACKLIST_WORKSHEET_NAME.casefold(),
        WHITELIST_WORKSHEET_NAME.casefold(),
    ]
    if len(worksheet_names) != len(set(worksheet_names)):
        raise ConfigError("Названия трёх листов должны различаться")
    form_columns = {FORM_GROUP_COLUMN.casefold(), FORM_USERNAME_COLUMN.casefold()}
    if len(form_columns) != 2:
        raise ConfigError("Колонки группы и username на листе формы должны различаться")
    blacklist_columns = {
        BLACKLIST_USERNAME_COLUMN.casefold(),
        BLACKLIST_REASON_COLUMN.casefold(),
        BLACKLIST_DATE_COLUMN.casefold(),
    }
    if len(blacklist_columns) != 3:
        raise ConfigError("Колонки черного списка должны различаться")
    try:
        re.compile(GROUP_PATTERN)
    except re.error as exc:
        raise ConfigError(f"Некорректный GROUP_PATTERN: {exc}") from exc


def validate_core() -> None:
    """Validate everything required by the Bot API process."""
    _validate_sheet_settings()
    _require_nonempty([("BOT_TOKEN", BOT_TOKEN), ("DB_PATH", DB_PATH)])
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", BOT_TOKEN):
        raise ConfigError("BOT_TOKEN имеет некорректный формат")
    _validate_chat_id("MANAGED_CHAT_ID", MANAGED_CHAT_ID)
    _validate_chat_id("LOG_CHAT_ID", LOG_CHAT_ID)
    active_chat_ids = [MANAGED_CHAT_ID, LOG_CHAT_ID]
    if REQUIRE_SECOND_FACTOR_GROUP or SECOND_FACTOR_CHAT_ID:
        _validate_chat_id("SECOND_FACTOR_CHAT_ID", SECOND_FACTOR_CHAT_ID)
        active_chat_ids.append(SECOND_FACTOR_CHAT_ID)
    if len(active_chat_ids) != len(set(active_chat_ids)):
        raise ConfigError(
            "MANAGED_CHAT_ID, SECOND_FACTOR_CHAT_ID и LOG_CHAT_ID должны различаться"
        )
    if RECHECK_INTERVAL_MINUTES <= 0:
        raise ConfigError("RECHECK_INTERVAL_MINUTES должен быть положительным")


def validate_audit() -> None:
    """Validate the Telethon audit and ensure it targets the managed group."""
    _validate_sheet_settings()
    _require_nonempty(
        [
            ("TELEGRAM_API_HASH", TELEGRAM_API_HASH),
            ("TELETHON_SESSION_NAME", TELETHON_SESSION_NAME),
        ]
    )
    _validate_chat_id("MANAGED_CHAT_ID", MANAGED_CHAT_ID)
    _validate_chat_id("AUDIT_GROUP_ID", AUDIT_GROUP_ID)
    _validate_chat_id("LOG_CHAT_ID", LOG_CHAT_ID)
    if AUDIT_GROUP_ID != MANAGED_CHAT_ID:
        raise ConfigError("AUDIT_GROUP_ID должен совпадать с MANAGED_CHAT_ID")
    if LOG_CHAT_ID == MANAGED_CHAT_ID:
        raise ConfigError("LOG_CHAT_ID должен отличаться от MANAGED_CHAT_ID")
    if TELEGRAM_API_ID <= 0:
        raise ConfigError("TELEGRAM_API_ID должен быть положительным")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", TELEGRAM_API_HASH):
        raise ConfigError("TELEGRAM_API_HASH должен содержать 32 hex-символа")
    if REQUIRE_SECOND_FACTOR_GROUP:
        _validate_chat_id("SECOND_FACTOR_CHAT_ID", SECOND_FACTOR_CHAT_ID)
        if SECOND_FACTOR_CHAT_ID in {MANAGED_CHAT_ID, LOG_CHAT_ID}:
            raise ConfigError("SECOND_FACTOR_CHAT_ID должен отличаться от других групп")
