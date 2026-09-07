import copy
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import config
import google_sheets


class FakeWorksheet:
    def __init__(self, title, sheet_id, rows):
        self.title = title
        self.id = sheet_id
        self.rows = copy.deepcopy(rows)

    def get_all_values(self):
        return copy.deepcopy(self.rows)


class FakeSpreadsheet:
    def __init__(self, worksheets):
        self.worksheets = {worksheet.title: worksheet for worksheet in worksheets}
        self.by_id = {worksheet.id: worksheet for worksheet in worksheets}
        self.batch_calls = []
        self._apply_lock = threading.Lock()

    def worksheet(self, name):
        return self.worksheets[name]

    def batch_update(self, body):
        with self._apply_lock:
            self.batch_calls.append(copy.deepcopy(body))
            for request in body["requests"]:
                if "appendCells" in request:
                    operation = request["appendCells"]
                    row = [
                        cell.get("userEnteredValue", {}).get("stringValue", "")
                        for cell in operation["rows"][0]["values"]
                    ]
                    self.by_id[operation["sheetId"]].rows.append(row)
                else:
                    operation = request["deleteDimension"]["range"]
                    del self.by_id[operation["sheetId"]].rows[
                        operation["startIndex"] : operation["endIndex"]
                    ]


def make_sheet(form_rows, blacklist_rows=None, whitelist_rows=None):
    return FakeSpreadsheet(
        [
            FakeWorksheet("Форма", 1, form_rows),
            FakeWorksheet(
                "Черный список",
                2,
                blacklist_rows
                if blacklist_rows is not None
                else [["username", "причина", "дата"]],
            ),
            FakeWorksheet(
                "Белый список",
                3,
                whitelist_rows if whitelist_rows is not None else [["username"]],
            ),
        ]
    )


