import unittest
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from models.task_node import TaskNode
from ui.main_window import MainWindow
from ui.resource_usage_dialog import ResourceSettingsDialog, ResourceUsageDialog
from utils.resource_allocation import ResourceDefinition, analyze_projects
from utils.resource_allocation import accept_conflict


def _task(name, start, end, room, status="Not Started"):
    node = TaskNode(name)
    node.start_date, node.end_date = start, end
    node.start_rule = {"mode": "fixed", "date": start}
    node.end_rule = {"mode": "fixed", "date": end}
    node.status = status
    node.task_tokens = [
        {"id": "case-1", "label": "RLN2-1", "kind": "case"},
        {"id": f"room-{room}", "label": room, "kind": "room"},
    ]
    return node


def _project(project_id, name, roots):
    return {"id": project_id, "name": name, "roots": roots,
            "metadata": {"holidays": [], "exclude_weekends": True}}


def _enable_overlap(window, resource_id="case-1", kind="case", label="RLN2-1"):
    """Configure the Metadata checkbox for tests that exercise warning flows."""
    window._metadata_resource_definitions = lambda: [ResourceDefinition(
        resource_id, kind, label, conflict_enabled=True).to_dict()]


class ResourceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_rundown_filters_timeline_and_jump_use_stable_ids(self):
        first = _task("Duplicate", "2026-09-01", "2026-09-10", "Room 1")
        second = _task("Duplicate", "2026-09-07", "2026-09-14", "Room 3")
        completed = _task("History", "2026-08-01", "2026-08-03", "Room 1",
                          status="Completed")
        analysis = analyze_projects([
            _project("P1", "Alpha", [first, completed]),
            _project("P2", "Beta", [second]),
        ])
        dialog = ResourceUsageDialog(analysis, "case-1")
        try:
            self.assertEqual("case-1", dialog.resource_filter.currentData())
            self.assertEqual(2, dialog.details.rowCount())
            self.assertEqual(1, dialog.summary.rowCount())
            self.assertTrue(dialog.timeline.conflicts)
            self.assertFalse(any(dialog.details.item(row, 2).text() == "History"
                                 for row in range(dialog.details.rowCount())))
            dialog.include_completed.setChecked(True)
            self.assertEqual(3, dialog.details.rowCount())
            captured = []
            dialog.jumpRequested.connect(lambda project, task:
                                         captured.append((project, task)))
            dialog._jump(dialog.details.item(0, 0))
            self.assertEqual("P1", captured[0][0])
            self.assertEqual(first.id, captured[0][1])
        finally:
            dialog.close()

    def test_metadata_checkbox_overrides_old_automatic_conflict_value(self):
        with patch.object(MainWindow, "_restore_startup_state", return_value=True):
            window = MainWindow()
        first = _task("A", "2026-09-01", "2026-09-03", "Room 1")
        second = _task("B", "2026-09-02", "2026-09-04", "Room 3")
        try:
            window.resource_definitions = [ResourceDefinition(
                "case-1", "case", "RLN2-1",
                conflict_enabled=True).to_dict()]
            window._metadata_resource_definitions = lambda: [ResourceDefinition(
                "case-1", "case", "RLN2-1",
                conflict_enabled=False).to_dict()]
            window._add_project_from_data("P", {}, [first, second],
                                          project_id="P")
            unchecked = window.recheck_resource_conflicts()
            self.assertFalse(unchecked.conflicts)
            self.assertEqual(4, len(unchecked.assignments))

            _enable_overlap(window)
            checked = window.recheck_resource_conflicts()
            self.assertEqual(["case-1"],
                             [value.resource_id for value in checked.conflicts])
        finally:
            window.unsaved_changes = False
            window.close()

    def test_legacy_resource_settings_synchronize_metadata_checkbox(self):
        items = [{
            "id": "room-1", "name": "Room 1", "kind": "room",
            "header": "Rooms", "flag_overlaps": False,
        }]
        definitions = [ResourceDefinition(
            "room-1", "room", "Room 1", conflict_enabled=True)]
        with patch("utils.template_catalog.load_templates",
                   return_value=items), \
                patch("utils.template_catalog.save_templates") as save:
            MainWindow._sync_overlap_flags_to_metadata_catalog(definitions)
        self.assertTrue(items[0]["flag_overlaps"])
        save.assert_called_once_with(items)

    def test_resource_settings_edits_capacity_and_locks_phase_conflicts(self):
        definitions = [
            ResourceDefinition("case-1", "case", "C"),
            ResourceDefinition("phase-1", "phase", "Design",
                               conflict_enabled=False),
        ]
        dialog = ResourceSettingsDialog(definitions)
        try:
            dialog.table.item(0, 2).setText("2")
            values = dialog.result_definitions()
            self.assertEqual(2, values[0].capacity)
            self.assertFalse(values[1].conflict_enabled)
            self.assertFalse(dialog.table.item(1, 3).flags() &
                             Qt.ItemFlag.ItemIsEnabled)
        finally:
            dialog.close()

    def test_main_window_analyzes_all_tabs_refreshes_and_marks_grid_health(self):
        with patch.object(MainWindow, "_restore_startup_state", return_value=True):
            window = MainWindow()
        _enable_overlap(window)
        first = _task("Task A", "2026-09-01", "2026-09-10", "Room 1")
        second = _task("Task B", "2026-09-07", "2026-09-14", "Room 3")
        try:
            p1 = window._add_project_from_data(
                "Alpha", _project("P1", "Alpha", [first])["metadata"],
                [first], project_id="P1")
            p2 = window._add_project_from_data(
                "Beta", _project("P2", "Beta", [second])["metadata"],
                [second], project_id="P2")
            result = window.recheck_resource_conflicts()
            case_conflict = next(c for c in result.conflicts
                                 if c.resource_id == "case-1")
            self.assertEqual(4, case_conflict.overlap_workdays)
            self.assertIn("⚠", p1.tree_view.topLevelItem(0).text(0))
            self.assertTrue(any("Resource conflict" in text
                                for text, _node in window._data_health_issues(p1)))
            self.assertIn("(", window.resource_usage_action.text())

            window._refresh_all()
            self.assertIn("resource assignments", window.statusBar().currentMessage())
            self.assertEqual("2026-09-01", first.start_date)
            self.assertEqual("2026-09-07", second.start_date)
        finally:
            window.unsaved_changes = False
            window.close()

    def test_undo_redo_recomputes_conflict_state(self):
        with patch.object(MainWindow, "_restore_startup_state", return_value=True):
            window = MainWindow()
        _enable_overlap(window)
        first = _task("A", "2026-09-01", "2026-09-03", "Room 1")
        second = _task("B", "2026-09-02", "2026-09-04", "Room 3")
        try:
            project = window._add_project_from_data(
                "P", {}, [first, second], project_id="P")
            project.reset_history_baseline()
            self.assertTrue(window.recheck_resource_conflicts().unresolved)
            second.start_date, second.end_date = "2026-09-10", "2026-09-11"
            second.start_rule = {"mode": "fixed", "date": second.start_date}
            second.end_rule = {"mode": "fixed", "date": second.end_date}
            project.tree_view.item_changed_signal.emit(second)
            self.assertFalse(window.recheck_resource_conflicts().unresolved)
            project.undo()
            self.assertTrue(window.recheck_resource_conflicts().unresolved)
            project.redo()
            self.assertFalse(window.recheck_resource_conflicts().unresolved)
        finally:
            window.unsaved_changes = False
            window.close()

    def test_conflicting_assignment_requests_actionable_warning(self):
        with patch.object(MainWindow, "_restore_startup_state", return_value=True):
            window = MainWindow()
        _enable_overlap(window)
        first = _task("A", "2026-09-01", "2026-09-03", "Room 1")
        second = _task("B", "2026-09-02", "2026-09-04", "Room 3")
        try:
            project = window._add_project_from_data("P", {}, [first, second],
                                                    project_id="P")
            with patch.object(window, "_show_assignment_conflict") as warning:
                window._resource_assignment_changed(
                    project, second, {"tokens": [], "name": "B"})
            warning.assert_called_once()
            conflicts = warning.call_args.args[3]
            self.assertTrue(any(c.resource_id == "case-1" for c in conflicts))
        finally:
            window.unsaved_changes = False
            window.close()

    def test_full_acceptance_overlap_move_and_undo(self):
        with patch.object(MainWindow, "_restore_startup_state", return_value=True):
            window = MainWindow()
        _enable_overlap(window)
        first = _task("DOE Type 1", "2026-09-08", "2026-10-05", "Room 1")
        second = _task("DOE Type 2", "2026-09-24", "2026-10-21", "Room 3")
        second.baseline_duration = 20
        second.baseline_end = second.end_date
        before = (second.start_date, second.end_date,
                  dict(second.start_rule), second.baseline_duration,
                  list(second.revisions))
        try:
            project = window._add_project_from_data(
                "Acceptance", {}, [first, second], project_id="P")
            project.reset_history_baseline()
            result = window.recheck_resource_conflicts()
            conflict = next(c for c in result.conflicts
                            if c.resource_id == "case-1")
            self.assertEqual(8, conflict.overlap_workdays)
            warning = window._resource_conflict_warning_text([conflict])
            for expected in ("DOE Type 1", "DOE Type 2", "Room 1", "Room 3",
                             "8 overlapping workday"):
                self.assertIn(expected, warning)
            self.assertEqual(before, (
                second.start_date, second.end_date, second.start_rule,
                second.baseline_duration, second.revisions))

            with patch("ui.main_window.QMessageBox.question",
                       return_value=QMessageBox.StandardButton.Yes), \
                    patch("ui.main_window.usage_logger.timed_exec",
                          return_value=True):
                window._move_resource_to_next_available(project, second, conflict)
            moved = project.tree_view._find_item_by_id(second.id).node
            self.assertGreater(moved.start_date, first.end_date)
            self.assertEqual(20, moved.baseline_duration)
            self.assertEqual([], moved.revisions)
            self.assertFalse(any(c.resource_id == "case-1"
                                 for c in window.recheck_resource_conflicts().conflicts))

            project.undo()
            restored = project.tree_view.get_all_nodes_flat()[1]
            self.assertEqual("2026-09-24", restored.start_date)
            self.assertTrue(any(c.resource_id == "case-1"
                                for c in window.recheck_resource_conflicts().conflicts))
        finally:
            window.unsaved_changes = False
            window.close()

    def test_clear_replace_and_duration_changes_recompute_conflicts(self):
        with patch.object(MainWindow, "_restore_startup_state", return_value=True):
            window = MainWindow()
        _enable_overlap(window)
        first = _task("A", "2026-09-01", "2026-09-10", "Room 1")
        second = _task("B", "2026-09-07", "2026-09-08", "Room 3")
        try:
            project = window._add_project_from_data("P", {}, [first, second],
                                                    project_id="P")
            initial = next(c for c in window.recheck_resource_conflicts().conflicts
                           if c.resource_id == "case-1")
            self.assertEqual(2, initial.overlap_workdays)

            second.end_rule = {"mode": "duration", "days": 5}
            project.tree_view.commit_structure_change(second)
            extended = next(c for c in window.recheck_resource_conflicts().conflicts
                            if c.resource_id == "case-1")
            self.assertEqual(4, extended.overlap_workdays)

            second.task_tokens[0] = {
                "id": "case-2", "label": "RLN2-2", "kind": "case"}
            self.assertFalse(window.recheck_resource_conflicts().conflicts)
            second.task_tokens = []
            second.resources = {}
            self.assertFalse(window.recheck_resource_conflicts().conflicts)
        finally:
            window.unsaved_changes = False
            window.close()

    def test_metadata_timeline_update_changes_resource_conflict_interval(self):
        with patch.object(MainWindow, "_restore_startup_state", return_value=True):
            window = MainWindow()
        _enable_overlap(window, "doe", "activity", "DOE")
        activity = {"id": "doe", "label": "DOE", "kind": "activity",
                    "duration": 2, "header": "Lab Testing"}
        first = TaskNode("DOE A")
        first.start_rule = {"mode": "fixed", "date": "2026-09-01"}
        first.end_rule = {"mode": "duration", "days": 2}
        first.task_tokens = [dict(activity)]
        second = TaskNode("DOE B")
        second.start_rule = {"mode": "fixed", "date": "2026-09-02"}
        second.end_rule = {"mode": "duration", "days": 2}
        second.task_tokens = [dict(activity)]
        try:
            project = window._add_project_from_data("P", {}, [first, second],
                                                    project_id="P")
            project.tree_view.recalculate_all_dates()
            before = next(c for c in window.recheck_resource_conflicts().conflicts
                          if c.resource_id == "doe")
            self.assertEqual(1, before.overlap_workdays)
            window._sync_tasks_to_metadata([{
                "id": "doe", "name": "DOE", "kind": "activity",
                "duration": 5, "header": "Lab Testing"}])
            after = next(c for c in window.recheck_resource_conflicts().conflicts
                         if c.resource_id == "doe")
            self.assertEqual(4, after.overlap_workdays)
        finally:
            window.unsaved_changes = False
            window.close()

    def test_inactive_resource_is_hidden_from_picker_but_existing_use_is_audited(self):
        from ui.tree_grid_view import TreeGridView
        from utils.resource_allocation import legacy_resource_id
        tree = TreeGridView()
        resource_id = legacy_resource_id("case", "Unique Legacy Case")
        tree.resource_lists["case"] = ["Unique Legacy Case"]
        tree.resource_definitions = {
            resource_id: ResourceDefinition(
                resource_id, "case", "Unique Legacy Case", active=False)}
        try:
            self.assertNotIn("Unique Legacy Case",
                             [value["label"] for value in tree._task_lookup_options()])
            existing = TaskNode("Existing")
            existing.start_date = existing.end_date = "2026-09-01"
            existing.resources = {"case": "Unique Legacy Case"}
            result = analyze_projects([_project("P", "P", [existing])],
                                      list(tree.resource_definitions.values()))
            self.assertEqual(1, len(result.assignments))
        finally:
            tree.close()

    def test_cancel_assignment_restores_previous_stable_token_and_name(self):
        with patch.object(MainWindow, "_restore_startup_state", return_value=True):
            window = MainWindow()
        node = _task("New Case", "2026-09-01", "2026-09-02", "Room 1")
        previous_token = {"id": "case-old", "label": "Old Case", "kind": "case"}
        try:
            project = window._add_project_from_data("P", {}, [node], project_id="P")
            window._restore_resource_assignment(
                project, node,
                {"tokens": [previous_token], "name": "Original work"})
            self.assertEqual([previous_token], node.task_tokens)
            self.assertEqual("Old Case", node.resources["case"])
            self.assertEqual("Original work", node.name)
            self.assertEqual("[Old Case] Original work",
                             project.tree_view.topLevelItem(0).text(0))
        finally:
            window.unsaved_changes = False
            window.close()

    def test_accepted_conflict_remains_visible_but_not_unresolved(self):
        with patch.object(MainWindow, "_restore_startup_state", return_value=True):
            window = MainWindow()
        _enable_overlap(window)
        first = _task("A", "2026-09-01", "2026-09-03", "Room 1")
        second = _task("B", "2026-09-02", "2026-09-04", "Room 3")
        try:
            project = window._add_project_from_data("P", {}, [first, second],
                                                    project_id="P")
            initial = window.recheck_resource_conflicts()
            resolutions = [accept_conflict(value, "Controlled shared setup")
                           for value in initial.conflicts]
            window.resource_conflict_resolutions = resolutions
            accepted = window.recheck_resource_conflicts()
            self.assertFalse(accepted.unresolved)
            self.assertTrue(accepted.conflicts)
            self.assertIn("✓", project.tree_view.topLevelItem(0).text(0))
            issues = [text for text, _node in window._data_health_issues(project)]
            self.assertTrue(any("(accepted)" in text for text in issues))
        finally:
            window.unsaved_changes = False
            window.close()


if __name__ == "__main__":
    unittest.main()
