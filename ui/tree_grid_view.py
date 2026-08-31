from datetime import datetime, timedelta
from PyQt6.QtWidgets import (QTreeWidget, QTreeWidgetItem, QHeaderView, 
                            QAbstractItemView, QMenu, QMessageBox, QStyledItemDelegate,
                            QCalendarWidget, QDateEdit, QStyle, QStyleOptionButton,
                            QStyleOptionViewItem, QApplication)
from PyQt6.QtCore import (Qt, pyqtSignal, QPoint, QDate, QTimer, QRect,
                          QEvent, QItemSelectionModel)
from PyQt6.QtGui import QAction, QColor, QBrush, QKeySequence

from vpm_tracker_core import Columns, Colors, AppConstants, Status
from models.task_node import TaskNode
from ui.dialogs import BulkEditDialog, BulkPasteDialog, ImpactReviewDialog, LinkTaskDialog
from ui.date_rule_dialog import DateRuleDialog
from ui.inline_task_editor import InlineTaskEditor
from ui.header_filter import FilterHeaderView
from utils.workday_calculator import WorkdayCalculator
from utils import usage_logger

# Drag payload from the docked notes pad (see ui/notes_panel.py). Kept as a
# bare string literal to avoid importing the pad here (one-way dependency).
NOTE_MIME = "application/x-vpm-note"


def display_date(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%m-%d-%y")
    except ValueError:
        return value


def model_date(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%m-%d-%y", "%m/%d/%y", "%m-%d-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text


def money_text(value) -> str:
    if value in ("", None):
        return ""
    try:
        return f"${float(value):,.1f}"
    except (TypeError, ValueError):
        return ""


def parse_money(value):
    text = (value or "").strip()
    if not text:
        return None
    return float(text.replace("$", "").replace(",", ""))

class DateDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        editor = QDateEdit(parent)
        editor.setCalendarPopup(True)
        editor.setDisplayFormat("MM-dd-yy")

        # Explicitly highlight today in the calendar popup. Qt's default
        # outline can get swallowed by dark-theme stylesheets, so we set a
        # bold, accent-colored text format on today's cell.
        from PyQt6.QtGui import QTextCharFormat, QColor, QFont
        cal = editor.calendarWidget()
        if cal is not None:
            fmt = QTextCharFormat()
            fmt.setFontWeight(QFont.Weight.Bold)
            fmt.setForeground(QColor("#FFD54F"))  # amber — pops on dark + light
            fmt.setBackground(QColor(255, 213, 79, 60))  # subtle amber tint
            cal.setDateTextFormat(QDate.currentDate(), fmt)

        # Auto-open calendar popup
        def open_popup():
            from PyQt6.QtWidgets import QToolButton
            for child in editor.children():
                if isinstance(child, QToolButton):
                    child.animateClick()
                    return

        QTimer.singleShot(100, open_popup)
        return editor

    def setEditorData(self, editor, index):
        date_str = index.model().data(index, Qt.ItemDataRole.EditRole)
        if date_str:
            try:
                dt = datetime.strptime(model_date(date_str), "%Y-%m-%d")
                editor.setDate(QDate(dt.year, dt.month, dt.day))
            except ValueError:
                editor.setDate(QDate.currentDate())
        else:
            editor.setDate(QDate.currentDate())

    def setModelData(self, editor, model, index):
        date = editor.date()
        model.setData(index, date.toString("yyyy-MM-dd"), Qt.ItemDataRole.EditRole)

class StatusDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        from PyQt6.QtWidgets import QComboBox
        editor = QComboBox(parent)
        editor.addItems([s.value for s in Status if s.value != "Overdue"])
        QTimer.singleShot(100, editor.showPopup)
        return editor

    def setEditorData(self, editor, index):
        value = index.model().data(index, Qt.ItemDataRole.EditRole)
        if value:
            editor.setCurrentText(value)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)


class MoneyDelegate(QStyledItemDelegate):
    def displayText(self, value, locale):
        return money_text(value)


class RealizedMoneyDelegate(MoneyDelegate):
    def createEditor(self, parent, option, index):
        tree = self.parent()
        item = tree.itemFromIndex(index) if tree else None
        if item and item.node.vave_stage != "Savings Verified":
            QMessageBox.information(tree, "Savings Not Verified",
                "Set the VAVE stage to Savings Verified before entering realized savings.")
            return None
        return super().createEditor(parent, option, index)


class TaskNameDelegate(QStyledItemDelegate):
    """Continuous row entry plus inline contextual ``=`` lookup."""

    def editorEvent(self, event, model, option, index):
        """Open an Identity Story only when the pointer is on a chip."""
        if event.type() == QEvent.Type.MouseButtonDblClick:
            tree = self.parent()
            item = tree.itemFromIndex(index) if tree else None
            if item and hasattr(item, "node"):
                style_option = QStyleOptionViewItem(option)
                self.initStyleOption(style_option, index)
                text_rect = tree.style().subElementRect(
                    QStyle.SubElement.SE_ItemViewItemText, style_option, tree)
                cursor_x = text_rect.left()
                click_x = event.position().toPoint().x()
                conflicts = getattr(item.node, "resource_conflict_details", [])
                unresolved = {value.get("resource_id") for value in conflicts
                              if value.get("state") != "accepted"}
                accepted = {value.get("resource_id") for value in conflicts
                            if value.get("state") == "accepted"}
                for token in item.node.task_tokens:
                    marker = ("⚠ " if token.get("id") in unresolved else
                              "✓ " if token.get("id") in accepted else "")
                    chip = f"{marker}[{token.get('label', '')}]"
                    width = style_option.fontMetrics.horizontalAdvance(chip)
                    if cursor_x <= click_x <= cursor_x + width:
                        kind = str(token.get("kind") or "identity")
                        if (token.get("id") and token.get("label") and
                                kind.casefold() not in
                                {"phase", "campaign", "header"}):
                            tree.identity_story_requested.emit(
                                str(token["id"]), str(token["label"]), kind)
                            return True
                    cursor_x += width + style_option.fontMetrics.horizontalAdvance(" ")
        return super().editorEvent(event, model, option, index)

    def createEditor(self, parent, option, index):
        tree = self.parent()
        editor = InlineTaskEditor(tree._task_lookup_options(), parent)
        editor.setProperty("vpm_node_id", index.data(Qt.ItemDataRole.UserRole))
        editor.selectionCommitted.connect(self._commit_popup_selection)
        if getattr(tree, "_open_lookup_with_editor", False):
            tree._open_lookup_with_editor = False
            QTimer.singleShot(0, editor.start_lookup)
        return editor

    def setEditorData(self, editor, index):
        item = self.parent().itemFromIndex(index) if self.parent() else None
        if item and hasattr(item, "node"):
            tokens = list(item.node.task_tokens)
            if not tokens:
                from utils.resource_allocation import legacy_resource_id
                tokens = [{"id": legacy_resource_id(kind, value), "label": value,
                           "kind": kind, "duration": None,
                           "header": kind.title()}
                          for kind, value in item.node.resources.items() if value]
            editor_name = item.node.name
            if tokens and editor_name == tokens[0].get("label"):
                editor_name = ""
            editor.set_value(editor_name, tokens)
            editor.setProperty("vpm_node_id", item.node.id)
        else:
            editor.setText(index.data(Qt.ItemDataRole.EditRole) or "")

    def setModelData(self, editor, model, index):
        tree = self.parent()
        # Clicking the detached results popup causes a Windows focus-out before
        # QListWidget.itemPressed. At that instant the lookup is active but no
        # token has been appended yet. Treat this as an in-progress selection,
        # never as permission to overwrite the task with an empty value.
        if editor.lookup_active and not editor.tokens_for_commit():
            return
        item = tree.itemFromIndex(index) if tree else None
        # A click in the floating lookup can make Qt invalidate the delegate's
        # model index before it asks us to commit. The editor keeps the stable
        # task id, so fall back to it instead of silently saving a blank cell.
        if tree and (not item or not hasattr(item, "node")):
            item = tree._find_item_by_id(editor.property("vpm_node_id"))
            if item:
                index = tree.indexFromItem(item, Columns.TREE)
        if item and hasattr(item, "node"):
            self._apply_task_value(
                item, model, index, editor.tokens_for_commit(),
                editor.plain_name())
            return
        super().setModelData(editor, model, index)

    def _commit_popup_selection(self, payload):
        """Commit a popup choice by task id, independent of editor lifetime."""
        tree = self.parent()
        if not tree or not isinstance(payload, dict):
            return
        item = tree._find_item_by_id(payload.get("node_id"))
        if not item:
            return
        index = tree.indexFromItem(item, Columns.TREE)
        self._apply_task_value(
            item, tree.model(), index, payload.get("tokens") or [],
            payload.get("name") or "")
        QTimer.singleShot(
            0, lambda n=item.node, old={
                "tokens": payload.get("previous_tokens") or [],
                "name": payload.get("previous_name") or ""}:
            tree.resource_assignment_committed.emit(n, old))

    def _apply_task_value(self, item, model, index, tokens, name):
        tree = self.parent()
        node = item.node
        node.task_tokens = [dict(token) for token in tokens]
        node.resources = {}
        if not node.task_tokens:
            node.template_snapshot = {}
        duration = None
        for token in node.task_tokens:
            kind = (token.get("kind") or "").casefold()
            if kind in {"room", "case", "cassette", "article", "test article"}:
                key = ("article" if kind in
                       {"cassette", "article", "test article"} else kind)
                node.resources[key] = token.get("label", "")
            if token.get("duration") not in (None, ""):
                duration = max(1, int(token["duration"]))
                node.template_snapshot = dict(token)
        if duration is not None:
            node.end_rule = {"mode": "duration", "days": duration}
            tree.recalculate_all_dates()
        usage_logger.log("task_inline_lookup_apply", tokens=len(node.task_tokens),
                         duration=duration is not None)
        name = (name or "").strip()
        if not name:
            primary = next((token for token in node.task_tokens
                            if token.get("label")), None)
            if primary:
                name = primary.get("label", "")
        model.setData(index, name, Qt.ItemDataRole.EditRole)

    def eventFilter(self, editor, event):
        if event.type() == QEvent.Type.KeyPress:
            if editor.handle_lookup_key(event):
                return True
            tree = self.parent()
            node_id = editor.property("vpm_node_id")
            if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                self.commitData.emit(editor)
                self.closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.NoHint)
                QTimer.singleShot(0, lambda: tree._restructure_during_fast_entry(
                    node_id, event.key() == Qt.Key.Key_Backtab))
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                QTimer.singleShot(0, lambda: tree._finish_fast_row(node_id))
        return super().eventFilter(editor, event)

class WaitingOnDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        from PyQt6.QtWidgets import QComboBox
        from utils.config_manager import ConfigManager
        editor = QComboBox(parent)

        waiting_people = set(ConfigManager().get_owners())
        tree_view = self.parent()
        if tree_view and hasattr(tree_view, 'get_all_nodes_flat'):
            for n in tree_view.get_all_nodes_flat():
                if getattr(n, "waiting_on", ""):
                    waiting_people.add(n.waiting_on)

        editor.addItem("")
        editor.addItems(sorted(p for p in waiting_people if p))
        editor.setEditable(True) 
        QTimer.singleShot(100, editor.showPopup) # Open immediately
        return editor

    def setEditorData(self, editor, index):
        tree_view = self.parent()
        item = tree_view.itemFromIndex(index) if tree_view else None
        if item and hasattr(item, "node"):
            editor.setCurrentText(item.node.waiting_on or "")

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)

class NotesDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        # Return None to prevent default editor from opening
        return None

    def editorEvent(self, event, model, option, index):
        from PyQt6.QtCore import QEvent
        # Check for Double Click
        if event.type() == QEvent.Type.MouseButtonDblClick:
            self.open_dialog(index)
            return True # Event consumed
        return super().editorEvent(event, model, option, index)

    def open_dialog(self, index):
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QPlainTextEdit, QDialogButtonBox
        from PyQt6.QtGui import QShortcut, QKeySequence
        from models.task_node import TaskNode
        
        tree_view = self.parent()
        if not tree_view or not hasattr(tree_view, 'itemFromIndex'):
            return

        item = tree_view.itemFromIndex(index)
        if not item or not hasattr(item, 'node'):
            return
            
        node = item.node
        usage_logger.log("task_note_open", via="notes_cell")
        current_notes = node.notes
        today_tag = datetime.now().strftime("[%Y-%m-%d]: ")
        
        edit_text = current_notes
        cursor_pos = 0
        
        lines = edit_text.split('\n') if edit_text else []
        first_line = lines[0] if lines else ""
        
        is_blank_today = first_line.strip() == today_tag.strip()
        
        if is_blank_today:
            cursor_pos = len(today_tag)
        else:
            if edit_text:
                edit_text = today_tag + " \n" + edit_text
            else:
                edit_text = today_tag + " "
            cursor_pos = len(today_tag)
        
        dialog = QDialog(tree_view)
        dialog.setWindowTitle(f"Notes - {node.name}")
        dialog.resize(400, 300)
        layout = QVBoxLayout(dialog)
        
        text_edit = QPlainTextEdit(edit_text)
        layout.addWidget(text_edit)
        
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def escape_to_overall_notes():
            dialog.setProperty("clear_task_note_context", True)
            dialog.reject()

        escape_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), dialog)
        escape_shortcut.activated.connect(escape_to_overall_notes)
        
        cursor = text_edit.textCursor()
        cursor.setPosition(cursor_pos)
        text_edit.setTextCursor(cursor)
        text_edit.setFocus()
        
        if usage_logger.timed_exec(dialog, "notes_edit"):
            new_text = text_edit.toPlainText()
            node.notes = new_text
            was_blocked = tree_view.blockSignals(True)
            try:
                item.update_from_node()
            finally:
                tree_view.blockSignals(was_blocked)
            tree_view.item_changed_signal.emit(node)
        elif dialog.property("clear_task_note_context"):
            tree_view.clearSelection()
            usage_logger.log("task_note_escape_to_overall")

    def paint(self, painter, option, index):
        text = index.data()
        if text:
            first_line = text.split('\n')[0]
            opt = option
            opt.text = first_line
            super().paint(painter, opt, index)
        else:
            super().paint(painter, option, index)

