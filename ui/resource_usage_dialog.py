"""Resource usage rundown, filters, allocation lanes, and settings UI."""
from datetime import datetime, timedelta

from utils.config_manager import ConfigManager
from utils.workday_calculator import WorkdayCalculator

from PyQt6.QtCore import QDate, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFormLayout, QHeaderView, QHBoxLayout, QLabel, QMessageBox,
    QScrollArea, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)


def _next_weekday(text, holidays=None, exclude_weekends=None):
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except (TypeError, ValueError):
        return "—"
    try:
        res = WorkdayCalculator.get_next_workday(
            text, holidays=holidays, exclude_weekends=exclude_weekends)
        return res or "—"
    except (TypeError, ValueError):
        return "—" 


class AllocationTimeline(QWidget):
    """Compact one-lane-per-resource allocation overview."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.assignments = []
        self.conflicts = []
        self.setMinimumHeight(220)

    def set_data(self, assignments, conflicts):
        self.assignments = list(assignments)
        self.conflicts = list(conflicts)
        counts = {}
        for assignment in self.assignments:
            counts[assignment.resource_id] = counts.get(assignment.resource_id, 0) + 1
        height = sum(max(54, 30 + count * 18) for count in counts.values())
        self.setMinimumHeight(max(220, height + 55))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        if not self.assignments:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "No resource assignments match these filters")
            return
        starts = [datetime.strptime(a.start_date, "%Y-%m-%d")
                  for a in self.assignments]
        ends = [datetime.strptime(a.end_date, "%Y-%m-%d")
                for a in self.assignments]
        first, last = min(starts), max(ends)
        span = max(1, (last - first).days + 1)
        left, right = 170, 18
        width = max(1, self.width() - left - right)
        by_resource = {}
        for assignment in self.assignments:
            by_resource.setdefault(
                (assignment.resource_id, assignment.resource_label), []).append(assignment)
        conflict_lookup = {}
        for conflict in self.conflicts:
            for assignment_id in conflict.assignment_ids:
                conflict_lookup.setdefault(assignment_id, []).append(conflict)
        y = 35
        for (_resource_id, label), assignments in by_resource.items():
            painter.setPen(QColor("#344054"))
            painter.drawText(8, y + 19, 156, 22,
                             Qt.AlignmentFlag.AlignVCenter, label)
            painter.setPen(QPen(QColor("#e4e7ec"), 1))
            painter.drawLine(left, y + 31, left + width, y + 31)
            for offset, assignment in enumerate(assignments):
                start = datetime.strptime(assignment.start_date, "%Y-%m-%d")
                end = datetime.strptime(assignment.end_date, "%Y-%m-%d")
                x = left + int((start - first).days / span * width)
                w = max(6, int(((end - start).days + 1) / span * width))
                bar_y = y + offset * 18
                painter.fillRect(x, bar_y, w, 9, QColor("#93c5fd"))
                painter.setPen(QColor("#1e3a5f"))
                painter.drawText(x + 2, bar_y - 10, max(50, w), 18,
                                 Qt.AlignmentFlag.AlignLeft,
                                 f"{assignment.task_name} · {assignment.location_label}")
                for conflict in conflict_lookup.get(assignment.id, []):
                    c_start = datetime.strptime(conflict.overlap_start, "%Y-%m-%d")
                    c_end = datetime.strptime(conflict.overlap_end, "%Y-%m-%d")
                    cx = left + int((c_start - first).days / span * width)
                    cw = max(5, int(((c_end - c_start).days + 1) / span * width))
                    color = QColor("#f59e0b") if conflict.resolution_state == "accepted" else QColor("#dc2626")
                    painter.fillRect(cx, bar_y, cw, 9, color)
                    painter.setPen(QColor("#ffffff") if cw >= 70 else color.darker(130))
                    label_y = bar_y + 8 if cw >= 70 else bar_y + 20
                    painter.drawText(cx + 2, label_y,
                                     f"{conflict.overlap_workdays}d overlap")
            y += max(54, 30 + len(assignments) * 18)
        painter.setPen(QColor("#667085"))
        painter.drawText(left, 16, first.strftime("%Y-%m-%d"))
        painter.drawText(left + width - 90, 16, 100, 20,
                         Qt.AlignmentFlag.AlignRight, last.strftime("%Y-%m-%d"))


class ResourceUsageDialog(QDialog):
    jumpRequested = pyqtSignal(str, str)

    def __init__(self, analysis, focus_resource_id=None, parent=None):
        super().__init__(parent)
        self.analysis = analysis
        self.setWindowTitle("Resource Usage & Conflicts")
        self.resize(1180, 720)
        layout = QVBoxLayout(self)

        filters = QHBoxLayout()
        self.type_filter = QComboBox()
        self.resource_filter = QComboBox()
        self.project_filter = QComboBox()
        self.location_filter = QComboBox()
        for label, widget in (("Type", self.type_filter),
                              ("Resource", self.resource_filter),
                              ("Project", self.project_filter),
                              ("Room / location", self.location_filter)):
            filters.addWidget(QLabel(label + ":"))
            filters.addWidget(widget)
        self.start_filter = QDateEdit()
        self.end_filter = QDateEdit()
        for widget in (self.start_filter, self.end_filter):
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy-MM-dd")
        filters.addWidget(QLabel("From:"))
        filters.addWidget(self.start_filter)
        filters.addWidget(QLabel("To:"))
        filters.addWidget(self.end_filter)
        self.conflicts_only = QCheckBox("Conflicts only")
        self.include_completed = QCheckBox("Include completed")
        filters.addWidget(self.conflicts_only)
        filters.addWidget(self.include_completed)
        layout.addLayout(filters)

        self.tabs = QTabWidget()
        self.summary = QTableWidget(0, 6)
        self.summary.setHorizontalHeaderLabels([
            "Resource", "Type", "Scheduled uses", "Planned workdays",
            "Conflicts", "After last use"])
        self.details = QTableWidget(0, 9)
        self.details.setHorizontalHeaderLabels([
            "Resource", "Project", "Task path", "Location", "Start", "End",
            "Workdays", "Status", "Conflict"])
        for table in (self.summary, self.details):
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.details.itemDoubleClicked.connect(self._jump)
        self.timeline = AllocationTimeline()
        timeline_scroll = QScrollArea()
        timeline_scroll.setWidgetResizable(True)
        timeline_scroll.setWidget(self.timeline)
        self.tabs.addTab(self.summary, "Summary")
        self.tabs.addTab(self.details, "Detailed Usage")
        self.tabs.addTab(timeline_scroll, "Allocation Timeline")
        layout.addWidget(self.tabs, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._populate_filters(focus_resource_id)
        for widget in (self.type_filter, self.resource_filter,
                       self.project_filter, self.location_filter):
            widget.currentIndexChanged.connect(self.refresh)
        for widget in (self.start_filter, self.end_filter):
            widget.dateChanged.connect(self.refresh)
        self.conflicts_only.toggled.connect(self.refresh)
        self.include_completed.toggled.connect(self.refresh)
        self.refresh()

    def _populate_filters(self, focus_resource_id):
        assignments = self.analysis.assignments
        for combo, values in (
            (self.type_filter, sorted({a.resource_type for a in assignments})),
            (self.resource_filter, sorted({(a.resource_label, a.resource_id)
                                           for a in assignments})),
            (self.project_filter, sorted({a.project_name for a in assignments})),
            (self.location_filter, sorted({a.location_label for a in assignments})),
        ):
            combo.addItem("All", None)
            for value in values:
                if combo is self.resource_filter:
                    combo.addItem(value[0], value[1])
                else:
                    combo.addItem(value, value)
        dates = [QDate.fromString(value, "yyyy-MM-dd")
                 for a in assignments for value in (a.start_date, a.end_date)]
        valid = [date for date in dates if date.isValid()]
        self.start_filter.blockSignals(True)
        self.end_filter.blockSignals(True)
        self.start_filter.setDate(min(valid) if valid else QDate.currentDate().addYears(-1))
        self.end_filter.setDate(max(valid) if valid else QDate.currentDate().addYears(1))
        self.start_filter.blockSignals(False)
        self.end_filter.blockSignals(False)
        if focus_resource_id:
            index = self.resource_filter.findData(focus_resource_id)
            if index >= 0:
                self.resource_filter.setCurrentIndex(index)

    def _filtered(self):
        conflict_assignment_ids = {
            assignment_id for conflict in self.analysis.conflicts
            for assignment_id in conflict.assignment_ids}
        rows = []
        for assignment in self.analysis.assignments:
            if (self.type_filter.currentData() and
                    assignment.resource_type != self.type_filter.currentData()):
                continue
            if (self.resource_filter.currentData() and
                    assignment.resource_id != self.resource_filter.currentData()):
                continue
            if (self.project_filter.currentData() and
                    assignment.project_name != self.project_filter.currentData()):
                continue
            if (self.location_filter.currentData() and
                    assignment.location_label != self.location_filter.currentData()):
                continue
            if assignment.end_date < self.start_filter.date().toString("yyyy-MM-dd"):
                continue
            if assignment.start_date > self.end_filter.date().toString("yyyy-MM-dd"):
                continue
            if not self.include_completed.isChecked() and assignment.status == "Completed":
                continue
            if self.conflicts_only.isChecked() and assignment.id not in conflict_assignment_ids:
                continue
            rows.append(assignment)
        return rows

    def refresh(self):
        rows = self._filtered()
        visible_ids = {a.id for a in rows}
        conflicts = [c for c in self.analysis.conflicts
                     if any(value in visible_ids for value in c.assignment_ids)]
        conflicts_by_assignment = {}
        for conflict in conflicts:
            for assignment_id in conflict.assignment_ids:
                conflicts_by_assignment.setdefault(assignment_id, []).append(conflict)
        self.details.setRowCount(len(rows))
        for row, assignment in enumerate(rows):
            related = conflicts_by_assignment.get(assignment.id, [])
            conflict_text = "; ".join(
                f"{c.overlap_workdays}d {c.resolution_state}" for c in related)
            values = [assignment.resource_label, assignment.project_name,
                      assignment.task_path, assignment.location_label,
                      assignment.start_date, assignment.end_date,
                      str(assignment.workdays), assignment.status, conflict_text]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole,
                             (assignment.project_id, assignment.task_id))
                self.details.setItem(row, col, item)
        grouped = {}
        for assignment in rows:
            grouped.setdefault(assignment.resource_id, []).append(assignment)
        self.summary.setRowCount(len(grouped))
        for row, (_resource_id, uses) in enumerate(grouped.items()):
            ids = {a.id for a in uses}
            related = [c for c in conflicts if any(value in ids
                                                   for value in c.assignment_ids)]
            latest_use = max(uses, key=lambda a: a.end_date or "")
            cfg = ConfigManager.snapshot_project(getattr(latest_use, "project_id", None))
            after_last = _next_weekday(
                latest_use.end_date,
                holidays=cfg.get("holidays"),
                exclude_weekends=cfg.get("exclude_weekends", True)
            )
            values = [uses[0].resource_label, uses[0].resource_type,
                      str(len(uses)), str(sum(a.workdays for a in uses)),
                      str(len(related)), after_last]
            for col, value in enumerate(values):
                self.summary.setItem(row, col, QTableWidgetItem(value))
        self.timeline.set_data(rows, conflicts)

    def _jump(self, item):
        project_id, task_id = item.data(Qt.ItemDataRole.UserRole)
        self.accept()
        self.jumpRequested.emit(project_id, task_id)


class ResourceSettingsDialog(QDialog):
    def __init__(self, definitions, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Resource Settings")
        self.resize(1000, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Flag overlapping use is also available beside each Metadata option. "
            "Unchecked identities remain visible in usage and Story views."))
        self.table = QTableWidget(len(definitions), 8)
        self.table.setHorizontalHeaderLabels([
            "Resource", "Type", "Capacity", "Flag overlapping use", "Active",
            "Home location", "Notes", "Policy"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for row, definition in enumerate(definitions):
            label = QTableWidgetItem(definition.label)
            label.setData(Qt.ItemDataRole.UserRole, definition.id)
            label.setFlags(label.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, label)
            type_item = QTableWidgetItem(definition.type)
            type_item.setFlags(type_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 1, type_item)
            self.table.setItem(row, 2, QTableWidgetItem(str(definition.capacity)))
            for col, checked in ((3, definition.conflict_enabled),
                                 (4, definition.active)):
                item = QTableWidgetItem()
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if checked else
                                   Qt.CheckState.Unchecked)
                if definition.type.casefold() == "phase" and col == 3:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                self.table.setItem(row, col, item)
            self.table.setItem(row, 5, QTableWidgetItem(definition.home_location))
            self.table.setItem(row, 6, QTableWidgetItem(definition.notes))
            self.table.setItem(row, 7, QTableWidgetItem(definition.policy))
        layout.addWidget(self.table, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result_definitions(self):
        from utils.resource_allocation import ResourceDefinition
        results = []
        labels = set()
        for row in range(self.table.rowCount()):
            label_item = self.table.item(row, 0)
            label = label_item.text().strip()
            kind = self.table.item(row, 1).text().strip()
            key = (kind.casefold(), label.casefold())
            if key in labels:
                raise ValueError(f"Duplicate label '{label}' in {kind}")
            labels.add(key)
            try:
                capacity = max(1, int(self.table.item(row, 2).text()))
            except (TypeError, ValueError):
                raise ValueError(f"Capacity for '{label}' must be a whole number")
            policy = self.table.item(row, 7).text().strip().casefold() or "warning"
            if policy not in {"warning", "prohibited"}:
                raise ValueError(
                    f"Policy for '{label}' must be 'warning' or 'prohibited'")
            results.append(ResourceDefinition(
                str(label_item.data(Qt.ItemDataRole.UserRole)), kind, label,
                capacity,
                (kind.casefold() != "phase" and
                 self.table.item(row, 3).checkState() == Qt.CheckState.Checked),
                self.table.item(row, 4).checkState() == Qt.CheckState.Checked,
                self.table.item(row, 5).text().strip(),
                self.table.item(row, 6).text().strip(),
                policy))
        return results
