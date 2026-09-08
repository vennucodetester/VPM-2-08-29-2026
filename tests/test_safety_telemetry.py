import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QDialog

from models.task_node import TaskNode
from utils.file_guard import ProjectFileGuard
from utils.usage_logger import (
    CLOSE_EXPORT_TIMEOUT_SECONDS,
    REPO_TELEMETRY_DIRNAME,
    UsageLogger,
    dialog_outcome,
    repo_telemetry_dir,
    timed_exec,
)
from utils.usage_report import build_report
from utils.version_info import install_dir
from utils.vpmt_io import load_projects, save_projects


class FileGuardTests(unittest.TestCase):
    def test_second_instance_cannot_lock_same_project(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "project.vpmt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}")
            first, second = ProjectFileGuard(), ProjectFileGuard()
            try:
                self.assertTrue(first.acquire(path))
                self.assertFalse(second.acquire(path))
                first.release()
                self.assertTrue(second.acquire(path))
            finally:
                first.release()
                second.release()

    def test_external_change_is_detected_and_refreshable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "project.vpmt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("old")
            guard = ProjectFileGuard()
            try:
                self.assertTrue(guard.acquire(path))
                self.assertFalse(guard.changed_on_disk())
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("new content")
                self.assertTrue(guard.changed_on_disk())
                guard.refresh()
                self.assertFalse(guard.changed_on_disk())
            finally:
                guard.release()


