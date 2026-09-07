"""Telegram Bot API process for newly observed and locally tracked members."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, Message

import config
import google_sheets
import storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("sheet_guard_bot")

# Constructed only after config.validate_core(), keeping imports testable.
bot: Bot | None = None
dp = Dispatcher()
_sync_lock = asyncio.Lock()


def _active_bot() -> Bot:
    if bot is None:
        raise RuntimeError("Бот ещё не инициализирован")
    return bot


async def log(text: str) -> None:
    """Send operational messages only to the configured log group."""
    try:
        await _active_bot().send_message(config.LOG_CHAT_ID, text)
    except Exception:
        logger.exception("Не удалось отправить сообщение в LOG_CHAT_ID")


def _is_present(member) -> bool:
    if member.status in {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
    }:
        return True
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return False


def _is_protected(member) -> bool:
    return bool(member.user.is_bot) or member.status in {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
    }


async def is_in_second_factor_group(user_id: int) -> bool | None:
    """Return membership, or None when Telegram could not establish it."""
    if not config.REQUIRE_SECOND_FACTOR_GROUP:
        return True
    try:
        member = await _active_bot().get_chat_member(
            config.SECOND_FACTOR_CHAT_ID, user_id
        )
        return _is_present(member)
    except Exception:
        logger.exception(
            "Не удалось проверить вторую группу для user_id=%s", user_id
        )
        return None


async def check_full_access(
    user_id: int,
    username: str | None,
    snapshot: google_sheets.SheetSnapshot | None = None,
) -> google_sheets.AccessDecision:
    """Evaluate one user, preserving unknown external state."""
    if snapshot is None:
        try:
            cleanup = await asyncio.to_thread(google_sheets.clean_and_get_snapshot)
            snapshot = cleanup.snapshot
        except Exception:
            logger.exception("Ошибка проверки таблицы для user_id=%s", user_id)
            return google_sheets.AccessDecision("unknown", "ошибка проверки таблицы")

    preliminary = google_sheets.decide_access(snapshot, username, True)
    normalized = google_sheets.normalize(username)
    needs_second_factor = (
        preliminary.state == "allow"
        and config.REQUIRE_SECOND_FACTOR_GROUP
        and normalized not in snapshot.whitelist
    )
    if not needs_second_factor:
        return preliminary
    membership = await is_in_second_factor_group(user_id)
    return google_sheets.decide_access(snapshot, username, membership)


async def kick_user(
    user_id: int, reason: str, expected_username: str | None = None
) -> bool:
    """Remove a current group member while allowing a later re-entry."""
    current = await _current_managed_member(user_id)
    if current is None:
        await log(f"⚠️ Удаление id={user_id} отложено: Telegram API недоступен.")
        return False
    if not _is_present(current) or _is_protected(current):
        return False
    if google_sheets.normalize(current.user.username) != google_sheets.normalize(
        expected_username
    ):
        logger.info("Username user_id=%s изменился перед удалением", user_id)
        return False
    try:
        managed_chat = await _active_bot().get_chat(config.MANAGED_CHAT_ID)
        if managed_chat.type == ChatType.SUPERGROUP:
            # Removes a current supergroup member and allows re-entry in one call.
            await _active_bot().unban_chat_member(
                config.MANAGED_CHAT_ID, user_id, only_if_banned=False
            )
        elif managed_chat.type == ChatType.GROUP:
            # Basic groups use their supported one-call kick operation.
            await _active_bot().ban_chat_member(config.MANAGED_CHAT_ID, user_id)
        else:
            raise RuntimeError("MANAGED_CHAT_ID не является группой")
    except Exception:
        logger.exception("Не удалось удалить user_id=%s", user_id)
        await log(f"⚠️ Не удалось удалить id={user_id}; операция будет повторена позже.")
        return False
    storage.remove_member(config.MANAGED_CHAT_ID, user_id)
    logger.info("Удалён user_id=%s (%s)", user_id, reason)
    return True


async def _load_clean_snapshot() -> google_sheets.CleanupResult | None:
    try:
        return await asyncio.to_thread(google_sheets.clean_and_get_snapshot)
    except Exception:
        logger.exception("Не удалось очистить/прочитать таблицу")
        await log("⚠️ Таблица недоступна или имеет неверные заголовки; удаление отложено.")
        return None


@dp.chat_member()
async def on_chat_member_update(event: ChatMemberUpdated) -> None:
    """Track and evaluate membership updates only in the managed group."""
    if event.chat.id != config.MANAGED_CHAT_ID:
        return
    member = event.new_chat_member
    user = member.user
    if user.is_bot:
        storage.remove_member(event.chat.id, user.id)
        return
    if not _is_present(member):
        storage.remove_member(event.chat.id, user.id)
        return

    storage.upsert_member(event.chat.id, user.id, user.username, member.status.value)
    if _is_protected(member) or not config.KICK_ON_JOIN:
        return

    async with _sync_lock:
        cleanup = await _load_clean_snapshot()
        if cleanup is None:
            return
        current = await _current_managed_member(user.id)
        if current is None:
            await log(
                f"⚠️ Проверка нового участника id={user.id} отложена: Telegram API недоступен."
            )
            return
        if not _is_present(current):
            storage.remove_member(event.chat.id, user.id)
            return
        storage.upsert_member(
            event.chat.id, user.id, current.user.username, current.status.value
        )
        if _is_protected(current):
            return
        decision = await check_full_access(
            user.id, current.user.username, cleanup.snapshot
        )
        if decision.state == "unknown":
            await log(
                f"⚠️ Проверка нового участника id={user.id} не завершена; удаление отложено."
            )
            return
        if decision.state == "deny" and await kick_user(
            user.id, decision.reason, current.user.username
        ):
            username = (
                f"@{current.user.username}"
                if current.user.username
                else "без username"
            )
            await log(
                f"👋 Удалён при вступлении: {user.full_name} ({username}) — "
                f"{decision.reason}."
            )


async def _current_managed_member(user_id: int):
    try:
        return await _active_bot().get_chat_member(config.MANAGED_CHAT_ID, user_id)
    except Exception:
        logger.exception("Не удалось обновить участника user_id=%s", user_id)
        return None


async def _recheck_with_snapshot(
    snapshot: google_sheets.SheetSnapshot,
) -> tuple[int, int, int]:
    checked = kicked = deferred = 0
    for user_id, _stored_username in storage.list_active_members(
        config.MANAGED_CHAT_ID
    ):
        current = await _current_managed_member(user_id)
        if current is None:
            deferred += 1
            continue
        if not _is_present(current):
            storage.remove_member(config.MANAGED_CHAT_ID, user_id)
            continue
        status = current.status.value
        storage.upsert_member(
            config.MANAGED_CHAT_ID, user_id, current.user.username, status
        )
        if _is_protected(current):
            continue
        checked += 1
        decision = await check_full_access(
            user_id, current.user.username, snapshot
        )
        if decision.state == "unknown":
            deferred += 1
            continue
        if decision.state == "deny":
            if await kick_user(user_id, decision.reason, current.user.username):
                kicked += 1
                username = (
                    f"@{current.user.username}"
                    if current.user.username
                    else "без username"
                )
                await log(
                    f"🔁 Удалён при пере-проверке: {username} — {decision.reason}."
                )
            else:
                deferred += 1
    return checked, kicked, deferred


async def recheck_managed_chat() -> tuple[int, int, int]:
    """Clean the form and recheck all currently known managed members."""
    async with _sync_lock:
        cleanup = await _load_clean_snapshot()
        if cleanup is None:
            return 0, 0, 0
        return await _recheck_with_snapshot(cleanup.snapshot)


async def full_sync() -> dict:
    """Serialize cleanup and the destructive membership cycle."""
    async with _sync_lock:
        cleanup = await _load_clean_snapshot()
        if cleanup is None:
            raise RuntimeError("таблица недоступна или настроена неверно")
        checked, kicked, deferred = await _recheck_with_snapshot(cleanup.snapshot)
        return {
            "validation": cleanup.summary(),
            "checked": checked,
            "kicked": kicked,
            "deferred": deferred,
        }


def _in_log_chat(message: Message) -> bool:
    return message.chat.id == config.LOG_CHAT_ID


async def _is_admin_in_log_chat(user_id: int | None) -> bool | None:
    if user_id is None:
        return False
    try:
        administrators = await _active_bot().get_chat_administrators(
            config.LOG_CHAT_ID
        )
    except Exception:
        logger.exception("Не удалось проверить права команды user_id=%s", user_id)
        return None
    return any(member.user.id == user_id for member in administrators)


async def _authorize_command(message: Message) -> bool:
    if not _in_log_chat(message):
        return False
    if message.sender_chat is not None:
        await message.reply("Анонимные команды и команды от имени канала не поддерживаются.")
        return False
    user_id = message.from_user.id if message.from_user else None
    admin = await _is_admin_in_log_chat(user_id)
    if admin is None:
        await message.reply("Не удалось проверить права администратора. Повторите позже.")
        return False
    if not admin:
        await message.reply("Команда доступна только администраторам.")
        return False
    return True


@dp.message(Command("sync"))
async def cmd_sync(message: Message) -> None:
    if not await _authorize_command(message):
        return
    status_message = await message.reply(
        "🔄 Проверяю форму и сверяю управляемую группу..."
    )
    try:
        result = await full_sync()
    except Exception:
        logger.exception("Ошибка full_sync")
        await status_message.edit_text(
            "❌ Синхронизация прервана. Часть операций могла завершиться; "
            "проверьте журнал и повторите запуск."
        )
        return
    validation = result["validation"]
    await status_message.edit_text(
        "✅ Готово.\n\n"
        f"📋 Форма: проверено {validation['checked']}, "
        f"добавлено в черный список {validation['moved_to_blacklist']}, "
        f"удалено строк {validation['deleted_rows']}, "
        f"оставлено по белому списку {validation['kept_whitelisted']}.\n"
        f"👥 Группа 1: проверено {result['checked']}, "
        f"удалено {result['kicked']}, отложено {result['deferred']}.\n\n"
        "Проверяются участники, уже известные локальной базе. Полную первичную "
        "сверку выполняет audit_telethon.py."
    )


def _sheet_status_lines(
    snapshot: google_sheets.SheetSnapshot, username: str | None
) -> list[str]:
    normalized = google_sheets.normalize(username)
    lines = [
        "⛔ в черном списке"
        if normalized in snapshot.blacklist
        else "✅ не в черном списке"
    ]
    lines.append(
        "✅ есть в очищенной форме"
        if normalized in snapshot.form
        else "❌ отсутствует в очищенной форме"
    )
    if normalized in snapshot.whitelist:
        lines.append("⭐ в белом списке — проверка второй группы не требуется")
    return lines


@dp.message(Command("check"))
async def cmd_check(message: Message) -> None:
    if not await _authorize_command(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not google_sheets.normalize(parts[1]):
        await message.reply("Использование: /check @username")
        return
    requested = parts[1].strip()
    try:
        async with _sync_lock:
            cleanup = await asyncio.to_thread(
                google_sheets.clean_and_get_snapshot, True
            )
    except Exception:
        logger.exception("Ошибка таблицы в /check")
        await message.reply("Таблица недоступна или имеет неверные заголовки.")
        return

    stored = storage.find_member_by_username(config.MANAGED_CHAT_ID, requested)
    current = None
    current_username: str | None = requested
    if stored:
        current = await _current_managed_member(stored[0])
        if current is not None and _is_present(current):
            current_username = current.user.username
            storage.upsert_member(
                config.MANAGED_CHAT_ID,
                stored[0],
                current_username,
                current.status.value,
            )
        elif current is not None:
            storage.remove_member(config.MANAGED_CHAT_ID, stored[0])

    title = requested
    if stored and current is not None and _is_present(current):
        refreshed = f"@{current_username}" if current_username else "без username"
        if google_sheets.normalize(requested) != google_sheets.normalize(current_username):
            title += f" (текущий username: {refreshed})"
    lines = _sheet_status_lines(cleanup.snapshot, current_username)
    normalized = google_sheets.normalize(current_username)
    if config.REQUIRE_SECOND_FACTOR_GROUP and normalized not in cleanup.snapshot.whitelist:
        if stored is None:
            lines.append("⚠️ вторая группа: неизвестно — нет сохранённого numeric user_id")
        elif current is None:
            lines.append("⚠️ вторая группа: неизвестно — не удалось обновить участника")
        elif not _is_present(current):
            lines.append("ℹ️ пользователь больше не состоит в управляемой группе")
        else:
            second = await is_in_second_factor_group(stored[0])
            if second is None:
                lines.append("⚠️ вторая группа: статус неизвестен")
            elif second:
                lines.append("✅ состоит во второй группе")
            else:
                lines.append("❌ не состоит во второй группе")
    lines.append("ℹ️ Показан прогноз после очистки; таблица командой /check не изменяется")
    await message.reply(f"{title}:\n" + "\n".join(lines))


async def background_sync_loop() -> None:
    while True:
        await asyncio.sleep(config.RECHECK_INTERVAL_MINUTES * 60)
        logger.info("Фоновая синхронизация: старт")
        try:
            result = await full_sync()
        except Exception:
            logger.exception("Ошибка фоновой синхронизации")
            await log(
                "⚠️ Фоновая синхронизация не завершена; удаления при ошибке отложены."
            )
            continue
        validation = result["validation"]
        await log(
            "🕒 Фоновая синхронизация.\n"
            f"Форма: проверено {validation['checked']}, "
            f"добавлено в черный список {validation['moved_to_blacklist']}, "
            f"удалено строк {validation['deleted_rows']}.\n"
            f"Группа 1: проверено {result['checked']}, удалено {result['kicked']}, "
            f"отложено {result['deferred']}."
        )


async def preflight() -> None:
    """Verify Bot API rights required for safe operation."""
    current_bot = _active_bot()
    me = await current_bot.get_me()
    managed_chat = await current_bot.get_chat(config.MANAGED_CHAT_ID)
    if managed_chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        raise RuntimeError(
            "MANAGED_CHAT_ID должен указывать на группу или супергруппу Telegram"
        )
    managed = await current_bot.get_chat_member(config.MANAGED_CHAT_ID, me.id)
    if managed.status not in {
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    } or not getattr(managed, "can_restrict_members", managed.status == ChatMemberStatus.CREATOR):
        raise RuntimeError(
            "Боту нужны права администратора с блокировкой пользователей в MANAGED_CHAT_ID"
        )
    if config.REQUIRE_SECOND_FACTOR_GROUP:
        second = await current_bot.get_chat_member(config.SECOND_FACTOR_CHAT_ID, me.id)
        if second.status not in {
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }:
            raise RuntimeError(
                "Для проверки других пользователей бот должен быть администратором SECOND_FACTOR_CHAT_ID"
            )
    log_member = await current_bot.get_chat_member(config.LOG_CHAT_ID, me.id)
    if not _is_present(log_member):
        raise RuntimeError("Бот не состоит в LOG_CHAT_ID")


async def main() -> None:
    global bot
    config.validate_core()
    bot = Bot(token=config.BOT_TOKEN)
    storage.init_db()
    background_task: asyncio.Task | None = None
    try:
        await preflight()
        initial = await full_sync()
        await log(
            "✅ Начальная синхронизация завершена: "
            f"проверено {initial['checked']}, удалено {initial['kicked']}, "
            f"отложено {initial['deferred']}."
        )
        background_task = asyncio.create_task(background_sync_loop())
        await dp.start_polling(
            bot,
            allowed_updates=["message", "chat_member"],
            close_bot_session=False,
        )
    finally:
        if background_task is not None:
            background_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await background_task
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
