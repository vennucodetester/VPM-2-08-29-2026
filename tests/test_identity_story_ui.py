import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import (QApplication, QAbstractItemView, QDialog,
                             QDialogButtonBox, QLineEdit, QMessageBox, QStyle,
                             QStyleOptionViewItem)

from models.task_node import TaskNode
from ui.identity_story_panel import (
    IdentityStoryDialog, IdentityStoryPanel, IdentityTimeline,
)
from ui.metadata_editor import MetadataEditorDialog
from ui.tree_grid_view import TreeGridView
from vpm_tracker_core import Columns
from utils.identity_story import build_identity_story
from utils.identity_story import shortest_unique_event_labels
from utils.usage_logger import UsageLogger, timed_exec


def token(identity_id, label, kind="case"):
    return {"id": identity_id, "label": label, "kind": kind}


def task(name, start=None, end=None, tokens=()):
    value = TaskNode(name)
    value.start_date, value.end_date = start, end
    value.task_tokens = [dict(item) for item in tokens]
    return value


def story_fixture():
    selected = token("case-1", "RLN2MA-1")
    values = [
        task("Case VAVE", "2026-08-31", "2026-09-18", [selected]),
        task("Cassette VAVE", "2026-08-31", "2026-09-11", [selected]),
        task("Later", "2026-10-05", "2026-10-23", [selected]),
        task("Needs dates", tokens=[selected]),
    ]
    return build_identity_story(
        [{"id": "project-1", "name": "Acceptance", "roots": values}],
        "case-1", "RLN2MA-1", "case")


class IdentityStoryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_graph_geometry_has_lanes_overlap_and_unscheduled(self):
        timeline = IdentityTimeline()
        timeline.resize(900, 420)
        timeline.set_story(story_fixture())
        geometry = timeline.geometry_snapshot(900)
        self.assertEqual(3, len(geometry["bars"]))
        self.assertEqual(1, len(geometry["overlaps"]))
        self.assertEqual(1, len(geometry["unscheduled"]))
        band, overlap = geometry["overlaps"][0]
        self.assertGreater(band.width(), 0)
        self.assertEqual("2026-09-11", overlap.overlap_end)

    def test_timeline_uses_unique_breadcrumbs_for_repeated_task_names(self):
        selected = token("case-1", "RLN2MA-1")
        first_parent = task("Cassette VAVE", "2026-08-01", "2026-09-30")
        first = task("Lab Testing", "2026-08-31", "2026-09-18", [selected])
        first.parent = first_parent
        first_parent.children = [first]
        second_parent = task("Case VAVE", "2026-08-01", "2026-09-30")
        second = task("Lab Testing", "2026-08-31", "2026-09-11", [selected])
        second.parent = second_parent
        second_parent.children = [second]
        story = build_identity_story([{
            "id": "p", "name": "VAVE-MB 2.0",
            "roots": [first_parent, second_parent],
        }], "case-1")
        timeline = IdentityTimeline()
        timeline.set_story(story)
        self.assertEqual(shortest_unique_event_labels(story.events),
                         timeline._event_labels)
        self.assertEqual({
            "Cassette VAVE › Lab Testing",
            "Case VAVE › Lab Testing",
        }, set(timeline._event_labels.values()))

    def test_timeline_keeps_parent_for_uniquely_worded_lab_testing_row(self):
        selected = token("case-1", "RLN2MA-2")
        parent = task("LT Aluminum coil", "2026-08-01", "2026-10-31")
        child = task("[Lab Testing] - no clear timeline yet",
                     "2026-10-05", "2026-10-23", [selected])
        child.parent = parent
        parent.children = [child]
        sibling_parent = task("Low Torque compressor - MT",
                              "2026-08-01", "2026-10-31")
        sibling = task("Lab Testing", "2026-09-01", "2026-09-20", [selected])
        sibling.parent = sibling_parent
        sibling_parent.children = [sibling]
        story = build_identity_story([{
            "id": "p", "name": "VAVE-MB 2.0",
            "roots": [parent, sibling_parent],
        }], "case-1")
        timeline = IdentityTimeline()
        timeline.set_story(story)
        self.assertEqual({
            "LT Aluminum coil › [Lab Testing] - no clear timeline yet",
            "Low Torque compressor - MT › Lab Testing",
        }, set(timeline._event_labels.values()))

    def test_bar_double_click_emits_project_and_task(self):
        timeline = IdentityTimeline()
        timeline.resize(900, 420)
        timeline.set_story(story_fixture())
        timeline.show()
        self.app.processEvents()
        rect, event = timeline.geometry_snapshot(900)["bars"][2]
        spy = QSignalSpy(timeline.jumpRequested)
        QTest.mouseDClick(timeline, Qt.MouseButton.LeftButton,
                         pos=rect.center())
        self.assertEqual(1, len(spy))
        self.assertEqual([event.project_id, event.task_id], list(spy[0]))
        timeline.close()

    def test_empty_story_has_clear_message(self):
        timeline = IdentityTimeline()
        empty = build_identity_story([], "missing", "Unused", "room")
        timeline.set_story(empty)
        self.assertEqual([], timeline.geometry_snapshot()["bars"])
        self.assertEqual("No scheduled usage for this identity",
                         timeline.empty_message)

    def test_panel_connections_receive_only_story_connections(self):
        selected = token("case-1", "Case")
        node = task("Use", "2026-09-01", "2026-09-02", [
            selected, token("room-1", "Room 1", "room")])
        story = build_identity_story(
            [{"id": "p", "name": "P", "roots": [node]}], "case-1")
        panel = IdentityStoryPanel()
        panel.set_story(story)
        self.assertEqual(["room-1"], [item.identity_id for item in
                                      panel.connections.story.connected_identities])

    def _dialog(self):
        selected = token("case-1", "RLN2MA-1")
        projects = [{"id": "p", "name": "P", "roots": [
            task("Use", "2026-09-01", "2026-09-02", [selected])]}]
        return MetadataEditorDialog([
            {"id": "case-1", "name": "RLN2MA-1", "kind": "case",
             "header": "Cases", "duration": 10},
        ], projects=projects)

    def test_metadata_single_click_does_not_open_double_click_opens_once(self):
        dialog = self._dialog()
        dialog.show()
        self.app.processEvents()
        table = dialog.tables["case"]
        index = table.model().index(0, 0)
        pos = table.visualRect(index).center()
        spy = QSignalSpy(table.identityStoryRequested)
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=pos)
        self.assertEqual(0, len(spy))
        QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=pos)
        self.assertEqual(1, len(spy))
        self.assertNotEqual(QAbstractItemView.State.EditingState, table.state())
        self.assertEqual("Story: RLN2MA-1", dialog.story_panel.heading.text().split(" ·")[0])
        dialog.close()

    def test_f2_and_edit_button_begin_editing(self):
        dialog = self._dialog()
        dialog.show()
        self.app.processEvents()
        table = dialog.tables["case"]
        table.setCurrentCell(0, 0)
        QTest.keyClick(table, Qt.Key.Key_F2)
        self.app.processEvents()
        self.assertIsInstance(QApplication.focusWidget(), QLineEdit)
        QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Escape)
        dialog.edit_button.click()
        self.app.processEvents()
        self.assertIsInstance(QApplication.focusWidget(), QLineEdit)
        dialog.close()

    def test_overlap_flag_defaults_off_and_round_trips_in_metadata(self):
        dialog = self._dialog()
        table = dialog.tables["case"]
        flag = table.item(0, 2)
        self.assertEqual(Qt.CheckState.Unchecked, flag.checkState())
        flag.setCheckState(Qt.CheckState.Checked)
        saved = dialog.result_items()
        option = next(value for value in saved if value.get("id") == "case-1")
        self.assertTrue(option["flag_overlaps"])
        reopened = MetadataEditorDialog(saved)
        try:
            self.assertEqual(
                Qt.CheckState.Checked,
                reopened.tables["case"].item(0, 2).checkState())
        finally:
            reopened.close()
        dialog.close()

    def test_phase_overlap_flag_is_disabled_and_new_options_default_off(self):
        dialog = MetadataEditorDialog([{
            "id": "phase-1", "name": "Design", "kind": "phase",
            "header": "Project Phases", "duration": 10,
        }])
        try:
            phase_flag = dialog.tables["phase"].item(0, 2)
            self.assertFalse(phase_flag.flags() & Qt.ItemFlag.ItemIsEnabled)
            row = dialog.tables["room"].append_option({"name": "Room 1"})
            self.assertEqual(
                Qt.CheckState.Unchecked,
                dialog.tables["room"].item(row, 2).checkState())
        finally:
            dialog.close()

    def test_project_phase_double_click_edits_in_place(self):
        dialog = MetadataEditorDialog([
            {"id": "phase-1", "name": "Design", "kind": "phase",
             "header": "Project Phases", "duration": 10}])
        dialog.show()
        self.app.processEvents()
        table = dialog.tables["phase"]
        spy = QSignalSpy(table.identityStoryRequested)
        pos = table.visualRect(table.model().index(0, 0)).center()
        QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=pos)
        self.app.processEvents()
        self.assertEqual(0, len(spy))
        self.assertIsInstance(QApplication.focusWidget(), QLineEdit)
        dialog.close()

    def test_new_row_gets_id_and_blank_double_click_does_not_open(self):
        dialog = self._dialog()
        table = dialog.tables["case"]
        row = table.append_option()
        self.assertTrue(table.item(row, 0).data(Qt.ItemDataRole.UserRole))
        spy = QSignalSpy(table.identityStoryRequested)
        table.resize(500, 240)
        table.show()
        self.app.processEvents()
        QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton,
                         pos=table.visualRect(table.model().index(row, 0)).center())
        self.assertEqual(0, len(spy))
        dialog.close()

    def test_delete_requires_confirmation(self):
        dialog = self._dialog()
        table = dialog.tables["case"]
        dialog.tabs.setCurrentWidget(table)
        table.setCurrentCell(0, 0)
        with patch.object(QMessageBox, "question",
                          return_value=QMessageBox.StandardButton.No):
            dialog.delete_button.click()
        self.assertEqual(1, table.rowCount())
        with patch.object(QMessageBox, "question",
                          return_value=QMessageBox.StandardButton.Yes):
            dialog.delete_button.click()
        self.assertEqual(1, table.rowCount())
        self.assertEqual("", table.item(0, 0).text())
        dialog.close()

    def test_normalized_duplicate_names_are_rejected(self):
        dialog = MetadataEditorDialog([
            {"id": "case-1", "name": "Room   One", "kind": "case"},
            {"id": "case-2", "name": " room one ", "kind": "case"},
        ])
        with self.assertRaisesRegex(ValueError, "must be unique"):
            dialog.result_items()
        dialog.close()

    def test_project_chip_double_click_opens_story_but_name_still_edits(self):
        tree = TreeGridView()
        node = task("Normal task name", "2026-09-01", "2026-09-02", [
            token("room-1", "Room 1", "room")])
        tree.load_project([node])
        tree.resize(900, 360)
        tree.show()
        self.app.processEvents()
        item = tree.topLevelItem(0)
        index = tree.indexFromItem(item, Columns.TREE)
        delegate = tree.itemDelegateForColumn(Columns.TREE)
        option = QStyleOptionViewItem()
        option.initFrom(tree.viewport())
        option.rect = tree.visualItemRect(item)
        delegate.initStyleOption(option, index)
        text_rect = tree.style().subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, option, tree)
        chip_width = option.fontMetrics.horizontalAdvance("[Room 1]")
        chip_pos = text_rect.topLeft()
        chip_pos.setX(text_rect.left() + chip_width // 2)
        chip_pos.setY(text_rect.center().y())
        spy = QSignalSpy(tree.identity_story_requested)
        QTest.mouseDClick(tree.viewport(), Qt.MouseButton.LeftButton,
                         pos=chip_pos)
        self.assertEqual(1, len(spy))
        self.assertEqual(["room-1", "Room 1", "room"], list(spy[0]))

        name_x = text_rect.left() + chip_width + option.fontMetrics.horizontalAdvance(
            "  Normal")
        QTest.mouseDClick(tree.viewport(), Qt.MouseButton.LeftButton,
                         pos=type(chip_pos)(name_x, text_rect.center().y()))
        self.app.processEvents()
        self.assertIsInstance(QApplication.focusWidget(), QLineEdit)
        tree.close()

    def test_story_close_paths_accept_not_reject(self):
        """Close, Esc, and reject() finish a viewed story as Accepted."""
        dialog = IdentityStoryDialog(story_fixture())
        try:
            dialog.reject()
            self.assertEqual(QDialog.DialogCode.Accepted, dialog.result())
        finally:
            dialog.close()

        dialog = IdentityStoryDialog(story_fixture())
        try:
            buttons = dialog.findChild(QDialogButtonBox)
            close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
            close_btn.click()
            self.assertEqual(QDialog.DialogCode.Accepted, dialog.result())
        finally:
            dialog.close()

        dialog = IdentityStoryDialog(story_fixture())
        try:
            dialog.show()
            self.app.processEvents()
            QTest.keyClick(dialog, Qt.Key.Key_Escape)
            self.app.processEvents()
            self.assertEqual(QDialog.DialogCode.Accepted, dialog.result())
        finally:
            dialog.close()

    def test_timed_exec_logs_ok_when_story_is_closed(self):
        """usage_report cancel rate must not treat a viewed Story as cancel."""
        with tempfile.TemporaryDirectory() as folder:
            logger = UsageLogger(folder, enabled=True, import_legacy=False)
            dialog = IdentityStoryDialog(story_fixture())
            QTimer.singleShot(0, dialog.reject)
            with patch("utils.usage_logger.usage", logger):
                result = timed_exec(dialog, "identity_story")
            self.assertEqual(QDialog.DialogCode.Accepted, result)
            rows = [json.loads(line) for line in logger.path().read_text(
                encoding="utf-8").splitlines()]
            dialog_rows = [row for row in rows if row["ev"] == "dialog"]
            self.assertEqual(1, len(dialog_rows))
            self.assertEqual("identity_story", dialog_rows[0]["d"]["name"])
            self.assertEqual("ok", dialog_rows[0]["d"]["outcome"])


if __name__ == "__main__":
    unittest.main()
