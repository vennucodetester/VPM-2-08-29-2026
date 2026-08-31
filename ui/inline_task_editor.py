"""Inline Task-cell editor with contextual ``=`` autocomplete."""
import time
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QAbstractItemView, QFrame, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout
from utils import usage_logger


class InlineTaskEditor(QLineEdit):
    # Carries a complete, editor-independent snapshot. On Windows the tree
    # delegate may begin closing this editor before a detached popup delivers
    # itemPressed, so the receiver must not depend on the editor still owning
    # a valid model index.
    selectionCommitted = pyqtSignal(object)

    def __init__(self, options, parent=None):
        super().__init__(parent)
        self.options = list(options)
        self.tokens = []
        self.initial_tokens = []
        self.initial_name = ""
        self.lookup_active = False
        self.query = ""
        self.lookup_started = None
        self.lookup_base_text = ""
        self.popup = QFrame(None, Qt.WindowType.ToolTip)
        layout = QVBoxLayout(self.popup)
        layout.setContentsMargins(2, 2, 2, 2)
        self.results = QListWidget()
        self.results.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # Use press rather than click so the query is replaced before the
        # tree delegate can close the editor because of an outside click.
        self.results.itemPressed.connect(self._choose_item)
        layout.addWidget(self.results)
        # Tokens are stored separately from their visible ``[Label]`` chips.
        # If a user edits away a chip (especially Ctrl+A, Delete), remove the
        # corresponding hidden state instead of resurrecting it on commit.
        self.textEdited.connect(self._sync_tokens_to_visible_text)

    def close_popup(self, restore=False):
        if restore:
            self.setText(self.lookup_base_text)
            self.setCursorPosition(len(self.text()))
        self.lookup_active = False
        self.query = ""
        self.popup.hide()

    def set_value(self, name, tokens):
        self.tokens = [dict(token) for token in (tokens or [])]
        self.initial_tokens = [dict(token) for token in self.tokens]
        self.initial_name = name or ""
        self.setText(self._prefix() + (name or ""))
        self.setCursorPosition(len(self.text()))

    def plain_name(self):
        prefix = self._prefix()
        text = self.lookup_base_text if self.lookup_active else self.text()
        return text[len(prefix):].strip() if text.startswith(prefix) else text.strip()

    def tokens_for_commit(self):
        """Return only tokens whose chips are still present in the editor."""
        if not self.tokens:
            return []
        if self.lookup_active:
            text = self.lookup_base_text
        else:
            text = self.text()
        return ([dict(token) for token in self.tokens]
                if text.startswith(self._prefix()) else [])

    def _sync_tokens_to_visible_text(self, text):
        if self.lookup_active or not self.tokens:
            return
        if not text.startswith(self._prefix()):
            self.tokens = []

    def _prefix(self):
        return "".join(f"[{token.get('label', '')}] " for token in self.tokens)

    def start_lookup(self):
        self.lookup_active = True
        self.query = ""
        self.lookup_base_text = self.text()
        self._show_query()
        self.lookup_started = time.monotonic()
        usage_logger.log("task_inline_lookup", outcome="opened")
        self._refresh_results()

    def handle_lookup_key(self, event: QKeyEvent):
        if not self.lookup_active:
            if event.text() == "=":
                # Replacing a full-cell selection must replace its hidden
                # tokens too. The delegate intercepts '=' before QLineEdit can
                # perform the normal selected-text replacement itself.
                if (self.hasSelectedText()
                        and self.selectionStart() == 0
                        and len(self.selectedText()) == len(self.text())):
                    self.tokens = []
                    self.setText("")
                    self.setCursorPosition(0)
                self.start_lookup()
                return True
            return False
        key = event.key()
        if key == Qt.Key.Key_Escape:
            usage_logger.log("task_inline_lookup", outcome="canceled",
                             ms=int((time.monotonic() - self.lookup_started) * 1000)
                             if self.lookup_started else 0)
            self.close_popup(restore=True)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
            item = self.results.currentItem()
            if item and item.data(Qt.ItemDataRole.UserRole):
                self._choose_item(item)
            return True
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            count = self.results.count()
            if count:
                step = -1 if key == Qt.Key.Key_Up else 1
                row = self.results.currentRow()
                for _ in range(count):
                    row = (row + step) % count
                    candidate = self.results.item(row)
                    if candidate.flags() & Qt.ItemFlag.ItemIsSelectable:
                        self.results.setCurrentRow(row)
                        break
            return True
        if key == Qt.Key.Key_Backspace:
            if self.query:
                self.query = self.query[:-1]
                self._show_query()
                self._refresh_results()
            else:
                self.close_popup(restore=True)
            return True
        if event.text() and event.text().isprintable():
            self.query += event.text()
            self._show_query()
            self._refresh_results()
            return True
        return True

    def _show_query(self):
        self.setText(f"{self.lookup_base_text}={self.query}")
        self.setCursorPosition(len(self.text()))

    def _refresh_results(self):
        self.results.clear()
        query = self.query.casefold()
        grouped = {}
        for option in self.options:
            header = option.get("header", "Options")
            if query in header.casefold() or query in option.get("label", "").casefold():
                grouped.setdefault(header, []).append(option)
        # A category headline is also a valid parent selection. If a real
        # option has the same label (for example Lab Testing), use its full
        # metadata and show it only once—as the selectable headline.
        header_names = {header.casefold() for header in grouped}
        parent_options = {
            option.get("label", "").casefold(): option
            for option in self.options
            if option.get("label", "").casefold() in header_names
        }
        first_selectable = None
        # Put headers matching the typed query first. For ``=lab`` this makes
        # Lab Testing the first result instead of its enclosing Project Phases
        # group; for ``=room`` it puts Rooms first.
        ordered_groups = sorted(
            grouped.items(),
            key=lambda pair: (query not in pair[0].casefold(),
                              pair[0].casefold()))
        for header, options in ordered_groups:
            heading = QListWidgetItem(header)
            parent = parent_options.get(header.casefold()) or {
                "id": f"header:{header}", "label": header,
                "kind": "header", "duration": None, "header": header,
            }
            heading.setData(Qt.ItemDataRole.UserRole, parent)
            heading.setFlags(Qt.ItemFlag.ItemIsEnabled |
                             Qt.ItemFlag.ItemIsSelectable)
            heading.setForeground(Qt.GlobalColor.darkGray)
            self.results.addItem(heading)
            if first_selectable is None:
                first_selectable = heading
            for option in options:
                if option.get("label", "").casefold() in header_names:
                    continue
                suffix = (f"  · {option['duration']} days"
                          if option.get("duration") not in (None, "") else "")
                item = QListWidgetItem(f"  {option['label']}{suffix}")
                item.setData(Qt.ItemDataRole.UserRole, option)
                self.results.addItem(item)
                if first_selectable is None:
                    first_selectable = item
        if first_selectable is not None:
            self.results.setCurrentItem(first_selectable)
        rows = max(1, min(10, self.results.count()))
        self.popup.resize(max(360, self.width()), rows * 28 + 8)
        self.popup.move(self.mapToGlobal(self.rect().bottomLeft()))
        self.popup.show()

    def _choose_item(self, item):
        option = item.data(Qt.ItemDataRole.UserRole)
        if not option:
            return
        # Restore the exact pre-query value first. This guarantees that the
        # visible ``=query`` span cannot leak into the committed task name.
        self.setText(self.lookup_base_text)
        name = self.plain_name()
        token = {key: option.get(key) for key in
                 ("id", "label", "kind", "duration", "header")}
        self.tokens.append(token)
        self.setText(self._prefix() + name)
        self.setCursorPosition(len(self.text()))
        self.close_popup()
        self.setFocus()
        usage_logger.log("task_inline_lookup", outcome="selected",
                         kind=option.get("kind"),
                         has_timeline=option.get("duration") not in (None, ""),
                         ms=int((time.monotonic() - self.lookup_started) * 1000)
                         if self.lookup_started else 0)
        self.selectionCommitted.emit({
            "node_id": self.property("vpm_node_id"),
            "tokens": [dict(value) for value in self.tokens],
            "previous_tokens": [dict(value) for value in self.initial_tokens],
            "previous_name": self.initial_name,
            "name": name,
        })

    def hideEvent(self, event):
        self.popup.hide()
        super().hideEvent(event)

    def deleteLater(self):
        self.popup.deleteLater()
        super().deleteLater()
