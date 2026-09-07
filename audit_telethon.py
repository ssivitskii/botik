"""Full managed-group audit through a Telegram user account (Telethon)."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Literal

from telethon import TelegramClient
from telethon.errors import UserNotParticipantError
from telethon.tl.types import Chat, ChannelParticipantBanned, ChannelParticipantLeft

import config
import google_sheets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("audit")


def _permissions_show_membership(permissions) -> bool | None:
    if permissions is None:
        return None
    participant = getattr(permissions, "participant", None)
    if isinstance(participant, ChannelParticipantLeft):
        return False
    if isinstance(participant, ChannelParticipantBanned):
        rights = getattr(participant, "banned_rights", None)
        if getattr(participant, "left", False) or getattr(rights, "view_messages", False):
            return False
    return True


async def is_in_group(client: TelegramClient, group, user_id: int) -> bool | None:
    """Return explicit membership, explicit absence, or unknown on API failure."""
    try:
        permissions = await client.get_permissions(group, user_id)
    except UserNotParticipantError:
        return False
    except Exception:
        logger.exception("Не удалось проверить группу для user_id=%s", user_id)
        return None
    return _permissions_show_membership(permissions)


async def is_admin(client: TelegramClient, group, user_id: int) -> bool | None:
    try:
        permissions = await client.get_permissions(group, user_id)
    except UserNotParticipantError:
        return False
    except Exception:
        logger.exception("Не удалось проверить права user_id=%s", user_id)
        return None
    if permissions is None:
        return None
    return bool(permissions.is_admin)


async def kick_participant(
    client: TelegramClient, group, user_id: int
) -> Literal["removed", "temporary", "failed"]:
    """Remove from basic chats atomically or use a bounded supergroup ban."""
    if isinstance(group, Chat):
        try:
            await client.kick_participant(group, user_id)
            return "removed"
        except Exception:
            logger.exception("Не удалось удалить user_id=%s из обычной группы", user_id)
            return "failed"

    # An expiring ban prevents a permanent lockout even if every unban retry fails.
    try:
        await client.edit_permissions(
            group, user_id, timedelta(minutes=1), view_messages=False
        )
    except Exception:
        logger.exception("Не удалось удалить user_id=%s из супергруппы", user_id)
        return "failed"
    for attempt in range(1, 4):
        try:
            await client.edit_permissions(group, user_id)
            return "removed"
        except Exception:
            logger.exception(
                "Не удалось снять временный бан user_id=%s, попытка %d/3",
                user_id,
                attempt,
            )
    logger.error(
        "Временный бан user_id=%s не снят; он автоматически истечёт примерно через минуту",
        user_id,
    )
    return "temporary"


async def _real_audit_log(client: TelegramClient, text: str) -> None:
    try:
        await client.send_message(config.LOG_CHAT_ID, text)
    except Exception:
        logger.exception("Не удалось отправить журнал аудита в LOG_CHAT_ID")


async def run_audit(client: TelegramClient) -> dict[str, int]:
    """Run one audit using an already connected client (convenient for tests)."""
    config.validate_audit()
    cleanup = await asyncio.to_thread(
        google_sheets.clean_and_get_snapshot, config.AUDIT_DRY_RUN
    )
    snapshot = cleanup.snapshot
    managed_group = await client.get_entity(config.AUDIT_GROUP_ID)
    second_group = None
    if config.REQUIRE_SECOND_FACTOR_GROUP:
        second_group = await client.get_entity(config.SECOND_FACTOR_CHAT_ID)
    me = await client.get_me()
    participants = await client.get_participants(managed_group)
    logger.info("В группе найдено %d участников", len(participants))

    checked = kicked = deferred = protected = 0
    planned: list[tuple[object, str]] = []
    for user in participants:
        if user.id == me.id or user.bot:
            protected += 1
            continue
        admin = await is_admin(client, managed_group, user.id)
        if admin is None:
            deferred += 1
            continue
        if admin:
            protected += 1
            continue

        checked += 1
        preliminary = google_sheets.decide_access(snapshot, user.username, True)
        normalized = google_sheets.normalize(user.username)
        if (
            preliminary.state == "allow"
            and config.REQUIRE_SECOND_FACTOR_GROUP
            and normalized not in snapshot.whitelist
        ):
            second_membership = await is_in_group(client, second_group, user.id)
            decision = google_sheets.decide_access(
                snapshot, user.username, second_membership
            )
        else:
            decision = preliminary

        if decision.state == "unknown":
            deferred += 1
        elif decision.state == "deny":
            planned.append((user, decision.reason))

    logger.info("Под удаление подпадает %d человек", len(planned))
    for user, reason in planned:
        label = f"@{user.username}" if user.username else f"id={user.id} (без username)"
        if config.AUDIT_DRY_RUN:
            logger.info("[DRY RUN] Был бы удалён: %s — %s", label, reason)
            continue
        try:
            refreshed = await client.get_entity(user.id)
        except Exception:
            logger.exception("Не удалось обновить user_id=%s перед удалением", user.id)
            deferred += 1
            continue
        present = await is_in_group(client, managed_group, user.id)
        if present is None:
            deferred += 1
            continue
        if not present:
            continue
        refreshed_admin = await is_admin(client, managed_group, user.id)
        if refreshed_admin is None:
            deferred += 1
            continue
        if refreshed_admin:
            protected += 1
            continue
        refreshed_preliminary = google_sheets.decide_access(
            snapshot, refreshed.username, True
        )
        refreshed_normalized = google_sheets.normalize(refreshed.username)
        if (
            refreshed_preliminary.state == "allow"
            and config.REQUIRE_SECOND_FACTOR_GROUP
            and refreshed_normalized not in snapshot.whitelist
        ):
            refreshed_second = await is_in_group(client, second_group, user.id)
            refreshed_decision = google_sheets.decide_access(
                snapshot, refreshed.username, refreshed_second
            )
        else:
            refreshed_decision = refreshed_preliminary
        if refreshed_decision.state == "unknown":
            deferred += 1
            continue
        if refreshed_decision.state == "allow":
            continue
        current_label = (
            f"@{refreshed.username}"
            if refreshed.username
            else f"id={refreshed.id} (без username)"
        )
        kick_result = await kick_participant(client, managed_group, user.id)
        if kick_result != "failed":
            kicked += 1
            logger.info("Удалён: %s — %s", current_label, refreshed_decision.reason)
            await _real_audit_log(
                client,
                f"🔎 Аудит удалил: {current_label} — {refreshed_decision.reason}.",
            )
            if kick_result == "temporary":
                await _real_audit_log(
                    client,
                    f"⚠️ Для {current_label} разблокировка не подтверждена; "
                    "временный бан истечёт примерно через минуту.",
                )
        else:
            deferred += 1

    if config.AUDIT_DRY_RUN:
        logger.info("DRY RUN завершён: таблица и Telegram не изменялись")
    else:
        await _real_audit_log(
            client,
            f"✅ Полный аудит: проверено {checked}, удалено {kicked}, "
            f"отложено {deferred}, защищено {protected}.",
        )
    return {
        "checked": checked,
        "planned": len(planned),
        "kicked": kicked,
        "deferred": deferred,
        "protected": protected,
    }


async def main() -> None:
    config.validate_audit()
    if not config.AUDIT_DRY_RUN:
        logger.warning(
            "Реальный аудит включён. Остановите bot.py на время очистки таблицы."
        )
    client = TelegramClient(
        config.TELETHON_SESSION_NAME,
        config.TELEGRAM_API_ID,
        config.TELEGRAM_API_HASH,
    )
    async with client:
        result = await run_audit(client)
    logger.info(
        "Аудит завершён: проверено=%d, кандидатов=%d, удалено=%d, "
        "отложено=%d, защищено=%d",
        result["checked"],
        result["planned"],
        result["kicked"],
        result["deferred"],
        result["protected"],
    )


if __name__ == "__main__":
    asyncio.run(main())
