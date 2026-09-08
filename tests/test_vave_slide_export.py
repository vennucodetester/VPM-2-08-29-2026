import os
import tempfile
import unittest

from openpyxl import load_workbook

from models.task_node import TaskNode
from utils.excel_export import export_projects


def _node(name, potential=None, realized=None, status="In Progress"):
    node = TaskNode(name)
    node.vave_potential = potential
    node.vave_realized = realized
    node.status = status
    node.end_date = "2026-09-15"
    return node


def _attach(parent, children):
    parent.children = list(children)
    for child in parent.children:
        child.parent = parent
    return parent


class VaveSlideExportTests(unittest.TestCase):
    def _export(self, projects):
        tmp = tempfile.TemporaryDirectory()
        filename = os.path.join(tmp.name, "export.xlsx")
        export_projects(projects, filename)
        return tmp, load_workbook(filename, data_only=False)

    def test_non_vave_export_keeps_original_two_sheet_shape(self):
        project = {
            "name": "Standard",
            "metadata": {},
            "roots": [_node("Ordinary task")],
            "is_vave": False,
        }
        tmp, wb = self._export([project])
        self.addCleanup(tmp.cleanup)

        self.assertEqual(wb.sheetnames, ["Standard Tasks", "Standard Metadata"])
        headers = [cell.value for cell in wb["Standard Tasks"][1]]
        self.assertNotIn("Potential $", headers)
        self.assertNotIn("Realized $", headers)

    def test_export_duration_uses_each_projects_calendar(self):
        node = _node("Friday through Monday")
        node.start_date, node.end_date = "2026-09-04", "2026-09-07"
        projects = [{"name": "Weekdays", "metadata": {
            "exclude_weekends": True, "holidays": []}, "roots": [node]},
                    {"name": "Calendar Days", "metadata": {
            "exclude_weekends": False, "holidays": []}, "roots": [node]}]
        tmp, wb = self._export(projects)
        self.addCleanup(tmp.cleanup)
        for sheet, expected in (("Weekdays Tasks", "2"),
                                ("Calendar Days Tasks", "4")):
            headers = [cell.value for cell in wb[sheet][1]]
            self.assertEqual(expected, wb[sheet].cell(
                2, headers.index("Duration") + 1).value)

    def test_export_describes_same_as_start_and_end_rules(self):
        source = _node("Source")
        linked = _node("Linked")
        linked.start_rule = {"mode": "same_as", "task_id": source.id,
                             "field": "start"}
        linked.end_rule = {"mode": "same_as", "task_id": source.id,
                           "field": "end"}
        tmp, wb = self._export([{"name": "Rules", "metadata": {},
                                "roots": [source, linked]}])
        self.addCleanup(tmp.cleanup)
        sheet = wb["Rules Tasks"]
        headers = [cell.value for cell in sheet[1]]
        self.assertEqual("Start = Source start; End = Source end",
                         sheet.cell(3, headers.index("Depends On") + 1).value)

    def test_f09_excel_export_dependency_includes_signed_offset_and_units(self):
        source = _node("Source")
        t1 = _node("Task1")
        t1.start_rule = {"mode": "same_as", "task_id": source.id, "field": "start",
                         "offset": 3, "offset_unit": "workdays"}
        t2 = _node("Task2")
        t2.start_rule = {"mode": "continue_after", "task_id": source.id, "field": "end",
                         "offset": 1, "offset_unit": "workdays"}
        t3 = _node("Task3")
        t3.end_rule = {"mode": "same_as", "task_id": source.id, "field": "end",
                       "offset": -2, "offset_unit": "calendar_days"}
        t4 = _node("Task4")
        t4.start_rule = {"mode": "same_as", "task_id": source.id, "field": "start",
                         "offset": 0, "offset_unit": "workdays"}

        tmp, wb = self._export([{"name": "Rules", "metadata": {},
                                 "roots": [source, t1, t2, t3, t4]}])
        self.addCleanup(tmp.cleanup)
        sheet = wb["Rules Tasks"]
        headers = [cell.value for cell in sheet[1]]
        dep_col = headers.index("Depends On") + 1

        self.assertEqual("Start = Source start +3 workdays", sheet.cell(3, dep_col).value)
        self.assertEqual("Start after Source end +1 workdays", sheet.cell(4, dep_col).value)
        self.assertEqual("End = Source end -2 calendar days", sheet.cell(5, dep_col).value)
        self.assertEqual("Start = Source start", sheet.cell(6, dep_col).value)

    def test_vave_export_creates_data_and_slide_ready_previews(self):
        cassette = _attach(
            _node("Cassette VAVE activities"),
            [_node(f"Cassette idea {i}", potential=float(i)) for i in range(1, 9)],
        )
        case = _attach(
            _node("Case VAVE activities"),
            [_node(f"Case idea {i}", potential=float(i)) for i in range(1, 8)],
        )
        activity_root = _attach(_node("VAVE activities"), [cassette, case])
        project = {
            "name": "VAVE Project",
            "metadata": {},
            "roots": [activity_root],
            "is_vave": True,
        }
        tmp, wb = self._export([project])
        self.addCleanup(tmp.cleanup)

        self.assertIn("VAVE Project VAVE Data", wb.sheetnames)
        self.assertIn("Cassette VAVE activities", wb.sheetnames)
        self.assertIn("Case VAVE activities", wb.sheetnames)

        data = wb["VAVE Project VAVE Data"]
        self.assertEqual(data.max_row - 1, 15)
        self.assertEqual(data["N2"].value, "On Track")
        cassette_positions = [
            (data.cell(row, 5).value, data.cell(row, 6).value)
            for row in range(2, 10)
        ]
        self.assertEqual(cassette_positions[:4],
                         [("Left", 1), ("Left", 2), ("Left", 3), ("Left", 4)])
        self.assertEqual(cassette_positions[4:],
                         [("Right", 1), ("Right", 2), ("Right", 3), ("Right", 4)])

        cassette_preview = wb["Cassette VAVE activities"]
        case_preview = wb["Case VAVE activities"]
        self.assertEqual(cassette_preview["D6"].value, "On Track")
        self.assertEqual(case_preview["I6"].value, "On Track")
        self.assertEqual(
            cassette_preview["I3"].value,
            '=SUMIFS(\'VAVE Project VAVE Data\'!$K$2:$K$16,'
            '\'VAVE Project VAVE Data\'!$C$2:$C$16,"Cassette VAVE activities",'
            '\'VAVE Project VAVE Data\'!$O$2:$O$16,1)',
        )
        self.assertEqual(
            case_preview["I3"].value,
            '=SUMIFS(\'VAVE Project VAVE Data\'!$K$2:$K$16,'
            '\'VAVE Project VAVE Data\'!$C$2:$C$16,"Case VAVE activities",'
            '\'VAVE Project VAVE Data\'!$O$2:$O$16,1)',
        )

    def test_new_vave_group_lines_export_before_savings_are_entered(self):
        group = _attach(
            _node("Cassette VAVE activities"),
            [_node("Priced idea", potential=12.5), _node("New unpriced idea")],
        )
        project = {
            "name": "VAVE Project",
            "metadata": {},
            "roots": [_attach(_node("VAVE activities"), [group])],
            "is_vave": True,
        }
        tmp, wb = self._export([project])
        self.addCleanup(tmp.cleanup)

        data = wb["VAVE Project VAVE Data"]
        self.assertEqual(data.max_row - 1, 2)
        self.assertEqual(data["H3"].value, "New unpriced idea")
        self.assertIsNone(data["I3"].value)

    def test_explicit_realized_value_controls_slide_savings_and_status(self):
        section = _attach(
            _node("Savings VAVE activities"),
            [_node("Implemented idea", potential=25.0, realized=18.0,
                   status="Completed")],
        )
        project = {
            "name": "VAVE",
            "metadata": {},
            "roots": [section],
            "is_vave": True,
        }
        tmp, wb = self._export([project])
        self.addCleanup(tmp.cleanup)

        data = wb["VAVE VAVE Data"]
        self.assertEqual(data["K2"].value, 18.0)
        self.assertEqual(data["L2"].value, "Realized")
        self.assertEqual(data["N2"].value, "REALIZED")
        preview = wb["Savings VAVE activities"]
        self.assertEqual(preview["G6"].value, 18.0)
        self.assertEqual(preview["I6"].value, "REALIZED")

    def test_more_than_fourteen_rows_creates_continuation_sheet(self):
        section = _attach(
            _node("Large VAVE activities"),
            [_node(f"Idea {i}", potential=float(i)) for i in range(1, 16)],
        )
        project = {
            "name": "VAVE",
            "metadata": {},
            "roots": [section],
            "is_vave": True,
        }
        tmp, wb = self._export([project])
        self.addCleanup(tmp.cleanup)

        self.assertIn("Large VAVE activities", wb.sheetnames)
        self.assertIn("Large VAVE activities (2)", wb.sheetnames)
        self.assertEqual(wb["Large VAVE activities (2)"]["A1"].value,
                         "Large VAVE activities (continued)")
        data = wb["VAVE VAVE Data"]
        self.assertEqual(data["D16"].value, 2)
        self.assertEqual(data["E16"].value, "Full")
        self.assertEqual(data["F16"].value, 1)


if __name__ == "__main__":
    unittest.main()
