import json
import os
import tempfile
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

from models.task_node import TaskNode
from utils.file_guard import ProjectFileGuard
from utils.usage_logger import UsageLogger
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
