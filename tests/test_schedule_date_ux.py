"""Focused tests for the 9/10 schedule/date UX plan.

1. Date cells use the dropdown editor, not the rule menu.
2. Calendar picks on automatic/duration tasks do not force fixed.
3. Routine date moves do not open the impact-review popup.
4. Intentional date edits (including rule-dialog / hotbox-style
   end changes) always journal old → new.
"""
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication, QDateEdit

from models.task_node import TaskNode
from ui.tree_grid_view import (
    Columns, DateDelegate, TreeGridView, model_date,
)
from utils.scheduler import schedule
from utils.workday_calculator import WorkdayCalculator


class ScheduleDateUxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _duration_task(self, name, start, days):
        node = TaskNode(name)
        node.start_date = start
        node.start_rule = {"mode": "fixed", "date": start}
        node.end_rule = {"mode": "duration", "days": days}
        node.end_date = WorkdayCalculator.add_workdays(start, days)
        return node

    def test_model_date_strips_rule_markers(self):
        self.assertEqual("2026-09-10", model_date("09-10-26  ="))
        self.assertEqual("2026-09-10", model_date("09-10-26  +"))
        self.assertEqual("2026-09-10", model_date("09-10-26  📌"))
        self.assertEqual("2026-09-10", model_date("2026-09-10"))

    def test_edit_item_on_start_end_uses_date_dropdown(self):
        tree = TreeGridView()
        try:
            node = self._duration_task("Hotbox LT-S3", "2026-09-01", 3)
            tree.load_project([node])
            item = tree.topLevelItem(0)
            opened = []

            def capture(_item, field, global_pos=None):
                opened.append(field)

            with patch.object(tree, "open_date_choices", side_effect=capture), \
                 patch("PyQt6.QtWidgets.QTreeWidget.editItem"):
                tree.editItem(item, Columns.END)
                tree.editItem(item, Columns.START)
            self.assertEqual([], opened)
            self.assertIsInstance(tree.itemDelegateForColumn(Columns.END),
                                  DateDelegate)
            self.assertIsInstance(tree.itemDelegateForColumn(Columns.START),
                                  DateDelegate)
        finally:
            tree.close()

    def test_predecessor_still_opens_rule_choices(self):
        tree = TreeGridView()
        try:
            node = self._duration_task("Task", "2026-09-01", 1)
            tree.load_project([node])
            opened = []
            with patch.object(
                    tree, "open_date_choices",
                    side_effect=lambda item, field, pos=None: opened.append(field)):
                tree.editItem(tree.topLevelItem(0), Columns.PREDECESSOR)
            self.assertEqual(["start"], opened)
        finally:
            tree.close()

    def test_date_delegate_is_qdate_edit(self):
        tree = TreeGridView()
        try:
            delegate = tree.itemDelegateForColumn(Columns.END)
            editor = delegate.createEditor(tree, None, tree.model().index(0, 0))
            self.assertIsInstance(editor, QDateEdit)
            self.assertTrue(editor.calendarPopup())
        finally:
            tree.close()

    def test_end_calendar_pick_updates_duration_not_fixed(self):
        tree = TreeGridView()
        try:
            node = self._duration_task("new plastic base injection foam",
                                      "2026-09-01", 3)
            tree.load_project([node])
            self.assertEqual("duration", node.end_rule["mode"])
            tree.apply_picked_date(node, "end", "2026-09-10")
            self.assertEqual("duration", node.end_rule["mode"])
            self.assertNotEqual("fixed", node.end_rule.get("mode"))
            self.assertEqual("2026-09-10", node.end_date)
            self.assertEqual(
                WorkdayCalculator.calculate_duration("2026-09-01", "2026-09-10"),
                int(node.end_rule["days"]))
        finally:
            tree.close()

    def test_start_calendar_pick_on_automatic_child_stays_rule_driven(self):
        tree = TreeGridView()
        try:
            parent = TaskNode("Hotbox LT-S3")
            parent.start_rule = {"mode": "fixed", "date": "2026-09-01"}
            parent.start_date = "2026-09-01"
            child = TaskNode("new plastic base injection foam", parent=parent)
            child.start_rule = {"mode": "automatic"}
            child.end_rule = {"mode": "duration", "days": 2}
            parent.children = [child]
            schedule([parent])
            tree.load_project([parent])
            before_start = child.start_date
            later = WorkdayCalculator.add_workdays(before_start, 4)
            tree.apply_picked_date(child, "start", later)
            self.assertNotEqual("fixed", child.start_rule.get("mode"))
            self.assertEqual(later, child.start_date)
            self.assertEqual("duration", child.end_rule.get("mode"))
        finally:
            tree.close()

    def test_commit_records_old_to_new_without_impact_dialog(self):
        tree = TreeGridView()
        journal = []
        tree.journal_event.connect(journal.append)
        try:
            node = self._duration_task("new plastic base injection foam",
                                      "2026-09-01", 3)
            other = self._duration_task("Unrelated", "2026-12-01", 2)
            tree.load_project([node, other])
            old_end = node.end_date
            with patch("ui.tree_grid_view.usage_logger.timed_exec") as timed:
                tree.commit_picked_date(node, "end", "2026-09-10", via="dropdown")
                timed.assert_not_called()
            self.assertTrue(any(
                "new plastic base injection foam" in line
                and "end" in line
                and old_end in line
                and "2026-09-10" in line
                for line in journal))
            self.assertFalse(any("Unrelated" in line for line in journal))
        finally:
            tree.close()

    def test_date_rule_apply_records_the_actual_date_move(self):
        """The 5:14pm miss: rule dialog converted duration → fixed and
        only journaled 'rule → Fixed: date' with no old → new."""
        tree = TreeGridView()
        journal = []
        tree.journal_event.connect(journal.append)
        try:
            node = self._duration_task("new plastic base injection foam",
                                      "2026-09-01", 3)
            tree.load_project([node])
            item = tree.topLevelItem(0)
            old_end = node.end_date
            tree._apply_date_rule(item, "end", {
                "mode": "fixed", "date": "2026-09-15",
            })
            self.assertEqual("2026-09-15", node.end_date)
            self.assertTrue(any(
                "end" in line and old_end in line and "2026-09-15" in line
                for line in journal))
        finally:
            tree.close()

    def test_routine_ripple_does_not_require_impact_review(self):
        tree = TreeGridView()
        journal = []
        tree.journal_event.connect(journal.append)
        try:
            first = self._duration_task("First", "2026-09-01", 2)
            second = TaskNode("Second")
            second.start_rule = {
                "mode": "continue_after", "task_id": first.id,
                "field": "end", "offset": 0, "offset_unit": "workdays",
            }
            second.end_rule = {"mode": "duration", "days": 2}
            schedule([first, second])
            tree.load_project([first, second])
            with patch("ui.dialogs.ImpactReviewDialog") as dialog_cls:
                tree.commit_picked_date(first, "end", "2026-09-10", via="dropdown")
                dialog_cls.assert_not_called()
            self.assertTrue(any("First" in line and "end" in line
                                for line in journal))
            self.assertFalse(any("'Second'" in line for line in journal))
        finally:
            tree.close()

    def test_project_finish_move_is_one_journal_line(self):
        tree = TreeGridView()
        journal = []
        tree.journal_event.connect(journal.append)
        try:
            node = self._duration_task("Last", "2026-10-01", 2)
            tree.load_project([node])
            tree.commit_picked_date(node, "end", "2026-10-20", via="dropdown")
            finish_lines = [line for line in journal
                            if line.startswith("Project end")]
            self.assertEqual(1, len(finish_lines))
            self.assertIn("Last", finish_lines[0])
        finally:
            tree.close()


if __name__ == "__main__":
    unittest.main()
