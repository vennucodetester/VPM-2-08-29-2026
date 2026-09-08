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

    def test_note_edit_marks_project_dirty_before_history_debounce(self):
        project = ProjectWidget("Notes", {}, [])
        changed = []
        project.project_changed.connect(lambda: changed.append(True))
        try:
            project.notes_panel.editor.insertPlainText("new note")
            self.assertTrue(project.notes_panel._debounce.isActive())
            self.assertTrue(changed)
        finally:
            project.notes_panel._debounce.stop()
            project.close()

    def test_f08_multi_task_cut_single_undo(self):
        a = TaskNode("Task A")
        a1 = TaskNode("Task A1", parent=a)
        a.add_child(a1)
        b = TaskNode("Task B")
        b.start_rule = {"mode": "same_as", "task_id": a.id, "field": "start"}
        c = TaskNode("Task C")

        project = ProjectWidget("P", {}, [a, b, c])
        try:
            tree = project.tree_view
            item_a = tree.topLevelItem(0)
            item_b = tree.topLevelItem(1)

            self.assertEqual(3, len(tree.root_nodes))
            tree.clearSelection()
            item_a.setSelected(True)
            item_b.setSelected(True)

            tree.cut_selected_tasks()

            self.assertEqual(1, len(tree.root_nodes))
            self.assertEqual("Task C", tree.root_nodes[0].name)

            project.undo()
            self.assertEqual(3, len(tree.root_nodes))
            names = [n.name for n in tree.root_nodes]
            self.assertEqual(["Task A", "Task B", "Task C"], names)
            restored_a = tree.root_nodes[0]
            restored_b = tree.root_nodes[1]
            self.assertEqual(1, len(restored_a.children))
            self.assertEqual("Task A1", restored_a.children[0].name)
            self.assertEqual(restored_a.id, restored_b.start_rule.get("task_id"))

            project.redo()
            self.assertEqual(1, len(tree.root_nodes))
            self.assertEqual("Task C", tree.root_nodes[0].name)
        finally:
            project.close()

    def test_paste_remaps_same_as_start_and_end_rules(self):
        first = TaskNode("Source")
        second = TaskNode("Linked")
        second.start_rule = {"mode": "same_as", "task_id": first.id,
                             "field": "start"}
        second.end_rule = {"mode": "same_as", "task_id": first.id,
                           "field": "end"}
        tree = TreeGridView()
        try:
            tree.load_project([first, second])
            TreeGridView._task_clipboard = [first.to_dict(), second.to_dict()]
            tree.paste_tasks()
            copied_source, copied_link = tree.root_nodes[-2:]
            self.assertEqual(copied_source.id,
                             copied_link.start_rule["task_id"])
            self.assertEqual(copied_source.id,
                             copied_link.end_rule["task_id"])
        finally:
            tree.close()

    def test_delete_clears_all_date_rules_targeting_removed_task(self):
        source = TaskNode("Source")
        linked = TaskNode("Linked")
        linked.start_rule = {"mode": "same_as", "task_id": source.id,
                             "field": "start"}
        linked.end_rule = {"mode": "same_as", "task_id": source.id,
                           "field": "end"}
        tree = TreeGridView()
        try:
            tree.load_project([source, linked])
            tree.delete_task(tree.topLevelItem(0))
            self.assertEqual("automatic", linked.start_rule["mode"])
            self.assertEqual("duration", linked.end_rule["mode"])
            self.assertNotIn("task_id", linked.start_rule)
            self.assertNotIn("task_id", linked.end_rule)
        finally:
            tree.close()

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


    def test_d2_reopened_unsupported_json_raises_value_error(self):
        from ui.main_window import MainWindow
        with tempfile.NamedTemporaryFile(suffix=".vpmt", delete=False, mode="w", encoding="utf-8") as tf:
            tf.write('{"unexpected": "not a project"}')
            bad_path = tf.name
        try:
            with self.assertRaises(ValueError):
                load_projects(bad_path)

            # Test MainWindow preserves tabs on bad load
            with patch.object(MainWindow, "_restore_startup_state", return_value=False):
                window = MainWindow()
            try:
                window._add_project_from_data("Existing", {}, [TaskNode("KeepMe")])
                initial_count = window.project_tabs.count()
                with patch("ui.main_window.QMessageBox.critical"):
                    ok = window._load_path(bad_path, prompt_unsaved=False)
                self.assertFalse(ok)
                self.assertEqual(initial_count, window.project_tabs.count())
                self.assertEqual("Existing", window.active_project().name)
            finally:
                window.unsaved_changes = False
                window.file_guard.release()
                window.close()
        finally:
            if os.path.exists(bad_path):
                os.remove(bad_path)

    def test_f17_undo_settles_pending_notes_before_reversing_tasks(self):
        project = ProjectWidget("NotesTest", {}, [])
        project.load_snapshot({"name": "NotesTest", "tasks": [{"id": "1", "name": "Task A", "start": "2026-07-01", "end": "2026-07-05", "color": "#ffffff"}]})
        project.reset_history_baseline()
        try:
            # Edit task name to Task B
            project.tree_view.model().setData(project.tree_view.model().index(0, 0), "Task B")
            self.assertEqual("Task B", project.tree_view.root_nodes[0].name)

            # Type into notes panel without waiting for 900ms debounce
            project.notes_panel.editor.insertPlainText("Important note")
            self.assertTrue(project.notes_panel._debounce.isActive())

            # First undo should undo the pending notes, NOT the task rename
            project.undo()
            self.assertFalse(project.notes_panel._debounce.isActive())
            self.assertEqual("Task B", project.tree_view.root_nodes[0].name)
            self.assertEqual("", project.notes_panel.plain_text().strip())

            # Second undo should undo the task rename
            project.undo()
            self.assertEqual("Task A", project.tree_view.root_nodes[0].name)
        finally:
            project.close()

    def test_f18_critical_path_fixed_siblings_no_invented_predecessors(self):
        from utils.critical_path import CriticalPathAnalyzer
        parent = TaskNode("Parent")
        a = TaskNode("A", parent=parent)
        a.start_rule = {"mode": "fixed", "date": "2026-09-07"}
        a.end_rule = {"mode": "duration", "days": 5}

        b = TaskNode("B", parent=parent)
        b.start_rule = {"mode": "fixed", "date": "2026-09-07"}
        b.end_rule = {"mode": "duration", "days": 1}

        parent.children = [a, b]
        schedule([parent])

        analyzer = CriticalPathAnalyzer([parent])
        self.assertEqual([], analyzer._get_predecessors(b))

        results = analyzer.analyze()
        self.assertEqual("2026-09-07", results["early_start"][b.id].strftime("%Y-%m-%d"))
        self.assertEqual(0, results["slack"][a.id])
        self.assertEqual(4, results["slack"][b.id])
        self.assertIn(a.id, results["critical_path_ids"])
        self.assertNotIn(b.id, results["critical_path_ids"])

    def test_f19_focus_view_escapes_html_task_names(self):
        from ui.focus_view import _link
        from PyQt6.QtGui import QTextDocument
        t = TaskNode("Test <case> & verify")
        link_html = _link(t)
        self.assertIn("&lt;case&gt;", link_html)
        self.assertIn("&amp;", link_html)

        doc = QTextDocument()
        doc.setHtml(link_html)
        self.assertEqual("Test <case> & verify", doc.toPlainText())

    def test_f20_successful_save_as_retires_recovery_file(self):
        from ui.main_window import MainWindow
        with patch.object(MainWindow, "_restore_startup_state", return_value=False):
            window = MainWindow()
        try:
            window.current_filepath = None
            window._session_recovery_path = None
            window._add_project_from_data("Test", {}, [TaskNode("Node")])
            window.unsaved_changes = True
            window._autosave()
            rec_path = window._session_recovery_path
            self.assertIsNotNone(rec_path)
            self.assertTrue(os.path.exists(rec_path))

            with tempfile.NamedTemporaryFile(suffix=".vpmt", delete=False) as tf:
                target_path = tf.name

            try:
                with patch("ui.main_window.QFileDialog.getSaveFileName", return_value=(target_path, "")):
                    window.save_project_file_as()

                self.assertTrue(os.path.exists(target_path))
                self.assertFalse(window.unsaved_changes)
                self.assertFalse(os.path.exists(rec_path))
                self.assertIn(os.path.basename(rec_path), window._handled_recoveries())
                self.assertNotIn(rec_path, window._recovery_candidates())
            finally:
                if os.path.exists(target_path):
                    os.remove(target_path)
        finally:
            window.unsaved_changes = False
            window.file_guard.release()
            window.close()


    def test_f21_focus_view_driver_chains_respect_fixed_and_same_as(self):
        from ui.focus_view import FocusView
        from models.task_node import TaskNode

        fv = FocusView()
        parent = TaskNode("Parent")
        child_a = TaskNode("Task A")
        child_a.start_rule = {"mode": "automatic"}
        child_b = TaskNode("Task B (Fixed)")
        child_b.start_rule = {"mode": "fixed", "date": "2026-09-10"}
        child_c = TaskNode("Task C (Same As B)")
        child_c.start_rule = {"mode": "same_as", "task_id": child_b.id}
        child_d = TaskNode("Task D (Parallel)")
        child_d.is_parallel = True
        child_d.start_rule = {"mode": "automatic"}

        parent.children = [child_a, child_b, child_c, child_d]
        for c in parent.children:
            c.parent = parent
        fv.root_nodes = [parent]
        node_map = {n.id: n for n in [parent, child_a, child_b, child_c, child_d]}

        # Child A: automatic, first child -> drivers from parent
        self.assertIsNone(fv._driver_of(child_a, node_map))
        # Child B: fixed -> no driver
        self.assertIsNone(fv._driver_of(child_b, node_map))
        # Child C: same as B -> driver is B
        self.assertEqual(fv._driver_of(child_c, node_map), child_b)
        # Child D: parallel -> driver is parent driver (None here)
        self.assertIsNone(fv._driver_of(child_d, node_map))

    def test_f22_refresh_all_restores_active_project(self):
        from ui.main_window import MainWindow
        from utils.config_manager import ConfigManager
        from models.task_node import TaskNode

        with patch.object(MainWindow, "_restore_startup_state", return_value=False):
            window = MainWindow()
        try:
            window._add_project_from_data("Proj1", {"holidays": ["2026-12-25"]}, [TaskNode("P1Task")])
            window._add_project_from_data("Proj2", {"holidays": ["2026-01-01"]}, [TaskNode("P2Task")])
            p1 = window.project_tabs.widget(0)
            p2 = window.project_tabs.widget(1)
            window.project_tabs.setCurrentWidget(p1)
            p1.activate()
            self.assertEqual(ConfigManager.active_project_id(), p1.project_id)

            window._refresh_all()

            # Active project must be restored to p1, not leaked from p2
            self.assertEqual(ConfigManager.active_project_id(), p1.project_id)
            self.assertEqual(window.active_project(), p1)
        finally:
            window.unsaved_changes = False
            window.file_guard.release()
            window.close()

    def test_f23_vave_data_health_ignores_execution_only_tasks(self):
        from ui.main_window import MainWindow
        from models.task_node import TaskNode

        with patch.object(MainWindow, "_restore_startup_state", return_value=False):
            window = MainWindow()
        try:
            exec_task = TaskNode("Execution Only")
            exec_task.status = "Completed"
            exec_task.vave_potential = None
            exec_task.vave_realized = None
            exec_task.savings_disposition = ""

            savings_task = TaskNode("With Savings")
            savings_task.status = "Completed"
            savings_task.vave_potential = 5000
            savings_task.vave_realized = None
            savings_task.savings_disposition = ""

            window._add_project_from_data("VAVE Proj", {"is_vave": True}, [exec_task, savings_task])
            proj = window.active_project()
            proj.is_vave = True

            issues = [issue for issue, node in window._data_health_issues()]
            vave_issues = [i for i in issues if "savings disposition" in i]

            # Only savings_task should be flagged, not exec_task
            self.assertEqual(1, len(vave_issues))
        finally:
            window.unsaved_changes = False
            window.file_guard.release()
            window.close()

    def test_f24_data_health_flags_broken_dependencies(self):
        from ui.main_window import MainWindow
        from models.task_node import TaskNode

        with patch.object(MainWindow, "_restore_startup_state", return_value=False):
            window = MainWindow()
        try:
            broken_start = TaskNode("Broken Start")
            broken_start.start_rule = {"mode": "continue_after", "task_id": "nonexistent-123"}

            broken_end = TaskNode("Broken End")
            broken_end.end_rule = {"mode": "same_as", "task_id": "nonexistent-456"}

            window._add_project_from_data("Test Proj", {}, [broken_start, broken_end])
            issues = [issue for issue, node in window._data_health_issues()]

            self.assertIn("Task start depends on a missing task", issues)
            self.assertIn("Task end depends on a missing task", issues)
        finally:
            window.unsaved_changes = False
            window.file_guard.release()
            window.close()

    def test_f25_metadata_editor_raises_for_invalid_durations(self):
        from ui.metadata_editor import MetadataEditorDialog

        dialog = MetadataEditorDialog([
            {"id": "h1", "name": "Lists", "kind": "resource", "header": "Resource Lists", "version": 1}
        ])
        table = dialog.tabs.widget(0)
        table.setRowCount(1)
        from PyQt6.QtWidgets import QTableWidgetItem
        table.setItem(0, 0, QTableWidgetItem("Item 1"))
        table.setItem(0, 1, QTableWidgetItem("not-a-number"))

        with self.assertRaises(ValueError) as ctx:
            dialog.result_items()
        self.assertIn("Duration must be a positive integer", str(ctx.exception))

        # Also test 0 or negative
        table.setItem(0, 1, QTableWidgetItem("0"))
        with self.assertRaises(ValueError) as ctx:
            dialog.result_items()
        self.assertIn("Duration must be a positive integer", str(ctx.exception))

    def test_f26_gantt_predecessor_and_trace_respect_fixed_and_same_as(self):
        from ui.gantt_chart import GanttTimeline, GanttChartWidget
        from models.task_node import TaskNode

        timeline = GanttTimeline()
        parent = TaskNode("Parent")
        a = TaskNode("Task A")
        b = TaskNode("Task B Fixed")
        b.start_rule = {"mode": "fixed", "date": "2026-09-08"}
        c = TaskNode("Task C Same As A")
        c.start_rule = {"mode": "same_as", "task_id": a.id}

        parent.children = [a, b, c]
        for ch in parent.children:
            ch.parent = parent
        timeline.node_map = {n.id: n for n in [parent, a, b, c]}

        # In Gantt timeline:
        self.assertIsNone(timeline._find_predecessor(b))
        self.assertEqual(timeline._find_predecessor(c), a)

        # In GanttChartWidget trace:
        widget = GanttChartWidget()
        widget.node_map = timeline.node_map
        widget.slack_data = {}
        trace = widget._compute_trace(b)
        # Trace for fixed task B should only contain B, not link back to A or Parent
        self.assertEqual(1, len(trace))
        self.assertEqual(trace[0]["task"], b)

    def test_f27_resource_usage_next_weekday_respects_holidays_and_weekends(self):
        from ui.resource_usage_dialog import _next_weekday

        # Friday Sept 4, 2026. Monday Sept 7 is holiday. Next workday should be Tuesday Sept 8.
        res = _next_weekday("2026-09-04", holidays=["2026-09-07"], exclude_weekends=True)
        self.assertEqual("2026-09-08", res)

        # When weekends are NOT excluded, next day after Friday Sept 4 is Saturday Sept 5
        res_we = _next_weekday("2026-09-04", exclude_weekends=False)
        self.assertEqual("2026-09-05", res_we)

        # Invalid date
        self.assertEqual("—", _next_weekday("invalid-date"))
        self.assertEqual("—", _next_weekday(None))
        self.assertEqual("—", _next_weekday(""))


if __name__ == "__main__":
    unittest.main()
