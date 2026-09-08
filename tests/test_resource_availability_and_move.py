import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication, QMessageBox

from models.task_node import TaskNode
from ui.main_window import MainWindow
from utils.resource_allocation import (
    ResourceDefinition, analyze_projects, next_available_block, next_available_start
)
from utils.scheduler import schedule


def _enable_overlap(window, defs):
    window._metadata_resource_definitions = lambda: [
        ResourceDefinition(d["id"], d.get("kind", "room"), d["label"],
                           conflict_enabled=True).to_dict()
        for d in defs
    ]


class ResourceAvailabilityAndMoveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_f02_availability_search_shares_ancestor_deduplication(self):
        # Parent and child reserve Room 1 Sept 7-11
        r_def = ResourceDefinition("room-1", "room", "Room 1", capacity=1, conflict_enabled=True)
        t = {"id": "room-1", "label": "Room 1", "kind": "room"}

        parent = TaskNode("Parent")
        parent.start_date, parent.end_date = "2026-09-07", "2026-09-11"
        parent.task_tokens = [t]

        child = TaskNode("Child", parent=parent)
        child.start_date, child.end_date = "2026-09-07", "2026-09-11"
        child.task_tokens = [t]
        parent.children = [child]

        proj = {"id": "p1", "name": "P1", "roots": [parent],
                "metadata": {"holidays": [], "exclude_weekends": True}}
        analysis = analyze_projects([proj], [r_def], attach=False)
        self.assertEqual(0, len(analysis.conflicts))

        child_assign = [a for a in analysis.assignments if a.task_id == child.id][0]
        # Availability search for child must not treat parent as competing booking
        # on a date after Sept 11, or overstate capacity against unrelated tasks
        preview = next_available_start(child_assign, analysis.assignments, capacity=1)
        # Should be free right after current reservation (Sept 14) or not blocked by parent
        self.assertIsNotNone(preview)

    def test_f14_parent_move_considers_descendant_resources(self):
        with patch.object(MainWindow, "_restore_startup_state", return_value=True):
            window = MainWindow()
        window._autosave_timer.stop()
        try:
            _enable_overlap(window, [
                {"id": "room-r", "kind": "room", "label": "Room R"},
                {"id": "case-s", "kind": "case", "label": "Case S"},
            ])

            # Blocker 1 blocks Room R on Sept 7-11
            blocker1 = TaskNode("Blocker R")
            blocker1.start_date, blocker1.end_date = "2026-09-07", "2026-09-11"
            blocker1.start_rule = {"mode": "fixed", "date": "2026-09-07"}
            blocker1.end_rule = {"mode": "duration", "days": 5}
            blocker1.task_tokens = [{"id": "room-r", "label": "Room R", "kind": "room"}]

            # Blocker 2 blocks Case S on Sept 14-18
            blocker2 = TaskNode("Blocker S")
            blocker2.start_date, blocker2.end_date = "2026-09-14", "2026-09-18"
            blocker2.start_rule = {"mode": "fixed", "date": "2026-09-14"}
            blocker2.end_rule = {"mode": "duration", "days": 5}
            blocker2.task_tokens = [{"id": "case-s", "label": "Case S", "kind": "case"}]

            # Parent reserves Room R on Sept 7-11
            parent = TaskNode("Parent")
            parent.start_date, parent.end_date = "2026-09-07", "2026-09-11"
            parent.start_rule = {"mode": "fixed", "date": "2026-09-07"}
            parent.end_rule = {"mode": "duration", "days": 5}
            parent.task_tokens = [{"id": "room-r", "label": "Room R", "kind": "room"}]

            # Child reserves Case S on Sept 7-11
            child = TaskNode("Child", parent=parent)
            child.start_date, child.end_date = "2026-09-07", "2026-09-11"
            child.start_rule = {"mode": "fixed", "date": "2026-09-07"}
            child.end_rule = {"mode": "duration", "days": 5}
            child.task_tokens = [{"id": "case-s", "label": "Case S", "kind": "case"}]
            parent.children = [child]

            schedule([blocker1, blocker2, parent])
            project = window._add_project_from_data(
                "Test", {"holidays": [], "exclude_weekends": True},
                [blocker1, blocker2, parent])
            project.reset_history_baseline()

            conflicts = window.recheck_resource_conflicts().conflicts
            # Initial conflict on Room R between blocker1 and parent
            r_conflicts = [c for c in conflicts if c.resource_id == "room-r"]
            self.assertEqual(1, len(r_conflicts))

            with patch("ui.main_window.QMessageBox.question",
                       return_value=QMessageBox.StandardButton.Yes), \
                    patch("ui.main_window.usage_logger.timed_exec",
                          return_value=True):
                window._move_resource_to_next_available(project, parent, r_conflicts[0])

            # Moving parent must NOT move into Sept 14-18 because Case S is blocked on Sept 14-18!
            # It must move to Sept 21-25 (where both R and S are free)
            moved_parent = project.tree_view._find_item_by_id(parent.id).node
            self.assertGreaterEqual(moved_parent.start_date, "2026-09-21")
            remaining_conflicts = window.recheck_resource_conflicts().conflicts
            self.assertEqual([], remaining_conflicts,
                             f"Move created unexpected conflicts: {[c.resource_label for c in remaining_conflicts]}")

        finally:
            window.unsaved_changes = False
            window.close()

    def test_f13_parent_move_rejects_externally_locked_child(self):
        with patch.object(MainWindow, "_restore_startup_state", return_value=True):
            window = MainWindow()
        window._autosave_timer.stop()
        try:
            _enable_overlap(window, [{"id": "room-r", "kind": "room", "label": "Room R"}])

            # Blocker reserves Room R Sept 7-11
            blocker = TaskNode("Blocker R")
            blocker.start_date, blocker.end_date = "2026-09-07", "2026-09-11"
            blocker.start_rule = {"mode": "fixed", "date": "2026-09-07"}
            blocker.end_rule = {"mode": "duration", "days": 5}
            blocker.task_tokens = [{"id": "room-r", "label": "Room R", "kind": "room"}]

            # External task fixed on Sept 7
            external = TaskNode("External")
            external.start_date, external.end_date = "2026-09-07", "2026-09-11"
            external.start_rule = {"mode": "fixed", "date": "2026-09-07"}
            external.end_rule = {"mode": "duration", "days": 5}

            # Parent reserves Room R Sept 7-11
            parent = TaskNode("Parent")
            parent.task_tokens = [{"id": "room-r", "label": "Room R", "kind": "room"}]

            # Child is locked to External start
            child = TaskNode("Child", parent=parent)
            child.start_rule = {"mode": "same_as", "task_id": external.id, "field": "start"}
            child.end_rule = {"mode": "duration", "days": 5}
            parent.children = [child]

            schedule([blocker, external, parent])
            project = window._add_project_from_data(
                "Test", {"holidays": [], "exclude_weekends": True},
                [blocker, external, parent])
            project.reset_history_baseline()

            conflicts = window.recheck_resource_conflicts().conflicts
            self.assertEqual(1, len(conflicts))

            warnings = []
            with patch("ui.main_window.QMessageBox.warning",
                       side_effect=lambda *args: warnings.append(args)), \
                    patch("ui.main_window.QMessageBox.question",
                          return_value=QMessageBox.StandardButton.Yes), \
                    patch("ui.main_window.usage_logger.timed_exec",
                          return_value=True):
                window._move_resource_to_next_available(project, parent, conflicts[0])

            # Should warn that child is externally locked and cannot move
            self.assertTrue(len(warnings) > 0, "Expected warning about externally locked child")
        finally:
            window.unsaved_changes = False
            window.close()

    def test_f15_availability_block_isolates_supplied_calendar_from_active_profile(self):
        from utils.resource_allocation import ResourceAssignment, next_available_block
        from utils.config_manager import ConfigManager

        a1 = ResourceAssignment(
            id="a1", project_id="p1", project_name="P1", task_id="t1", task_name="T1", task_path="T1",
            resource_type="room", resource_id="r1", resource_label="Room 1", location_label="",
            start_date="2026-09-11", end_date="2026-09-11", occupied_dates=("2026-09-11",),
            capacity_units=1, status="Not Started", ancestor_task_ids=()
        )
        a2 = ResourceAssignment(
            id="a2", project_id="p1", project_name="P1", task_id="t2", task_name="T2", task_path="T2",
            resource_type="room", resource_id="r2", resource_label="Room 2", location_label="",
            start_date="2026-09-14", end_date="2026-09-14", occupied_dates=("2026-09-14",),
            capacity_units=1, status="Not Started", ancestor_task_ids=()
        )
        tasks = [a1, a2]

        config = ConfigManager()
        orig_exclude = config.get_exclude_weekends()
        orig_holidays = config.get_holidays()
        try:
            # 1. Test across opposing active profile exclude_weekends settings
            config.set_exclude_weekends(True)
            res_true = next_available_block(tasks, tasks, holidays=[], exclude_weekends=True)

            config.set_exclude_weekends(False)
            res_false = next_available_block(tasks, tasks, holidays=[], exclude_weekends=True)

            self.assertEqual(("2026-09-14", "2026-09-15"), res_true)
            self.assertEqual(res_true, res_false)

            # 2. Test with explicit supplied project holidays regardless of active profile
            res_holiday = next_available_block(tasks, tasks, holidays=["2026-09-15"], exclude_weekends=True)
            self.assertEqual(("2026-09-14", "2026-09-16"), res_holiday)
        finally:
            config.set_exclude_weekends(orig_exclude)
            config.set_holidays(orig_holidays)


if __name__ == "__main__":
    unittest.main()
