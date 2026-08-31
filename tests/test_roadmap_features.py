import os
import json
from datetime import datetime, timedelta
import tempfile
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import (QApplication, QLineEdit, QMessageBox, QPushButton,
                             QTabWidget)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtTest import QTest

from models.task_node import TaskNode
from ui.dialogs import ImpactReviewDialog
from ui.project_widget import ProjectWidget
from ui.tree_grid_view import TreeGridView
from ui.inline_task_editor import InlineTaskEditor
from utils.scheduler import schedule
from utils.template_catalog import load_templates
from utils.vpmt_io import load_projects, save_projects
from utils.config_manager import ConfigManager


class RoadmapFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _fixed(self, name, start, days, resource=None):
        node = TaskNode(name)
        node.start_rule = {"mode": "fixed", "date": start}
        node.end_rule = {"mode": "duration", "days": days}
        node.resources = dict(resource or {})
        return node

    def test_normal_impact_review_has_no_delay_input(self):
        dialog = ImpactReviewDialog({"task_name": "T", "delay_slip": 10})
        try:
            self.assertFalse(hasattr(dialog, "delay_reason"))
            self.assertFalse(hasattr(dialog, "_reason_edit"))
        finally:
            dialog.close()

    def test_template_catalog_and_inline_picker_include_phases_tests_campaign(self):
        kinds = {item["kind"] for item in load_templates()}
        self.assertTrue({"phase", "activity", "campaign"} <= kinds)
        tree = TreeGridView()
        try:
            options = tree._task_lookup_options()
            self.assertTrue(any(item["kind"] == "phase" for item in options))
            self.assertTrue(any(item["kind"] == "activity" for item in options))
        finally:
            tree.close()

    def test_resource_metadata_does_not_change_fixed_dates(self):
        first = self._fixed("First", "2026-09-01", 3, {"room": "R1"})
        second = self._fixed("Second", "2026-09-02", 2, {"room": "R1"})
        schedule([first, second])
        self.assertEqual("2026-09-02", second.start_date)
        self.assertEqual([], second.schedule_conflicts)

    def test_resource_without_timeline_does_not_move_related_task(self):
        blocker = self._fixed("Blocker", "2026-09-01", 3, {"case": "C1"})
        predecessor = self._fixed("Pred", "2026-08-31", 1)
        movable = TaskNode("Movable")
        movable.start_rule = {"mode": "continue_after", "task_id": predecessor.id,
                              "field": "end", "offset": 0,
                              "offset_unit": "workdays"}
        movable.end_rule = {"mode": "duration", "days": 1}
        movable.resources = {"case": "C1"}
        schedule([blocker, predecessor, movable])
        self.assertEqual("2026-09-01", movable.start_date)
        self.assertEqual("", movable.schedule_reason)

    def test_vave_money_is_never_inferred_and_fields_round_trip(self):
        node = TaskNode("VAVE")
        node.status = "Completed"
        node.vave_potential = 500
        self.assertEqual(0, node.vave_total_realized())
        self.assertIsNone(node.vave_display_realized())
        node.vave_stage = "Savings Verified"
        node.vave_realized = 125
        node.savings_disposition = "Verified amount entered"
        restored = TaskNode.from_dict(node.to_dict())
        self.assertEqual("Savings Verified", restored.vave_stage)
        self.assertEqual(125, restored.vave_realized)
        self.assertTrue(restored.savings_disposition)

    def test_project_extras_and_stable_id_persist(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "p.vpmt")
            save_projects([{
                "id": "proj-stable", "name": "P", "metadata": {}, "roots": [],
                "reviewed_through": "2026-08-29",
                "note_tabs": [{"name": "Risks", "html": "risk"}],
                "resources": {"room": ["R1"]},
            }], path)
            loaded = load_projects(path)[0]
            self.assertEqual("proj-stable", loaded["id"])
            self.assertEqual("2026-08-29", loaded["reviewed_through"])
            self.assertEqual("Risks", loaded["note_tabs"][0]["name"])
            self.assertEqual(["R1"], loaded["resources"]["room"])

    def test_enter_adds_blank_row_and_immediately_edits_it(self):
        tree = TreeGridView()
        try:
            root = TaskNode("Existing")
            tree.load_project([root])
            tree.add_sibling_below(tree.topLevelItem(0))
            self.app.processEvents()
            self.assertEqual("", tree.root_nodes[1].name)
            self.assertEqual(tree.state(), tree.State.EditingState)
        finally:
            tree.close()

    def test_project_snapshot_keeps_custom_notes_and_resources(self):
        project = ProjectWidget("P", roots=[], note_tabs=[
            {"name": "Meetings", "html": "<p>hello</p>"}],
            resources={"case": ["C1"]}, project_id="proj-one")
        try:
            snapshot = project.to_persistable()
            self.assertEqual("proj-one", snapshot["id"])
            self.assertEqual("Meetings", snapshot["note_tabs"][0]["name"])
            self.assertEqual(["C1"], snapshot["resources"]["case"])
        finally:
            project.close()

    def test_real_delay_records_variance_without_changing_dates_or_baseline(self):
        tree = TreeGridView()
        try:
            node = TaskNode("Late")
            node.start_date = "2026-09-01"
            node.end_date = "2026-09-07"
            node.baseline_duration = 3
            before = (node.start_date, node.end_date, node.baseline_duration)
            def accept_with_reason(dialog, _name):
                dialog.findChild(QLineEdit).setText("Vendor delay")
                return 1
            with patch("ui.tree_grid_view.usage_logger.timed_exec",
                       side_effect=accept_with_reason):
                tree.record_real_delay(node)
            self.assertEqual(before,
                             (node.start_date, node.end_date, node.baseline_duration))
            self.assertEqual(2, node.revisions[-1]["variance"])
            self.assertEqual("Vendor delay", node.revisions[-1]["reason"])
        finally:
            tree.close()

    def test_escape_from_task_notes_clears_task_context(self):
        tree = TreeGridView()
        try:
            node = TaskNode("Task")
            tree.load_project([node])
            item = tree.topLevelItem(0)
            item.setSelected(True)
            def escape(dialog, _name):
                dialog.setProperty("clear_task_note_context", True)
                return 0
            delegate = tree.itemDelegateForColumn(9)
            with patch("ui.tree_grid_view.usage_logger.timed_exec", side_effect=escape):
                delegate.open_dialog(tree.indexFromItem(item, 9))
            self.assertEqual([], tree.selectedItems())
        finally:
            tree.close()

    def test_inline_lookup_filters_headers_and_options_and_stays_active(self):
        tree = TreeGridView()
        # Use fixed fixtures: the user's editable metadata catalog is allowed
        # to rename/remove DOE and must not make this interaction test flaky.
        editor = InlineTaskEditor([
            {"id": "phase-lab", "label": "Lab Testing", "kind": "phase",
             "duration": 1, "header": "Project Phases"},
            {"id": "test-doe", "label": "DOE", "kind": "activity",
             "duration": 10, "header": "Lab Testing"},
            {"id": "room-1", "label": "Room 1", "kind": "room",
             "duration": None, "header": "Rooms"},
        ])
        try:
            editor.start_lookup()
            for char in "lab":
                QTest.keyClicks(editor, char)
            self.assertTrue(editor.text().endswith("=lab"))
            visible = [editor.results.item(i).text().strip()
                       for i in range(editor.results.count())]
            self.assertIn("Lab Testing", visible)
            self.assertIn("DOE  · 10 days", visible)
            doe = next(editor.results.item(i) for i in range(editor.results.count())
                       if editor.results.item(i).text().strip().startswith("DOE"))
            editor._choose_item(doe)
            self.assertFalse(editor.lookup_active)
            self.assertIn("[DOE]", editor.text())
            editor.start_lookup()
            self.assertTrue(editor.lookup_active)
        finally:
            editor.close()
            tree.close()

    def test_inline_lookup_replaces_query_with_selected_token(self):
        editor = InlineTaskEditor([
            {"id": "phase-lab", "label": "Lab Testing", "kind": "phase",
             "duration": 1, "header": "Project Phases"},
            {"id": "test-doe", "label": "DOE", "kind": "activity",
             "duration": 10, "header": "Lab Testing"},
        ])
        try:
            editor.show()
            editor.setFocus()
            editor.start_lookup()
            QTest.keyClicks(editor, "la")
            self.app.processEvents()
            choice = next(
                editor.results.item(i) for i in range(editor.results.count())
                if editor.results.item(i).text().strip() == "Lab Testing")
            QTest.mouseClick(
                editor.results.viewport(), Qt.MouseButton.LeftButton,
                pos=editor.results.visualItemRect(choice).center())
            self.app.processEvents()
            self.assertNotIn("=la", editor.text())
            self.assertEqual("[Lab Testing]", editor.text().strip())
            self.assertEqual(len(editor.text()), editor.cursorPosition())
        finally:
            editor.close()

    def test_tree_mouse_pick_of_lab_header_commits_nonempty_token(self):
        tree = TreeGridView()
        node = TaskNode("")
        try:
            tree.resize(800, 400)
            tree.load_project([node])
            tree.show()
            tree.setCurrentItem(tree.topLevelItem(0), 0)
            tree.setFocus()
            QTest.keyClicks(tree, "=")
            self.app.processEvents()
            editor = QApplication.focusWidget()
            self.assertIsInstance(editor, InlineTaskEditor)
            QTest.keyClicks(editor, "lab")
            self.app.processEvents()
            choice = next(
                editor.results.item(i) for i in range(editor.results.count())
                if editor.results.item(i).text().strip() == "Lab Testing")
            QTest.mouseClick(
                editor.results.viewport(), Qt.MouseButton.LeftButton,
                pos=editor.results.visualItemRect(choice).center())
            self.app.processEvents()
            self.assertEqual("[Lab Testing]", editor.text().strip())
            QTest.keyClick(editor, Qt.Key.Key_Return)
            self.app.processEvents()
            self.app.processEvents()
            self.assertEqual("Lab Testing", node.name)
            self.assertEqual("Lab Testing", node.task_tokens[0]["label"])
        finally:
            tree.close()

    def test_windows_focus_out_before_lab_press_cannot_commit_blank(self):
        """Reproduce the native Windows order: focus-out commit, then press."""
        tree = TreeGridView()
        node = TaskNode("")
        try:
            tree.load_project([node])
            item = tree.topLevelItem(0)
            index = tree.indexFromItem(item, 0)
            delegate = tree.itemDelegateForColumn(0)
            editor = delegate.createEditor(tree.viewport(), None, index)
            delegate.setEditorData(editor, index)
            editor.start_lookup()
            QTest.keyClicks(editor, "lab")
            self.app.processEvents()
            choice = next(
                editor.results.item(i) for i in range(editor.results.count())
                if editor.results.item(i).text().strip() == "Lab Testing")

            # This is the premature setModelData call seen in production.
            delegate.setModelData(editor, tree.model(), index)
            self.assertEqual("", node.name)
            self.assertEqual([], node.task_tokens)

            # The later popup press must now apply by stable node id.
            editor._choose_item(choice)
            self.assertEqual("Lab Testing", node.name)
            self.assertEqual("Lab Testing", node.task_tokens[0]["label"])
            self.assertEqual("[Lab Testing]", item.text(0))
        finally:
            tree.close()

    def test_deleting_entire_token_cell_removes_hidden_connection(self):
        tree = TreeGridView()
        node = TaskNode("Lab Testing")
        node.task_tokens = [{
            "id": "phase-lab", "label": "Lab Testing", "kind": "phase",
            "duration": 1, "header": "Project Phases",
        }]
        node.template_snapshot = {"id": "phase-lab", "name": "Lab Testing"}
        try:
            tree.load_project([node])
            item = tree.topLevelItem(0)
            index = tree.indexFromItem(item, 0)
            delegate = tree.itemDelegateForColumn(0)
            editor = delegate.createEditor(tree.viewport(), None, index)
            delegate.setEditorData(editor, index)
            editor.selectAll()
            QTest.keyClick(editor, Qt.Key.Key_Delete)
            delegate.setModelData(editor, tree.model(), index)
            self.assertEqual("", node.name)
            self.assertEqual([], node.task_tokens)
            self.assertEqual({}, node.template_snapshot)
            self.assertEqual("", item.text(0))
        finally:
            tree.close()

    def test_full_cell_selection_replaces_lab_token_with_new_connection(self):
        editor = InlineTaskEditor([
            {"id": "phase-lab", "label": "Lab Testing", "kind": "phase",
             "duration": 1, "header": "Project Phases"},
            {"id": "test-doe", "label": "DOE", "kind": "activity",
             "duration": 10, "header": "Lab Testing"},
        ])
        try:
            editor.set_value("", [{
                "id": "phase-lab", "label": "Lab Testing", "kind": "phase",
                "duration": 1, "header": "Project Phases",
            }])
            editor.selectAll()
            event = QKeyEvent(
                QKeyEvent.Type.KeyPress, Qt.Key.Key_Equal,
                Qt.KeyboardModifier.NoModifier, "=")
            self.assertTrue(editor.handle_lookup_key(event))
            self.assertEqual([], editor.tokens)
            self.assertEqual("", editor.lookup_base_text)
            QTest.keyClicks(editor, "doe")
            choice = next(
                editor.results.item(i) for i in range(editor.results.count())
                if editor.results.item(i).text().strip().startswith("DOE"))
            editor._choose_item(choice)
            self.assertEqual(["DOE"], [token["label"] for token in editor.tokens])
            self.assertEqual("[DOE]", editor.text().strip())
        finally:
            editor.close()

    def test_metadata_editor_has_editable_lists_and_plus_tab(self):
        from ui.metadata_editor import MetadataEditorDialog
        dialog = MetadataEditorDialog([
            {"id": "one", "name": "Idea", "kind": "phase",
             "header": "Project Phases", "duration": 1},
            {"id": "two", "name": "DOE", "kind": "activity",
             "header": "Lab Testing", "duration": 10},
        ])
        try:
            dialog.show()
            self.app.processEvents()
            self.assertEqual(6, dialog.tabs.count())
            self.assertEqual(
                ["Project Phases", "Lab Testing", "Rooms", "Cases",
                 "Cassettes / Test Articles", "+"],
                [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())])
            self.assertNotIn("+ Option", [
                button.text() for button in dialog.findChildren(QPushButton)])
            phase_table = dialog.tables["phase"]
            self.assertEqual(3, phase_table.columnCount())
            phase_table.item(0, 0).setText("Concept")
            saved = dialog.result_items()
            self.assertIn("Concept", [item.get("name") for item in saved])
        finally:
            dialog.close()

    def test_metadata_tabs_can_be_renamed_added_and_persisted_when_empty(self):
        from ui.metadata_editor import MetadataEditorDialog
        dialog = MetadataEditorDialog([])
        try:
            bar = dialog.tabs.tabBar()
            bar.begin_edit(0)
            bar._editor.setText("Product Stages")
            bar._finish_edit()
            self.assertEqual("Product Stages", dialog.tabs.tabText(0))

            dialog.tabs.setCurrentIndex(dialog.tabs.count() - 1)
            self.app.processEvents()
            self.assertEqual("+", dialog.tabs.tabText(dialog.tabs.count() - 1))
            new_index = dialog.tabs.currentIndex()
            bar.begin_edit(new_index)
            bar._editor.setText("Suppliers")
            bar._finish_edit()

            saved = dialog.result_items()
            headers = [item for item in saved
                       if item.get("record_type") == "header"]
            self.assertIn("Product Stages", [item["header"] for item in headers])
            self.assertIn("Suppliers", [item["header"] for item in headers])

            reopened = MetadataEditorDialog(saved)
            try:
                labels = [reopened.tabs.tabText(i)
                          for i in range(reopened.tabs.count())]
                self.assertIn("Product Stages", labels)
                self.assertIn("Suppliers", labels)
            finally:
                reopened.close()
        finally:
            dialog.close()

    def test_duplicate_metadata_resource_labels_are_rejected(self):
        from ui.metadata_editor import MetadataEditorDialog
        dialog = MetadataEditorDialog([
            {"id": "case-1", "name": "RLN2-1", "kind": "case",
             "header": "Cases"},
            {"id": "case-2", "name": "RLN2-1", "kind": "case",
             "header": "Cases"},
        ])
        try:
            with self.assertRaisesRegex(ValueError, "must be unique"):
                dialog.result_items()
        finally:
            dialog.close()

    def test_room_token_commit_survives_invalid_delegate_index(self):
        from PyQt6.QtCore import QModelIndex
        tree = TreeGridView()
        node = TaskNode("")
        try:
            tree.load_project([node])
            item = tree.topLevelItem(0)
            delegate = tree.itemDelegateForColumn(0)
            editor = InlineTaskEditor([], tree)
            editor.setProperty("vpm_node_id", node.id)
            editor.tokens = [{
                "id": "room-1", "label": "Room 1", "kind": "room",
                "duration": None, "header": "Rooms",
            }]
            editor.setText("[Room 1] ")
            delegate.setModelData(editor, tree.model(), QModelIndex())
            self.assertEqual("Room 1", node.name)
            self.assertEqual("Room 1", node.resources["room"])
            self.assertEqual("[Room 1]", item.text(0))
        finally:
            tree.close()

    def test_metadata_enter_while_editing_adds_row_and_delete_removes_section(self):
        from ui.metadata_editor import MetadataEditorDialog
        dialog = MetadataEditorDialog([
            {"id": "one", "name": "Idea", "kind": "phase",
             "header": "Project Phases", "duration": 1},
            {"id": "two", "name": "Design", "kind": "phase",
             "header": "Project Phases", "duration": 10},
        ])
        try:
            dialog.show()
            self.app.processEvents()
            table = dialog.tables["phase"]
            self.assertEqual(2, table.rowCount())
            table.setCurrentCell(1, 0)
            table.editItem(table.item(1, 0))
            self.app.processEvents()
            editor = QApplication.focusWidget()
            self.assertIsInstance(editor, QLineEdit)
            QTest.keyClick(editor, Qt.Key.Key_Return)
            self.app.processEvents()
            self.assertEqual(3, table.rowCount())
            self.assertEqual(2, table.currentRow())
            table.setCurrentCell(0, 0)
            with patch.object(QMessageBox, "question",
                              return_value=QMessageBox.StandardButton.Yes):
                QTest.keyClick(table, Qt.Key.Key_Delete)
            self.assertEqual(2, table.rowCount())
        finally:
            dialog.close()

    def test_deleted_builtin_metadata_does_not_return(self):
        from utils.template_catalog import save_templates
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "templates.json")
            remaining = [{
                "id": "phase-idea", "name": "Renamed Idea", "kind": "phase",
                "header": "Renamed Header", "duration": 2,
            }]
            with patch("utils.template_catalog._path", return_value=path):
                save_templates(remaining)
                loaded = load_templates()
            self.assertEqual(["phase-idea"], [item["id"] for item in loaded])
            self.assertEqual("Renamed Idea", loaded[0]["name"])

    def test_fast_entry_keeps_cursor_through_indent_and_next_row(self):
        tree = TreeGridView()
        tree.resize(700, 400)
        tree.show()
        try:
            root = TaskNode("Existing")
            tree.load_project([root])
            tree.setCurrentItem(tree.topLevelItem(0), 0)
            QTest.keyClick(tree, Qt.Key.Key_Return)
            self.app.processEvents()
            editor = QApplication.focusWidget()
            self.assertIsInstance(editor, InlineTaskEditor)
            QTest.keyClicks(editor, "Child")
            QTest.keyClick(editor, Qt.Key.Key_Tab)
            self.app.processEvents()
            self.app.processEvents()
            self.assertEqual("Child", root.children[0].name)
            editor = QApplication.focusWidget()
            self.assertIsInstance(editor, InlineTaskEditor)
            QTest.keyClick(editor, Qt.Key.Key_Return)
            self.app.processEvents()
            self.app.processEvents()
            self.assertEqual(2, len(root.children))
            self.assertIsInstance(QApplication.focusWidget(), InlineTaskEditor)
        finally:
            tree.close()

    def test_direct_same_as_shortcut_uses_grid_row_pick(self):
        tree = TreeGridView()
        tree.resize(700, 400)
        tree.show()
        try:
            target = TaskNode("Target")
            source = TaskNode("Source")
            tree.load_project([target, source])
            target_item = tree.topLevelItem(0)
            source_item = tree.topLevelItem(1)
            tree.start_date_pick_mode(source_item, "start", "same_as")
            QTest.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton,
                             pos=tree.visualItemRect(target_item).center())
            self.assertEqual("same_as", source.start_rule["mode"])
            self.assertEqual(target.id, source.start_rule["task_id"])
        finally:
            tree.close()

    def test_enter_starts_fast_entry_in_empty_grid(self):
        tree = TreeGridView()
        tree.resize(600, 300)
        tree.show()
        try:
            tree.setFocus()
            QTest.keyClick(tree, Qt.Key.Key_Return)
            self.app.processEvents()
            self.assertEqual(1, len(tree.root_nodes))
            self.assertEqual("", tree.root_nodes[0].name)
            self.assertIsInstance(QApplication.focusWidget(), InlineTaskEditor)
        finally:
            tree.close()

    def test_owner_defaults_migrate_outside_install_and_into_old_projects(self):
        import utils.config_manager as config_module
        old_instance = ConfigManager._instance
        old_projects = ConfigManager._projects
        old_active = ConfigManager._active_id
        try:
            with tempfile.TemporaryDirectory() as folder:
                legacy = os.path.join(folder, "legacy.json")
                durable = os.path.join(folder, "durable", "vpm_config.json")
                with open(legacy, "w", encoding="utf-8") as handle:
                    json.dump({"owners": ["Alice", "Bob"]}, handle)
                ConfigManager._instance = None
                ConfigManager._projects = {}
                ConfigManager._active_id = "__default__"
                with patch.object(config_module, "CONFIG_FILE", durable), \
                     patch.object(config_module, "_CONFIG_DIR", os.path.dirname(durable)), \
                     patch.object(config_module, "LEGACY_CONFIG_FILES", [legacy]):
                    ConfigManager()
                    ConfigManager.register_project(
                        "old-project", {"owners": ["Unassigned", "Me"]})
                    self.assertEqual(["Alice", "Bob"],
                                     ConfigManager.snapshot_project("old-project")["owners"])
                    self.assertTrue(os.path.exists(durable))
        finally:
            ConfigManager._instance = old_instance
            ConfigManager._projects = old_projects
            ConfigManager._active_id = old_active

    def test_impact_review_is_skipped_when_project_finish_and_other_tasks_hold(self):
        tree = TreeGridView()
        try:
            first = self._fixed("First", "2026-09-01", 2)
            last = self._fixed("Last", "2026-10-01", 2)
            schedule([first, last])
            tree.load_project([first, last])
            review = tree._simulate_impact(first, new_end="2026-09-04")
            self.assertIsNone(review)
            review = tree._simulate_impact(last, new_end="2026-10-10")
            self.assertIsNotNone(review)
            self.assertNotEqual(0, review["project_delta"])
        finally:
            tree.close()

    def test_data_health_reports_without_changing_tasks(self):
        from ui.main_window import MainWindow
        with patch.object(MainWindow, "_restore_startup_state", return_value=False):
            window = MainWindow()
        try:
            project = window.active_project()
            task = TaskNode("Contradiction")
            task.status = "In Progress"
            task.status_manual = True
            task.end_date = (datetime.now().date() - timedelta(days=2)).isoformat()
            task.revisions = [{"reason": ""}]
            before = task.to_dict()
            project.tree_view.load_project([task])
            issues = [text for text, _ in window._data_health_issues(project)]
            self.assertTrue(any("Overdue" in text for text in issues))
            self.assertTrue(any("missing reason" in text for text in issues))
            self.assertEqual(before, task.to_dict())
        finally:
            window.unsaved_changes = False
            window.file_guard.release()
            window.close()

    def test_metadata_timeline_change_updates_placed_tasks_not_overrides(self):
        from ui.main_window import MainWindow
        with patch.object(MainWindow, "_restore_startup_state", return_value=False):
            window = MainWindow()
        try:
            project = window.active_project()
            linked = TaskNode("DOE")
            linked.start_rule = {"mode": "fixed", "date": "2026-09-01"}
            linked.end_rule = {"mode": "duration", "days": 10}
            linked.task_tokens = [{
                "id": "test-doe", "label": "DOE", "kind": "activity",
                "duration": 10, "header": "Lab Testing",
            }]
            overridden = TaskNode("DOE custom")
            overridden.start_rule = {"mode": "fixed", "date": "2026-09-01"}
            overridden.end_rule = {"mode": "duration", "days": 4}
            overridden.task_tokens = [{
                "id": "test-doe", "label": "DOE", "kind": "activity",
                "duration": 10, "header": "Lab Testing",
            }]
            project.tree_view.load_project([linked, overridden])

            count = window._sync_tasks_to_metadata([{
                "id": "test-doe", "name": "DOE Updated", "kind": "activity",
                "duration": 15, "header": "Lab Testing",
            }])

            self.assertEqual(2, count)
            self.assertEqual(15, linked.end_rule["days"])
            self.assertEqual(15, linked.task_tokens[0]["duration"])
            self.assertEqual("DOE Updated", linked.name)
            self.assertEqual(4, overridden.end_rule["days"])
            self.assertEqual(15, overridden.task_tokens[0]["duration"])
        finally:
            window.unsaved_changes = False
            window.file_guard.release()
            window.close()


if __name__ == "__main__":
    unittest.main()
