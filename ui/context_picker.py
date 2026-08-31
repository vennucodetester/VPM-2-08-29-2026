"""Shared keyboard/search/recent-choice shell for contextual cell pickers."""
from PyQt6.QtCore import Qt, QSettings
from PyQt6.QtWidgets import QComboBox, QDialog, QVBoxLayout


class ContextPickerDialog(QDialog):
    def __init__(self, context_key, parent=None):
        super().__init__(parent)
        self.context_key = context_key
        self.picker_layout = QVBoxLayout(self)
        self._settings = QSettings("VPM", "VPMTracker")

    def searchable_combo(self, choices):
        """Create a type-to-filter combo ordered by recent selection."""
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        recent = self._settings.value(f"picker_recent/{self.context_key}", []) or []
        if isinstance(recent, str):
            recent = [recent]
        ordered = sorted(choices, key=lambda pair: (
            recent.index(str(pair[1])) if str(pair[1]) in recent else len(recent),
            pair[0].lower()))
        for label, value in ordered:
            combo.addItem(label, value)
        combo.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        combo.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        return combo

    def remember_choice(self, value):
        if value in (None, ""):
            return
        key = f"picker_recent/{self.context_key}"
        recent = self._settings.value(key, []) or []
        if isinstance(recent, str):
            recent = [recent]
        value = str(value)
        self._settings.setValue(key, [value] + [v for v in recent if v != value][:7])
