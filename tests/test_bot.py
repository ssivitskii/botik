import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiogram.enums import ChatMemberStatus, ChatType

import bot
import config
import google_sheets


def user(user_id=10, username="person", is_bot=False):
    return types.SimpleNamespace(
        id=user_id,
        username=username,
        is_bot=is_bot,
        full_name=username or "No Username",
    )


def member(status=ChatMemberStatus.MEMBER, username="person", is_member=True, user_id=10):
    return types.SimpleNamespace(
        status=status,
        user=user(user_id, username),
        is_member=is_member,
    )


def cleanup(snapshot):
    return google_sheets.CleanupResult(0, 0, 0, 0, snapshot)


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_bot = bot.bot
        self.fake_bot = types.SimpleNamespace(
            get_chat_member=AsyncMock(),
            get_chat_administrators=AsyncMock(),
            get_chat=AsyncMock(return_value=types.SimpleNamespace(type="supergroup")),
            unban_chat_member=AsyncMock(),
            ban_chat_member=AsyncMock(),
            send_message=AsyncMock(),
        )
        bot.bot = self.fake_bot

    async def asyncTearDown(self):
        bot.bot = self.original_bot

    async def test_restricted_membership_uses_is_member(self):
        self.assertTrue(bot._is_present(member(ChatMemberStatus.RESTRICTED, is_member=True)))
        self.assertFalse(bot._is_present(member(ChatMemberStatus.RESTRICTED, is_member=False)))

    async def test_api_failure_during_recheck_defers_without_kick(self):
        snapshot = google_sheets.SheetSnapshot(frozenset(), frozenset(), frozenset())
        with (
            patch.object(bot.storage, "list_active_members", return_value=[(10, "old")]),
            patch.object(bot, "_current_managed_member", AsyncMock(return_value=None)),
            patch.object(bot, "kick_user", AsyncMock()) as kick,
        ):
            self.assertEqual(await bot._recheck_with_snapshot(snapshot), (0, 0, 1))
        kick.assert_not_awaited()

    async def test_recheck_uses_current_username(self):
        snapshot = google_sheets.SheetSnapshot(
            frozenset({"new"}), frozenset(), frozenset()
        )
        current = member(username="new")
        with (
            patch.multiple(config, REQUIRE_SECOND_FACTOR_GROUP=False),
            patch.object(bot.storage, "list_active_members", return_value=[(10, "old")]),
            patch.object(bot.storage, "upsert_member"),
            patch.object(bot, "_current_managed_member", AsyncMock(return_value=current)),
            patch.object(bot, "kick_user", AsyncMock()) as kick,
        ):
            result = await bot._recheck_with_snapshot(snapshot)
        self.assertEqual(result, (1, 0, 0))
        kick.assert_not_awaited()

    async def test_join_refreshes_username_before_decision(self):
        snapshot = google_sheets.SheetSnapshot(
            frozenset({"new"}), frozenset(), frozenset()
        )
        event = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-1001),
            new_chat_member=member(username="old"),
        )
        with (
            patch.multiple(
                config,
                MANAGED_CHAT_ID=-1001,
                KICK_ON_JOIN=True,
                REQUIRE_SECOND_FACTOR_GROUP=False,
            ),
            patch.object(bot.storage, "upsert_member"),
            patch.object(bot.storage, "remove_member"),
            patch.object(bot, "_load_clean_snapshot", AsyncMock(return_value=cleanup(snapshot))),
            patch.object(
                bot, "_current_managed_member", AsyncMock(return_value=member(username="new"))
            ),
            patch.object(bot, "kick_user", AsyncMock()) as kick,
        ):
            await bot.on_chat_member_update(event)
        kick.assert_not_awaited()

    async def test_kick_failure_is_logged_only_to_log_chat(self):
        current = member(username="bad")
        self.fake_bot.unban_chat_member.side_effect = RuntimeError("offline")
        with (
            patch.multiple(config, MANAGED_CHAT_ID=-1001, LOG_CHAT_ID=-1003),
            patch.object(bot, "_current_managed_member", AsyncMock(return_value=current)),
        ):
            result = await bot.kick_user(10, "bad", "bad")
        self.assertFalse(result)
        self.fake_bot.send_message.assert_awaited_once()
        self.assertEqual(self.fake_bot.send_message.await_args.args[0], -1003)

    async def test_denied_join_uses_group_specific_removal_and_logs_only_to_log_chat(self):
        snapshot = google_sheets.SheetSnapshot(frozenset(), frozenset(), frozenset())
        event = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-1001),
            new_chat_member=member(username="bad"),
        )
        for chat_type, method_name in (
            (ChatType.SUPERGROUP, "unban_chat_member"),
            (ChatType.GROUP, "ban_chat_member"),
        ):
            with self.subTest(chat_type=chat_type):
                self.fake_bot.get_chat.return_value = types.SimpleNamespace(type=chat_type)
                self.fake_bot.unban_chat_member.reset_mock()
                self.fake_bot.ban_chat_member.reset_mock()
                self.fake_bot.send_message.reset_mock()
                with (
                    patch.multiple(
                        config,
                        MANAGED_CHAT_ID=-1001,
                        LOG_CHAT_ID=-1003,
                        KICK_ON_JOIN=True,
                        REQUIRE_SECOND_FACTOR_GROUP=False,
                    ),
                    patch.object(bot.storage, "upsert_member"),
                    patch.object(bot.storage, "remove_member"),
                    patch.object(
                        bot,
                        "_load_clean_snapshot",
                        AsyncMock(return_value=cleanup(snapshot)),
                    ),
                    patch.object(
                        bot,
                        "_current_managed_member",
                        AsyncMock(return_value=member(username="bad")),
                    ),
                ):
                    await bot.on_chat_member_update(event)
                getattr(self.fake_bot, method_name).assert_awaited_once()
                self.fake_bot.send_message.assert_awaited_once()
                self.assertEqual(self.fake_bot.send_message.await_args.args[0], -1003)

    async def test_sender_chat_command_is_rejected_without_admin_lookup(self):
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-1003),
            sender_chat=types.SimpleNamespace(id=-1003),
            from_user=user(1, "admin"),
            reply=AsyncMock(),
        )
        with patch.object(config, "LOG_CHAT_ID", -1003):
            self.assertFalse(await bot._authorize_command(message))
        self.fake_bot.get_chat_administrators.assert_not_awaited()
        message.reply.assert_awaited_once()

    async def test_check_uses_known_numeric_id_and_is_read_only(self):
        snapshot = google_sheets.SheetSnapshot(
            frozenset({"new"}), frozenset(), frozenset()
        )
        self.fake_bot.get_chat_administrators.return_value = [member(user_id=1)]
        self.fake_bot.get_chat_member.side_effect = [
            member(username="new", user_id=10),
            member(username="new", user_id=10),
        ]
        message = types.SimpleNamespace(
            chat=types.SimpleNamespace(id=-1003),
            sender_chat=None,
            from_user=user(1, "admin"),
            text="/check @old",
            reply=AsyncMock(),
        )
        clean_mock = Mock(return_value=cleanup(snapshot))
        with (
            patch.multiple(
                config,
                LOG_CHAT_ID=-1003,
                MANAGED_CHAT_ID=-1001,
                SECOND_FACTOR_CHAT_ID=-1002,
                REQUIRE_SECOND_FACTOR_GROUP=True,
            ),
            patch.object(bot.google_sheets, "clean_and_get_snapshot", clean_mock),
            patch.object(bot.storage, "find_member_by_username", return_value=(10, "old")),
            patch.object(bot.storage, "upsert_member"),
        ):
            await bot.cmd_check(message)
        clean_mock.assert_called_once_with(True)
        calls = self.fake_bot.get_chat_member.await_args_list
        self.assertEqual(calls[0].args, (-1001, 10))
        self.assertEqual(calls[1].args, (-1002, 10))
        response = message.reply.await_args.args[0]
        self.assertIn("текущий username: @new", response)
        self.assertIn("таблица командой /check не изменяется", response)

    async def test_command_outside_log_chat_is_silent(self):
        message = types.SimpleNamespace(chat=types.SimpleNamespace(id=-999))
        with patch.object(config, "LOG_CHAT_ID", -1003):
            self.assertFalse(await bot._authorize_command(message))
        self.fake_bot.get_chat_administrators.assert_not_awaited()

    async def test_non_admin_and_admin_api_failure_never_start_sync(self):
        for administrators, side_effect in (([], None), (None, RuntimeError("offline"))):
            with self.subTest(side_effect=side_effect):
                self.fake_bot.get_chat_administrators.reset_mock()
                self.fake_bot.get_chat_administrators.return_value = administrators
                self.fake_bot.get_chat_administrators.side_effect = side_effect
                message = types.SimpleNamespace(
                    chat=types.SimpleNamespace(id=-1003),
                    sender_chat=None,
                    from_user=user(2, "ordinary"),
                    reply=AsyncMock(),
                )
                with (
                    patch.object(config, "LOG_CHAT_ID", -1003),
                    patch.object(bot, "full_sync", AsyncMock()) as sync,
                ):
                    await bot.cmd_sync(message)
                sync.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