class SheetsTests(unittest.TestCase):
    def setUp(self):
        google_sheets._reset_state_for_tests()
        self.settings = patch.multiple(
            config,
            FORM_WORKSHEET_NAME="Форма",
            FORM_GROUP_COLUMN="группа",
            FORM_USERNAME_COLUMN="username",
            BLACKLIST_WORKSHEET_NAME="Черный список",
            BLACKLIST_USERNAME_COLUMN="username",
            BLACKLIST_REASON_COLUMN="причина",
            BLACKLIST_DATE_COLUMN="дата",
            WHITELIST_WORKSHEET_NAME="Белый список",
            WHITELIST_USERNAME_COLUMN="username",
            GROUP_PATTERN=r"^M31[0-9]{2}$",
            SHEET_CACHE_MINUTES=5,
            KICK_USERS_WITHOUT_USERNAME=True,
            REQUIRE_SECOND_FACTOR_GROUP=False,
        )
        self.settings.start()

    def tearDown(self):
        self.settings.stop()
        google_sheets._reset_state_for_tests()

    def test_reordered_headers_exact_pattern_dedup_and_string_values(self):
        sheet = make_sheet(
            [
                ["extra", "USERNAME", "ГРУППА"],
                ["", "Good", "M3112"],
                ["", "lower", "m3112"],
                ["", "lower", "bad"],
                ["", "White", "bad"],
                ["", "=formula", "M31١٢"],
            ],
            [["ДАТА", "ПРИЧИНА", "USERNAME"], ["old", "old", "LOWER"]],
            [["USERNAME"], ["white"]],
        )
        with patch.object(google_sheets, "_open_spreadsheet", return_value=sheet):
            result = google_sheets.clean_and_get_snapshot()

        self.assertEqual(result.snapshot.form, frozenset({"good", "white"}))
        self.assertEqual(result.snapshot.blacklist, frozenset({"lower", "=formula"}))
        self.assertEqual(result.moved_to_blacklist, 1)
        self.assertEqual(result.deleted_rows, 3)
        self.assertEqual(len(sheet.batch_calls), 1)
        requests = sheet.batch_calls[0]["requests"]
        append = requests[0]["appendCells"]
        self.assertEqual(append["fields"], "userEnteredValue")
        values = append["rows"][0]["values"]
        self.assertEqual(values[2]["userEnteredValue"], {"stringValue": "=formula"})
        starts = [
            item["deleteDimension"]["range"]["startIndex"]
            for item in requests[1:]
        ]
        self.assertEqual(starts, sorted(starts, reverse=True))

    def test_all_headers_valid_with_no_data(self):
        sheet = make_sheet([["группа", "username"]])
        with patch.object(google_sheets, "_open_spreadsheet", return_value=sheet):
            result = google_sheets.clean_and_get_snapshot()
        self.assertEqual(result.checked, 0)
        self.assertEqual(result.snapshot.form, frozenset())
        self.assertEqual(sheet.batch_calls, [])

    def test_missing_header_aborts_before_write(self):
        sheet = make_sheet([["username"], ["someone"]])
        with patch.object(google_sheets, "_open_spreadsheet", return_value=sheet):
            with self.assertRaisesRegex(RuntimeError, "группа"):
                google_sheets.clean_and_get_snapshot()
        self.assertEqual(sheet.batch_calls, [])

    def test_dry_run_projects_without_write_or_cache_poison(self):
        sheet = make_sheet([["группа", "username"], ["bad", "person"]])
        with patch.object(google_sheets, "_open_spreadsheet", return_value=sheet):
            result = google_sheets.clean_and_get_snapshot(dry_run=True)
        self.assertEqual(result.snapshot.form, frozenset())
        self.assertIn("person", result.snapshot.blacklist)
        self.assertEqual(sheet.batch_calls, [])
        self.assertIsNone(google_sheets._cached_snapshot)

    def test_force_refresh_failure_never_returns_stale_snapshot(self):
        sheet = make_sheet([["группа", "username"], ["M3112", "person"]])
        with patch.object(google_sheets, "_open_spreadsheet", return_value=sheet):
            google_sheets.get_snapshot(force_refresh=True)
        with patch.object(google_sheets, "_open_spreadsheet", side_effect=OSError("down")):
            with self.assertRaises(OSError):
                google_sheets.get_snapshot(force_refresh=True)

    def test_concurrent_cleanup_is_serialized_and_idempotent(self):
        sheet = make_sheet([["группа", "username"], ["bad", "person"]])
        with patch.object(google_sheets, "_open_spreadsheet", return_value=sheet):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: google_sheets.clean_and_get_snapshot(), range(2)))
        self.assertEqual(len(sheet.batch_calls), 1)
        self.assertEqual(sum(result.moved_to_blacklist for result in results), 1)
        self.assertEqual(sheet.worksheets["Черный список"].rows[-1][0], "person")

    def test_policy_blacklist_precedence_and_whitelist_not_blanket_allow(self):
        snapshot = google_sheets.SheetSnapshot(
            frozenset({"both"}), frozenset({"both"}), frozenset({"both", "whiteonly"})
        )
        self.assertEqual(google_sheets.decide_access(snapshot, "both").state, "deny")
        self.assertEqual(google_sheets.decide_access(snapshot, "whiteonly").state, "deny")

    def test_no_username_exemption_still_requires_second_factor(self):
        snapshot = google_sheets.SheetSnapshot(frozenset(), frozenset(), frozenset())
        with patch.multiple(
            config, KICK_USERS_WITHOUT_USERNAME=False, REQUIRE_SECOND_FACTOR_GROUP=True
        ):
            self.assertEqual(
                google_sheets.decide_access(snapshot, None, False).state, "deny"
            )
            self.assertEqual(
                google_sheets.decide_access(snapshot, None, None).state, "unknown"
            )


class ConfigTests(unittest.TestCase):
    def test_bool_parser_is_strict(self):
        with patch.dict(os.environ, {"STRICT_BOOL": "perhaps"}):
            with self.assertRaises(config.ConfigError):
                config._bool("STRICT_BOOL", False)

    def test_blank_pattern_and_role_alias_are_rejected(self):
        with patch.multiple(config, GROUP_PATTERN="   ", GOOGLE_SHEET_ID="sheet"):
            with self.assertRaisesRegex(config.ConfigError, "GROUP_PATTERN"):
                config._validate_sheet_settings()
        with patch.multiple(
            config,
            FORM_WORKSHEET_NAME="same",
            BLACKLIST_WORKSHEET_NAME="SAME",
            WHITELIST_WORKSHEET_NAME="white",
            GOOGLE_SHEET_ID="sheet",
        ):
            with self.assertRaisesRegex(config.ConfigError, "листов"):
                config._validate_sheet_settings()

    def test_example_imports_after_only_required_bot_values_are_filled(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        example = (root / ".env.example").read_text(encoding="utf-8")
        additions = """
BOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyzABCDE
MANAGED_CHAT_ID=-1001
LOG_CHAT_ID=-1003
GOOGLE_SHEET_ID=test-sheet
"""
        with tempfile.TemporaryDirectory() as directory:
            pathlib.Path(directory, ".env").write_text(
                example + additions, encoding="utf-8"
            )
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(root),
            }
            result = subprocess.run(
                [sys.executable, "-c", "import config; config.validate_core()"],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
