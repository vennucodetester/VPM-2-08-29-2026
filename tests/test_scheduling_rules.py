import unittest

from PyQt6.QtWidgets import QApplication

from models.task_node import TaskNode
from utils.scheduler import schedule


class SchedulingRuleMigrationTests(unittest.TestCase):
    def test_locked_legacy_task_migrates_without_date_change(self):
        node = TaskNode.from_dict({
            "id": "locked", "name": "Locked",
            "start_date": "2026-09-14", "end_date": "2026-09-18",
            "dates_locked": True, "children": [],
        })
        self.assertEqual({"mode": "fixed", "date": "2026-09-14"}, node.start_rule)
        self.assertEqual({"mode": "fixed", "date": "2026-09-18"}, node.end_rule)
        self.assertEqual(("2026-09-14", "2026-09-18"),
                         (node.start_date, node.end_date))

    def test_predecessor_migrates_to_continue_after(self):
        node = TaskNode.from_dict({
            "name": "Next", "start_date": "2026-09-17",
            "end_date": "2026-09-17", "predecessor_id": "first",
            "children": [],
        })
        self.assertEqual("continue_after", node.start_rule["mode"])
        self.assertEqual("first", node.start_rule["task_id"])
        self.assertEqual("end", node.start_rule["field"])

    def test_parallel_child_migrates_to_same_start_as_parent(self):
        root = TaskNode.from_dict({
            "id": "parent", "name": "Parent", "start_date": "2026-09-14",
            "end_date": "2026-09-18", "children": [{
                "id": "child", "name": "Child", "start_date": "2026-09-14",
                "end_date": "2026-09-15", "is_parallel": True, "children": [],
            }],
        })
        self.assertEqual({
            "mode": "same_as", "task_id": "parent", "field": "start",
            "offset": 0, "offset_unit": "workdays",
        }, root.children[0].start_rule)


class SchedulingRuleTests(unittest.TestCase):
    def _task(self, node_id, start, days=1):
        node = TaskNode(node_id)
        node.id = node_id
        node.start_date = start
        node.start_rule = {"mode": "fixed", "date": start}
        node.end_rule = {"mode": "duration", "days": days}
        return node

    def test_same_as_and_continue_after(self):
        first = self._task("first", "2026-09-14", 3)
        same = self._task("same", "2026-01-01", 2)
        same.start_rule = {
            "mode": "same_as", "task_id": "first", "field": "start",
            "offset": 0, "offset_unit": "workdays",
        }
        after = self._task("after", "2026-01-01", 1)
        after.start_rule = {
            "mode": "continue_after", "task_id": "first", "field": "end",
            "offset": 0, "offset_unit": "workdays",
        }
        schedule([first, same, after])
        self.assertEqual(("2026-09-14", "2026-09-16"),
                         (first.start_date, first.end_date))
        self.assertEqual(("2026-09-14", "2026-09-15"),
                         (same.start_date, same.end_date))
        self.assertEqual("2026-09-17", after.start_date)

    def test_continue_after_workday_offset(self):
        first = self._task("first", "2026-09-14", 3)
        after = self._task("after", "2026-01-01", 1)
        after.start_rule = {
            "mode": "continue_after", "task_id": "first", "field": "end",
            "offset": 3, "offset_unit": "workdays",
        }
        schedule([first, after])
        self.assertEqual("2026-09-22", after.start_date)

    def test_start_and_end_can_be_controlled_independently(self):
        node = self._task("task", "2026-09-14", 5)
        schedule([node])
        self.assertEqual("2026-09-18", node.end_date)
        node.end_rule = {"mode": "fixed", "date": "2026-09-30"}
        schedule([node])
        self.assertEqual("2026-09-30", node.end_date)
        self.assertEqual("2026-09-14", node.start_date)

    def test_rules_round_trip(self):
        node = self._task("task", "2026-09-14", 4)
        restored = TaskNode.from_dict(node.to_dict())
        self.assertEqual(node.start_rule, restored.start_rule)
        self.assertEqual(node.end_rule, restored.end_rule)


class DateRuleUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_visible_choices_do_not_require_symbols(self):
        from ui.date_rule_dialog import DateRuleDialog
        node = TaskNode("Task")
        dialog = DateRuleDialog(node, [node], "start")
        try:
            labels = [button.text() for button in dialog.mode_buttons.values()]
            self.assertTrue(any("Calendar" in label for label in labels))
            self.assertTrue(any("Same As" in label for label in labels))
            self.assertTrue(any("Continue After" in label for label in labels))
            self.assertTrue(any("Automatic" in label for label in labels))
        finally:
            dialog.close()

    def test_active_cell_menu_exposes_all_discoverable_choices(self):
        from ui.tree_grid_view import TreeGridView
        tree = TreeGridView()
        try:
            node = TaskNode("Task")
            tree.load_project([node])
            menu = tree._date_choice_menu(tree.topLevelItem(0), "end")
            labels = [action.text() for action in menu.actions()]
            self.assertTrue(any("Calendar" in label for label in labels))
            self.assertTrue(any("Same As" in label for label in labels))
            self.assertTrue(any("Continue After" in label for label in labels))
            self.assertTrue(any("Automatic" in label for label in labels))
        finally:
            tree.close()

    def test_old_tree_dates_do_not_move_during_load(self):
        from ui.tree_grid_view import TreeGridView
        parent = TaskNode("Parent")
        parent.start_date = "2026-09-14"; parent.end_date = "2026-09-30"
        first = TaskNode("First", parent=parent)
        first.start_date = "2026-09-14"; first.end_date = "2026-09-14"
        second = TaskNode("Second", parent=parent)
        second.start_date = "2026-09-30"; second.end_date = "2026-09-30"
        parent.children = [first, second]
        tree = TreeGridView()
        try:
            tree.load_project([parent])
            self.assertEqual("2026-09-30", second.start_date)
        finally:
            tree.close()


if __name__ == "__main__":
    unittest.main()