class DelayDelegate(QStyledItemDelegate):
    """Double-click the Delay cell to log a timestamped delay reason."""

    def createEditor(self, parent, option, index):
        return None  # prevent default inline edit

    def editorEvent(self, event, model, option, index):
        from PyQt6.QtCore import QEvent
        if event.type() == QEvent.Type.MouseButtonDblClick:
            self._open_dialog(index)
            return True
        return super().editorEvent(event, model, option, index)

    def _open_dialog(self, index):
        from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                                     QPlainTextEdit, QLineEdit, QLabel,
                                     QDialogButtonBox, QPushButton)
        tree_view = self.parent()
        if not tree_view:
            return
        item = tree_view.itemFromIndex(index)
        if not item or not hasattr(item, 'node'):
            return
        node = item.node

        # Only allow logging when a baseline exists and there's a delay
        if node.baseline_duration is None:
            usage_logger.log("warning_shown", name="no_baseline")
            QMessageBox.information(tree_view, "No Baseline",
                "Set a baseline first (Edit → Set Baseline) before logging delays.")
            return

        try:
            total_diff = int(node.duration) - node.baseline_duration
        except (ValueError, TypeError):
            total_diff = 0
        # Slip not yet covered by a recorded revision — this entry's amount.
        new_slip = total_diff - node.logged_slip()

        today_str = datetime.now().strftime("%Y-%m-%d")
        next_rev = chr(ord('B') + len(node.revisions))
        slip_tag = f"+{new_slip}d" if new_slip > 0 else f"{new_slip}d"

        dialog = QDialog(tree_view)
        dialog.setWindowTitle(f"Record Real Delay — {node.name}")
        dialog.resize(460, 340)
        layout = QVBoxLayout(dialog)

        # Revision trail so far (Rev A baseline + confirmed slips)
        trail = node.revision_trail()
        if trail:
            layout.addWidget(QLabel("History:"))
            history = QPlainTextEdit(trail)
            history.setReadOnly(True)
            history.setMaximumHeight(140)
            layout.addWidget(history)

        # New revision input
        reason_edit = None
        if new_slip > 0:
            layout.addWidget(QLabel(
                f"New: Rev {next_rev}  {today_str}  {slip_tag}"
                f"  (end now {node.end_date or '?'}) — reason:"))
            reason_edit = QLineEdit()
            reason_edit.setPlaceholderText("e.g. Vendor late on parts")
            layout.addWidget(reason_edit)
            reason_edit.setFocus()
        else:
            layout.addWidget(QLabel("No new unlogged delay. History is shown read-only."))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        mark_btn = QPushButton("Mark On Track")
        set_btn = QPushButton("Record/Adjust Real Delay...")
        if node.children:
            mark_btn.setEnabled(False)
            set_btn.setEnabled(False)
        buttons.addButton(mark_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(set_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        mark_btn.clicked.connect(lambda: (tree_view.mark_on_track(node), dialog.accept()))
        set_btn.clicked.connect(lambda: (tree_view.record_real_delay(node), dialog.accept()))
        layout.addWidget(buttons)

        if usage_logger.timed_exec(dialog, "delay_log"):
            if new_slip <= 0 or reason_edit is None:
                return
            reason = reason_edit.text().strip()
            if not reason:
                usage_logger.log("warning_shown", name="delay_reason_required")
                QMessageBox.information(tree_view, "Reason Required",
                    "Enter a reason to record a real delay.")
                return
            node.log_delay_revision({
                "rev": next_rev,
                "date": today_str,
                "end": node.end_date or "",
                "slip": new_slip,
                "variance": total_diff,
                "reason": reason,
            })
            was_blocked = tree_view.blockSignals(True)
            try:
                item.update_from_node()
            finally:
                tree_view.blockSignals(was_blocked)
            tree_view.journal_event.emit(
                f"Delay logged — '{node.name}' {slip_tag}: {reason}")
            usage_logger.log("record_real_delay", outcome="applied")
            tree_view.item_changed_signal.emit(node)
class TaskTreeWidgetItem(QTreeWidgetItem):
    def __init__(self, node: TaskNode):
        super().__init__()
        self.node = node
        self.update_from_node()
        
    def update_from_node(self):
        conflict_details = getattr(self.node, "resource_conflict_details", [])
        unresolved_ids = {d.get("resource_id") for d in conflict_details
                          if d.get("state") != "accepted"}
        accepted_ids = {d.get("resource_id") for d in conflict_details
                        if d.get("state") == "accepted"}
        chips = []
        for token in self.node.task_tokens:
            marker = ("⚠ " if token.get("id") in unresolved_ids else
                      "✓ " if token.get("id") in accepted_ids else "")
            chips.append(f"{marker}[{token.get('label', '')}]")
        if not chips:
            chips = [f"[{key.title()}: {value}]"
                     for key, value in self.node.resources.items() if value]
        name = self.node.name
        if (self.node.task_tokens
                and name == self.node.task_tokens[0].get("label")):
            name = ""
        self.setText(Columns.TREE, " ".join(chips + [name]).strip())
        if self.node.template_snapshot or chips:
            template = self.node.template_snapshot.get("name", "Plain task")
            tooltip = f"Template: {template}\n" + "\n".join(chips)
            if self.node.schedule_conflicts:
                tooltip += "\n\nRESOURCE CONFLICT:\n" + "\n".join(
                    self.node.schedule_conflicts)
            self.setToolTip(Columns.TREE, tooltip)

        # Parallel Toggle (Checkbox styled as Radio)
        self.setCheckState(Columns.TREE, Qt.CheckState.Checked if self.node.is_parallel else Qt.CheckState.Unchecked)

        self.setData(Columns.START, Qt.ItemDataRole.DisplayRole, self._date_display("start"))
        self.setData(Columns.START, Qt.ItemDataRole.EditRole, self.node.start_date or "")
        self.setData(Columns.END, Qt.ItemDataRole.DisplayRole, self._date_display("end"))
        self.setData(Columns.END, Qt.ItemDataRole.EditRole, self.node.end_date or "")
        self.setToolTip(Columns.START, self._date_rule_tooltip("start"))
        self.setToolTip(Columns.END, self._date_rule_tooltip("end"))
        self.setText(Columns.DURATION, self.node.duration)
        val_potential = self.node.vave_display_potential()
        self.setData(Columns.POTENTIAL, Qt.ItemDataRole.DisplayRole, val_potential if val_potential is not None else "")
        self.setTextAlignment(Columns.POTENTIAL, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        val_realized = self.node.vave_display_realized()
        self.setData(Columns.REALIZED, Qt.ItemDataRole.DisplayRole, val_realized if val_realized is not None else "")
        self.setToolTip(Columns.POTENTIAL, f"VAVE stage: {self.node.vave_stage}")
        self.setToolTip(Columns.REALIZED,
                        f"VAVE stage: {self.node.vave_stage}. Realized savings are entered only when verified.")
        self.setTextAlignment(Columns.REALIZED, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setText(Columns.STATUS, self.node.status)
        waiting_text = self._waiting_display()
        self.setText(Columns.OWNER, waiting_text)
        self.setText(Columns.PREDECESSOR, self._predecessor_label())
        self.setText(Columns.NOTES, self.node.notes)

        # Delay column: duration vs baseline
        delay_text = ""
        delay_diff = 0
        if self.node.baseline_duration is not None:
            try:
                current = int(self.node.duration)
                delay_diff = current - self.node.baseline_duration
                if delay_diff > 0:
                    delay_text = f"+{delay_diff}d"
                elif delay_diff < 0:
                    delay_text = f"{delay_diff}d"
                else:
                    delay_text = "On track"
            except (ValueError, TypeError):
                pass
        elif self.node.start_date and self.node.end_date and self.node.duration:
            delay_text = "On track"
        # Parents: their "delay" is just a rollup of children — show it
        # muted with a ↑ marker so one slip never reads as two.
        is_parent = bool(self.node.children)
        if is_parent and delay_diff > 0:
            delay_text = f"↑ +{delay_diff}d"
        self.setText(Columns.DELAY, delay_text)
        # Tooltip: revision trail (Rev A/B/C…), legacy notes, or a hint.
        if is_parent and delay_diff > 0:
            self.setToolTip(Columns.DELAY, "Rolled up from delayed child tasks")
        else:
            trail = self.node.revision_trail()
            if self.node.revisions or self.node.delay_notes:
                self.setToolTip(Columns.DELAY, trail)
            elif delay_diff > 0:
                self.setToolTip(Columns.DELAY, "Double-click to log delay reason")
            else:
                self.setToolTip(Columns.DELAY, "")

        self.setFlags(self.flags() | Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsUserCheckable)

        # Status coloring: Red (overdue), Green (completed), Black (default)
        status_color = Colors.TEXT_WHITE

        is_completed = self.node.status == "Completed"
        is_overdue = self.check_is_overdue(self.node)

        if is_overdue:
            status_color = Colors.RED
        elif is_completed:
            status_color = Colors.GREEN
        else:
            status_color = Colors.TEXT_WHITE

        # Apply status color to all columns
        for col in range(Columns.COUNT):
            self.setForeground(col, QBrush(status_color))

        # Field-specific fixed dates remain visually distinct.
        if self.node.start_rule.get("mode") == "fixed":
            self.setForeground(Columns.START, QBrush(QColor("#FFD54F")))
        if self.node.end_rule.get("mode") == "fixed":
            self.setForeground(Columns.END, QBrush(QColor("#FFD54F")))

        if self.node.children and self.node.vave_potential is None and self.node.vave_display_potential() is not None:
            self.setForeground(Columns.POTENTIAL, QBrush(QColor("#666666")))
        if self.node.children and self.node.vave_realized is None and self.node.vave_display_realized() is not None:
            self.setForeground(Columns.REALIZED, QBrush(QColor("#666666")))

        # Delay column color (overrides status color for this column)
        if self.node.baseline_duration is not None or delay_text == "On track":
            if is_parent and delay_diff > 0:
                self.setForeground(Columns.DELAY, QBrush(QColor("#9E9E9E")))  # gray rollup
            elif delay_diff > 0:
                self.setForeground(Columns.DELAY, QBrush(QColor("#FF0000")))  # red
            elif delay_diff < 0:
                self.setForeground(Columns.DELAY, QBrush(QColor("#00C853")))  # green
            else:
                self.setForeground(Columns.DELAY, QBrush(Colors.GRAY))

        if waiting_text:
            self.setForeground(Columns.OWNER, QBrush(QColor("#FF8F00")))
            tip = f"Waiting on {self.node.waiting_on}"
            if self.node.waiting_since:
                tip += f" since {self.node.waiting_since}"
            self.setToolTip(Columns.OWNER, tip)
        else:
            self.setToolTip(Columns.OWNER, "")

        # Predecessor label colouring:
        #   explicit link  → white (user choice — already set above)
        #   manual date    → amber (stands out; user opted in deliberately)
        #   auto (∥ or ↑)  → gray (scheduler-managed, not a user decision)
        if self.node.dates_locked:
            self.setForeground(Columns.PREDECESSOR, QBrush(QColor("#FFD54F")))  # amber
        elif self._predecessor_is_implicit():
            self.setForeground(Columns.PREDECESSOR, QBrush(Colors.GRAY))

        # "This week" background highlight — light blue tint on rows
        # whose date range overlaps the current Mon–Fri week.
        this_week_bg = QBrush(QColor("#E3F2FD"))
        default_bg = QBrush(QColor(0, 0, 0, 0))  # transparent
        is_this_week = self._overlaps_this_week(self.node)
        for col in range(Columns.COUNT):
            self.setBackground(col, this_week_bg if is_this_week else default_bg)

    def _predecessor_label(self) -> str:
        """Render 'Depends On' cell.

        '⇦ Name'        — explicit predecessor link set by the user.
        '📌 Manual date' — start/end typed by hand (dates_locked=True).
        '∥ Parent name'  — Radio ON: task runs in parallel with its parent
                           (start = parent.start always).
        '↑ Name'         — Radio OFF: auto-chain from previous sibling.
        ''               — first child or root with no link.
        """
        start_rule = self.node.start_rule
        if start_rule.get("mode") in {"continue_after", "same_as"}:
            tree = self.treeWidget()
            name = None
            if tree is not None and hasattr(tree, 'resolve_node_name'):
                name = tree.resolve_node_name(start_rule.get("task_id"))
            symbol = "+" if start_rule.get("mode") == "continue_after" else "="
            field = start_rule.get("field", "end").title()
            return f"{symbol} {name} · {field}" if name else f"{symbol} (missing)"

        if self.node.dates_locked:
            return "📌 Manual date"

        if self.node.is_parallel and self.node.parent:
            return f"∥ {self.node.parent.name}"

        prev = self._implicit_predecessor()
        if prev is not None:
            return f"↑ {prev.name}"
        return ""

    def _date_display(self, field: str) -> str:
        value = self.node.start_date if field == "start" else self.node.end_date
        rule = self.node.start_rule if field == "start" else self.node.end_rule
        marker = {"fixed": "📌", "same_as": "=", "continue_after": "+"}.get(
            rule.get("mode"), "")
        rendered = display_date(value or "")
        return f"{rendered}  {marker}".rstrip()

    def _date_rule_tooltip(self, field: str) -> str:
        rule = self.node.start_rule if field == "start" else self.node.end_rule
        mode = rule.get("mode")
        if mode in {"same_as", "continue_after"}:
            tree = self.treeWidget()
            name = tree.resolve_node_name(rule.get("task_id")) if tree else ""
            relation = "Same as" if mode == "same_as" else "Continue after"
            explanation = f"{relation} {name or '(missing task)'} · {rule.get('field', field).title()}"
            return self._with_schedule_explanation(explanation)
        if mode == "automatic" and field == "start":
            previous = self._implicit_predecessor()
            if previous:
                return self._with_schedule_explanation(
                    f"Automatic: first workday after {previous.name} · End")
            if self.node.parent:
                return self._with_schedule_explanation(
                    f"Automatic: {self.node.parent.name} · Start")
        return self._with_schedule_explanation(self.node.rule_explanation(field))

    def _with_schedule_explanation(self, base):
        extra = []
        if self.node.schedule_reason:
            extra.append(self.node.schedule_reason)
        extra.extend(f"CONFLICT: {text}" for text in self.node.schedule_conflicts)
        return "\n".join([base] + extra)

    def _waiting_display(self) -> str:
        if not self.node.waiting_on:
            return ""
        days = 0
        if self.node.waiting_since:
            try:
                since = datetime.strptime(self.node.waiting_since, "%Y-%m-%d").date()
                days = max(0, (datetime.now().date() - since).days)
            except ValueError:
                days = 0
        return f"Waiting on {self.node.waiting_on} ({days}d)"

    def _implicit_predecessor(self):
        """Previous sibling the scheduler would chain from when no explicit link is set."""
        parent = self.node.parent
        if not parent:
            return None
        try:
            idx = parent.children.index(self.node)
        except ValueError:
            return None
        if idx <= 0:
            return None
        return parent.children[idx - 1]

    def _predecessor_is_implicit(self) -> bool:
        """True for auto-generated labels (gray); False for user-chosen labels (white)."""
        if self.node.predecessor_id:
            return False   # explicit link — user choice, show white
        if self.node.dates_locked:
            return False   # manual date — show in amber below
        return True        # parallel-snap or sibling-chain — auto, show gray

    def check_is_overdue(self, node: TaskNode) -> bool:
        """Red only when end date is in the past and task is not Completed.
        (Schedule conflicts with a locked parent are NOT overdue — they are
        shown via validate_child_dates dialogs instead.)
        """
        # 1. Check Self: past end date and not complete
        if node.status != "Completed" and node.end_date:
            try:
                end_dt = datetime.strptime(node.end_date, "%Y-%m-%d")
                if end_dt.date() < datetime.now().date():
                    return True
            except ValueError:
                pass

        # 2. Check Children (Recursive) — parent turns red if any child is overdue
        for child in node.children:
            if self.check_is_overdue(child):
                return True

        return False

    @staticmethod
    def _overlaps_this_week(node: TaskNode) -> bool:
        """True if the task's date range overlaps the current Mon–Sun week,
        or if any child does (so parents highlight when collapsed)."""
        today = datetime.now().date()
        # Monday of this week through Sunday
        week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=6)
        try:
            if node.start_date and node.end_date:
                s = datetime.strptime(node.start_date, "%Y-%m-%d").date()
                e = datetime.strptime(node.end_date, "%Y-%m-%d").date()
                if s <= week_end and e >= week_start:
                    return True
        except ValueError:
            pass
        # Check children so parents highlight too
        for child in node.children:
            if TaskTreeWidgetItem._overlaps_this_week(child):
                return True
        return False

class TreeGridView(QTreeWidget):
    item_changed_signal = pyqtSignal(TaskNode) # Signal when data changes
    # One human-readable line per user action, consumed by the project's
    # activity journal. NOTE: must be emitted while this widget's signals
    # are NOT blocked (on_item_changed queues lines and flushes at the end).
    journal_event = pyqtSignal(str)
    # Emitted with the raw dragged text after a note from the docked pad has
    # been consumed (promoted to a task or appended to a task's notes), so the
    # pad can drop that line.
    note_consumed = pyqtSignal(str)
    resource_assignment_committed = pyqtSignal(object, object)
    identity_story_requested = pyqtSignal(str, str, str)

    # Task clipboard for cut/copy/paste. Class-level on purpose: it is
    # shared by every project tab, which is what makes cross-project
    # paste work.
    _task_clipboard: list = []

    def __init__(self, parent=None):
        super().__init__(parent)
        self.root_nodes = [] # List of top-level TaskNodes
        self.active_filters = {} # {column: set_of_allowed_values}
        self.is_updating = False # Flag to prevent recursion
        self._zoom_factor = 1.0
        self.resource_lists = {"case": [], "room": [], "article": []}
        
        # Linking Mode State
        self.linking_mode = False
        self.linking_source_node = None
        self.linking_field = "start"
        self.linking_rule_mode = "continue_after"
        
        self.setup_ui()

    def setup_ui(self):
        self.setHeaderLabels(Columns.NAMES)
        
        # Custom Filter Header
        self.filter_header = FilterHeaderView(Qt.Orientation.Horizontal, self)
        self.setHeader(self.filter_header)
        self.filter_header.filter_changed.connect(self.apply_column_filter)
        
        header = self.header()
        header.setStretchLastSection(False)
        for col in range(Columns.COUNT):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(Columns.TREE, 330)
        header.resizeSection(Columns.START, 92)
        header.resizeSection(Columns.END, 92)
        header.resizeSection(Columns.DURATION, 78)
        header.resizeSection(Columns.POTENTIAL, 105)
        header.resizeSection(Columns.REALIZED, 105)
        header.resizeSection(Columns.STATUS, 120)
        header.resizeSection(Columns.OWNER, 115)
        header.resizeSection(Columns.PREDECESSOR, 125)
        header.resizeSection(Columns.NOTES, 420)
        header.resizeSection(Columns.DELAY, 85)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.set_vave_enabled(False)
        
        # Enable Extended Selection (Shift/Ctrl)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Signals
        self.itemChanged.connect(self.on_item_changed)
        self.itemCollapsed.connect(self.on_item_collapsed)
        self.itemExpanded.connect(self.on_item_expanded)
        
        # Delegates
        self.setItemDelegateForColumn(Columns.TREE, TaskNameDelegate(self))
        self.setItemDelegateForColumn(Columns.START, DateDelegate(self))
        self.setItemDelegateForColumn(Columns.END, DateDelegate(self))
        self.setItemDelegateForColumn(Columns.POTENTIAL, MoneyDelegate(self))
        self.setItemDelegateForColumn(Columns.REALIZED, RealizedMoneyDelegate(self))
        self.setItemDelegateForColumn(Columns.STATUS, StatusDelegate(self))
        self.setItemDelegateForColumn(Columns.OWNER, WaitingOnDelegate(self))
        self.setItemDelegateForColumn(Columns.NOTES, NotesDelegate(self))
        self.setItemDelegateForColumn(Columns.DELAY, DelayDelegate(self))
        
        # Adjustable Row Height (Global)
        # QTreeWidget items don't have a simple "setHeight". 
        # We can use a stylesheet to enforce minimum height.
        # Also style the indicator to look like a radio button (Round)
        self.apply_zoom(getattr(self, "_zoom_factor", 1.0))
        
        # Context Menu
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.open_context_menu)
        
        # Font Settings
        font = self.font()
        font.setPointSize(10)
        font.setBold(True)
        self.setFont(font)

        # Drag and Drop
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def set_vave_enabled(self, enabled: bool):
        self._vave_enabled = bool(enabled)
        self.setColumnHidden(Columns.POTENTIAL, not enabled)
        self.setColumnHidden(Columns.REALIZED, not enabled)

    def apply_zoom(self, factor: float):
        """Zoom = bigger TEXT, not bigger gaps. The grid owns its font (bold),
        so the app-wide zoom font never reaches it — scale it here directly.
        Row height hugs the text: it stays at the familiar 30px until the font
        actually needs more room, so zooming never just spreads the lines out."""
        from PyQt6.QtGui import QFontMetrics
        self._zoom_factor = factor
        font = self.font()
        font.setPointSizeF(max(6.0, 10.0 * factor))
        font.setBold(True)
        self.setFont(font)
        text_h = QFontMetrics(font).height()
        row_h = max(30, text_h + 8)
        indicator = max(8, int(10 * factor))
        radius = indicator // 2
        self.setStyleSheet(f"""
            QTreeView::item {{ height: {row_h}px; }}
            QTreeView::indicator {{ width: {indicator}px; height: {indicator}px; border-radius: {radius}px; border: 1px solid #999; }}
            QTreeView::indicator:checked {{ background-color: #999; border: 1px solid #999; image: none; }}
            QTreeView::indicator:unchecked {{ background-color: transparent; }}
        """)

    def event(self, e):
        """Intercept Tab/Shift+Tab before Qt's focus system and the view's
        own tab-navigation consume them — keyPressEvent never sees Tab,
        which is why indent/outdent silently moved the cursor instead."""
        if (e.type() == QEvent.Type.KeyPress
                and e.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)
                and self.state() != QAbstractItemView.State.EditingState):
            current = self.currentItem()
            if isinstance(current, TaskTreeWidgetItem):
                if e.key() == Qt.Key.Key_Tab:
                    self.indent_smart(current)
                else:
                    self.outdent_smart(current)
                return True
        return super().event(e)

    def keyPressEvent(self, event):
        if self.linking_mode and event.key() == Qt.Key.Key_Escape:
            self._cancel_linking_mode()
            return
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection_to_clipboard()
            return

        # Keyboard-first flow (only when no cell editor is open):
        #   Enter  → new task below the current row, name in edit mode
        #   Delete → confirm popup (Enter = yes) then delete the row
        #   (Tab / Shift+Tab handled in event() above)
        editing = self.state() == QAbstractItemView.State.EditingState
        current = self.currentItem()
        if not editing and isinstance(current, TaskTreeWidgetItem):
            key = event.key()
            column = self.currentColumn()
            if event.text() in ("=", "+") and column in (
                    Columns.START, Columns.END, Columns.PREDECESSOR):
                mode = "same_as" if event.text() == "=" else "continue_after"
                field = "start" if column == Columns.PREDECESSOR else (
                    "start" if column == Columns.START else "end")
                if mode == "continue_after" and field == "end":
                    self._show_link_hint("Continue After controls Start, not End.")
                    return
                self.start_date_pick_mode(current, field, mode)
                return
            if event.text() == "=" and column == Columns.TREE:
                self._open_lookup_with_editor = True
                self.editItem(current, Columns.TREE)
                return
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if column in (Columns.START, Columns.END, Columns.PREDECESSOR):
                    field = "start" if column in (Columns.START, Columns.PREDECESSOR) else "end"
                    self.open_date_choices(current, field)
                    return
                self.add_sibling_below(current)
                return
            if key == Qt.Key.Key_Delete:
                self._delete_with_confirm(current)
                return
        if (not editing and current is None
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            self.add_blank_root()
            return
        super().keyPressEvent(event)

    def _delete_with_confirm(self, item: 'TaskTreeWidgetItem'):
        """Delete-key flow: popup defaults to Yes so a bare Enter confirms —
        the whole add/indent/delete loop stays on the keyboard."""
        selected = [i for i in self.selectedItems()
                    if isinstance(i, TaskTreeWidgetItem)]
        count = max(len(selected), 1)
        label = (f"Delete '{item.node.name}'?" if count == 1
                 else f"Delete {count} selected task(s)?")
        box = QMessageBox(self)
        box.setWindowTitle("Delete task")
        box.setText(label)
        box.setStandardButtons(QMessageBox.StandardButton.Yes |
                               QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        if box.exec() == QMessageBox.StandardButton.Yes:
            self.delete_smart(item)

    def add_sibling_below(self, item: 'TaskTreeWidgetItem', begin_edit=True):
        """Insert a blank row so its structure can be set before typing."""
        anchor = item.node
        parent_node = anchor.parent
        new_node = TaskNode("", parent=parent_node)
        new_node.update_from_previous_sibling(anchor)

        siblings = parent_node.children if parent_node else self.root_nodes
        try:
            idx = siblings.index(anchor) + 1
        except ValueError:
            idx = len(siblings)
        siblings.insert(idx, new_node)
        self._baseline_new_task_on_track(new_node)

        was_blocked = self.blockSignals(True)
        try:
            new_item = TaskTreeWidgetItem(new_node)
            ui_parent = item.parent()
            if ui_parent:
                ui_parent.insertChild(ui_parent.indexOfChild(item) + 1, new_item)
            else:
                self.insertTopLevelItem(self.indexOfTopLevelItem(item) + 1, new_item)
        finally:
            self.blockSignals(was_blocked)

        self.journal_event.emit("Added blank task row")
        usage_logger.log("task_add", via="enter", count=1)
        self.commit_structure_change(new_node)
        self.setCurrentItem(new_item)
        self.setCurrentItem(new_item, Columns.TREE)
        if begin_edit:
            QTimer.singleShot(0, lambda: self.editItem(new_item, Columns.TREE))

    def _finish_fast_row(self, node_id):
        item = self._find_item_by_id(node_id)
        if (not item or (not item.node.name.strip()
                         and not item.node.task_tokens)):
            return
        usage_logger.log("task_row_complete", via="enter")
        self.add_sibling_below(item)

    def add_blank_root(self):
        node = TaskNode("")
        self.root_nodes.append(node)
        self._baseline_new_task_on_track(node)
        was_blocked = self.blockSignals(True)
        try:
            item = TaskTreeWidgetItem(node)
            self.addTopLevelItem(item)
        finally:
            self.blockSignals(was_blocked)
        self.journal_event.emit("Added blank task row")
        usage_logger.log("task_add", via="enter_empty_grid", count=1)
        self.commit_structure_change(node)
        self.setCurrentItem(item, Columns.TREE)
        QTimer.singleShot(0, lambda: self.editItem(item, Columns.TREE))

    def _restructure_during_fast_entry(self, node_id, outdent=False):
        item = self._find_item_by_id(node_id)
        if not item:
            return
        if outdent:
            self.outdent_smart(item)
        else:
            self.indent_smart(item)
        QTimer.singleShot(0, lambda: self.editItem(item, Columns.TREE))

    def _start_focused_task_lookup(self):
        editor = QApplication.focusWidget()
        if isinstance(editor, InlineTaskEditor):
            editor.start_lookup()

    def _task_lookup_options(self):
        from utils.template_catalog import load_templates
        options = []
        for entry in load_templates():
            kind = (entry.get("kind") or "activity").casefold()
            if kind == "campaign" or entry.get("record_type") == "header":
                continue
            definition = getattr(self, "resource_definitions", {}).get(
                entry.get("id"))
            if definition is not None and not definition.active:
                continue
            header = entry.get("header")
            if not header:
                if kind == "phase":
                    header = "Project Phases"
                elif kind == "activity":
                    header = "Lab Testing"
                else:
                    header = kind.replace("_", " ").title()
            options.append({
                "id": entry.get("id"), "label": entry.get("name", ""),
                "kind": kind, "duration": entry.get("duration"),
                "header": header,
            })
        labels = {"room": "Rooms", "case": "Cases", "article": "Cassettes / Test Articles"}
        from utils.resource_allocation import legacy_resource_id
        for kind, values in self.resource_lists.items():
            for value in values:
                resource_id = legacy_resource_id(kind, value)
                definition = getattr(self, "resource_definitions", {}).get(resource_id)
                if definition is not None and not definition.active:
                    continue
                options.append({"id": resource_id, "label": value,
                                "kind": kind, "duration": None,
                                "header": labels.get(kind, kind.title())})
        return options

    def _baseline_new_task_on_track(self, node: TaskNode):
        """Newly inserted planning rows are not delays by themselves."""
        if not self.has_baseline():
            return
        try:
            node.baseline_duration = int(node.duration)
        except (ValueError, TypeError):
            node.baseline_duration = None
        node.baseline_end = node.end_date
        node.revisions = []
        node.delay_notes = ""
        # The stamped baseline is a placeholder (1-day default dates). The
        # first real end/duration the user types re-baselines silently.
        node.baseline_provisional = True

    def _restamp_provisional_baseline(self, node: TaskNode):
        """First schedule entry on a fresh row: that IS the plan — stamp it as
        on-track (Rev A) with no popup and no delay. Later edits count."""
        try:
            node.baseline_duration = int(node.duration)
        except (ValueError, TypeError):
            node.baseline_duration = None
        node.baseline_end = node.end_date
        node.revisions = []
        node.baseline_provisional = False

    def quick_capture(self, text: str):
        """Drop a dateless task into the 'Inbox' root group (created on
        first use, pinned to the top). No dates → the scheduler ignores it
        until the task is moved into the real plan."""
        text = (text or "").strip()
        if not text:
            return
        inbox = next((r for r in self.root_nodes
                      if r.name.strip().lower() == "inbox"), None)
        was_blocked = self.blockSignals(True)
        try:
            if inbox is None:
                inbox = TaskNode("Inbox")
                inbox.dates_locked = True  # keep the scheduler's hands off
                # TaskNode defaults dates to today — Inbox must be dateless
                # so the scheduler skips it and it never shows as overdue.
                inbox.start_date = None
                inbox.end_date = None
                self.root_nodes.insert(0, inbox)
                inbox_item = TaskTreeWidgetItem(inbox)
                self.insertTopLevelItem(0, inbox_item)
            else:
                inbox_item = self._find_item_by_id(inbox.id)
            child = TaskNode(text, parent=inbox)
            child.start_date = None
            child.end_date = None
            inbox.children.append(child)
            if inbox_item is not None:
                child_item = TaskTreeWidgetItem(child)
                inbox_item.addChild(child_item)
                inbox_item.setExpanded(True)
        finally:
            self.blockSignals(was_blocked)
        self.journal_event.emit(f"Captured to Inbox: '{text}'")
        self.item_changed_signal.emit(child)
        self.update_filter_options()
            
    def copy_selection_to_clipboard(self):
        selected_items = self.selectedItems()
        if not selected_items:
            return
            
        # Sort by visual order
        # It's hard to sort by visual order easily without traversing, 
        # but let's just use the list order which might be random-ish or selection order.
        # For a simple copy, selection order is usually fine or we can try to sort.
        
        text_rows = []
        for item in selected_items:
            if isinstance(item, TaskTreeWidgetItem):
                # Tab-separated values
                row_data = [
                    item.text(Columns.TREE),
                    item.text(Columns.START),
                    item.text(Columns.END),
                    item.text(Columns.DURATION),
                    item.text(Columns.STATUS),
                    item.text(Columns.OWNER),
                    item.text(Columns.NOTES)
                ]
                text_rows.append("\t".join(row_data))
                
        clipboard_text = "\n".join(text_rows)
        QApplication.clipboard().setText(clipboard_text)

    def start_linking_mode(self, source_item: TaskTreeWidgetItem):
        """Enter click-to-link mode. No modal popup — cursor + status bar hint only."""
        self.start_date_pick_mode(source_item, "start", "continue_after")

    def start_date_pick_mode(self, source_item, field, mode):
        self.linking_mode = True
        self.linking_source_node = source_item.node
        self.linking_field = field
        self.linking_rule_mode = mode
        self.setCursor(Qt.CursorShape.CrossCursor)
        usage_logger.log("date_rule_open", field=field, shortcut=mode,
                         interaction="direct_grid_pick")
        relation = "same date as" if mode == "same_as" else "continue after"
        self._show_link_hint(
            f"{field.title()} for '{source_item.node.name}': click a task to {relation}. "
            f"Click the same task to return to automatic. Esc or right-click cancels."
        )

    def _show_link_hint(self, msg: str):
        """Write to the main window's status bar if one exists; silent otherwise."""
        w = self.window()
        status = getattr(w, 'statusBar', None)
        if callable(status):
            try:
                status().showMessage(msg, 8000)
            except Exception:
                pass

    def _cancel_linking_mode(self):
        if self.linking_mode:
            usage_logger.log("date_rule_apply", outcome="canceled",
                             interaction="direct_grid_pick")
        self.linking_mode = False
        self.linking_source_node = None
        self.linking_field = "start"
        self.linking_rule_mode = "continue_after"
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._show_link_hint("Linking cancelled.")

    def mousePressEvent(self, event):
        if self.linking_mode:
            # Right-click cancels cleanly.
            if event.button() == Qt.MouseButton.RightButton:
                self._cancel_linking_mode()
                return

            item = self.itemAt(event.position().toPoint())
            if not isinstance(item, TaskTreeWidgetItem):
                # Click on empty space: do nothing, stay in linking mode.
                return

            target_node = item.node
            source_node = self.linking_source_node

            source_item = self._find_item_by_id(source_node.id)
            field = self.linking_field
            mode = self.linking_rule_mode
            # Clicking the source row again clears the rule (toggle-off).
            if target_node.id == source_node.id:
                automatic = ({"mode": "automatic"} if field == "start"
                             else {"mode": "duration", "days": max(1, int(source_node.duration))})
                self._apply_date_rule(source_item, field, automatic)
                self.linking_mode = False
                self.linking_source_node = None
                self.setCursor(Qt.CursorShape.ArrowCursor)
                self._show_link_hint(f"Returned {field} to automatic on '{source_node.name}'.")
                return

            # Reject cycles: target cannot be a descendant or ancestor of source.
            if target_node in source_node.get_all_descendants():
                self._show_link_hint("Cannot link to a descendant (would create a cycle).")
                return
            ancestor = source_node.parent
            while ancestor:
                if ancestor.id == target_node.id:
                    self._show_link_hint("Cannot link to an ancestor (would create a cycle).")
                    return
                ancestor = ancestor.parent

            rule = {
                "mode": mode, "task_id": target_node.id,
                "field": field if mode == "same_as" else "end",
                "offset": 0, "offset_unit": "workdays",
            }
            if not self._apply_date_rule(source_item, field, rule):
                return
            self.linking_mode = False
            self.linking_source_node = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            symbol = "=" if mode == "same_as" else "+"
            self._show_link_hint(f"{source_node.name} {symbol} {target_node.name} applied.")
            return

        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """Double-click on the Depends On column jumps to the predecessor
        (explicit link or the implicit '↑ prev sibling') instead of opening
        an editor. Every other column behaves normally.
        """
        item = self.itemAt(event.position().toPoint())
        if isinstance(item, TaskTreeWidgetItem):
            col = self.columnAt(int(event.position().x()))
            if col in (Columns.START, Columns.END, Columns.PREDECESSOR):
                field = "start" if col in (Columns.START, Columns.PREDECESSOR) else "end"
                self.open_date_choices(
                    item, field, self.viewport().mapToGlobal(
                        self.visualItemRect(item).bottomLeft()))
                return
        super().mouseDoubleClickEvent(event)

    def load_project(self, nodes: list[TaskNode]):
        self.blockSignals(True)
        try:
            self.clear()
            self.root_nodes = nodes
            for node in nodes:
                self.add_node_to_tree(node, self.invisibleRootItem())
        finally:
            self.blockSignals(False)

        self.update_filter_options()

        # Refresh all item labels now that every item is attached to the tree.
        # _predecessor_label() calls treeWidget() to resolve a predecessor's name;
        # that call returns None during add_node_to_tree (item not yet in tree),
        # so all explicit predecessors would show "⇦ (missing)" until a later
        # refresh. Calling this here fixes the display immediately on load.
        # (refresh_entire_tree blocks signals internally — no popups during load.)
        self.refresh_entire_tree()

        # Do not reschedule during load. Migration must preserve every displayed
        # date until the user deliberately edits or refreshes the schedule.

    def add_node_to_tree(self, node: TaskNode, parent_item: QTreeWidgetItem):
        item = TaskTreeWidgetItem(node)
        parent_item.addChild(item)
        item.setExpanded(node.expanded)
        
        for child in node.children:
            self.add_node_to_tree(child, item)

    def update_filter_options(self):
        """Collect unique values for each column and pass to header."""
        # We need to traverse all items
        values = {col: set() for col in range(Columns.COUNT)}
        
        iterator = QTreeWidgetItemIterator(self)
        while iterator.value():
            item = iterator.value()
            for col in range(Columns.COUNT):
                text = item.text(col)
                if col == Columns.OWNER:
                    # Split by / or ,
                    parts = text.replace(',', '/').split('/')
                    for p in parts:
                        if p.strip():
                            values[col].add(p.strip())
                else:
                    values[col].add(text)
            iterator += 1
            
        for col, vals in values.items():
            self.filter_header.set_filter_values(col, list(vals))

    def apply_column_filter(self, column, allowed_values):
        if allowed_values is None:
            self.active_filters.pop(column, None)
        else:
            self.active_filters[column] = allowed_values
        usage_logger.log("filter_apply", column=column, values=len(allowed_values or []))
            
        self.filter_items()

    def filter_items(self):
        """Apply all active filters to the tree."""
        # Logic:
        # Iterate all items.
        # If an item matches ALL filters, it is visible.
        # If an item is visible, its parents must be visible (standard tree behavior).
        # If an item is hidden, its children are hidden (visually).
        
        # However, for a tree, usually if a child matches, we show the parent too.
        # Let's implement: Show item if it matches filters OR if any of its descendants match filters.
        
        # First pass: Determine match status for every item
        # We can do a bottom-up traversal or just recursive check.
        
        root = self.invisibleRootItem()
        self.filter_recursive(root)

    def filter_recursive(self, item):
        # Returns True if this item or any child is visible
        
        # Check if this item matches filters
        matches = True
        if self.active_filters:
            for col, allowed in self.active_filters.items():
                text = item.text(col)
                if col == Columns.OWNER:
                    # Intersection Logic
                    parts = set(p.strip() for p in text.replace(',', '/').split('/') if p.strip())
                    # If allowed contains any of parts, it's a match
                    # Note: allowed is a set of strings
                    if not parts.intersection(allowed):
                        matches = False
                        break
                else:
                    if text not in allowed:
                        matches = False
                        break
        
        # Check children
        child_visible = False
        for i in range(item.childCount()):
            child = item.child(i)
            if self.filter_recursive(child):
                child_visible = True
        
        # Visibility Logic:
        # Visible if (Matches Filters) OR (Has Visible Children)
        # Note: If filters are active, and I don't match, but my child does, I should be visible but maybe grayed out?
        # Standard behavior: Just show it.
        
        should_be_visible = matches or child_visible
        
        # If no filters are active, everything is visible
        if not self.active_filters:
            should_be_visible = True
            
        # Root item (invisible) doesn't need setHidden, but we return result for it
        if item != self.invisibleRootItem():
            item.setHidden(not should_be_visible)
            
        return should_be_visible

    def open_context_menu(self, position: QPoint):
        item = self.itemAt(position)
        usage_logger.log("menu_open", menu="task_context",
                         depth=0, on_row=bool(item))
        
        # Fallback: If clicked on empty space but have selection, use selection
        if not item and self.selectedItems():
            item = self.selectedItems()[0]
            
        menu = QMenu()
        
        if item:
            selected_items = self.selectedItems()
            
            # Bulk Operations for Selection (Status/Waiting/Link)
            if len(selected_items) > 1:
                status_action = QAction(f"Set Status ({len(selected_items)} items)...", self)
                status_action.triggered.connect(lambda: self.bulk_set_status(selected_items))
                menu.addAction(status_action)
                
                owner_action = QAction(f"Set Waiting On ({len(selected_items)} items)...", self)
                owner_action.triggered.connect(lambda: self.bulk_set_owner(selected_items))
                menu.addAction(owner_action)
                
                link_action = QAction(f"Link Selected Tasks ({len(selected_items)} items)", self)
                link_action.triggered.connect(lambda: self.link_selected_tasks(selected_items))
                menu.addAction(link_action)
                
                menu.addSeparator()

            # ---- Common, everyday actions stay at the top level ----
            add_action = QAction("Add Child Task", self)
            add_action.triggered.connect(lambda: self.add_child_task(item))
            menu.addAction(add_action)

            if item.node.waiting_on:
                clear_wait_action = QAction("Clear Waiting", self)
                clear_wait_action.triggered.connect(lambda: self.clear_waiting(item.node))
                menu.addAction(clear_wait_action)
            else:
                wait_action = QAction("Mark Waiting On...", self)
                wait_action.triggered.connect(lambda: self.mark_waiting_on(item.node))
                menu.addAction(wait_action)

            owner_action = QAction(
                "Change Owner..." if item.node.owner else "Assign Owner...", self)
            owner_action.triggered.connect(lambda: self.assign_owner(item.node))
            menu.addAction(owner_action)

            resource_tokens = item.node.resource_tokens()
            if resource_tokens:
                usage_action = QAction("Show Resource Usage…", self)
                usage_action.triggered.connect(
                    lambda checked=False, resource_id=resource_tokens[0].get("id"):
                    getattr(self.window(), "show_resource_usage")(resource_id))
                menu.addAction(usage_action)

            if getattr(self, "_vave_enabled", False):
                vave_action = QAction(
                    f"VAVE Stage: {item.node.vave_stage}...", self)
                vave_action.triggered.connect(lambda: self.set_vave_stage(item))
                menu.addAction(vave_action)

            menu.addSeparator()

            cut_action = QAction("Cut Task(s)", self)
            cut_action.triggered.connect(self.cut_selected_tasks)
            menu.addAction(cut_action)

            copy_action = QAction("Copy Task(s)", self)
            copy_action.triggered.connect(self.copy_selected_tasks)
            menu.addAction(copy_action)

            paste_action2 = QAction("Paste Below", self)
            paste_action2.setEnabled(bool(TreeGridView._task_clipboard))
            paste_action2.triggered.connect(lambda: self.paste_tasks(item))
            menu.addAction(paste_action2)

            menu.addSeparator()

            # ---- Structure ▸ ----
            structure_menu = menu.addMenu("Structure")
            indent_action = QAction("Indent", self)
            indent_action.triggered.connect(lambda: self.indent_smart(item))
            structure_menu.addAction(indent_action)
            outdent_action = QAction("Outdent", self)
            outdent_action.triggered.connect(lambda: self.outdent_smart(item))
            structure_menu.addAction(outdent_action)
            structure_menu.addSeparator()
            paste_children_action = QAction("Bulk Paste Children...", self)
            paste_children_action.triggered.connect(lambda: self.bulk_paste_children(item))
            structure_menu.addAction(paste_children_action)
            expand_all_action = QAction("Expand All", self)
            expand_all_action.triggered.connect(lambda: self.expand_all_selected(selected_items))
            structure_menu.addAction(expand_all_action)

            # ---- Dependencies ▸ ----
            dep_menu = menu.addMenu("Dependencies")
            pick_pred_action = QAction("Set Predecessor (Click Target)", self)
            pick_pred_action.setShortcut("Ctrl+L")
            pick_pred_action.triggered.connect(lambda: self.start_linking_mode(item))
            dep_menu.addAction(pick_pred_action)
            link_pred_action = QAction("Pick from list… (advanced)", self)
            link_pred_action.triggered.connect(lambda: self.open_link_dialog(item))
            dep_menu.addAction(link_pred_action)
            clear_link_action = QAction("Clear Predecessor", self)
            clear_link_action.setEnabled(bool(item.node.predecessor_id))
            clear_link_action.triggered.connect(lambda: self.clear_link(item))
            dep_menu.addAction(clear_link_action)
            if item.node.predecessor_id:
                jump_action = QAction("Jump to Predecessor", self)
                jump_action.triggered.connect(lambda: self.jump_to_predecessor(item))
                dep_menu.addAction(jump_action)

            # ---- Dates & Delay ▸ ----
            dates_menu = menu.addMenu("Dates && Delay")
            lock_action = QAction("Unset Manual Date" if item.node.dates_locked else "Set Manual Date", self)
            lock_action.triggered.connect(lambda: self.toggle_date_lock(item))
            dates_menu.addAction(lock_action)
            update_bl_action = QAction("Update Baseline (not a delay)", self)
            update_bl_action.setEnabled(item.node.baseline_duration is not None)
            update_bl_action.triggered.connect(lambda: self.update_baseline_for_node(item.node))
            dates_menu.addAction(update_bl_action)
            mark_track_action = QAction("Mark On Track...", self)
            mark_track_action.setEnabled(item.node.baseline_duration is not None and not item.node.children)
            mark_track_action.triggered.connect(lambda: self.mark_on_track(item.node))
            dates_menu.addAction(mark_track_action)
            set_delay_action = QAction("Record Real Delay...", self)
            set_delay_action.setEnabled(item.node.baseline_duration is not None and not item.node.children)
            set_delay_action.triggered.connect(lambda: self.record_real_delay(item.node))
            dates_menu.addAction(set_delay_action)

            menu.addSeparator()

            delete_action = QAction("Delete Task(s)", self)
            delete_action.triggered.connect(lambda: self.delete_smart(item))
            menu.addAction(delete_action)
        else:
            # No item selected (Root context)
            add_action = QAction("Add Root Task", self)
            add_action.triggered.connect(lambda: self.add_child_task(None))
            menu.addAction(add_action)

            paste_root_action = QAction("Paste", self)
            paste_root_action.setEnabled(bool(TreeGridView._task_clipboard))
            paste_root_action.triggered.connect(lambda: self.paste_tasks(None))
            menu.addAction(paste_root_action)
            
        chosen = menu.exec(self.viewport().mapToGlobal(position))
        if chosen is not None:
            depth = 0
            for obj in chosen.associatedObjects():
                if isinstance(obj, QMenu):
                    current = obj
                    candidate_depth = 0
                    while isinstance(current.parentWidget(), QMenu):
                        candidate_depth += 1
                        current = current.parentWidget()
                    depth = max(depth, candidate_depth)
            usage_logger.log("context_menu_action", depth=depth)

    def _simulate_impact(self, node, new_start=None, new_end=None):
        """Non-destructively compute the effect of changing a task date/span.

        Clones the whole project, applies the change, re-runs the REAL
        scheduler, and diffs against the current state. Because it uses the
        same scheduler the live edit will use — including predecessor links —
        the reported 'final date' is truthful, not a guess.

        Returns a payload dict for ImpactReviewDialog, or None when the date
        did not actually change.
        """
        from datetime import datetime
        from utils.scheduler import schedule
        try:
            from utils.critical_path import CriticalPathAnalyzer
        except Exception:
            CriticalPathAnalyzer = None

        def _cal_delta(a, b):
            try:
                return (datetime.strptime(b, "%Y-%m-%d")
                        - datetime.strptime(a, "%Y-%m-%d")).days
            except (ValueError, TypeError):
                return 0

        # No-op guard.
        if new_start is not None and new_start == node.start_date:
            return None
        if new_end is not None and new_end == node.end_date:
            return None

        live = self.get_all_nodes_flat()
        old_end = {n.id: n.end_date for n in live}
        old_name = {n.id: n.name for n in live}
        project_end_old = max((e for e in old_end.values() if e), default=None)

        # Deep clone via serialize/deserialize (preserves ids + links).
        clone_roots = [TaskNode.from_dict(r.to_dict()) for r in self.root_nodes]
        cmap = {}

        def _index(nodes):
            for n in nodes:
                cmap[n.id] = n
                _index(n.children)
        _index(clone_roots)

        target = cmap.get(node.id)
        if target is None:
            return None

        # Apply the hypothetical change to the clone and pin it so the
        # scheduler keeps it (an inline-editable date is always user-owned).
        if new_start is not None:
            if new_end is not None:
                target.start_date = new_start
                target.end_date = new_end
            elif target.start_date and target.end_date:
                dur = WorkdayCalculator.calculate_duration(
                    target.start_date, target.end_date)
                target.start_date = new_start
                target.end_date = WorkdayCalculator.add_workdays(new_start, dur)
            else:
                target.start_date = new_start
            d = _cal_delta(node.start_date, new_start) if node.start_date else 0
            change_desc = (f"start <b>{node.start_date}</b> → <b>{new_start}</b>"
                           f"  ({'+' if d > 0 else ''}{d} days)")
        else:
            target.set_date('end', new_end, force=True)
            d = _cal_delta(node.end_date, new_end) if node.end_date else 0
            change_desc = (f"end <b>{node.end_date}</b> → <b>{new_end}</b>"
                           f"  ({'+' if d > 0 else ''}{d} days)")
        target.dates_locked = True
        node_new_end = target.end_date

        schedule(clone_roots)

        new_end_map = {nid: n.end_date for nid, n in cmap.items()}
        project_end_new = max((e for e in new_end_map.values() if e), default=None)

        # Critical-path ids on the resulting plan → "drives end date" flag.
        critical = set()
        if CriticalPathAnalyzer is not None:
            try:
                res = CriticalPathAnalyzer(clone_roots).analyze()
                critical = (set(res.get('critical_path_ids', set()))
                            | set(res.get('critical_parent_ids', set())))
            except Exception:
                critical = set()

        # Per-task shift list (everything whose end moved), excluding the
        # edited node itself (shown in the header).
        impacts = []
        for nid, ne in new_end_map.items():
            if nid == node.id:
                continue
            oe = old_end.get(nid)
            if oe and ne and oe != ne:
                impacts.append({
                    'name': old_name.get(nid, cmap[nid].name),
                    'old_end': oe,
                    'new_end': ne,
                    'on_chain': nid in critical,
                })
        impacts.sort(key=lambda d: d['new_end'])

        # Delay vs baseline — only meaningful when the duration changed (end
        # edits). Mirrors DelayDelegate: slip not yet covered by a revision.
        delay_slip = 0
        delay_rev = ""
        if (new_end is not None and node.baseline_duration is not None
                and not node.children):
            try:
                new_dur = WorkdayCalculator.calculate_duration(
                    target.start_date, node_new_end)
                total_diff = int(new_dur) - node.baseline_duration
                delay_slip = max(0, total_diff - node.logged_slip())
            except (ValueError, TypeError):
                delay_slip = 0
            if delay_slip > 0:
                delay_rev = chr(ord('B') + len(node.revisions))

        project_delta = (_cal_delta(project_end_old, project_end_new)
                         if project_end_old and project_end_new else 0)
        # Routine edits apply immediately. Review is reserved for a real
        # downstream shift or a changed project finish.
        if not impacts and project_delta == 0:
            return None
        return {
            'task_name': node.name,
            'change_desc': change_desc,
            'project_end_old': project_end_old,
            'project_end_new': project_end_new,
            'project_delta': project_delta,
            'impacts': impacts,
            'delay_slip': delay_slip,
            'delay_rev': delay_rev,
            'node_new_end': node_new_end,
        }

    def editItem(self, item: QTreeWidgetItem, column: int = 0):
        if not isinstance(item, TaskTreeWidgetItem):
            super().editItem(item, column)
            return

        if column in (Columns.START, Columns.END, Columns.PREDECESSOR):
            field = "start" if column in (Columns.START, Columns.PREDECESSOR) else "end"
            self.open_date_choices(item, field)
            return

        # Delay column is read-only (auto-calculated from baseline)
        if column == Columns.DELAY:
            return

        super().editItem(item, column)

    def _rule_creates_cycle(self, node: TaskNode, rule: dict) -> bool:
        target_id = rule.get("task_id")
        if not target_id:
            return rule.get("mode") in {"same_as", "continue_after"}
        if target_id == node.id:
            return True
        # Parent-start equality is the supported legacy parallel relationship.
        if (node.parent and target_id == node.parent.id
                and rule.get("mode") == "same_as" and rule.get("field") == "start"):
            return False
        node_map = self._get_node_map()
        seen = set()
        pending = [target_id]
        while pending:
            current_id = pending.pop()
            if current_id == node.id:
                return True
            if current_id in seen:
                continue
            seen.add(current_id)
            current = node_map.get(current_id)
            if current is None:
                continue
            for candidate in (current.start_rule, current.end_rule):
                if candidate.get("mode") in {"same_as", "continue_after"}:
                    pending.append(candidate.get("task_id"))
        return False

    def open_date_rule_dialog(self, item: TaskTreeWidgetItem, field="start",
                              initial_mode=None):
        node = item.node
        usage_logger.log("date_rule_open", field=field,
                         shortcut=initial_mode or "click")
        dialog = DateRuleDialog(node, self.get_all_nodes_flat(), field,
                                initial_mode, self)
        if not usage_logger.timed_exec(dialog, f"{field}_rule"):
            return
        rule = dialog.result_rule()
        self._apply_date_rule(item, field, rule)

    def _date_choice_menu(self, item, field):
        menu = QMenu(self)
        fixed = menu.addAction("Calendar / Fixed Date...")
        same = menu.addAction("Same As (=) — click a task row")
        after = menu.addAction("Continue After (+) — click a task row")
        after.setEnabled(field == "start")
        automatic = menu.addAction("Automatic")
        menu.addSeparator()
        advanced = menu.addAction("More options / offset...")
        fixed.triggered.connect(
            lambda: self.open_date_rule_dialog(item, field, "fixed"))
        same.triggered.connect(
            lambda: self.start_date_pick_mode(item, field, "same_as"))
        after.triggered.connect(
            lambda: self.start_date_pick_mode(item, field, "continue_after"))
        automatic.triggered.connect(lambda: self._apply_date_rule(
            item, field,
            {"mode": "automatic"} if field == "start" else
            {"mode": "duration", "days": max(1, int(item.node.duration))}))
        advanced.triggered.connect(lambda: self.open_date_rule_dialog(item, field))
        return menu

    def open_date_choices(self, item, field, global_pos=None):
        """Small cell-anchored menu: symbols are optional, never required."""
        usage_logger.log("date_choice_menu", field=field, outcome="opened")
        menu = self._date_choice_menu(item, field)
        if global_pos is None:
            rect = self.visualItemRect(item)
            column = self.currentColumn()
            if column not in (Columns.START, Columns.END, Columns.PREDECESSOR):
                column = Columns.START if field == "start" else Columns.END
            global_pos = self.viewport().mapToGlobal(
                QPoint(self.columnViewportPosition(column), rect.bottom()))
        selected = menu.exec(global_pos)
        if selected is None:
            usage_logger.log("date_choice_menu", field=field, outcome="canceled")

    def _apply_date_rule(self, item, field, rule):
        if not isinstance(item, TaskTreeWidgetItem):
            return False
        node = item.node
        if self._rule_creates_cycle(node, rule):
            usage_logger.log("warning_shown", name="relationship_cycle")
            QMessageBox.warning(self, "Invalid Relationship",
                                "Select a task that does not create a scheduling cycle.")
            return False
        old_rule = dict(node.start_rule if field == "start" else node.end_rule)
        old_mode = old_rule.get("mode")
        new_mode = rule.get("mode")
        if (old_mode not in {"automatic", "duration", None}
                and new_mode != old_mode):
            usage_logger.log("date_rule_conflict", field=field,
                             old=old_mode, new=new_mode)
            if QMessageBox.question(
                    self, "Replace Scheduling Rule?",
                    f"This {field.title()} is controlled by {old_mode.replace('_', ' ')}.\n\n"
                    f"Replace it with {new_mode.replace('_', ' ')}?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                usage_logger.log("date_rule_apply", field=field,
                                 mode=new_mode, outcome="conflict_canceled")
                return False
        if field == "start":
            node.set_start_rule(rule)
            node.is_parallel = bool(
                rule.get("mode") == "same_as" and node.parent
                and rule.get("task_id") == node.parent.id
                and rule.get("field") == "start")
        else:
            node.set_end_rule(rule)
        self.recalculate_all_dates()
        self.refresh_entire_tree()
        if node.schedule_conflicts:
            usage_logger.log("resource_conflict", via="date_rule")
            QMessageBox.warning(
                self, "Resource Conflict",
                "The exact/fixed date was kept, but its resource is not available:\n\n"
                + "\n".join(node.schedule_conflicts))
        if node.baseline_provisional:
            self._restamp_provisional_baseline(node)
        usage_logger.log("date_rule_apply", field=field, mode=rule.get("mode"),
                         replaced=old_rule.get("mode"))
        self.journal_event.emit(
            f"'{node.name}': {field} rule → {node.rule_explanation(field)}")
        self.item_changed_signal.emit(node)
        return True

    def set_vave_stage(self, item):
        from PyQt6.QtWidgets import QInputDialog
        stages = ["Idea", "Testing", "Approved", "Implemented", "Savings Verified"]
        node = item.node
        selected, ok = QInputDialog.getItem(
            self, "VAVE Stage", "Stage:", stages,
            max(0, stages.index(node.vave_stage) if node.vave_stage in stages else 0), False)
        if not ok:
            usage_logger.log("vave_stage", outcome="canceled")
            return
        realized = node.vave_realized
        disposition = node.savings_disposition
        if selected == "Savings Verified":
            realized, ok = QInputDialog.getDouble(
                self, "Verified Savings", "Realized savings ($):",
                float(realized or 0), -1_000_000_000, 1_000_000_000, 2)
            if not ok:
                return
            disposition, ok = QInputDialog.getItem(
                self, "Savings Disposition", "Disposition:",
                ["Verified", "No Savings", "Deferred"], 0, False)
            if not ok:
                return
        node.vave_stage = selected
        if selected == "Savings Verified":
            node.vave_realized = float(realized)
            node.savings_disposition = disposition
        self.refresh_entire_tree()
        usage_logger.log("vave_stage", outcome="applied", stage=selected)
        self.journal_event.emit(f"VAVE stage set to {selected} for '{node.name}'")
        self.item_changed_signal.emit(node)

    def jump_to_predecessor(self, item: TaskTreeWidgetItem):
        """Select, expand ancestors of, scroll to, and briefly flash the
        task this item depends on. Handles both explicit predecessor_id
        links and the implicit previous-sibling default shown in
        'Depends On'.
        """
        target_node = None
        pred_id = item.node.predecessor_id
        if pred_id:
            t = self._find_item_by_id(pred_id)
            if t is not None:
                target_node = t
        if target_node is None:
            # Fall back to the implicit predecessor (the '↑ Name' entry)
            implicit = item._implicit_predecessor()
            if implicit is not None:
                target_node = self._find_item_by_id(implicit.id)
        if target_node is None:
            return

        # Expand every ancestor so the target row is visible.
        ancestor = target_node.parent()
        while ancestor is not None:
            ancestor.setExpanded(True)
            ancestor = ancestor.parent()

        self.clearSelection()
        target_node.setSelected(True)
        self.setCurrentItem(target_node)
        self.scrollToItem(target_node, QAbstractItemView.ScrollHint.PositionAtCenter)
        self._flash_item(target_node)

    def _flash_item(self, item: 'TaskTreeWidgetItem'):
        """Highlight a row in solid orange, then revert. Strong and opaque
        on purpose — it must stand out from selection blue and the
        light-blue 'this week' row tint."""
        from PyQt6.QtGui import QColor
        flash_color = QBrush(QColor("#FFB300"))  # solid vivid amber
        original = [item.background(col) for col in range(Columns.COUNT)]
        was_blocked = self.blockSignals(True)
        try:
            for col in range(Columns.COUNT):
                item.setBackground(col, flash_color)
        finally:
            self.blockSignals(was_blocked)

        def revert():
            was_blocked_inner = self.blockSignals(True)
            try:
                for col in range(Columns.COUNT):
                    item.setBackground(col, original[col])
            except RuntimeError:
                pass  # item was removed before timer fired
            finally:
                self.blockSignals(was_blocked_inner)

        QTimer.singleShot(2500, revert)

    def _find_item_by_id(self, node_id: str):
        iterator = QTreeWidgetItemIterator(self)
        while iterator.value():
            it = iterator.value()
            if isinstance(it, TaskTreeWidgetItem) and it.node.id == node_id:
                return it
            iterator += 1
        return None

    def jump_to_node_id(self, node_id: str):
        """Expand, scroll to and flash the row for this node id.
        Used by the Visuals tab and Search to jump into the Tracker.

        Deliberately does NOT select the row: Qt paints the selection
        color over the cell backgrounds, which made the flash invisible
        (it looked like any other light-blue selected row)."""
        item = self._find_item_by_id(node_id)
        if not item:
            return
        parent = item.parent()
        while parent:
            parent.setExpanded(True)
            parent = parent.parent()
        self.clearSelection()
        self.setCurrentItem(
            item, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
        self.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)
        self._flash_item(item)

    def toggle_date_lock(self, item: TaskTreeWidgetItem):
        item.node.dates_locked = not item.node.dates_locked
        self.journal_event.emit(
            f"'{item.node.name}': manual date "
            f"{'set' if item.node.dates_locked else 'unset'}")
        self.commit_structure_change(item.node)

    def link_selected_tasks(self, items):
        """
        Links selected tasks in visual order.
        Task[i].Start = Task[i-1].End
        """
        # Group by parent to be safe
        # Use ID as key because QTreeWidgetItem is not hashable
        grouped = {} # {parent_id: (parent_item, [children])}
        
        for item in items:
            parent = item.parent() or self.invisibleRootItem()
            pid = id(parent)
            if pid not in grouped:
                grouped[pid] = (parent, [])
            grouped[pid][1].append(item)
            
        for pid, (parent, siblings) in grouped.items():
            # Sort siblings by index
            siblings.sort(key=lambda x: parent.indexOfChild(x))
            
            # Apply links
            for i in range(1, len(siblings)):
                prev_item = siblings[i-1]
                curr_item = siblings[i]
                
                # Manual link: Set Start = Prev.End
                curr_item.node.update_from_previous_sibling(prev_item.node)
                
        self.commit_structure_change(items[-1].node if items else None)
        QMessageBox.information(self, "Tasks Linked", f"Linked {len(items)} tasks.\nStart dates aligned sequentially.")



    def bulk_paste_children(self, parent_item: QTreeWidgetItem):
        dialog = BulkPasteDialog(self)
        if usage_logger.timed_exec(dialog, "bulk_paste"):
            lines = dialog.get_lines()
            parent_node = parent_item.node
            
            for name in lines:
                new_node = TaskNode(name, parent=parent_node)
                parent_node.add_child(new_node)
                self.add_node_to_tree(new_node, parent_item)
            
            parent_item.setExpanded(True)
            if lines:
                usage_logger.log("task_add", via="paste", count=len(lines))
                self.commit_structure_change(parent_node)

    def bulk_set_status(self, items: list[QTreeWidgetItem]):
        # Get status options from Enum
        options = [s.value for s in Status if s.value != "Overdue"]
        dialog = BulkEditDialog("Bulk Set Status", self, options=options)
        if usage_logger.timed_exec(dialog, "bulk_status") and dialog.value:
            project = self._project_widget()
            if project:
                project.begin_batch()
            try:
                changed = []
                for item in items:
                    if isinstance(item, TaskTreeWidgetItem):
                        item.node.set_status(dialog.value)
                        item.update_from_node()
                        changed.append(item.node)
                for node in changed:
                    self.item_changed_signal.emit(node)
            finally:
                if project:
                    project.end_batch()
            self.update_filter_options()
            usage_logger.log("bulk_edit", what="status", count=len(changed))

    def bulk_set_owner(self, items: list[QTreeWidgetItem]):
        dialog = BulkEditDialog("Bulk Set Waiting On", self)
        if usage_logger.timed_exec(dialog, "bulk_owner") and dialog.value is not None:
            project = self._project_widget()
            if project:
                project.begin_batch()
            try:
                changed = []
                for item in items:
                    if isinstance(item, TaskTreeWidgetItem):
                        item.node.set_waiting_on(dialog.value)
                        item.update_from_node()
                        changed.append(item.node)
                for node in changed:
                    self.item_changed_signal.emit(node)
            finally:
                if project:
                    project.end_batch()
            self.update_filter_options()
            usage_logger.log("bulk_edit", what="waiting_on", count=len(changed))

    def _project_widget(self):
        w = self.parent()
        while w is not None:
            if hasattr(w, "begin_batch") and hasattr(w, "end_batch"):
                return w
            w = w.parent()
        return None

    def _full_path(self, node: TaskNode) -> str:
        parts = []
        n = node
        while n:
            parts.append(n.name)
            n = n.parent
        return " > ".join(reversed(parts))

    def _waiting_days(self, node: TaskNode) -> int:
        if not node.waiting_since:
            return 0
        try:
            since = datetime.strptime(node.waiting_since, "%Y-%m-%d").date()
            return max(0, (datetime.now().date() - since).days)
        except ValueError:
            return 0

    def mark_waiting_on(self, node: TaskNode):
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QFormLayout, QComboBox, QLineEdit, QDialogButtonBox
        from utils.config_manager import ConfigManager
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Mark Waiting On - {node.name}")
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        who_box = QComboBox()
        who_box.setEditable(True)
        choices = set(ConfigManager().get_owners())
        choices.update(n.waiting_on for n in self.get_all_nodes_flat() if n.waiting_on)
        who_box.addItems(sorted(c for c in choices if c))
        note_edit = QLineEdit()
        note_edit.setPlaceholderText("Optional note")
        form.addRow("Waiting on:", who_box)
        form.addRow("Note:", note_edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if not usage_logger.timed_exec(dialog, "waiting_on"):
            return
        who = who_box.currentText().strip()
        if not who:
            return
        node.set_waiting_on(who)
        self.refresh_entire_tree()
        note = note_edit.text().strip()
        suffix = f": {note}" if note else ""
        self.journal_event.emit(
            f"'{node.name}': waiting on {node.waiting_on} since {node.waiting_since}{suffix}")
        self.item_changed_signal.emit(node)
        usage_logger.log("waiting_set")

    def assign_owner(self, node: TaskNode):
        from PyQt6.QtWidgets import QInputDialog
        from utils.config_manager import ConfigManager
        choices = [""] + [name for name in ConfigManager().get_owners() if name]
        current = choices.index(node.owner) if node.owner in choices else 0
        owner, ok = QInputDialog.getItem(
            self, "Task Owner", "Owner (blank clears):", choices, current, True)
        if not ok:
            usage_logger.log("owner_edit", outcome="canceled")
            return
        old = node.owner
        node.set_owner(owner.strip())
        self.refresh_entire_tree()
        self.journal_event.emit(
            f"'{node.name}': owner {old or '—'} → {node.owner or '—'}")
        self.item_changed_signal.emit(node)
        usage_logger.log("owner_edit", outcome="applied", assigned=bool(node.owner))

    def clear_waiting(self, node: TaskNode):
        who = node.waiting_on
        days = self._waiting_days(node)
        node.set_waiting_on("")
        self.refresh_entire_tree()
        self.journal_event.emit(f"'{node.name}': cleared waiting on {who} after {days}d")
        self.item_changed_signal.emit(node)
        usage_logger.log("waiting_clear", days_waited=days)

    def _start_is_auto(self, node: TaskNode) -> bool:
        """True when the scheduler owns this task's start date, i.e. start
        is not user-editable. Mirrors the rules in utils.scheduler.

        Manual (returns False) when:
          - dates_locked=True  → "Set Manual Date" escape hatch, user owns it.
          - first root task    → schedule anchor, always user-typed.
        Auto (returns True) when:
          - predecessor_id is set, OR
          - node has a parent (all children are auto — radio ON snaps to
            parent.start, radio OFF chains from prev sibling).
        """
        if node.dates_locked:
            return False   # manual mode — user owns the start
        if node.predecessor_id:
            return True
        if node.parent is not None:
            return True
        # Root task: only the first root is manual.
        if self.root_nodes and node is self.root_nodes[0]:
            return False
        return True

    def _apply_predecessor_change(self, node: TaskNode, new_pred_id):
        """Single point of mutation for predecessor links.

        Funneling every path (inline click, context menu clear, advanced dialog)
        through here guarantees the Depends On column repaints immediately —
        the refresh step was missing on the earlier per-path call sites.

        Conflict guard: parallel mode and a predecessor link are mutually
        exclusive. Setting a predecessor while is_parallel=True is blocked.
        Clearing (new_pred_id=None) is always allowed.
        """
        if new_pred_id and node.is_parallel:
            usage_logger.log("warning_shown", name="conflicting_settings")
            QMessageBox.warning(
                self, "Conflicting settings",
                f"'{node.name}' is set to run in parallel with its parent.\n\n"
                "Disable parallel mode first before linking a predecessor."
            )
            # Cancel link — exit linking mode without changing anything.
            self._cancel_linking_mode()
            return

        node.predecessor_id = new_pred_id if new_pred_id else None
        self.commit_structure_change(node)

    def clear_link(self, item: TaskTreeWidgetItem):
        self._apply_predecessor_change(item.node, None)

    def open_link_dialog(self, item: TaskTreeWidgetItem):
        all_nodes = self.get_all_nodes_flat()
        dialog = LinkTaskDialog(item.node, all_nodes, self)
        if usage_logger.timed_exec(dialog, "link_task"):
            result_id = dialog.selected_node_id
            if result_id is not None:
                self._apply_predecessor_change(item.node, result_id or None)

    def get_all_nodes_flat(self) -> list[TaskNode]:
        nodes = []
        def traverse(node_list):
            for node in node_list:
                nodes.append(node)
                traverse(node.children)
        traverse(self.root_nodes)
        return nodes

    def resolve_node_name(self, node_id: str) -> str:
        """Lookup a node's display name by id. Used by the Depends On column."""
        node_map = self._get_node_map()
        n = node_map.get(node_id)
        return n.name if n else ""

    def _get_node_map(self) -> dict:
        """Fresh flat id→node map. Cheap enough for trees of a few thousand tasks."""
        return {n.id: n for n in self.get_all_nodes_flat()}

    def recalculate_all_dates(self):
        """Delegate to the shared scheduler (see utils.scheduler)."""
        from utils.scheduler import schedule
        schedule(self.root_nodes)

    def commit_structure_change(self, node: TaskNode = None):
        """Recalculate and repaint after any hierarchy/date mutation.

        The scheduler updates the model; refresh_entire_tree makes the screen
        match it immediately. Centralizing this keeps every structure edit from
        leaving stale dates behind.
        """
        self.recalculate_all_dates()
        self.refresh_entire_tree()
        self.update_filter_options()
        if node is not None:
            self.item_changed_signal.emit(node)

    # ---- Baseline / Delay tracking ----

    def has_baseline(self) -> bool:
        return any(n.baseline_duration is not None
                   for n in self.get_all_nodes_flat())

    def set_baseline(self):
        """Snapshot current durations + end dates as Rev A for all tasks.
        Warns first if delay history exists — re-baselining clears it."""
        if any(n.revisions for n in self.get_all_nodes_flat()):
            reply = QMessageBox.question(
                self, "Clear delay history?",
                "Setting a new baseline erases the existing delay history "
                "(all Rev B/C… entries) on every task.\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
        for node in self.get_all_nodes_flat():
            try:
                node.baseline_duration = int(node.duration)
            except (ValueError, TypeError):
                node.baseline_duration = None
            node.baseline_end = node.end_date
            node.revisions = []
            node.delay_notes = ""
            node.baseline_provisional = False
        self.refresh_entire_tree()
        usage_logger.log("baseline_set")

    def clear_baseline(self):
        """Remove all baseline + delay history data."""
        for node in self.get_all_nodes_flat():
            node.baseline_duration = None
            node.baseline_end = None
            node.revisions = []
            node.delay_notes = ""
        self.refresh_entire_tree()

    def update_baseline_for_node(self, node: TaskNode):
        """Re-stamp one task as a fresh Rev A (intentional change, not a delay).
        Clears that task's revision trail so the audit stays clean."""
        try:
            node.baseline_duration = int(node.duration)
        except (ValueError, TypeError):
            node.baseline_duration = None
        node.baseline_end = node.end_date
        node.revisions = []
        node.delay_notes = ""
        node.baseline_provisional = False
        self.refresh_entire_tree()

    def mark_on_track(self, node: TaskNode):
        if node.baseline_duration is None:
            usage_logger.log("warning_shown", name="no_baseline")
            QMessageBox.information(self, "No Baseline", "Set a baseline for this task first.")
            return
        if node.children:
            usage_logger.log("warning_shown", name="delay_parent_task")
            QMessageBox.information(self, "Parent Task", "Delay actions are available on leaf tasks only.")
            return
        from PyQt6.QtWidgets import QInputDialog
        try:
            old_diff = int(node.duration) - node.baseline_duration
        except (ValueError, TypeError):
            old_diff = 0
        reason, ok = QInputDialog.getText(
            self, "Mark On Track",
            f"Reason for marking '{node.name}' on track:")
        if not ok:
            return
        trail = node.revision_trail()
        if trail:
            self.journal_event.emit(
                f"Rebased delay history for '{node.name}' before marking on track:\n{trail}")
        try:
            node.baseline_duration = int(node.duration)
        except (ValueError, TypeError):
            node.baseline_duration = None
        node.baseline_end = node.end_date
        node.revisions = []
        node.delay_notes = ""
        self.refresh_entire_tree()
        usage_logger.log("baseline_update_node")
        self.journal_event.emit(
            f"Marked on track — '{node.name}' (was {old_diff:+d}d): {reason or '(no reason given)'}")
        self.item_changed_signal.emit(node)

    def record_real_delay(self, node: TaskNode):
        if node.baseline_duration is None:
            usage_logger.log("warning_shown", name="no_baseline")
            QMessageBox.information(self, "No Baseline", "Set a baseline for this task first.")
            return
        if node.children:
            usage_logger.log("warning_shown", name="delay_parent_task")
            QMessageBox.information(self, "Parent Task", "Delay actions are available on leaf tasks only.")
            return
        from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QFormLayout,
                                     QLineEdit, QDialogButtonBox, QLabel)
        try:
            current_duration = int(node.duration)
            variance = current_duration - node.baseline_duration
        except (ValueError, TypeError):
            return
        uncovered = variance - node.logged_slip()
        if variance <= 0 or uncovered <= 0:
            usage_logger.log("record_real_delay", outcome="not_positive")
            QMessageBox.information(
                self, "No Unrecorded Delay",
                "This task has no unrecorded positive variance. Use Still on track "
                "or Update Baseline instead.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Record Real Delay — {node.name}")
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        reason_edit = QLineEdit()
        reason_edit.setPlaceholderText("Reason required")
        form.addRow("Current variance:", QLabel(f"+{variance} workday(s) vs baseline"))
        if node.logged_slip():
            form.addRow("Unrecorded amount:", QLabel(f"+{uncovered} workday(s)"))
        form.addRow("Reason:", reason_edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        reason_edit.setFocus()
        if not usage_logger.timed_exec(dialog, "record_real_delay"):
            return
        reason = reason_edit.text().strip()
        if not reason:
            usage_logger.log("warning_shown", name="delay_reason_required")
            QMessageBox.information(self, "Reason Required", "Enter a reason to record a real delay.")
            return
        node.log_delay_revision({
            "rev": chr(ord('B') + len(node.revisions)),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "end": node.end_date or "",
            "slip": uncovered,
            "variance": variance,
            "reason": reason,
        })
        self.refresh_entire_tree()
        usage_logger.log("record_real_delay", outcome="applied")
        self.journal_event.emit(
            f"Real delay recorded — '{node.name}' +{variance}d vs baseline: {reason}")
        self.item_changed_signal.emit(node)

    def add_child_task(self, parent_item: QTreeWidgetItem = None):
        # If no parent selected, add to root
        parent_node = parent_item.node if parent_item else None
        new_node = TaskNode("New Task", parent=parent_node)
        
        # Auto-Link Logic: If there are existing children, link this new one to the last one
        siblings = parent_node.children if parent_node else self.root_nodes
        # Note: new_node is not in siblings list yet (added below)
        
        if siblings:
            # Link to the last sibling
            # We want: Start = PrevSibling.End
            # new_node.set_formula('start', "=PrevSibling.End")
            # Default duration 1 day?
            # new_node.set_formula('end', "=Start+1")
            
            # Use new manual logic
            prev = siblings[-1]
            new_node.update_from_previous_sibling(prev)
        
        if parent_node:
            parent_node.add_child(new_node)
            parent_item.setExpanded(True)
            self.add_node_to_tree(new_node, parent_item)
        else:
            self.root_nodes.append(new_node)
            self.add_node_to_tree(new_node, self.invisibleRootItem())
            
        self._baseline_new_task_on_track(new_node)

        where = f" under '{parent_node.name}'" if parent_node else ""
        self.journal_event.emit(f"Added task '{new_node.name}'{where}")
        usage_logger.log("task_add", via="context_menu", count=1)
        self.commit_structure_change(new_node)

    def delete_task(self, item: QTreeWidgetItem):
        if not item: return
        
        node = item.node
        parent_item = item.parent()
        
        if node.parent:
            node.parent.remove_child(node)
        elif node in self.root_nodes:
            self.root_nodes.remove(node)
            
        if parent_item:
            parent_item.removeChild(item)
        else:
            self.invisibleRootItem().removeChild(item)

        # Removal changes sibling anchoring — re-run the scheduler and
        # repaint (date propagation is the scheduler's job, see task_node).
        self.journal_event.emit(f"Deleted task '{node.name}'")
        usage_logger.log("task_delete", count=1)
        self.commit_structure_change(node)

    # ------------------------------------------------------------------
    # Cut / Copy / Paste (within and across project tabs)
    # ------------------------------------------------------------------

    def _top_level_selection(self):
        """Selected items minus any whose ancestor is also selected —
        copying a parent already carries its whole subtree."""
        items = [i for i in self.selectedItems()
                 if isinstance(i, TaskTreeWidgetItem)]
        sel_ids = {i.node.id for i in items}
        out = []
        for i in items:
            p = i.parent()
            covered = False
            while p:
                if isinstance(p, TaskTreeWidgetItem) and p.node.id in sel_ids:
                    covered = True
                    break
                p = p.parent()
            if not covered:
                out.append(i)
        return out

    def copy_selected_tasks(self):
        tops = self._top_level_selection()
        if tops:
            TreeGridView._task_clipboard = [i.node.to_dict() for i in tops]

    def cut_selected_tasks(self):
        tops = self._top_level_selection()
        if not tops:
            return
        TreeGridView._task_clipboard = [i.node.to_dict() for i in tops]
        # Every id leaving this project (subtrees included)
        removed = set()

        def collect(n):
            removed.add(n.id)
            for c in n.children:
                collect(c)
        for i in tops:
            collect(i.node)
        for i in tops:
            self.delete_task(i)
        # Tasks left behind that depended on a removed task would keep a
        # dangling link ("(missing)") — clear them instead.
        for n in self.get_all_nodes_flat():
            if n.predecessor_id in removed:
                n.predecessor_id = None
        self.commit_structure_change(tops[0].node)

    def paste_tasks(self, anchor_item: QTreeWidgetItem = None):
        """Paste clipboard subtrees as siblings after anchor_item
        (appended at root level when there is no anchor)."""
        import uuid
        if not TreeGridView._task_clipboard:
            return
        dest_ids = {n.id for n in self.get_all_nodes_flat()}

        nodes = [TaskNode.from_dict(d) for d in TreeGridView._task_clipboard]

        # Fresh ids on every paste (a copy must never duplicate ids).
        idmap = {}

        def remap(n):
            new_id = str(uuid.uuid4())
            idmap[n.id] = new_id
            n.id = new_id
            for c in n.children:
                remap(c)
        for n in nodes:
            remap(n)

        # Predecessor links: inside the pasted set → remap (survives across
        # projects). Outside the set → keep only if the target exists in
        # this project; otherwise clear and report.
        cleared = 0

        def fix_preds(n):
            nonlocal cleared
            if n.predecessor_id:
                if n.predecessor_id in idmap:
                    n.predecessor_id = idmap[n.predecessor_id]
                elif n.predecessor_id not in dest_ids:
                    n.predecessor_id = None
                    cleared += 1
            for c in n.children:
                fix_preds(c)
        for n in nodes:
            fix_preds(n)

        was_blocked = self.blockSignals(True)
        try:
            if anchor_item and isinstance(anchor_item, TaskTreeWidgetItem):
                anchor_node = anchor_item.node
                parent_node = anchor_node.parent
                siblings = parent_node.children if parent_node else self.root_nodes
                idx = siblings.index(anchor_node) + 1
                ui_parent = anchor_item.parent()
                for off, n in enumerate(nodes):
                    n.parent = parent_node
                    siblings.insert(idx + off, n)
                    item = TaskTreeWidgetItem(n)
                    if ui_parent:
                        ui_parent.insertChild(
                            ui_parent.indexOfChild(anchor_item) + 1 + off, item)
                    else:
                        self.insertTopLevelItem(
                            self.indexOfTopLevelItem(anchor_item) + 1 + off, item)
                    item.setExpanded(n.expanded)
                    for c in n.children:
                        self.add_node_to_tree(c, item)
            else:
                for n in nodes:
                    n.parent = None
                    self.root_nodes.append(n)
                    self.add_node_to_tree(n, self.invisibleRootItem())
        finally:
            self.blockSignals(was_blocked)

        names = ", ".join(f"'{n.name}'" for n in nodes[:3])
        more = f" +{len(nodes) - 3} more" if len(nodes) > 3 else ""
        self.journal_event.emit(f"Pasted {len(nodes)} task(s): {names}{more}")
        usage_logger.log("task_add", via="paste", count=len(nodes))
        self.commit_structure_change(nodes[0])

        if cleared:
            QMessageBox.information(
                self, "Links cleared",
                f"{cleared} predecessor link(s) pointed at tasks that are "
                "not in this project, so they were cleared.")

    def on_item_changed(self, item: QTreeWidgetItem, column: int):
        # Update model from UI
        if isinstance(item, TaskTreeWidgetItem):
            node = item.node
            text = item.text(column)

            # Journal: queue lines now, emit after signals are unblocked —
            # emitting while blocked would silently drop them.
            journal_lines = []
            old_vals = (node.start_date, node.end_date, node.status, node.owner, node.waiting_on)

            # Block signals to prevent recursion when we update the item programmatically
            self.blockSignals(True)
            
            if column == Columns.TREE:
                # Check if checkState changed
                new_state = item.checkState(Columns.TREE) == Qt.CheckState.Checked
                if node.is_parallel != new_state:
                    # Conflict guard: parallel and predecessor are mutually exclusive.
                    if new_state and node.predecessor_id:
                        usage_logger.log("warning_shown", name="conflicting_settings")
                        QMessageBox.warning(
                            self, "Conflicting settings",
                            f"'{node.name}' already has a predecessor link.\n\n"
                            "Remove the predecessor link first before enabling "
                            "parallel mode."
                        )
                        # Revert the checkbox — do NOT update node.is_parallel.
                        item.setCheckState(
                            Columns.TREE,
                            Qt.CheckState.Unchecked if not node.is_parallel
                            else Qt.CheckState.Checked
                        )
                    else:
                        node.is_parallel = new_state
                        self.recalculate_all_dates()
                        self.refresh_entire_tree()
                        journal_lines.append(
                            f"'{node.name}': parallel mode "
                            f"{'ON' if new_state else 'OFF'}")

                name_changed = node.name != text
                if name_changed:
                    journal_lines.append(f"Renamed '{node.name}' → '{text}'")
                node.name = text
                # If the name changed, refresh every row so any task that
                # points to this one via predecessor_id (Depends On column)
                # picks up the new label. _predecessor_label resolves by id.
                if name_changed:
                    self.refresh_entire_tree()
            elif column == Columns.START:
                # Smart Date Change Logic
                if self.is_updating: return
                text = model_date(text)

                # Block manual start edits whenever the scheduler owns the
                # start. Only the first root task and any is_parallel=ON task
                # are manually editable. See _start_is_auto for the rules.
                if self._start_is_auto(node):
                    if node.predecessor_id:
                        msg = ("This task has a predecessor link. Remove the "
                               "predecessor first before editing its Start "
                               "date manually.")
                    else:
                        msg = ("This task's start date is auto-scheduled.\n\n"
                               "To type a date manually, right-click the task "
                               "and choose 'Set Manual Date'.")
                    QMessageBox.warning(self, "Start date is automatic", msg)
                    usage_logger.log("warning_shown", name="start_is_auto")
                    item.setText(Columns.START, node.start_date or "")
                    item.setData(Columns.START, Qt.ItemDataRole.DisplayRole, display_date(node.start_date or ""))
                    item.setData(Columns.START, Qt.ItemDataRole.EditRole, node.start_date or "")
                    self.blockSignals(False)
                    return
                
                # 1. Simulate the impact non-destructively (the REAL scheduler
                #    on a clone) so the popup can show the true project-end
                #    effect — including predecessor-link ripple.
                review = self._simulate_impact(node, new_start=text)

                if review is None:
                    # Date didn't actually move → apply directly, no popup.
                    old_start = node.start_date
                    old_end = node.end_date
                    if old_start and old_end and text != old_start:
                        dur = WorkdayCalculator.calculate_duration(old_start, old_end)
                        node.start_date = text
                        node.end_date = WorkdayCalculator.add_workdays(text, dur)
                    else:
                        node.start_date = text
                    node.update_status_from_dates()
                    self.recalculate_all_dates()
                    self.validate_child_dates(item)
                    self.refresh_entire_tree()
                else:
                    # 2. Show the informative review popup. It leads with whether
                    #    the project end date slips or holds, then lists every
                    #    task that shifts. OK applies; Cancel reverts.
                    dialog = ImpactReviewDialog(review, self)
                    if usage_logger.timed_exec(dialog, "impact_review") and dialog.result_action:
                        if dialog.result_action == "update_all":
                            # Standard ripple: move this start, re-run the
                            # scheduler so children and downstream predecessors
                            # all update in one pass.
                            old_start = node.start_date
                            old_end = node.end_date
                            if old_start and old_end and text != old_start:
                                dur = WorkdayCalculator.calculate_duration(old_start, old_end)
                                node.start_date = text
                                node.end_date = WorkdayCalculator.add_workdays(text, dur)
                            else:
                                node.start_date = text
                            node.update_status_from_dates()
                            self.recalculate_all_dates()
                            self.validate_child_dates(item)
                            self.refresh_entire_tree()
                        elif dialog.result_action == "keep_others":
                            # Gap: shift only this task, leave siblings alone.
                            old_start = node.start_date
                            old_end = node.end_date
                            if old_start and old_end:
                                dur = WorkdayCalculator.calculate_duration(old_start, old_end)
                                node.start_date = text
                                node.end_date = WorkdayCalculator.add_workdays(text, dur)
                            else:
                                node.start_date = text
                            node.update_status_from_dates()
                            self.recalculate_all_dates()
                            self.refresh_entire_tree()
                    else:
                        # Cancel: revert the typed text.
                        item.setText(Columns.START, node.start_date)
                    
            elif column == Columns.END:
                text = model_date(text)
                # First end date typed on a fresh row: apply silently and
                # re-baseline — a brand-new line is on track by definition
                # until its schedule has been set once. No popup, no delay.
                if getattr(node, "baseline_provisional", False):
                    node.set_date('end', text)
                    self.validate_child_dates(item)
                    self.recalculate_all_dates()
                    self.refresh_entire_tree()
                    self._restamp_provisional_baseline(node)
                    review = None
                else:
                    # End-date replanning reviews schedule impact only. Recording
                    # a real delay is a separate, deliberate action.
                    review = self._simulate_impact(node, new_end=text)
                if review is not None:
                    dialog = ImpactReviewDialog(review, self)
                    if usage_logger.timed_exec(dialog, "impact_review") and dialog.result_action:
                        node.set_date('end', text)
                        self.validate_child_dates(item)
                        self.recalculate_all_dates()
                        self.refresh_entire_tree()
                        usage_logger.log("schedule_edit", field="end", outcome="applied")
                    else:
                        # Cancel: revert the typed end date.
                        item.setText(Columns.END, node.end_date or "")
            elif column == Columns.STATUS: 
                node.set_status(text)
                # Status change might affect parent, refresh tree
                self.refresh_entire_tree()
            elif column == Columns.OWNER:
                node.set_waiting_on(text)
                self.refresh_entire_tree()
            elif column == Columns.POTENTIAL:
                try:
                    node.vave_potential = parse_money(text)
                    usage_logger.log("vave_edit", col="Potential $")
                    self.refresh_entire_tree()
                except ValueError:
                    val_potential = node.vave_display_potential()
                    item.setData(Columns.POTENTIAL, Qt.ItemDataRole.DisplayRole, val_potential if val_potential is not None else "")
                    self.blockSignals(False)
                    window = self.window()
                    if hasattr(window, "statusBar"):
                        window.statusBar().showMessage("Invalid Potential $; keeping previous value.", 3000)
                    return
            elif column == Columns.REALIZED:
                try:
                    node.vave_realized = parse_money(text)
                    usage_logger.log("vave_edit", col="Realized $")
                    self.refresh_entire_tree()
                except ValueError:
                    val_realized = node.vave_display_realized()
                    item.setData(Columns.REALIZED, Qt.ItemDataRole.DisplayRole, val_realized if val_realized is not None else "")
                    self.blockSignals(False)
                    window = self.window()
                    if hasattr(window, "statusBar"):
                        window.statusBar().showMessage("Invalid Realized $; keeping previous value.", 3000)
                    return
            elif column == Columns.NOTES: node.notes = text
            elif column == Columns.DELAY: pass  # read-only, skip
            elif column == Columns.DURATION:
                # A duration change is normal replanning and gets an impact
                # review only. The handler tail re-normalizes the
                # Duration cell, so cancels/invalid input need no manual revert.
                try:
                    days = int(text)
                except ValueError:
                    days = None
                if days is not None and node.start_date:
                    new_end = WorkdayCalculator.add_workdays(
                        node.start_date, days if days >= 1 else 1)
                    # First duration typed on a fresh row: silent + re-baseline
                    # (same rule as the END branch above).
                    if getattr(node, "baseline_provisional", False):
                        node.set_duration(days)
                        self.validate_child_dates(item)
                        self.recalculate_all_dates()
                        self.refresh_entire_tree()
                        self._restamp_provisional_baseline(node)
                        review = None
                    else:
                        review = self._simulate_impact(node, new_end=new_end)
                    if review is not None:
                        dialog = ImpactReviewDialog(review, self)
                        if usage_logger.timed_exec(dialog, "impact_review") and dialog.result_action:
                            node.set_duration(days)
                            self.validate_child_dates(item)
                            self.recalculate_all_dates()
                            self.refresh_entire_tree()
                            usage_logger.log("schedule_edit", field="duration", outcome="applied")
                    else:
                        node.set_duration(days)
                        self.validate_child_dates(item)
                        self.recalculate_all_dates()
                        self.refresh_entire_tree()
                        usage_logger.log("schedule_edit", field="duration",
                                         outcome="applied_no_review")
            
            # Refresh duration (it's calculated)
            item.setText(Columns.DURATION, node.duration)
            
            # Re-apply colors in case status/dates changed
            item.update_from_node()

            self.blockSignals(False)

            # Journal the edited node's own field changes (one line each)
            if old_vals[0] != node.start_date:
                usage_logger.log("schedule_edit", field="start", outcome="applied")
                journal_lines.append(
                    f"'{node.name}': start {old_vals[0] or '—'} → {node.start_date}")
            if old_vals[1] != node.end_date:
                usage_logger.log("schedule_edit", field="end", outcome="applied")
                journal_lines.append(
                    f"'{node.name}': end {old_vals[1] or '—'} → {node.end_date}")
            if old_vals[2] != node.status:
                usage_logger.log("status_edit", outcome="applied")
                journal_lines.append(f"'{node.name}': status → {node.status}")
            if old_vals[4] != node.waiting_on:
                if node.waiting_on:
                    journal_lines.append(
                        f"'{node.name}': waiting on {node.waiting_on} since {node.waiting_since}")
                    usage_logger.log("waiting_set")
                else:
                    journal_lines.append(f"'{node.name}': cleared waiting on {old_vals[4]}")
                    usage_logger.log("waiting_clear", days_waited=0)
            if column == Columns.TREE and journal_lines:
                usage_logger.log("task_name_edit", outcome="applied")
            for line in journal_lines:
                self.journal_event.emit(line)

            self.item_changed_signal.emit(node)
            self.update_filter_options()

    def on_item_collapsed(self, item: QTreeWidgetItem):
        if not isinstance(item, TaskTreeWidgetItem):
            return
        item.node.expanded = False
        # Collapse all descendants iteratively using an explicit stack.
        # The old recursive approach caused RecursionError on large projects
        # because (a) it called itself directly AND (b) child.setExpanded(False)
        # fired itemCollapsed again — doubling the recursion depth per level.
        # Blocking signals here prevents that re-entrant signal cascade.
        stack = [item.child(i) for i in range(item.childCount())]
        was_blocked = self.blockSignals(True)
        try:
            while stack:
                child = stack.pop()
                child.setExpanded(False)
                if isinstance(child, TaskTreeWidgetItem):
                    child.node.expanded = False
                for i in range(child.childCount()):
                    stack.append(child.child(i))
        finally:
            self.blockSignals(was_blocked)

    def on_item_expanded(self, item: QTreeWidgetItem):
        if isinstance(item, TaskTreeWidgetItem):
            item.node.expanded = True

    def set_outline_level(self, level):
        """Expand the tree to show exactly `level` outline levels
        (None = expand everything). Used by the timeline's level buttons —
        the chart mirrors the grid, so this is also the chart's zoom-out
        to a clean phase view."""
        was_blocked = self.blockSignals(True)
        try:
            iterator = QTreeWidgetItemIterator(self)
            while iterator.value():
                item = iterator.value()
                iterator += 1
                if not isinstance(item, TaskTreeWidgetItem):
                    continue
                depth = 0
                p = item.parent()
                while p:
                    depth += 1
                    p = p.parent()
                expand = True if level is None else (depth <= level - 2)
                item.setExpanded(expand)
                item.node.expanded = expand
        finally:
            self.blockSignals(was_blocked)

    def refresh_parents(self, item: QTreeWidgetItem):
        """Update parent items visually after a rollup."""
        parent = item.parent()
        while parent:
            if isinstance(parent, TaskTreeWidgetItem):
                parent.update_from_node()
            parent = parent.parent()

    def refresh_entire_tree(self):
        """
        Recursively update all items from their nodes.
        This is necessary because a change in one node might cascade to
        siblings or children anywhere in the tree.

        Signals are blocked for the entire pass: this method is a pure
        visual repaint from the model and must never fire on_item_changed
        (which is for user edits only).

        IMPORTANT: blockSignals returns the *previous* state — we save it
        and restore it instead of force-unblocking. This is required because
        on_item_changed (and other callers) may already have blocked signals
        before invoking us. If we unconditionally unblock at the end, the
        outer caller's signal-suppression breaks and update_from_node()
        re-fires on_item_changed → spurious "Start date is automatic" popup
        loops on STATUS edits.
        """
        was_blocked = self.blockSignals(True)
        try:
            iterator = QTreeWidgetItemIterator(self)
            while iterator.value():
                item = iterator.value()
                if isinstance(item, TaskTreeWidgetItem):
                    item.update_from_node()
                iterator += 1
        finally:
            self.blockSignals(was_blocked)

    def validate_child_dates(self, item: TaskTreeWidgetItem):
        """
        Check if child dates exceed LOCKED parent dates.
        Offers: Abort (Snap to parent), Retry (Unlock parent), Ignore.
        """
        node = item.node
        parent_node = node.parent
        
        if not parent_node or not parent_node.dates_locked:
            return

        # Get Parent Dates
        try:
            p_start = datetime.strptime(parent_node.start_date, "%Y-%m-%d")
            p_end = datetime.strptime(parent_node.end_date, "%Y-%m-%d")
            c_start = datetime.strptime(node.start_date, "%Y-%m-%d") if node.start_date else None
            c_end = datetime.strptime(node.end_date, "%Y-%m-%d") if node.end_date else None
        except ValueError:
            return

        violation = False
        msg_text = "WARNING: Child Task Dates Outside Parent Range\n\n"
        
        if c_start and c_start < p_start:
            msg_text += f"Child starts {(p_start - c_start).days} days before parent.\n"
            violation = True
            
        if c_end and c_end > p_end:
            msg_text += f"Child ends {(c_end - p_end).days} days after parent.\n"
            violation = True
            
        if violation:
            msg_text += f"\nParent: {parent_node.name} (Locked: {parent_node.start_date} - {parent_node.end_date})"
            msg_text += f"\nChild: {node.name} ({node.start_date} - {node.end_date})"
            msg_text += "\n\nWhat would you like to do?"
            
            box = QMessageBox(self)
            box.setWindowTitle("Date Validation Warning")
            box.setText(msg_text)
            box.setIcon(QMessageBox.Icon.Warning)
            
            btn_abort = box.addButton("Abort (Snap to Parent)", QMessageBox.ButtonRole.ActionRole)
            btn_retry = box.addButton("Retry (Unlock Parent)", QMessageBox.ButtonRole.ActionRole)
            btn_ignore = box.addButton("Ignore", QMessageBox.ButtonRole.ActionRole)
            
            box.exec()
            
            if box.clickedButton() == btn_abort:
                # Snap to parent
                if c_start and c_start < p_start:
                    node.set_date('start', parent_node.start_date)
                    item.setText(Columns.START, parent_node.start_date) # Update UI immediately
                if c_end and c_end > p_end:
                    node.set_date('end', parent_node.end_date)
                    item.setText(Columns.END, parent_node.end_date) # Update UI immediately
            elif box.clickedButton() == btn_retry:
                # Unlock parent
                parent_node.dates_locked = False
                # Find parent item and update it
                parent_item = item.parent()
                if isinstance(parent_item, TaskTreeWidgetItem):
                    parent_item.update_from_node()
            # else Ignore: do nothing

    def dragEnterEvent(self, event):
        # Accept note lines dragged from the docked pad; otherwise fall back to
        # the tree's normal internal-move behavior.
        if event.mimeData().hasFormat(NOTE_MIME):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat(NOTE_MIME):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        # A note dragged out of the pad: drop ON a task → append to its Notes;
        # drop on empty space → promote to a new, auto-scheduled task.
        if event.mimeData().hasFormat(NOTE_MIME):
            self._handle_note_drop(event)
            return

        # Let Qt handle the visual move
        super().dropEvent(event)

        # Now sync the internal TaskNode hierarchy to match the visual QTreeWidget hierarchy
        self.sync_hierarchy()

        current = self.currentItem()
        node = current.node if isinstance(current, TaskTreeWidgetItem) else None
        self.commit_structure_change(node)

    # ------------------------------------------------------------------
    # Notes-pad drop handling (promote a note to a task / a task's notes)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_owner_tags(text: str):
        """Pull @owner tags out of a note line. Returns (clean_name, owner)
        where owner is '/'-joined to match the app's owner convention."""
        import re
        tags = re.findall(r'@([\w.\-]+)', text)
        clean = re.sub(r'\s*@[\w.\-]+', '', text).strip()
        owner = "/".join(tags)
        return (clean or text), owner

    # Record separator between notes in the pad's drag payload (see
    # ui/notes_panel.py NOTE_SEP). A single note may span multiple lines, so
    # notes are split on this, never on "\n".
    _NOTE_SEP = "\x1e"

    def _handle_note_drop(self, event):
        raw = bytes(event.mimeData().data(NOTE_MIME)).decode("utf-8", "replace")
        # Each element is one whole note (may itself be multi-line). Fall back
        # to line-splitting only for a plain-text payload with no separator.
        if self._NOTE_SEP in raw:
            notes = [n.strip() for n in raw.split(self._NOTE_SEP) if n.strip()]
        else:
            notes = [l for l in raw.split("\n") if l.strip()]
        if not notes:
            event.ignore()
            return
        event.acceptProposedAction()

        pos = event.position().toPoint()
        target = self.itemAt(pos)
        col = self.columnAt(pos.x())

        # Drop on the Notes column of a task → attach to that task's notes.
        # Drop anywhere else on a task → insert a new task right after it.
        # Drop below all rows → append a new task at the end.
        if isinstance(target, TaskTreeWidgetItem) and col == Columns.NOTES:
            self._append_lines_to_notes(target, notes)
        else:
            anchor = target if isinstance(target, TaskTreeWidgetItem) else None
            self._promote_lines_to_tasks(notes, anchor_item=anchor)

        # Tell the pad to drop the note(s) it handed us.
        self.note_consumed.emit(raw)

    @staticmethod
    def _split_note(block: str):
        """A note block → (first_line, remaining_body). The first line becomes
        a task name; the rest (if any) becomes that task's Notes."""
        parts = (block or "").split("\n", 1)
        first = parts[0].strip()
        rest = parts[1].strip() if len(parts) > 1 else ""
        return first, rest

    def _append_lines_to_notes(self, item: 'TaskTreeWidgetItem', lines):
        """Drop on a task's Notes column: append each note to its Notes,
        timestamped the same way the Notes editor does. Each element may be a
        multi-line note; the stamp prefixes the whole block."""
        from datetime import datetime
        stamp = datetime.now().strftime("[%Y-%m-%d] ")
        node = item.node
        addition = "\n\n".join(stamp + l for l in lines)
        node.notes = (node.notes + "\n" + addition) if node.notes else addition
        was = self.blockSignals(True)
        try:
            item.update_from_node()
        finally:
            self.blockSignals(was)
        self.journal_event.emit(f"Added note to '{node.name}' from the pad")
        self.item_changed_signal.emit(node)
        usage_logger.log("note_promoted", to="task_notes")
        self._flash_item(item)
        self._show_link_hint(f"Note added to '{node.name}'.")

    def _promote_lines_to_tasks(self, lines, anchor_item=None):
        """Each line becomes a new task. If dropped on a row, the task is
        inserted right after it at the same level (so it lands in context);
        otherwise it's appended at the end. Auto-chained off the previous task
        so it gets real dates. @tags set the owner."""
        was = self.blockSignals(True)
        last = None
        try:
            if isinstance(anchor_item, TaskTreeWidgetItem):
                anchor_node = anchor_item.node
                parent_node = anchor_node.parent
                siblings = parent_node.children if parent_node else self.root_nodes
                ui_parent = anchor_item.parent()
                try:
                    idx = siblings.index(anchor_node) + 1
                except ValueError:
                    idx = len(siblings)
                prev = anchor_node
                for off, line in enumerate(lines):
                    first, body = self._split_note(line)
                    name, owner = self._parse_owner_tags(first)
                    new_node = TaskNode(name, parent=parent_node)
                    new_node.update_from_previous_sibling(prev)
                    if body:
                        new_node.notes = body
                    if owner:
                        new_node.set_waiting_on(owner)
                    siblings.insert(idx + off, new_node)
                    new_item = TaskTreeWidgetItem(new_node)
                    if ui_parent:
                        ui_parent.insertChild(
                            ui_parent.indexOfChild(anchor_item) + 1 + off, new_item)
                    else:
                        self.insertTopLevelItem(
                            self.indexOfTopLevelItem(anchor_item) + 1 + off, new_item)
                    prev = new_node
                    last = new_node
            else:
                prev = self.root_nodes[-1] if self.root_nodes else None
                for line in lines:
                    first, body = self._split_note(line)
                    name, owner = self._parse_owner_tags(first)
                    new_node = TaskNode(name)
                    if prev is not None:
                        new_node.update_from_previous_sibling(prev)
                    if body:
                        new_node.notes = body
                    if owner:
                        new_node.set_waiting_on(owner)
                    self.root_nodes.append(new_node)
                    self.add_node_to_tree(new_node, self.invisibleRootItem())
                    prev = new_node
                    last = new_node
        finally:
            self.blockSignals(was)

        if last is not None:
            self.journal_event.emit(f"Promoted {len(lines)} note(s) to task(s)")
            usage_logger.log("note_promoted", to="task")
            usage_logger.log("task_add", via="note_drop", count=len(lines))
            self.commit_structure_change(last)
            # Scroll to & flash the new task so it never looks like it vanished.
            self.jump_to_node_id(last.id)
            self._show_link_hint(f"Note promoted to task '{last.name}'.")

    def sync_hierarchy(self):
        # Rebuild root_nodes list and parent/child relationships based on visual order
        new_roots = []
        
        def process_item(item, parent_node=None):
            node = item.node
            
            # Update parent
            if node.parent != parent_node:
                # Remove from old parent's children list if it was there
                if node.parent and node in node.parent.children:
                    node.parent.children.remove(node)
                node.parent = parent_node
            
            # Clear children list and rebuild it from visual children
            node.children = []
            for i in range(item.childCount()):
                child_item = item.child(i)
                if isinstance(child_item, TaskTreeWidgetItem):
                    node.children.append(child_item.node)
                    process_item(child_item, node)
                    
        # Process top-level items
        for i in range(self.topLevelItemCount()):
            item = self.topLevelItem(i)
            if isinstance(item, TaskTreeWidgetItem):
                new_roots.append(item.node)
                process_item(item, None)
                
        self.root_nodes = new_roots
        


    def indent_task(self, item):
        self._indent_item_internal(item)
        self.sync_hierarchy()
        self.commit_structure_change(item.node if isinstance(item, TaskTreeWidgetItem) else None)
        self._refocus_item(item)

    def _indent_item_internal(self, item):
        # Move item to be a child of the previous sibling
        parent = item.parent()
        index = parent.indexOfChild(item) if parent else self.indexOfTopLevelItem(item)
        
        if index > 0:
            prev_item = parent.child(index - 1) if parent else self.topLevelItem(index - 1)
            
            # Remove from current position
            if parent:
                parent.takeChild(index)
            else:
                self.takeTopLevelItem(index)
                
            # Add to prev_item
            prev_item.addChild(item)
            prev_item.setExpanded(True)

    def outdent_task(self, item):
        self._outdent_item_internal(item)
        self.sync_hierarchy()
        self.commit_structure_change(item.node if isinstance(item, TaskTreeWidgetItem) else None)
        self._refocus_item(item)

    def _refocus_item(self, item):
        """Keep the keyboard on the row the user just indented/outdented.
        Without this, Qt hands focus/selection to the old parent after the
        item is re-parented — so Tab-then-Enter flows kept landing on the
        wrong row and the user had to click the child again."""
        if not isinstance(item, TaskTreeWidgetItem):
            return
        was_blocked = self.blockSignals(True)
        try:
            self.clearSelection()
            self.setCurrentItem(item)
            item.setSelected(True)
        finally:
            self.blockSignals(was_blocked)

    def _outdent_item_internal(self, item):
        # Move item to be a sibling of its parent
        parent = item.parent()
        if parent:
            grandparent = parent.parent()
            index = parent.indexOfChild(item)
            parent_index = grandparent.indexOfChild(parent) if grandparent else self.indexOfTopLevelItem(parent)
            
            # Remove from parent
            parent.takeChild(index)
            
            # Add to grandparent (or root) after parent
            if grandparent:
                grandparent.insertChild(parent_index + 1, item)
            else:
                self.insertTopLevelItem(parent_index + 1, item)

    def indent_selected_tasks(self, items):
        # Sort items by visual order to ensure consistent behavior
        sorted_items = self._get_sorted_selection(items)
        
        for item in sorted_items:
            self._indent_item_internal(item)

        self.sync_hierarchy()
        self.commit_structure_change(sorted_items[-1].node if sorted_items else None)
        if sorted_items:
            self._refocus_item(sorted_items[-1])
            for it in sorted_items:
                it.setSelected(True)

    def outdent_selected_tasks(self, items):
        # Sort items by visual order
        sorted_items = self._get_sorted_selection(items)
        
        for item in sorted_items:
            self._outdent_item_internal(item)

        self.sync_hierarchy()
        self.commit_structure_change(sorted_items[-1].node if sorted_items else None)
        if sorted_items:
            self._refocus_item(sorted_items[-1])
            for it in sorted_items:
                it.setSelected(True)

    def _get_sorted_selection(self, items):
        """Returns the items sorted by their appearance in the tree."""
        selected_ids = {id(i) for i in items}
        sorted_list = []
        iterator = QTreeWidgetItemIterator(self)
        while iterator.value():
            item = iterator.value()
            if id(item) in selected_ids:
                sorted_list.append(item)
            iterator += 1
        return sorted_list

    def indent_smart(self, item):
        selected = self.selectedItems()
        if len(selected) > 1:
            self.indent_selected_tasks(selected)
            usage_logger.log("indent", count=len(selected))
        else:
            # If item passed from context menu is not selected, use it. 
            # Otherwise use selection (which might be just 1 item)
            target = item if item else (selected[0] if selected else None)
            if target:
                self.indent_task(target)
                usage_logger.log("indent", count=1)

    def outdent_smart(self, item):
        selected = self.selectedItems()
        if len(selected) > 1:
            self.outdent_selected_tasks(selected)
            usage_logger.log("outdent", count=len(selected))
        else:
            target = item if item else (selected[0] if selected else None)
            if target:
                self.outdent_task(target)
                usage_logger.log("outdent", count=1)

    def delete_smart(self, item):
        selected = self.selectedItems()
        if len(selected) > 1:
            # Bulk delete
            # Sort reverse to avoid index issues? 
            # Actually delete_task handles removal safely.
            # But we should probably ask for confirmation once.
            reply = QMessageBox.question(self, "Delete Tasks", 
                                       f"Are you sure you want to delete {len(selected)} tasks?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                for i in selected:
                    self.delete_task(i)
        else:
            target = item if item else (selected[0] if selected else None)
            if target:
                self.delete_task(target)

    def expand_all_selected(self, items):
        """Recursively expand selected items and their children."""
        for item in items:
            self.expand_recursive(item)

    def expand_recursive(self, item: QTreeWidgetItem):
        """Recursively expand an item and all its descendants."""
        item.setExpanded(True)
        if isinstance(item, TaskTreeWidgetItem):
            item.node.expanded = True
            
        for i in range(item.childCount()):
            child = item.child(i)
            self.expand_recursive(child)

from PyQt6.QtWidgets import QTreeWidgetItemIterator

