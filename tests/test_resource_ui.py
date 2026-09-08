import unittest
import os
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

    def test_metadata_rename_rebuilds_legacy_resource_projection(self):
        with patch.object(MainWindow, "_restore_startup_state", return_value=True):
            window = MainWindow()
        first = {"id": "room-one", "label": "Old Room", "kind": "room"}
        second = {"id": "room-two", "label": "Other Room", "kind": "room"}
        node = TaskNode("Scheduled work")
        node.start_date = node.end_date = "2026-09-01"
        node.task_tokens = [dict(first), dict(second)]
        node.resources = {"room": "Old Room", "legacy-kind": "Keep Me"}
        try:
            project = window._add_project_from_data("P", {}, [node], project_id="P")
            project.reset_history_baseline()

            count = window._sync_tasks_to_metadata([
                {"id": "room-one", "name": "New Room", "kind": "room"},
                {"id": "room-two", "name": "Other Room", "kind": "room"},
            ])

            current = project.tree_view.root_nodes[0]
            self.assertEqual(1, count)
            self.assertEqual(["room-one", "room-two"],
                             [token["id"] for token in current.task_tokens])
            self.assertEqual(["New Room", "Other Room"],
                             [token["label"] for token in current.task_tokens])
            self.assertEqual({"room": "Other Room", "legacy-kind": "Keep Me"},
                             current.resources)
            assignments = analyze_projects(
                [_project("P", "P", [current])], attach=False).assignments
            self.assertEqual({("room-one", "New Room"),
                              ("room-two", "Other Room"),
                              ("legacy:legacy-kind:keep me", "Keep Me")},
                             {(value.resource_id, value.resource_label)
                              for value in assignments})

            reloaded = TaskNode.from_dict(current.to_dict())
            self.assertEqual(current.task_tokens, reloaded.task_tokens)
            self.assertEqual(current.resources, reloaded.resources)

            project.undo()
            restored = project.tree_view.root_nodes[0]
            self.assertEqual("Old Room", restored.task_tokens[0]["label"])
            self.assertEqual("Old Room", restored.resources["room"])
            project.redo()
            redone = project.tree_view.root_nodes[0]
            self.assertEqual("New Room", redone.task_tokens[0]["label"])
            self.assertEqual("Other Room", redone.resources["room"])
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

    def test_f05_empty_local_metadata_preserves_document_conflict_enabled(self):
        with patch.object(MainWindow, "_restore_startup_state", return_value=True):
            window = MainWindow()
        window._metadata_resource_definitions = lambda: []
        window.resource_definitions = [
            {"id": "room-1", "type": "room", "label": "Room 1", "capacity": 1,
             "conflict_enabled": True, "active": True}
        ]
        first = _task("A", "2026-09-01", "2026-09-03", "1")
        second = _task("B", "2026-09-02", "2026-09-04", "1")
        try:
            window._add_project_from_data("P", {}, [first, second], project_id="P")
            result = window.recheck_resource_conflicts()
            # Must preserve conflict_enabled=True and report the overlap conflict
            room_def = next(d for d in result.definitions if d.id == "room-1")
            self.assertTrue(room_def.conflict_enabled)
            room_conflicts = [c for c in result.conflicts if c.resource_id == "room-1"]
            self.assertEqual(1, len(room_conflicts))
            self.assertEqual(2, room_conflicts[0].overlap_workdays)
        finally:
            window.unsaved_changes = False
            window.close()

    def test_f06_inactive_definitions_not_reintroduced_into_lists(self):
        with patch.object(MainWindow, "_restore_startup_state", return_value=True):
            window = MainWindow()
        window.resource_definitions = [
            {"id": "room-del", "type": "room", "label": "Deleted Room", "capacity": 1,
             "conflict_enabled": True, "active": False}
        ]
        captured_templates = []
        def mock_dialog(templates, **kwargs):
            captured_templates.extend(templates)
            from unittest.mock import MagicMock
            d = MagicMock()
            d.result_items.return_value = []
            return d

        with patch("utils.template_catalog.load_templates", return_value=[]), \
             patch("ui.metadata_editor.MetadataEditorDialog", side_effect=mock_dialog), \
             patch("ui.main_window.usage_logger.timed_exec", return_value=False):
            window.open_template_manager()

        self.assertFalse(any(t.get("id") == "room-del" for t in captured_templates))
        window.unsaved_changes = False
        window.close()

    def test_f10_independent_recoveries_not_dismissed_by_newer_decision(self):
        import tempfile
        from utils.vpmt_io import save_projects

        with tempfile.TemporaryDirectory() as folder:
            rec_new = os.path.join(folder, "recovery-new-1234.vpmt")
            rec_old = os.path.join(folder, "recovery-old-5678.vpmt")
            task_new = _task("New Recovery", "2026-09-01", "2026-09-02", "1")
            task_old = _task("Old Recovery", "2026-09-01", "2026-09-02", "2")
            save_projects([_project("P1", "P1", [task_new])], rec_new)
            save_projects([_project("P2", "P2", [task_old])], rec_old)

            os.utime(rec_old, (1000.0, 1000.0))
            os.utime(rec_new, (2000.0, 2000.0))

            fake_settings = {}
            class IsolatedSettings:
                def value(self, key, default=None):
                    return fake_settings.get(key, default)
                def setValue(self, key, val):
                    fake_settings[key] = val

            # Session 1: Both candidates offered independently; user declines rec_new, accepts rec_old
            with patch.object(MainWindow, "_restore_startup_state", return_value=True):
                window = MainWindow()
            window.settings = IsolatedSettings()
            window._recent_files = lambda: []
            window._loading_startup = True
            window._recovery_candidates = lambda: [rec_new, rec_old]

            dialog_answers = [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes]
            with patch("ui.main_window.QMessageBox.question", side_effect=dialog_answers) as mock_q:
                restored = MainWindow._restore_startup_state(window)
                self.assertTrue(restored)
                self.assertEqual(2, mock_q.call_count)
                self.assertEqual("P2", window.all_projects()[0].project_id)
            window.unsaved_changes = False
            window.close()

            # Session 2: If rec_new was handled, older rec_old is NOT dismissed if it hasn't been handled yet
            fake_settings.clear()
            fake_settings["recovery_handled_map"] = {"recovery-new-1234.vpmt": 2000.0}

            with patch.object(MainWindow, "_restore_startup_state", return_value=True):
                window2 = MainWindow()
            window2.settings = IsolatedSettings()
            window2._recent_files = lambda: []
            window2._loading_startup = True
            window2._recovery_candidates = lambda: [rec_new, rec_old]

            with patch("ui.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes) as mock_q2:
                restored2 = MainWindow._restore_startup_state(window2)
                self.assertTrue(restored2)
                self.assertEqual(1, mock_q2.call_count)
                self.assertEqual("P2", window2.all_projects()[0].project_id)
            window2.unsaved_changes = False
            window2.close()

            # Session 3: Failed load does not mark recovery handled
            rec_bad = os.path.join(folder, "corrupt.vpmt")
            with open(rec_bad, "w") as f:
                f.write("corrupt")
            fake_settings.clear()

            with patch.object(MainWindow, "_restore_startup_state", return_value=True):
                window3 = MainWindow()
            window3.settings = IsolatedSettings()
            window3._recent_files = lambda: []
            window3._loading_startup = True
            window3._recovery_candidates = lambda: [rec_bad]

            with patch("ui.main_window.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
                 patch("ui.main_window.QMessageBox.critical"):
                restored3 = MainWindow._restore_startup_state(window3)
                self.assertFalse(restored3)
                self.assertNotIn("corrupt.vpmt", fake_settings.get("recovery_handled_map", {}))
            window3.unsaved_changes = False
            window3.close()

    def test_f11_external_change_autosave_preserves_original_and_saves_emergency_recovery(self):
        import tempfile
        from utils.vpmt_io import save_projects, load_projects

        with tempfile.TemporaryDirectory() as folder:
            original_file = os.path.join(folder, "original.vpmt")
            rec_file = os.path.join(folder, "emergency_recovery.vpmt")
            init_task = _task("Initial", "2026-09-01", "2026-09-02", "1")
            save_projects([_project("P", "P", [init_task])], original_file)

            with patch.object(MainWindow, "_restore_startup_state", return_value=True):
                window = MainWindow()

            window._recovery_path = lambda: rec_file
            window.current_filepath = original_file
            # Simulate project loaded
            window._add_project_from_data("P", {}, [init_task], project_id="P")
            # In-memory edit
            window.all_projects()[0].tree_view.root_nodes[0].name = "Modified In Memory"
            window.unsaved_changes = True

            # Mock file_guard to report external change
            window.file_guard.changed_on_disk = lambda: True

            window._autosave()

            # 1. Original file must be completely untouched
            loaded_orig = load_projects(original_file)
            self.assertEqual("Initial", loaded_orig[0]["roots"][0].name)

            # 2. Recovery file must have been created and contain the pending edit
            self.assertTrue(os.path.exists(rec_file))
            loaded_rec = load_projects(rec_file)
            self.assertEqual("Modified In Memory", loaded_rec[0]["roots"][0].name)

            # 3. Status bar discloses emergency recovery
            self.assertIn("emergency recovery snapshot saved", window.statusBar().currentMessage())

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