class TelemetryTests(unittest.TestCase):
    def test_events_have_unique_ids_and_strip_sensitive_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            logger = UsageLogger(folder, enabled=True, import_legacy=False)
            logger.set_project("proj-safe")
            logger.log("file_open", path=r"C:\Users\person\Secret Project.vpmt")
            logger.log("file_open", path=r"C:\Users\person\Secret Project.vpmt")
            rows = [json.loads(line) for line in logger.path().read_text(
                encoding="utf-8").splitlines()]
            self.assertEqual(2, len({row["eid"] for row in rows}))
            self.assertEqual({".vpmt"}, {row["d"]["file_type"] for row in rows})
            self.assertNotIn("Secret Project", json.dumps(rows))
            self.assertEqual({"proj-safe"}, {row["project"] for row in rows})

    def test_events_strip_token_password_secret_and_email(self):
        with tempfile.TemporaryDirectory() as folder:
            logger = UsageLogger(folder, enabled=True, import_legacy=False)
            logger.log("auth", token="abc", password="p", secret="s",
                       email="a@b.c", via="menu")
            row = json.loads(logger.path().read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual("menu", row["d"]["via"])
            for key in ("token", "password", "secret", "email"):
                self.assertNotIn(key, row["d"])
            dumped = json.dumps(row)
            self.assertNotIn("abc", dumped)
            self.assertNotIn("a@b.c", dumped)

    def test_summary_is_written(self):
        with tempfile.TemporaryDirectory() as folder:
            logger = UsageLogger(folder, enabled=True, import_legacy=False)
            logger.log("app_start")
            logger.log("workflow_complete")
            logger.write_summary()
            summary = json.loads((logger.root / "summary-latest.json").read_text(
                encoding="utf-8"))
            self.assertEqual(2, summary["events"])
            self.assertEqual(1, summary["sessions"])

    def test_summary_separates_manual_and_automatic_saves(self):
        with tempfile.TemporaryDirectory() as folder:
            logger = UsageLogger(folder, enabled=True, import_legacy=False)
            logger.log("file_save", manual=False)
            logger.log("file_save", manual=True)
            logger.write_summary()
            summary = json.loads((logger.root / "summary-latest.json").read_text(
                encoding="utf-8"))
            self.assertEqual(1, summary["counts"]["file_save_automatic"])
            self.assertEqual(1, summary["counts"]["file_save_manual"])

    def test_long_running_app_rotates_session_after_inactivity(self):
        with tempfile.TemporaryDirectory() as folder:
            logger = UsageLogger(folder, enabled=True, import_legacy=False)
            logger.log("first")
            first_sid = logger.sid
            logger.last_activity -= 31 * 60
            logger.log("resumed")
            self.assertNotEqual(first_sid, logger.sid)

    def test_repo_export_copies_sanitized_month_summary_and_report(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "appdata")
            dest = os.path.join(folder, "repo-telemetry")
            logger = UsageLogger(source, enabled=True, import_legacy=False)
            logger.log("app_start")
            logger.log("file_open", path=r"C:\Users\person\Secret Project.vpmt")
            logger.write_summary()
            source_month = logger.path().read_text(encoding="utf-8")
            exported = logger.export_repo_telemetry(dest=dest)
            self.assertEqual(Path(dest), exported)
            copied = Path(dest) / logger.path().name
            self.assertTrue(copied.is_file())
            rows = [json.loads(line) for line in copied.read_text(
                encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(2, len(rows))
            self.assertEqual({".vpmt"}, {row["d"].get("file_type") for row in rows
                                         if row["ev"] == "file_open"})
            self.assertNotIn("Secret Project", copied.read_text(encoding="utf-8"))
            self.assertTrue((Path(dest) / "summary-latest.json").is_file())
            self.assertTrue((Path(dest) / "report-latest.md").is_file())
            report = (Path(dest) / "report-latest.md").read_text(encoding="utf-8")
            self.assertIn("VPM Tracker Usage Report", report)
            # Canonical AppData month file is unchanged.
            self.assertEqual(source_month, logger.path().read_text(encoding="utf-8"))

    def test_repo_export_rewrites_unsanitized_legacy_lines(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "appdata")
            dest = os.path.join(folder, "repo-telemetry")
            logger = UsageLogger(source, enabled=True, import_legacy=False)
            dirty = {
                "schema": 2, "eid": "dirty-1", "ts": "2026-09-01 12:00:00",
                "sid": "s", "install": "i", "project": None, "env": "production",
                "ev": "file_open",
                "d": {"path": r"C:\Users\person\Hidden.vpmt", "via": "menu"},
            }
            logger.path().write_text(json.dumps(dirty) + "\n", encoding="utf-8")
            logger.export_repo_telemetry(dest=dest, include_report=False)
            copied = json.loads((Path(dest) / logger.path().name).read_text(
                encoding="utf-8").splitlines()[0])
            self.assertEqual(".vpmt", copied["d"]["file_type"])
            self.assertNotIn("path", copied["d"])
            self.assertNotIn("Hidden", json.dumps(copied))

    def test_repo_export_includes_previous_month_when_present(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "appdata")
            dest = os.path.join(folder, "repo-telemetry")
            logger = UsageLogger(source, enabled=True, import_legacy=False)
            logger.log("app_start")
            now = datetime.now()
            prev_month = now.month - 1 or 12
            prev_year = now.year if now.month > 1 else now.year - 1
            stamp = f"{prev_year:04d}-{prev_month:02d}"
            previous = logger.path(stamp)
            previous.write_text(
                json.dumps({
                    "schema": 2, "eid": "old-1",
                    "ts": f"{stamp}-15 09:00:00",
                    "sid": "s", "install": "i", "project": None,
                    "env": "production", "ev": "app_start", "d": {},
                }) + "\n",
                encoding="utf-8",
            )
            logger.export_repo_telemetry(dest=dest, include_report=False)
            self.assertTrue((Path(dest) / previous.name).is_file())
            self.assertTrue((Path(dest) / logger.path().name).is_file())

    def test_repo_export_skips_when_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            dest = os.path.join(folder, "repo-telemetry")
            logger = UsageLogger(folder, enabled=False, import_legacy=False)
            self.assertIsNone(logger.export_repo_telemetry(dest=dest))
            self.assertFalse(os.path.exists(dest))

    def test_repo_export_does_not_write_test_env_into_install_dir(self):
        with tempfile.TemporaryDirectory() as folder:
            logger = UsageLogger(folder, enabled=True, import_legacy=False)
            logger.log("app_start")
            default = repo_telemetry_dir()
            before = {p.name for p in default.glob("*")} if default.exists() else set()
            self.assertIsNone(logger.export_repo_telemetry())
            after = {p.name for p in default.glob("*")} if default.exists() else set()
            self.assertEqual(before, after)
            self.assertEqual(REPO_TELEMETRY_DIRNAME, default.name)

    def test_bounded_export_skips_when_lock_already_held(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "appdata")
            dest = os.path.join(folder, "repo-telemetry")
            logger = UsageLogger(source, enabled=True, import_legacy=False)
            logger.log("app_start")
            self.assertTrue(logger._export_lock.acquire(blocking=False))
            try:
                start = time.time()
                result = logger.sync_repo_telemetry(dest=dest, timeout=1.0)
                elapsed = time.time() - start
                self.assertIsNone(result)
                self.assertLess(elapsed, 0.4)
                self.assertFalse(os.path.exists(dest))
            finally:
                logger._export_lock.release()

    def test_bounded_export_writes_when_lock_is_free(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "appdata")
            dest = os.path.join(folder, "repo-telemetry")
            logger = UsageLogger(source, enabled=True, import_legacy=False)
            logger.log("app_start")
            result = logger.sync_repo_telemetry(dest=dest, timeout=1.0)
            self.assertEqual(Path(dest), result)
            self.assertTrue((Path(dest) / logger.path().name).is_file())

    def test_background_export_writes_without_raising(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "appdata")
            dest = os.path.join(folder, "repo-telemetry")
            logger = UsageLogger(source, enabled=True, import_legacy=False)
            logger.log("app_start")
            returned = logger.sync_repo_telemetry(dest=dest, background=True)
            self.assertEqual(Path(dest), returned)
            if logger._export_thread is not None:
                logger._export_thread.join(timeout=2)
            self.assertTrue((Path(dest) / logger.path().name).is_file())

    def test_usage_report_accepts_explicit_root(self):
        with tempfile.TemporaryDirectory() as folder:
            logger = UsageLogger(folder, enabled=True, import_legacy=False)
            logger.log("search", chars=3, hits=1)
            report = build_report(months=1, root=logger.root)
            self.assertIn("search", report)


class GitignoreTelemetryTests(unittest.TestCase):
    def test_gitignore_tracks_telemetry_but_ignores_root_usage_jsonl(self):
        repo = install_dir()
        root_hit = subprocess.run(
            ["git", "-C", repo, "check-ignore", "-q", "usage-2099-01.jsonl"],
            capture_output=True, text=True)
        self.assertEqual(0, root_hit.returncode)
        tracked = subprocess.run(
            ["git", "-C", repo, "check-ignore", "-q",
             "telemetry/usage-2099-01.jsonl"],
            capture_output=True, text=True)
        self.assertEqual(1, tracked.returncode)
        readme = subprocess.run(
            ["git", "-C", repo, "check-ignore", "-q", "telemetry/README.md"],
            capture_output=True, text=True)
        self.assertEqual(1, readme.returncode)


class DialogOutcomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _dialog_rows(self, logger):
        return [json.loads(line) for line in logger.path().read_text(
            encoding="utf-8").splitlines() if json.loads(line)["ev"] == "dialog"]

    def test_dialog_outcome_keeps_ok_and_plain_cancel(self):
        dialog = QDialog()
        try:
            self.assertEqual("ok", dialog_outcome(dialog, 1))
            self.assertEqual("cancel", dialog_outcome(dialog, 0))
        finally:
            dialog.close()

    def test_dialog_outcome_maps_intentional_notes_navigation(self):
        marked = QDialog()
        flagged = QDialog()
        try:
            marked.setProperty("telemetry_outcome", "navigated")
            self.assertEqual("navigated", dialog_outcome(marked, 0))
            flagged.setProperty("clear_task_note_context", True)
            self.assertEqual("navigated", dialog_outcome(flagged, 0))
        finally:
            marked.close()
            flagged.close()

    def test_timed_exec_logs_navigated_for_escape_to_overall(self):
        """Esc / escape-to-overall must not inflate notes_edit cancel rate."""
        with tempfile.TemporaryDirectory() as folder:
            logger = UsageLogger(folder, enabled=True, import_legacy=False)
            dialog = QDialog()
            dialog.setProperty("clear_task_note_context", True)
            dialog.setProperty("telemetry_outcome", "navigated")
            QTimer.singleShot(0, dialog.reject)
            with patch("utils.usage_logger.usage", logger):
                result = timed_exec(dialog, "notes_edit")
            self.assertFalse(result)
            rows = self._dialog_rows(logger)
            self.assertEqual(1, len(rows))
            self.assertEqual("notes_edit", rows[0]["d"]["name"])
            self.assertEqual("navigated", rows[0]["d"]["outcome"])

    def test_timed_exec_still_logs_cancel_for_plain_reject(self):
        with tempfile.TemporaryDirectory() as folder:
            logger = UsageLogger(folder, enabled=True, import_legacy=False)
            dialog = QDialog()
            QTimer.singleShot(0, dialog.reject)
            with patch("utils.usage_logger.usage", logger):
                result = timed_exec(dialog, "notes_edit")
            self.assertFalse(result)
            rows = self._dialog_rows(logger)
            self.assertEqual(1, len(rows))
            self.assertEqual("cancel", rows[0]["d"]["outcome"])

    def test_timed_exec_logs_ok_for_accept(self):
        with tempfile.TemporaryDirectory() as folder:
            logger = UsageLogger(folder, enabled=True, import_legacy=False)
            dialog = QDialog()
            QTimer.singleShot(0, dialog.accept)
            with patch("utils.usage_logger.usage", logger):
                result = timed_exec(dialog, "notes_edit")
            self.assertTrue(result)
            rows = self._dialog_rows(logger)
            self.assertEqual(1, len(rows))
            self.assertEqual("ok", rows[0]["d"]["outcome"])

    def test_usage_report_does_not_count_navigated_as_cancel(self):
        with tempfile.TemporaryDirectory() as folder:
            logger = UsageLogger(folder, enabled=True, import_legacy=False)
            logger.log("dialog", name="notes_edit", outcome="navigated",
                       ms_open=1500)
            logger.log("dialog", name="notes_edit", outcome="cancel",
                       ms_open=800)
            logger.log("dialog", name="notes_edit", outcome="ok",
                       ms_open=96000)
            report = build_report(months=3, root=logger.root)
            self.assertIn("notes_edit: 3 shown, 33% cancel", report)


class ProjectIdentityTests(unittest.TestCase):
    def test_project_id_survives_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "project.vpmt")
            save_projects([{
                "id": "proj-stable",
                "name": "Project",
                "metadata": {},
                "roots": [TaskNode("Task")],
            }], path)
            loaded = load_projects(path)
            self.assertEqual("proj-stable", loaded[0]["id"])


class MainWindowSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from ui.main_window import MainWindow
        with patch.object(MainWindow, "_restore_startup_state", return_value=False):
            return MainWindow()

    def test_external_change_blocks_manual_save(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "project.vpmt")
            window = self._window()
            try:
                save_projects([window.all_projects()[0].to_persistable()], path)
                self.assertTrue(window.file_guard.acquire(path))
                window.current_filepath = path
                window.unsaved_changes = True
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(" ")
                with patch("ui.main_window.QMessageBox.warning") as warning:
                    window.save_project_file()
                warning.assert_called_once()
                self.assertTrue(window.unsaved_changes)
            finally:
                window.unsaved_changes = False
                window.close()

    def test_close_event_exports_repo_telemetry(self):
        window = self._window()
        window.unsaved_changes = False
        try:
            with patch("ui.main_window.usage_logger.sync_repo_telemetry") as sync:
                window.close()
                sync.assert_called()
                kwargs = sync.call_args.kwargs
                self.assertFalse(kwargs.get("background", True))
                self.assertEqual(
                    CLOSE_EXPORT_TIMEOUT_SECONDS, kwargs.get("timeout"))
        finally:
            if window.isVisible():
                window.close()

    def test_close_event_releases_file_guard_if_export_raises(self):
        window = self._window()
        window.unsaved_changes = False
        release = window.file_guard.release
        with patch.object(window.file_guard, "release", wraps=release) as mocked:
            with patch("ui.main_window.usage_logger.sync_repo_telemetry",
                       side_effect=RuntimeError("export failed")):
                window.close()
            mocked.assert_called()

    def test_startup_telemetry_timer_is_child_and_stopped_on_close(self):
        window = self._window()
        window.unsaved_changes = False
        self.assertIs(window._startup_telemetry_timer.parent(), window)
        self.assertTrue(window._startup_telemetry_timer.isSingleShot())
        self.assertTrue(window._startup_telemetry_timer.isActive())
        with patch("ui.main_window.usage_logger.sync_repo_telemetry"):
            window.close()
        self.assertFalse(window._startup_telemetry_timer.isActive())
        self.assertFalse(window._telemetry_export_timer.isActive())

    def test_second_window_cannot_open_locked_project(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "project.vpmt")
            save_projects([{
                "id": "proj-lock-test", "name": "Project",
                "metadata": {}, "roots": [TaskNode("Task")],
            }], path)
            first, second = self._window(), self._window()
            try:
                self.assertTrue(first._load_path(path, prompt_unsaved=False))
                with patch("ui.main_window.QMessageBox.warning") as warning:
                    self.assertFalse(second._load_path(path, prompt_unsaved=False))
                warning.assert_called_once()
            finally:
                first.close()
                second.close()


if __name__ == "__main__":
    unittest.main()
