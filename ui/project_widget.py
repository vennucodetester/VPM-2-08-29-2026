"""
ProjectWidget — one project's Tracker + Visuals pair.

The outer MainWindow creates one ProjectWidget per project tab. Each widget
owns its own TreeGridView, FocusView, metadata, and undo stack, and
registers itself with ConfigManager so scheduler calls always resolve
settings against the active project.
"""
import uuid
from typing import Dict, List

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QTabWidget, QSplitter, QDialog, QLabel
from PyQt6.QtCore import pyqtSignal, Qt, QTimer, QSettings

from models.task_node import TaskNode
from vpm_tracker_core import Columns
from ui.tree_grid_view import TreeGridView
from ui.focus_view import FocusView
from ui.timeline_pane import TimelineContainer
from ui.notes_panel import NotesPanel
from utils.config_manager import ConfigManager
from utils.history import HistoryStack
from utils import usage_logger


class ProjectWidget(QWidget):
    # Fires when anything in this project changes (for the dirty flag / title).
    project_changed = pyqtSignal()

    def __init__(self, name: str = "Project 1", metadata: Dict = None,
                 roots: List[TaskNode] = None, journal: list = None,
                 notes: list = None, notepad_html: str = None,
                 is_vave: bool = False, project_id: str = None,
                 note_tabs: list = None, resources: dict = None, parent=None):
        super().__init__(parent)
        self.project_id = project_id or f"proj-{uuid.uuid4().hex}"
        self.name = name or "Project 1"
        self.history = HistoryStack(max_depth=50)
        # Set before _build_ui so the pad's signal handlers are always safe.
        self._restoring = False
        self._suppress_notes_history = False
        self._batch_depth = 0
        self._batch_changed = False
        # Activity journal: the app's own diary of every change, persisted
        # in the .vpmt file. Entries: {"ts": "2026-06-10 14:32", "text": "..."}
        self.journal: list = list(journal) if journal else []
        self.is_vave = bool(is_vave)
        self.reviewed_through = (metadata or {}).get("reviewed_through")
        self._loaded_note_tabs = list(note_tabs or [])
        self.resource_lists = {key: list((resources or {}).get(key, []))
                               for key in ("case", "room", "article")}

        # Register metadata into the per-project config store.
        ConfigManager.register_project(self.project_id, metadata or {})

        self._build_ui()

        # Initial load — important to do after ConfigManager is registered
        # so the scheduler reads the right holidays/weekend rules.
        self._activate_config()
        self.tree_view.load_project(list(roots) if roots else [])
        # The timeline's dollar bar refreshed once during _build_ui, BEFORE
        # these roots existed (totals 0 → it hid itself). Wake it now that the
        # project's tasks are loaded, so a VAVE file shows its $ bar on open.
        self.timeline.refresh()
        if notepad_html:
            self.notes_panel.set_html(notepad_html)
        else:
            self.notes_panel.load_notes(list(notes) if notes else [])

        # Undo timing: we snapshot AFTER a mutation, but push the *previous*
        # snapshot (held in _last_snapshot). That way undo restores the state
        # the user saw *before* the edit, not the one they just produced.
        self._last_snapshot = self.get_snapshot()
        self._restoring = False  # suppress history pushes during undo/redo restore
        self.tree_view.item_changed_signal.connect(self._on_tree_changed)
        self.tree_view.journal_event.connect(self.log_event)

    # ---- UI ----
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.inner_tabs = QTabWidget()
        layout.addWidget(self.inner_tabs)

        self.tree_view = TreeGridView()
        self.tree_view.resource_lists = self.resource_lists

        # Tracker tab = grid + attached timeline (MS Project style):
        # same rows, same scroll, same collapse state — the timeline asks
        # the grid where each row is, so they can never drift apart.
        self.timeline = TimelineContainer(self.tree_view)
        tracker_split = QSplitter(Qt.Orientation.Horizontal)
        tracker_split.addWidget(self.tree_view)
        tracker_split.addWidget(self.timeline)
        # The grid (left) is the home view and never collapses. The bar chart
        # and the notes pad are BOTH collapsible — drag their handle fully in,
        # or use the chart's Hide button.
        tracker_split.setCollapsible(0, False)
        tracker_split.setCollapsible(1, True)
        # Default layout: grid dominant, bar chart a slim strip, notes hidden.
        # Extra width always goes to the grid; the user widens the chart or pad
        # only when they want to.
        tracker_split.setStretchFactor(0, 1)
        tracker_split.setStretchFactor(1, 0)
        tracker_split.setSizes([1000, 240])
        self.tracker_split = tracker_split

        self.tracker_tab = QWidget()
        tracker_layout = QVBoxLayout(self.tracker_tab)
        tracker_layout.setContentsMargins(0, 0, 0, 0)
        tracker_layout.setSpacing(0)
        tracker_layout.addWidget(tracker_split, 1)
        self.vave_totals_label = QLabel()
        self.vave_totals_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.vave_totals_label.setStyleSheet(
            "QLabel { padding: 6px 12px; font-weight: bold; border-top: 1px solid #c8c8c8; }"
        )
        tracker_layout.addWidget(self.vave_totals_label)
        self.inner_tabs.addTab(self.tracker_tab, "Tracker")

        self.notes_dialog = QDialog(self)
        self.notes_dialog.setWindowTitle(f"Notes Center — {self.name}")
        self.notes_dialog.resize(760, 640)  # bigger default; user can resize
        self.notes_dialog.setModal(False)
        notes_layout = QVBoxLayout(self.notes_dialog)
        notes_layout.setContentsMargins(0, 0, 0, 0)
        from PyQt6.QtWidgets import QHBoxLayout, QPushButton, QListWidget, QInputDialog
        note_tools = QHBoxLayout()
        add_tab = QPushButton("+ Note Tab")
        rename_tab = QPushButton("Rename")
        delete_tab = QPushButton("Delete")
        note_tools.addWidget(add_tab)
        note_tools.addWidget(rename_tab)
        note_tools.addWidget(delete_tab)
        note_tools.addStretch()
        notes_layout.addLayout(note_tools)
        self.notes_tabs = QTabWidget()
        notes_layout.addWidget(self.notes_tabs, 1)
        self.notes_panel = NotesPanel()
        self.notes_tabs.addTab(self.notes_panel, "Overall")
        self.all_task_notes = QListWidget()
        self.all_task_notes.itemActivated.connect(self._jump_from_all_notes)
        self.notes_tabs.addTab(self.all_task_notes, "All Task Notes")
        self.custom_note_panels = []
        for saved in self._loaded_note_tabs:
            self._add_note_tab(saved.get("name", "Notes"), saved.get("html", ""), mark=False)
        add_tab.clicked.connect(self._prompt_add_note_tab)
        rename_tab.clicked.connect(self._rename_note_tab)
        delete_tab.clicked.connect(self._delete_note_tab)

        # Remember the pad's size/position across opens and app restarts.
        self._notes_settings = QSettings("VPM", "VPMTracker")

        def hide_notes(event):
            event.ignore()
            self._notes_settings.setValue(
                "notes_pad_geometry", self.notes_dialog.saveGeometry())
            self.notes_dialog.hide()

        self.notes_dialog.closeEvent = hide_notes

        # Promoting a note into the grid removes it from the pad; pad edits
        # flow into the same undo history as task changes.
        self.tree_view.note_consumed.connect(self._on_note_consumed)
        self.notes_panel.notes_changed.connect(self._on_notes_changed)
        self.notes_panel.notes_edited.connect(self._on_notes_edited)
        self.notes_panel.make_tasks_requested.connect(self._make_tasks_from_notes)

        self.gantt_view = FocusView()  # name kept so callers don't change
        self.gantt_view.task_activated.connect(self._jump_to_task)
        self.gantt_view.task_action_requested.connect(self._handle_focus_action)
        self.inner_tabs.addTab(self.gantt_view, "Visuals")

        self._focus_refresh_timer = QTimer(self)
        self._focus_refresh_timer.setSingleShot(True)
        self._focus_refresh_timer.setInterval(1000)
        self._focus_refresh_timer.timeout.connect(self._refresh_focus_if_visible)
        self.inner_tabs.currentChanged.connect(self._on_inner_tab_changed)
        self.tree_view.set_vave_enabled(self.is_vave)
        self._update_vave_totals()

    def _on_inner_tab_changed(self, index: int):
        labels = {0: "tracker", 1: "visuals"}
        usage_logger.log("tab_switch", to=labels.get(index, "project_view"))
        if index == 1:  # Visuals
            self.gantt_view.load_nodes(self.tree_view.root_nodes)

    def set_vave_enabled(self, enabled: bool):
        enabled = bool(enabled)
        if self.is_vave == enabled:
            return
        self.is_vave = enabled
        self.tree_view.set_vave_enabled(enabled)
        self.timeline.refresh()  # dollar bar + $ labels follow the VAVE flag
        self._update_vave_totals()
        self.log_event(f"VAVE Project {'enabled' if enabled else 'disabled'}")
        usage_logger.log("vave_toggle", on=enabled)
        self._on_tree_changed()

    def _refresh_focus_if_visible(self):
        if self.inner_tabs.currentIndex() == 1:
            self.gantt_view.load_nodes(self.tree_view.root_nodes)

    def _jump_to_task(self, node_id: str):
        """Click on a task name in Visuals → show that row in the Tracker."""
        self.inner_tabs.setCurrentIndex(0)
        self.tree_view.jump_to_node_id(node_id)

    def _handle_focus_action(self, node_id: str, action: str):
        item = self.tree_view._find_item_by_id(node_id)
        if not item:
            return
        node = item.node
        if action == "complete":
            if node.status != "Completed":
                node.status = "Completed"
                node.update_status_from_dates()
                self.tree_view.journal_event.emit(
                    f"'{node.name}': status set to Completed from Visuals")
                self.tree_view.commit_structure_change(node)
        elif action == "delay":
            self.tree_view.record_real_delay(node)
        self.gantt_view.load_nodes(self.tree_view.root_nodes)

    def _add_note_tab(self, name, html="", mark=True):
        panel = NotesPanel()
        panel.set_html(html)
        panel.notes_changed.connect(self._on_notes_changed)
        panel.notes_edited.connect(self._on_notes_edited)
        panel.make_tasks_requested.connect(self._make_tasks_from_notes)
        self.custom_note_panels.append(panel)
        self.notes_tabs.addTab(panel, (name or "Notes").strip())
        if mark:
            self.notes_tabs.setCurrentWidget(panel)
            self._on_notes_changed()

    def _prompt_add_note_tab(self):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self.notes_dialog, "New Note Tab", "Name:")
        if ok and name.strip():
            self._add_note_tab(name.strip())

    def _rename_note_tab(self):
        from PyQt6.QtWidgets import QInputDialog
        index = self.notes_tabs.currentIndex()
        if index < 2:
            return
        old = self.notes_tabs.tabText(index)
        name, ok = QInputDialog.getText(self.notes_dialog, "Rename Note Tab", "Name:", text=old)
        if ok and name.strip() and name.strip() != old:
            self.notes_tabs.setTabText(index, name.strip())
            self._on_notes_changed()

    def _delete_note_tab(self):
        from PyQt6.QtWidgets import QMessageBox
        index = self.notes_tabs.currentIndex()
        if index < 2:
            return
        if QMessageBox.question(
                self.notes_dialog, "Delete Note Tab",
                f"Delete '{self.notes_tabs.tabText(index)}'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        panel = self.notes_tabs.widget(index)
        self.notes_tabs.removeTab(index)
        if panel in self.custom_note_panels:
            self.custom_note_panels.remove(panel)
        panel.deleteLater()
        self._on_notes_changed()

    def _note_tabs_data(self):
        return [{"name": self.notes_tabs.tabText(self.notes_tabs.indexOf(panel)),
                 "html": panel.get_html()}
                for panel in self.custom_note_panels]

    def _set_note_tabs_data(self, values):
        for panel in list(self.custom_note_panels):
            self.notes_tabs.removeTab(self.notes_tabs.indexOf(panel))
            panel.deleteLater()
        self.custom_note_panels = []
        for saved in values or []:
            self._add_note_tab(saved.get("name", "Notes"), saved.get("html", ""), mark=False)

    def _refresh_all_task_notes(self):
        self.all_task_notes.clear()
        for node in self.tree_view.get_all_nodes_flat():
            if not node.notes.strip():
                continue
            parts, current = [], node
            while current:
                parts.append(current.name)
                current = current.parent
            label = f"[{self.name}] {' > '.join(reversed(parts))}\n{node.notes.strip()}"
            from PyQt6.QtWidgets import QListWidgetItem
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, node.id)
            self.all_task_notes.addItem(item)

    def _jump_from_all_notes(self, item):
        node_id = item.data(Qt.ItemDataRole.UserRole)
        if node_id:
            self.notes_dialog.hide()
            self._jump_to_task(node_id)

    def show_notes_and_capture(self):
        """Show the notes pad as a floating notepad window.

        Closing it only hides it; the same widget stays alive, so its text
        remains in the background and is still saved with the project.
        """
        self.inner_tabs.setCurrentIndex(0)  # Tracker tab
        self.notes_dialog.setWindowTitle(f"Notes Center — {self.name}")
        self._refresh_all_task_notes()
        geo = self._notes_settings.value("notes_pad_geometry")
        if geo is not None:
            self.notes_dialog.restoreGeometry(geo)
        self.notes_dialog.show()
        self.notes_dialog.raise_()
        self.notes_dialog.activateWindow()
        self.notes_panel.focus_capture()
        usage_logger.log("notes_pad_open")

    def show_contextual_notes(self):
        """Ctrl+Space opens selected task notes; otherwise the Notes Center."""
        selected = [item for item in self.tree_view.selectedItems()
                    if hasattr(item, "node")]
        if selected:
            item = selected[0]
            delegate = self.tree_view.itemDelegateForColumn(Columns.NOTES)
            if hasattr(delegate, "open_dialog"):
                usage_logger.log("task_note_open", via="ctrl_space")
                delegate.open_dialog(self.tree_view.indexFromItem(item, Columns.NOTES))
                return
        self.show_notes_and_capture()

    # ---- lifecycle ----
    def activate(self):
        """Called when this project's tab becomes visible."""
        self._activate_config()
        if self.inner_tabs.currentIndex() == 1:
            self.gantt_view.load_nodes(self.tree_view.root_nodes)

    def close_project(self):
        """Called before the tab is removed — release per-project state."""
        ConfigManager.unregister_project(self.project_id)

    def _activate_config(self):
        ConfigManager.set_active_project(self.project_id)

    # ---- snapshot / history ----
    def get_snapshot(self) -> Dict:
        """Serialize current state to a plain dict (safe to deepcopy)."""
        from utils.config_manager import ConfigManager as CM
        return {
            "id": self.project_id,
            "name": self.name,
            "metadata": CM.snapshot_project(self.project_id),
            "tasks": [n.to_dict() for n in self.tree_view.root_nodes],
            "notes": self.notes_panel.get_notes(),
            "notepad_html": self.notes_panel.get_html(),
            "is_vave": self.is_vave,
            "reviewed_through": self.reviewed_through,
            "note_tabs": self._note_tabs_data(),
            "resources": {key: list(values) for key, values in self.resource_lists.items()},
        }

    def load_snapshot(self, snap: Dict):
        """Restore state from a snapshot dict (reverse of get_snapshot)."""
        if not snap:
            return
        self.name = snap.get("name", self.name)
        self.reviewed_through = snap.get("reviewed_through")
        self.resource_lists.clear()
        self.resource_lists.update({key: list((snap.get("resources") or {}).get(key, []))
                                    for key in ("case", "room", "article")})
        md = snap.get("metadata", {}) or {}
        ConfigManager.register_project(self.project_id, md)
        self._activate_config()

        roots = [TaskNode.from_dict(d) for d in (snap.get("tasks") or [])]
        # Guard against the restore's own itemChanged storms flooding history.
        self._restoring = True
        try:
            self.tree_view.load_project(roots)
            if snap.get("notepad_html"):
                self.notes_panel.set_html(snap["notepad_html"])
            else:
                self.notes_panel.load_notes(snap.get("notes") or [])
            self._set_note_tabs_data(snap.get("note_tabs") or [])
            self.is_vave = bool(snap.get("is_vave", False))
            self.tree_view.set_vave_enabled(self.is_vave)
            self.timeline.refresh()  # keep the timeline's VAVE bar in sync
            self._update_vave_totals()
        finally:
            self._restoring = False

    def reset_history_baseline(self):
        """Drop all history and pin the current state as the new undo baseline.

        Called after programmatic loads (file open, test-data seed, load_snapshot
        from an external snapshot) so that Ctrl+Z can't rewind past the user's
        intended starting point.
        """
        self.history.clear()
        self._last_snapshot = self.get_snapshot()
        self._update_vave_totals()

    def _settle_pending_notes(self):
        """Flush any debounced note edits into history before undo/redo."""
        panels = [self.notes_panel]
        if hasattr(self, "custom_note_panels"):
            panels.extend(self.custom_note_panels)
        for panel in panels:
            if hasattr(panel, "flush"):
                panel.flush()
            elif hasattr(panel, "_debounce") and panel._debounce.isActive():
                panel._debounce.stop()
                panel.notes_changed.emit()

    def undo(self):
        self._settle_pending_notes()
        if not self.history.can_undo():
            return
        current = self.get_snapshot()
        prev = self.history.undo(current)
        if prev is not None:
            self.load_snapshot(prev)
            self._last_snapshot = prev
            self._update_vave_totals()
            self.project_changed.emit()

    def redo(self):
        self._settle_pending_notes()
        if not self.history.can_redo():
            return
        current = self.get_snapshot()
        nxt = self.history.redo(current)
        if nxt is not None:
            self.load_snapshot(nxt)
            self._last_snapshot = nxt
            self._update_vave_totals()
            self.project_changed.emit()

    def _on_tree_changed(self, *_):
        """Called every time a task is mutated through the tree widget.

        We push the *previous* snapshot (captured in _last_snapshot before
        the mutation landed) so that Ctrl+Z restores what the user saw
        before their edit, not the edit itself. Then we refresh
        _last_snapshot to the new post-edit state for next time.
        """
        if self._restoring:
            return
        if self._batch_depth:
            self._batch_changed = True
            return
        self.history.push(self._last_snapshot)
        self._last_snapshot = self.get_snapshot()
        self._update_vave_totals()
        self.project_changed.emit()
        if self.inner_tabs.currentIndex() == 1:
            self._focus_refresh_timer.start()

    def begin_batch(self):
        self._batch_depth += 1

    def end_batch(self):
        if self._batch_depth == 0:
            return
        self._batch_depth -= 1
        if self._batch_depth == 0 and self._batch_changed:
            self._batch_changed = False
            self.history.push(self._last_snapshot)
            self._last_snapshot = self.get_snapshot()
            self._update_vave_totals()
            self.project_changed.emit()
            if self.inner_tabs.currentIndex() == 1:
                self._focus_refresh_timer.start()

    def _on_note_consumed(self, raw: str):
        """A note was promoted into the grid. Drop it from the pad WITHOUT
        recording its own undo step — the task creation already pushed one, so
        a single Ctrl+Z undoes the whole drop (task removed, note restored)."""
        self._suppress_notes_history = True
        try:
            self.notes_panel.remove_line(raw)
        finally:
            self._suppress_notes_history = False

    def _on_notes_edited(self):
        if not self._restoring:
            self.project_changed.emit()

    def _on_notes_changed(self):
        """Pad edits (add / edit / delete a line) are undoable too — they go
        through the same history as task changes. Skipped while restoring a
        snapshot or mid-promotion (those are handled elsewhere)."""
        if self._restoring or self._suppress_notes_history:
            self.project_changed.emit()
            return
        self._on_tree_changed()

    def _make_tasks_from_notes(self, lines: list):
        """'To task' button in the notepad → create tasks from the given
        line(s). The note itself is left intact (the user keeps their notes)."""
        if not lines:
            return
        self.inner_tabs.setCurrentIndex(0)  # show the Tracker
        self.tree_view._promote_lines_to_tasks(lines)

    def log_event(self, text: str):
        """Append a dated line to this project's activity journal."""
        if self._restoring:
            return  # undo/redo replays are not new events
        from datetime import datetime
        self.journal.append({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "text": text,
        })
        if len(self.journal) > 2000:
            self.journal = self.journal[-2000:]

    def _update_vave_totals(self):
        if not getattr(self, "vave_totals_label", None):
            return
        if not self.is_vave:
            self.vave_totals_label.hide()
            return
        potential = 0.0
        realized = 0.0
        for node in self.tree_view.root_nodes:
            potential += node.vave_total_potential()
            realized += node.vave_total_realized()
        self.vave_totals_label.setText(
            f"Total Potential: ${potential:,.1f}     Total Realized: ${realized:,.1f}"
        )
        self.vave_totals_label.show()

    # ---- convenience used by MainWindow ----
    def to_persistable(self) -> Dict:
        """Shape matching utils.vpmt_io.save_projects()."""
        from utils.config_manager import ConfigManager as CM
        return {
            "id": self.project_id,
            "name": self.name,
            "metadata": {
                **CM.snapshot_project(self.project_id),
                "reviewed_through": self.reviewed_through,
            },
            "roots": list(self.tree_view.root_nodes),
            "journal": list(self.journal),
            "notes": self.notes_panel.get_notes(),
            "notepad_html": self.notes_panel.get_html(),
            "is_vave": self.is_vave,
            "reviewed_through": self.reviewed_through,
            "note_tabs": self._note_tabs_data(),
            "resources": {key: list(values) for key, values in self.resource_lists.items()},
        }
