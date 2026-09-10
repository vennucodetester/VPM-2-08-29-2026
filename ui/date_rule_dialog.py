"""Compact, discoverable editor for Start and End scheduling rules."""
from datetime import datetime

from PyQt6.QtCore import QDate
from PyQt6.QtWidgets import (
    QButtonGroup, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QLabel, QPushButton, QSpinBox,
    QStackedWidget, QWidget,
)
from ui.context_picker import ContextPickerDialog


class DateRuleDialog(ContextPickerDialog):
    MODES = ("fixed", "same_as", "continue_after", "automatic")

    def __init__(self, node, all_nodes, field="start", initial_mode=None, parent=None):
        super().__init__(f"date_{field}", parent)
        self.node = node
        self.field = field
        self.all_nodes = [candidate for candidate in all_nodes if candidate.id != node.id]
        self.setWindowTitle(f"{field.title()} scheduling")
        self.setMinimumWidth(520)
        layout = self.picker_layout

        mode_row = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        labels = {
            "fixed": "Calendar / Fixed Date",
            "same_as": "Same As  (=)",
            "continue_after": "Continue After  (+)",
            "automatic": "Automatic",
        }
        self.mode_buttons = {}
        for index, mode in enumerate(self.MODES):
            button = QPushButton(labels[mode])
            button.setCheckable(True)
            button.setToolTip({
                "fixed": "Pin this field to a fixed date (the cell dropdown changes dates without pinning)",
                "same_as": "Match another task date exactly; shortcut =",
                "continue_after": "Start after another task; shortcut +",
                "automatic": "Let the scheduler control this field",
            }[mode])
            if field == "end" and mode == "continue_after":
                button.setEnabled(False)
                button.setToolTip("Continue After controls Start dates")
            self.mode_group.addButton(button, index)
            self.mode_buttons[mode] = button
            mode_row.addWidget(button)
        layout.addLayout(mode_row)

        self.pages = QStackedWidget()
        layout.addWidget(self.pages)
        self._build_fixed_page()
        self._build_same_page()
        self._build_continue_page()
        self._build_automatic_page()
        self.mode_group.idClicked.connect(self.pages.setCurrentIndex)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        current = node.start_rule if field == "start" else node.end_rule
        mode = initial_mode or current.get("mode", "automatic" if field == "start" else "duration")
        if mode == "duration":
            mode = "automatic"
        self.select_mode(mode)
        self._load_rule(current)

    def _task_combo(self):
        return self.searchable_combo(
            [("Select a task…", None)]
            + [(candidate.name, candidate.id) for candidate in self.all_nodes])

    def _build_fixed_page(self):
        page = QWidget(); form = QFormLayout(page)
        self.fixed_date = QDateEdit(QDate.currentDate())
        self.fixed_date.setCalendarPopup(True)
        self.fixed_date.setDisplayFormat("MM-dd-yy")
        form.addRow("Date", self.fixed_date)
        self.pages.addWidget(page)

    def _build_same_page(self):
        page = QWidget(); form = QFormLayout(page)
        self.same_task = self._task_combo()
        self.same_field = QComboBox(); self.same_field.addItems(["Start", "End"])
        form.addRow("Task", self.same_task); form.addRow("Match", self.same_field)
        self.pages.addWidget(page)

    def _build_continue_page(self):
        page = QWidget(); form = QFormLayout(page)
        self.after_task = self._task_combo()
        self.after_field = QComboBox(); self.after_field.addItems(["End", "Start"])
        self.offset = QSpinBox(); self.offset.setRange(0, 10000); self.offset.setSuffix(" extra day(s)")
        self.offset_unit = QComboBox(); self.offset_unit.addItem("Workdays", "workdays")
        self.offset_unit.addItem("Calendar days", "calendar_days")
        form.addRow("Task", self.after_task); form.addRow("Continue after", self.after_field)
        form.addRow("Offset", self.offset); form.addRow("Offset uses", self.offset_unit)
        self.pages.addWidget(page)

    def _build_automatic_page(self):
        page = QWidget(); form = QFormLayout(page)
        if self.field == "end":
            self.duration_days = QSpinBox(); self.duration_days.setRange(1, 10000)
            self.duration_days.setSuffix(" workday(s)")
            form.addRow("Duration", self.duration_days)
        else:
            self.duration_days = None
            form.addRow(QLabel("Start follows the project hierarchy and automatic sequencing."))
        self.pages.addWidget(page)

    def select_mode(self, mode):
        if mode == "continue_after" and self.field == "end":
            mode = "automatic"
        index = self.MODES.index(mode) if mode in self.MODES else 3
        self.mode_buttons[self.MODES[index]].setChecked(True)
        self.pages.setCurrentIndex(index)

    @staticmethod
    def _set_combo_data(combo, value):
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _load_rule(self, rule):
        if self.node.start_date if self.field == "start" else self.node.end_date:
            raw = self.node.start_date if self.field == "start" else self.node.end_date
            try:
                date = datetime.strptime(raw, "%Y-%m-%d")
                self.fixed_date.setDate(QDate(date.year, date.month, date.day))
            except (TypeError, ValueError):
                pass
        mode = rule.get("mode")
        if mode == "same_as":
            self._set_combo_data(self.same_task, rule.get("task_id"))
            self.same_field.setCurrentText(rule.get("field", self.field).title())
        elif mode == "continue_after":
            self._set_combo_data(self.after_task, rule.get("task_id"))
            self.after_field.setCurrentText(rule.get("field", "end").title())
            self.offset.setValue(int(rule.get("offset", 0) or 0))
            self._set_combo_data(self.offset_unit, rule.get("offset_unit", "workdays"))
        if self.duration_days is not None:
            self.duration_days.setValue(int(rule.get("days", self.node.duration or 1) or 1))

    def selected_mode(self):
        return self.MODES[self.mode_group.checkedId()]

    def result_rule(self):
        mode = self.selected_mode()
        self.remember_choice(mode)
        if mode == "fixed":
            return {"mode": "fixed", "date": self.fixed_date.date().toString("yyyy-MM-dd")}
        if mode == "same_as":
            self.remember_choice(self.same_task.currentData())
            return {"mode": "same_as", "task_id": self.same_task.currentData(),
                    "field": self.same_field.currentText().lower(), "offset": 0,
                    "offset_unit": "workdays"}
        if mode == "continue_after":
            self.remember_choice(self.after_task.currentData())
            return {"mode": "continue_after", "task_id": self.after_task.currentData(),
                    "field": self.after_field.currentText().lower(),
                    "offset": self.offset.value(),
                    "offset_unit": self.offset_unit.currentData()}
        if self.field == "end":
            return {"mode": "duration", "days": self.duration_days.value()}
        return {"mode": "automatic"}
