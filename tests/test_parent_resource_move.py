import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication, QMessageBox

from models.task_node import TaskNode
from ui.main_window import MainWindow
from utils.resource_allocation import ResourceDefinition
from utils.scheduler import schedule


def _enable_overlap(window, resource_id="room-1", kind="room", label="Room 1"):
    window._metadata_resource_definitions = lambda: [ResourceDefinition(
        resource_id, kind, label, conflict_enabled=True).to_dict()]


class ParentResourceMoveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_parent_resource_move_shifts_children_and_preserves_valid_interval(self):
        with patch.object(MainWindow, "_restore_startup_state", return_value=True):
            window = MainWindow()
        window._autosave_timer.stop()
        try:
            _enable_overlap(window)

            # First task occupies Room 1 on Sept 7-11
            first = TaskNode("First Task")
            first.start_date, first.end_date = "2026-09-07", "2026-09-11"
            first.start_rule = {"mode": "fixed", "date": "2026-09-07"}
            first.end_rule = {"mode": "duration", "days": 5}
            first.task_tokens = [{"id": "room-1", "label": "Room 1", "kind": "room"}]

            # Parent task with Room 1, with a child task
            parent = TaskNode("Parent Task")
            parent.task_tokens = [{"id": "room-1", "label": "Room 1", "kind": "room"}]

            child = TaskNode("Child Task", parent=parent)
            child.start_date, child.end_date = "2026-09-07", "2026-09-11"
            child.start_rule = {"mode": "fixed", "date": "2026-09-07"}
            child.end_rule = {"mode": "duration", "days": 5}
            parent.children = [child]

            schedule([first, parent])
            self.assertEqual("2026-09-07", parent.start_date)
            self.assertEqual("2026-09-11", parent.end_date)

            project = window._add_project_from_data(
                "Test", {"holidays": [], "exclude_weekends": True}, [first, parent])
            project.reset_history_baseline()

            conflicts = window.recheck_resource_conflicts().conflicts
            self.assertEqual(1, len(conflicts))
            conflict = conflicts[0]

            with patch("ui.main_window.QMessageBox.question",
                       return_value=QMessageBox.StandardButton.Yes), \
                    patch("ui.main_window.usage_logger.timed_exec",
                          return_value=True):
                window._move_resource_to_next_available(project, parent, conflict)

            moved_parent = project.tree_view._find_item_by_id(parent.id).node
            moved_child = project.tree_view._find_item_by_id(child.id).node

            # Invariant: parent start must not be after parent end!
            self.assertLessEqual(moved_parent.start_date, moved_parent.end_date,
                                 f"Invalid interval: start {moved_parent.start_date} > end {moved_parent.end_date}")
            # The parent and child should have moved to the next available block (Sept 14-18)
            self.assertEqual("2026-09-14", moved_parent.start_date)
            self.assertEqual("2026-09-18", moved_parent.end_date)
            self.assertEqual("2026-09-14", moved_child.start_date)
            self.assertEqual("2026-09-18", moved_child.end_date)

            # No conflicts should remain
            self.assertEqual([], window.recheck_resource_conflicts().conflicts)

            # Single undo restores original state cleanly
            project.undo()
            restored_parent = project.tree_view._find_item_by_id(parent.id).node
            restored_child = project.tree_view._find_item_by_id(child.id).node
            self.assertEqual("2026-09-07", restored_parent.start_date)
            self.assertEqual("2026-09-11", restored_parent.end_date)
            self.assertEqual("2026-09-07", restored_child.start_date)
            self.assertEqual("2026-09-11", restored_child.end_date)
            self.assertEqual(1, len(window.recheck_resource_conflicts().conflicts))

            # Redo restores the moved state
            project.redo()
            redo_parent = project.tree_view._find_item_by_id(parent.id).node
            self.assertEqual("2026-09-14", redo_parent.start_date)
            self.assertEqual("2026-09-18", redo_parent.end_date)
            self.assertEqual([], window.recheck_resource_conflicts().conflicts)

        finally:
            window.unsaved_changes = False
            window.close()

    def test_parent_resource_move_with_automatic_children_and_save_reload(self):
        with patch.object(MainWindow, "_restore_startup_state", return_value=True):
            window = MainWindow()
        window._autosave_timer.stop()
        try:
            _enable_overlap(window)

            first = TaskNode("First Task")
            first.start_date, first.end_date = "2026-09-07", "2026-09-11"
            first.start_rule = {"mode": "fixed", "date": "2026-09-07"}
            first.end_rule = {"mode": "duration", "days": 5}
            first.task_tokens = [{"id": "room-1", "label": "Room 1", "kind": "room"}]

            parent = TaskNode("Parent Task")
            parent.start_rule = {"mode": "fixed", "date": "2026-09-07"}
            parent.task_tokens = [{"id": "room-1", "label": "Room 1", "kind": "room"}]

            # Child 1 is parallel (starts at parent start, 3 days)
            child1 = TaskNode("Child 1", parent=parent)
            child1.is_parallel = True
            child1.start_rule = {"mode": "automatic"}
            child1.end_rule = {"mode": "duration", "days": 3}

            # Child 2 follows child 1 sequentially (2 days)
            child2 = TaskNode("Child 2", parent=parent)
            child2.is_parallel = False
            child2.start_rule = {"mode": "automatic"}
            child2.end_rule = {"mode": "duration", "days": 2}
            parent.children = [child1, child2]

            schedule([first, parent])
            self.assertEqual("2026-09-07", parent.start_date)
            self.assertEqual("2026-09-11", parent.end_date)

            project = window._add_project_from_data(
                "Test", {"holidays": [], "exclude_weekends": True}, [first, parent])
            project.reset_history_baseline()

            conflicts = window.recheck_resource_conflicts().conflicts
            self.assertEqual(1, len(conflicts))

            with patch("ui.main_window.QMessageBox.question",
                       return_value=QMessageBox.StandardButton.Yes), \
                    patch("ui.main_window.usage_logger.timed_exec",
                          return_value=True):
                window._move_resource_to_next_available(project, parent, conflicts[0])

            moved_parent = project.tree_view._find_item_by_id(parent.id).node
            moved_c1 = project.tree_view._find_item_by_id(child1.id).node
            moved_c2 = project.tree_view._find_item_by_id(child2.id).node

            self.assertLessEqual(moved_parent.start_date, moved_parent.end_date)
            self.assertEqual("2026-09-14", moved_parent.start_date)
            self.assertEqual("2026-09-18", moved_parent.end_date)
            self.assertEqual("2026-09-14", moved_c1.start_date)
            self.assertEqual("2026-09-16", moved_c1.end_date)
            self.assertEqual("2026-09-17", moved_c2.start_date)
            self.assertEqual("2026-09-18", moved_c2.end_date)

            # Serialization round-trip
            snap = project.get_snapshot()
            reloaded_parent = TaskNode.from_dict(snap["tasks"][1])
            self.assertEqual("2026-09-14", reloaded_parent.start_date)
            self.assertEqual("2026-09-18", reloaded_parent.end_date)
            self.assertEqual(2, len(reloaded_parent.children))
            self.assertEqual("2026-09-14", reloaded_parent.children[0].start_date)
            self.assertEqual("2026-09-17", reloaded_parent.children[1].start_date)

        finally:
            window.unsaved_changes = False
            window.close()


if __name__ == "__main__":
    unittest.main()
