"""
NotesPanel — a per-project RICH-TEXT notepad (one free-form note).

One note per project that behaves like a real notepad: type freely, press
Enter for a new line, make bullet lists, bold / italic / underline, and
highlight text. It persists as HTML inside the .vpmt (per project) and its
plain text is included in Ctrl+F "Search Everything".

The "To task" button turns the selected line(s) — or the current line — into
task(s) in the tracker (first line = task name), preserving the old
note→task workflow without forcing the note to be a list of one-liners.
"""
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QTextEdit, QToolButton)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import (QTextListFormat, QTextCharFormat, QColor, QFont,
                         QKeySequence, QShortcut)

# Retained so older imports/drag code keep resolving.
NOTE_MIME = "application/x-vpm-note"
HIGHLIGHT = "#FFF59D"


class NotesPanel(QWidget):
    """Rich-text notepad: formatting toolbar + one QTextEdit note."""

    notes_changed = pyqtSignal()
    notes_edited = pyqtSignal()
    make_tasks_requested = pyqtSignal(list)  # list[str] lines -> tasks

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QLabel("📝 Notes")
        header.setStyleSheet("font-weight:bold; font-size:13px;")
        layout.addWidget(header)

        bar = QHBoxLayout()
        bar.setSpacing(4)

        def tool(label, tip, slot, checkable=False, style=""):
            b = QToolButton()
            b.setText(label)
            b.setToolTip(tip)
            b.setCheckable(checkable)
            if style:
                b.setStyleSheet(style)
            b.clicked.connect(slot)
            bar.addWidget(b)
            return b

        self.btn_bold = tool("B", "Bold (Ctrl+B)", self._toggle_bold, True,
                             "font-weight:bold;")
        self.btn_italic = tool("I", "Italic (Ctrl+I)", self._toggle_italic, True,
                               "font-style:italic;")
        self.btn_under = tool("U", "Underline (Ctrl+U)", self._toggle_underline,
                              True, "text-decoration:underline;")
        self.btn_bullet = tool("• List", "Bullet list", self._toggle_bullets)
        self.btn_high = tool("🖍", "Highlight selection", self._toggle_highlight)
        bar.addStretch()
        self.btn_task = tool("➕ To task",
                             "Turn the selected line(s) into task(s)",
                             self._emit_make_tasks)
        layout.addLayout(bar)

        self.editor = QTextEdit()
        self.editor.setAcceptRichText(True)
        self.editor.setPlaceholderText(
            "Write freely…  Enter = new line.  Use the buttons above for "
            "bullets, bold, and highlight.  Select a line and press “To task”.")
        layout.addWidget(self.editor, 1)

        # Debounce: don't push an undo step / dirty flag on every keystroke.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(900)
        self._debounce.timeout.connect(self.notes_changed.emit)
        self.editor.textChanged.connect(self.notes_edited.emit)
        self.editor.textChanged.connect(self._debounce.start)
        self.editor.currentCharFormatChanged.connect(self._sync_buttons)

        # Keyboard shortcuts scoped to the editor.
        for seq, slot in (("Ctrl+B", self._toggle_bold_key),
                          ("Ctrl+I", self._toggle_italic_key),
                          ("Ctrl+U", self._toggle_underline_key)):
            sc = QShortcut(QKeySequence(seq), self.editor)
            sc.activated.connect(slot)

    # ---- formatting ----
    def _merge(self, fmt):
        self.editor.mergeCurrentCharFormat(fmt)

    def _toggle_bold(self):
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.Bold if self.btn_bold.isChecked()
                          else QFont.Weight.Normal)
        self._merge(fmt)

    def _toggle_italic(self):
        fmt = QTextCharFormat()
        fmt.setFontItalic(self.btn_italic.isChecked())
        self._merge(fmt)

    def _toggle_underline(self):
        fmt = QTextCharFormat()
        fmt.setFontUnderline(self.btn_under.isChecked())
        self._merge(fmt)

    def _toggle_bold_key(self):
        self.btn_bold.setChecked(not self.btn_bold.isChecked())
        self._toggle_bold()

    def _toggle_italic_key(self):
        self.btn_italic.setChecked(not self.btn_italic.isChecked())
        self._toggle_italic()

    def _toggle_underline_key(self):
        self.btn_under.setChecked(not self.btn_under.isChecked())
        self._toggle_underline()

    def _toggle_bullets(self):
        self.editor.textCursor().createList(QTextListFormat.Style.ListDisc)

    def _toggle_highlight(self):
        cursor = self.editor.textCursor()
        fmt = QTextCharFormat()
        current = cursor.charFormat().background().color()
        if current == QColor(HIGHLIGHT):
            fmt.setBackground(QColor(Qt.GlobalColor.transparent))
        else:
            fmt.setBackground(QColor(HIGHLIGHT))
        self._merge(fmt)

    def _sync_buttons(self, fmt):
        self.btn_bold.setChecked(fmt.fontWeight() >= QFont.Weight.Bold)
        self.btn_italic.setChecked(fmt.fontItalic())
        self.btn_under.setChecked(fmt.fontUnderline())

    # ---- to task ----
    def _emit_make_tasks(self):
        cursor = self.editor.textCursor()
        text = cursor.selectedText()
        if not text:
            cursor.select(cursor.SelectionType.LineUnderCursor)
            text = cursor.selectedText()
        # QTextEdit uses U+2029 as the paragraph separator in selectedText.
        lines = [l.strip() for l in text.replace(" ", "\n").split("\n")
                 if l.strip()]
        if lines:
            self.make_tasks_requested.emit(lines)

    def flush(self):
        """If a debounced text change is pending, cancel the timer and emit notes_changed now."""
        if hasattr(self, "_debounce") and self._debounce.isActive():
            self._debounce.stop()
            self.notes_changed.emit()

    # ---- focus ----
    def focus_capture(self):
        self.editor.setFocus()

    # ---- persistence ----
    def set_html(self, html):
        was = self.editor.blockSignals(True)
        try:
            self.editor.setHtml(html or "")
        finally:
            self.editor.blockSignals(was)

    def get_html(self):
        return self.editor.toHtml()

    def plain_text(self):
        return self.editor.toPlainText()

    def load_notes(self, lines):
        """Back-compat: older files stored a list of note-line strings."""
        lines = [str(l) for l in (lines or []) if str(l).strip()]
        was = self.editor.blockSignals(True)
        try:
            if lines:
                self.editor.setPlainText("\n".join(lines))
            else:
                self.editor.clear()
        finally:
            self.editor.blockSignals(was)

    def get_notes(self):
        """Plain-text lines — kept so Ctrl+F search and older savers still work."""
        return [l for l in self.plain_text().splitlines() if l.strip()]

    def remove_line(self, raw):
        """Legacy no-op: drag-from-pad promotion was replaced by the To-task
        button, so there is no separate list line to remove."""
        return
