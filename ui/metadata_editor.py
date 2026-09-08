"""Editable metadata lists with spreadsheet-style rows and browser-like tabs."""
import uuid

from PyQt6.QtCore import QEvent, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QStyledItemDelegate, QTabBar, QTabWidget,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ui.identity_story_panel import IdentityStoryPanel
from utils.identity_story import (
    build_identity_story, find_duplicate_identities, identity_flag_overlaps,
)


TAB_LABELS = {
    "phase": "Project Phases",
    "activity": "Lab Testing",
    "room": "Rooms",
    "case": "Cases",
    "article": "Cassettes / Test Articles",
}


class EditableTabBar(QTabBar):
    """A tab bar whose labels can be edited in place with a double-click."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._editor = QLineEdit(self)
        self._editor.hide()
        self._editing_index = -1
        self._editor.returnPressed.connect(self._finish_edit)
        self._editor.editingFinished.connect(self._finish_edit)

    def mouseDoubleClickEvent(self, event):
        index = self.tabAt(event.position().toPoint())
        if index >= 0 and self.tabText(index) != "+":
            self.begin_edit(index)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def begin_edit(self, index):
        if index < 0 or index >= self.count() or self.tabText(index) == "+":
            return
        self._editing_index = index
        self._editor.setGeometry(self.tabRect(index).adjusted(2, 2, -2, -2))
        self._editor.setText(self.tabText(index))
        self._editor.selectAll()
        self._editor.show()
        self._editor.setFocus()

    def _finish_edit(self):
        if self._editing_index < 0:
            return
        index = self._editing_index
        self._editing_index = -1
        text = self._editor.text().strip()
        self._editor.hide()
        if text and index < self.count() and self.tabText(index) != "+":
            self.setTabText(index, text)


class _OptionDelegate(QStyledItemDelegate):
    def eventFilter(self, editor, event):
        if (event.type() == QEvent.Type.KeyPress and
                event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            self.commitData.emit(editor)
            self.closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.NoHint)
            QTimer.singleShot(0, self.parent().advance_row)
            return True
        return super().eventFilter(editor, event)


class OptionTable(QTableWidget):
    """Editable options plus an explicit, opt-in overlap-warning setting."""

    identityStoryRequested = pyqtSignal(str, str, str)

    def __init__(self, kind, header_id=None, parent=None):
        super().__init__(0, 3, parent)
        self.kind = kind
        self.header_id = header_id or f"header-{uuid.uuid4().hex}"
        self.setHorizontalHeaderLabels([
            "Option", "Timeline (days, optional)", "Flag overlapping use"])
        self.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        self.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeaderItem(2).setToolTip(
            "Checked: overlapping scheduled uses produce a conflict warning "
            "and appear as overlap bands on the Story timeline. "
            "Unchecked: reuse is allowed and Story draws no overlap graph.")
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed)
        self.setAlternatingRowColors(True)
        self.setItemDelegate(_OptionDelegate(self))
        # Do not inherit the operating system's purple selection accent.
        self.setStyleSheet("""
            QTableWidget { background: #ffffff; alternate-background-color: #f5f7fa;
                           color: #111111; gridline-color: #d0d5dd; }
            QTableWidget::item:selected { background: #dbeafe; color: #111111; }
        """)

    @staticmethod
    def _cell(text=""):
        return QTableWidgetItem(str(text or ""))

    def append_option(self, item=None):
        item = item or {}
        row = self.rowCount()
        self.insertRow(row)
        name = self._cell(item.get("name", ""))
        name.setData(Qt.ItemDataRole.UserRole,
                     item.get("id") or f"custom-{uuid.uuid4().hex}")
        self.setItem(row, 0, name)
        duration = item.get("duration")
        self.setItem(row, 1, self._cell(
            "" if duration in (None, "") else duration))
        flag = QTableWidgetItem()
        flag.setFlags((flag.flags() | Qt.ItemFlag.ItemIsUserCheckable) &
                      ~Qt.ItemFlag.ItemIsEditable)
        enabled = bool(item.get("flag_overlaps", False))
        flag.setCheckState(Qt.CheckState.Checked if enabled else
                           Qt.CheckState.Unchecked)
        flag.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        flag.setToolTip(
            "Enable only when overlapping scheduled use of this exact identity "
            "should be flagged and drawn on the Story timeline. "
            "Unchecked identities keep their history bars but hide overlap "
            "bands, shading, and labels.")
        if self.kind.casefold() == "phase":
            flag.setFlags(flag.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            flag.setToolTip("Project phases are repeatable and do not create conflicts.")
        self.setItem(row, 2, flag)
        return row

    def advance_row(self):
        row = self.currentRow() + 1
        if row >= self.rowCount():
            row = self.append_option()
        self.setCurrentCell(row, 0)
        self.editItem(self.item(row, 0))

    def delete_selected_rows(self):
        rows = sorted({index.row() for index in self.selectedIndexes()}, reverse=True)
        if not rows and self.currentRow() >= 0:
            rows = [self.currentRow()]
        for row in rows:
            self.removeRow(row)
        if not self.rowCount():
            self.append_option()

    def begin_edit(self):
        row = max(0, self.currentRow())
        column = max(0, self.currentColumn())
        if not self.rowCount():
            row = self.append_option()
        if column == 2:
            item = self.item(row, column)
            if item and item.flags() & Qt.ItemFlag.ItemIsEnabled:
                item.setCheckState(
                    Qt.CheckState.Unchecked
                    if item.checkState() == Qt.CheckState.Checked
                    else Qt.CheckState.Checked)
            return
        self.setCurrentCell(row, column)
        self.editItem(self.item(row, column))

    def mouseDoubleClickEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if index.isValid():
            if self.kind.casefold() == "phase":
                self.setCurrentCell(index.row(), index.column())
                self.editItem(self.item(index.row(), index.column()))
                event.accept()
                return
            name_item = self.item(index.row(), 0)
            name = name_item.text().strip() if name_item else ""
            identity_id = (name_item.data(Qt.ItemDataRole.UserRole)
                           if name_item else None)
            if name and identity_id:
                self.setCurrentCell(index.row(), index.column())
                self.identityStoryRequested.emit(
                    str(identity_id), name, self.kind)
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_F2:
            self.begin_edit()
            return
        if event.key() == Qt.Key.Key_Delete:
            parent = self.window()
            answer = QMessageBox.question(
                parent, "Delete Metadata Option",
                "Delete the selected metadata option?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                self.delete_selected_rows()
            return
        super().keyPressEvent(event)


class MetadataEditorDialog(QDialog):
    """Edit, rename, add, and remove metadata lists and their values."""

    jumpRequested = pyqtSignal(str, str)

    def __init__(self, items, parent=None, projects=None):
        super().__init__(parent)
        self.projects = list(projects or [])
        self.setWindowTitle("Metadata Lists")
        self.resize(1180, 680)
        self._campaigns = [dict(item) for item in items
                           if item.get("kind") == "campaign"]
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Double-click an identity to open its Story. F2 or Edit changes a value; "
            "double-click a tab to rename it; + adds a list. Check 'Flag overlapping "
            "use' only when overlapping schedules should create a warning and "
            "appear on the Story graph."))
        duplicates = find_duplicate_identities(items)
        if duplicates:
            warning = QLabel(
                f"⚠ {len(duplicates)} duplicate identity name(s) use different IDs. "
                "They remain separate; no history was merged.")
            warning.setStyleSheet(
                "QLabel { background:#fff7ed; color:#9a3412; padding:6px; "
                "border:1px solid #fdba74; }")
            layout.addWidget(warning)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setTabBar(EditableTabBar(self.tabs))
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.tables = {}
        self._build_tabs(items)
        self._plus_page = QWidget()
        self.tabs.addTab(self._plus_page, "+")
        self._refresh_close_buttons()
        left_layout.addWidget(self.tabs, 1)
        controls = QHBoxLayout()
        self.new_button = QPushButton("+ New")
        self.edit_button = QPushButton("Edit")
        self.delete_button = QPushButton("Delete")
        controls.addWidget(self.new_button)
        controls.addWidget(self.edit_button)
        controls.addWidget(self.delete_button)
        controls.addStretch(1)
        left_layout.addLayout(controls)
        self.new_button.clicked.connect(self._new_option)
        self.edit_button.clicked.connect(self._edit_option)
        self.delete_button.clicked.connect(self._delete_option)
        splitter.addWidget(left)
        self.story_panel = IdentityStoryPanel()
        self.story_panel.jumpRequested.connect(self.jumpRequested)
        splitter.addWidget(self.story_panel)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_tabs(self, items):
        usable = [item for item in items if item.get("kind") != "campaign"]
        for kind, default_label in TAB_LABELS.items():
            related = [item for item in usable if item.get("kind") == kind]
            header_record = next((item for item in related
                                  if item.get("record_type") == "header"), None)
            first_option = next((item for item in related
                                 if item.get("record_type") != "header"), None)
            label = ((header_record or first_option or {}).get("header") or
                     (header_record or {}).get("name") or default_label)
            table = self._add_list(kind, label,
                                   (header_record or {}).get("id"))
            for item in related:
                if item.get("record_type") != "header":
                    table.append_option(item)

        known = set(TAB_LABELS)
        custom_kinds = []
        for item in usable:
            kind = item.get("kind")
            if kind and kind not in known and kind not in custom_kinds:
                custom_kinds.append(kind)
        for kind in custom_kinds:
            related = [item for item in usable if item.get("kind") == kind]
            header_record = next((item for item in related
                                  if item.get("record_type") == "header"), None)
            first_option = next((item for item in related
                                 if item.get("record_type") != "header"), None)
            label = ((header_record or first_option or {}).get("header") or
                     (header_record or {}).get("name") or "New List")
            table = self._add_list(kind, label,
                                   (header_record or {}).get("id"))
            for item in related:
                if item.get("record_type") != "header":
                    table.append_option(item)

        for table in self.tables.values():
            if not table.rowCount():
                table.append_option()

    def _add_list(self, kind, label, header_id=None, before_plus=False):
        table = OptionTable(kind, header_id)
        table.identityStoryRequested.connect(self._show_story)
        self.tables[kind] = table
        index = self.tabs.count()
        if before_plus and hasattr(self, "_plus_page"):
            index = self.tabs.indexOf(self._plus_page)
            self.tabs.insertTab(index, table, label)
        else:
            self.tabs.addTab(table, label)
        return table

    def _current_table(self):
        table = self.tabs.currentWidget()
        return table if isinstance(table, OptionTable) else None

    def _new_option(self):
        table = self._current_table()
        if not table:
            return
        row = table.append_option()
        table.setCurrentCell(row, 0)
        table.begin_edit()

    def _edit_option(self):
        table = self._current_table()
        if table:
            table.begin_edit()

    def _delete_option(self):
        table = self._current_table()
        if not table:
            return
        answer = QMessageBox.question(
            self, "Delete Metadata Option",
            "Delete the selected metadata option?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            table.delete_selected_rows()

    def _flag_overlaps_for(self, identity_id):
        for table in self.tables.values():
            for row in range(table.rowCount()):
                name_item = table.item(row, 0)
                if (name_item and
                        str(name_item.data(Qt.ItemDataRole.UserRole) or "") ==
                        str(identity_id)):
                    flag = table.item(row, 2)
                    return bool(flag and
                                flag.checkState() == Qt.CheckState.Checked)
        return identity_flag_overlaps(identity_id)

    def _show_story(self, identity_id, label, kind):
        self.story_panel.set_story(build_identity_story(
            self.projects, identity_id, identity_label=label,
            identity_kind=kind,
            include_overlaps=self._flag_overlaps_for(identity_id)))

    def _tab_changed(self, index):
        if (hasattr(self, "_plus_page") and index >= 0 and
                self.tabs.widget(index) is self._plus_page):
            kind = f"custom_{uuid.uuid4().hex}"
            table = self._add_list(kind, "New List", before_plus=True)
            table.append_option()
            new_index = self.tabs.indexOf(table)
            self.tabs.setCurrentIndex(new_index)
            self._refresh_close_buttons()
            QTimer.singleShot(0, lambda: self.tabs.tabBar().begin_edit(new_index))

    def _refresh_close_buttons(self):
        plus_index = self.tabs.indexOf(getattr(self, "_plus_page", None))
        if plus_index >= 0:
            bar = self.tabs.tabBar()
            for side in (QTabBar.ButtonPosition.LeftSide,
                         QTabBar.ButtonPosition.RightSide):
                button = bar.tabButton(plus_index, side)
                if button:
                    button.hide()

    def _close_tab(self, index):
        page = self.tabs.widget(index)
        if page is getattr(self, "_plus_page", None) or not isinstance(page, OptionTable):
            return
        answer = QMessageBox.question(
            self, "Remove Metadata List",
            f"Remove '{self.tabs.tabText(index)}' and all of its options?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.tables.pop(page.kind, None)
        self.tabs.removeTab(index)
        page.deleteLater()
        self._refresh_close_buttons()

    def result_items(self):
        saved = list(self._campaigns)
        seen_labels = set()
        plus_page = getattr(self, "_plus_page", None)
        for index in range(self.tabs.count()):
            table = self.tabs.widget(index)
            if table is plus_page or not isinstance(table, OptionTable):
                continue
            header = self.tabs.tabText(index).strip() or "New List"
            saved.append({
                "id": table.header_id, "name": header, "kind": table.kind,
                "header": header, "record_type": "header", "version": 1,
            })
            for row in range(table.rowCount()):
                name_item = table.item(row, 0)
                name = name_item.text().strip() if name_item else ""
                if not name:
                    continue
                duplicate_key = (
                    table.kind.casefold(), " ".join(name.split()).casefold())
                if duplicate_key in seen_labels:
                    raise ValueError(
                        f"'{name}' appears more than once in '{header}'. "
                        "Resource labels must be unique within a list.")
                seen_labels.add(duplicate_key)
                timeline_item = table.item(row, 1)
                timeline_text = timeline_item.text().strip() if timeline_item else ""
                if timeline_text:
                    try:
                        duration = int(timeline_text)
                        if duration < 1:
                            raise ValueError
                    except ValueError:
                        raise ValueError(
                            f"Invalid duration '{timeline_text}' for '{name}' in '{header}'. "
                            "Duration must be a positive integer.")
                else:
                    duration = None
                overlap_item = table.item(row, 2)
                flag_overlaps = bool(
                    overlap_item and
                    overlap_item.checkState() == Qt.CheckState.Checked)
                saved.append({
                    "id": name_item.data(Qt.ItemDataRole.UserRole) or
                          f"custom-{uuid.uuid4().hex}",
                    "name": name, "kind": table.kind, "duration": duration,
                    "flag_overlaps": flag_overlaps,
                    "header": header, "sequence": "sequential", "version": 1,
                })
        return saved
