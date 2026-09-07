import types
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

import audit_telethon
import config
import google_sheets


def person(user_id, username, bot=False):
    return types.SimpleNamespace(id=user_id, username=username, bot=bot)


def permissions(admin=False):
    return types.SimpleNamespace(is_admin=admin, participant=object())


def cleanup(snapshot):
    return google_sheets.CleanupResult(0, 0, 0, 0, snapshot)


class FakeClient:
    def __init__(self, participants):
        self.managed = types.SimpleNamespace(id=-1001)
        self.second = types.SimpleNamespace(id=-1002)
        self.participants = participants
        self.get_entity = AsyncMock(side_effect=self._get_entity)
        self.get_me = AsyncMock(return_value=person(999, "self"))
        self.get_participants = AsyncMock(return_value=participants)
        self.get_permissions = AsyncMock(return_value=permissions())
        self.edit_permissions = AsyncMock()
        self.kick_participant = AsyncMock()
        self.send_message = AsyncMock()

    async def _get_entity(self, entity_id):
        if entity_id == -1001:
            return self.managed
        if entity_id == -1002:
            return self.second
        return next(user for user in self.participants if user.id == entity_id)


class AuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_has_zero_telegram_or_sheet_writes(self):
        snapshot = google_sheets.SheetSnapshot(frozenset(), frozenset(), frozenset())
        client = FakeClient([person(10, "bad")])
        sheet_call = Mock(return_value=cleanup(snapshot))
        with (
            patch.multiple(
                config,
                AUDIT_DRY_RUN=True,
                AUDIT_GROUP_ID=-1001,
                REQUIRE_SECOND_FACTOR_GROUP=False,
            ),
            patch.object(config, "validate_audit"),
            patch.object(audit_telethon.google_sheets, "clean_and_get_snapshot", sheet_call),
        ):
            result = await audit_telethon.run_audit(client)
        sheet_call.assert_called_once_with(True)
        self.assertEqual(result["planned"], 1)
        client.edit_permissions.assert_not_awaited()
        client.kick_participant.assert_not_awaited()
        client.send_message.assert_not_awaited()

    async def test_permission_failure_defers_and_never_kicks(self):
        snapshot = google_sheets.SheetSnapshot(frozenset(), frozenset(), frozenset())
        client = FakeClient([person(10, "bad")])
        client.get_permissions.side_effect = RuntimeError("offline")
        with (
            patch.multiple(
                config,
                AUDIT_DRY_RUN=False,
                AUDIT_GROUP_ID=-1001,
                REQUIRE_SECOND_FACTOR_GROUP=False,
            ),
            patch.object(config, "validate_audit"),
            patch.object(
                audit_telethon.google_sheets,
                "clean_and_get_snapshot",
                return_value=cleanup(snapshot),
            ),
        ):
            result = await audit_telethon.run_audit(client)
        self.assertEqual(result["deferred"], 1)
        client.edit_permissions.assert_not_awaited()
        client.kick_participant.assert_not_awaited()

    async def test_real_audit_rechecks_current_username_before_removal(self):
        snapshot = google_sheets.SheetSnapshot(
            frozenset({"allowed"}), frozenset(), frozenset()
        )
        old = person(10, "old")
        client = FakeClient([old])
        refreshed = person(10, "allowed")
        client.get_entity.side_effect = [client.managed, refreshed]
        with (
            patch.multiple(
                config,
                AUDIT_DRY_RUN=False,
                AUDIT_GROUP_ID=-1001,
                REQUIRE_SECOND_FACTOR_GROUP=False,
            ),
            patch.object(config, "validate_audit"),
            patch.object(
                audit_telethon.google_sheets,
                "clean_and_get_snapshot",
                return_value=cleanup(snapshot),
            ),
        ):
            result = await audit_telethon.run_audit(client)
        self.assertEqual(result["planned"], 1)
        self.assertEqual(result["kicked"], 0)
        client.edit_permissions.assert_not_awaited()

    async def test_admin_is_protected(self):
        snapshot = google_sheets.SheetSnapshot(frozenset(), frozenset(), frozenset())
        client = FakeClient([person(10, "admin")])
        client.get_permissions.return_value = permissions(admin=True)
        with (
            patch.multiple(
                config,
                AUDIT_DRY_RUN=True,
                AUDIT_GROUP_ID=-1001,
                REQUIRE_SECOND_FACTOR_GROUP=False,
            ),
            patch.object(config, "validate_audit"),
            patch.object(
                audit_telethon.google_sheets,
                "clean_and_get_snapshot",
                return_value=cleanup(snapshot),
            ),
        ):
            result = await audit_telethon.run_audit(client)
        self.assertEqual(result["protected"], 1)
        self.assertEqual(result["planned"], 0)

    async def test_failed_unban_is_reported_as_temporary_removal(self):
        client = types.SimpleNamespace(edit_permissions=AsyncMock())
        client.edit_permissions.side_effect = [None, OSError(), OSError(), OSError()]
        result = await audit_telethon.kick_participant(
            client, types.SimpleNamespace(id=-1001), 10
        )
        self.assertEqual(result, "temporary")
        self.assertEqual(client.edit_permissions.await_count, 4)
        first = client.edit_permissions.await_args_list[0]
        self.assertFalse(first.kwargs["view_messages"])
        self.assertGreaterEqual(first.args[2], timedelta(seconds=30))
        self.assertLessEqual(first.args[2], timedelta(minutes=5))

    async def test_invalid_audit_roles_abort_before_sheet_cleanup(self):
        sheet_cleanup = Mock()
        common = dict(
            GOOGLE_SHEET_ID="sheet",
            GROUP_PATTERN=r"^M31[0-9]{2}$",
            SHEET_CACHE_MINUTES=5,
            TELEGRAM_API_ID=123,
            TELEGRAM_API_HASH="a" * 32,
            AUDIT_DRY_RUN=True,
            REQUIRE_SECOND_FACTOR_GROUP=False,
            LOG_CHAT_ID=-1003,
            MANAGED_CHAT_ID=-1001,
            AUDIT_GROUP_ID=-9999,
        )
        with (
            patch.multiple(config, **common),
            patch.object(
                audit_telethon.google_sheets,
                "clean_and_get_snapshot",
                sheet_cleanup,
            ),
        ):
            with self.assertRaisesRegex(config.ConfigError, "AUDIT_GROUP_ID"):
                await audit_telethon.run_audit(AsyncMock())
        sheet_cleanup.assert_not_called()

        sheet_cleanup.reset_mock()
        with (
            patch.multiple(
                config,
                **{
                    **common,
                    "AUDIT_GROUP_ID": -1001,
                    "LOG_CHAT_ID": -1001,
                },
            ),
            patch.object(
                audit_telethon.google_sheets,
                "clean_and_get_snapshot",
                sheet_cleanup,
            ),
        ):
            with self.assertRaisesRegex(config.ConfigError, "LOG_CHAT_ID"):
                await audit_telethon.run_audit(AsyncMock())
        sheet_cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
