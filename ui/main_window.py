"""
MainWindow — outer shell for multiple projects.

Each project lives in its own ProjectWidget, which itself holds a
Tracker + Visuals tab pair. MainWindow:
  - maintains the outer project-tabs QTabWidget (up to MAX_PROJECTS),
  - owns File / Options / Edit menus,
  - persists to and loads from a single .vpmt file (v2.0).
"""
import os
import hashlib
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QFileDialog, QMessageBox,
    QTabWidget, QInputDialog, QMenu, QLabel,
)
from PyQt6.QtGui import QAction, QKeySequence, QShortcut, QFont
from PyQt6.QtCore import Qt, QSettings, QStandardPaths, QTimer

from ui.project_widget import ProjectWidget
from models.task_node import TaskNode
from vpm_tracker_core import AppConstants
from utils import usage_logger
from utils.file_guard import ProjectFileGuard


MAX_PROJECTS = 5


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{AppConstants.APP_NAME} - {AppConstants.REVISION_LABEL}")
        self.resize(1200, 800)

        self.current_filepath = None
        self.unsaved_changes = False
        self.settings = QSettings("VPM", "VPMTracker")
        self.file_guard = ProjectFileGuard()
        self._session_recovery_path = None
        self._search_dialog = None
        self._loading_startup = True
        self.resource_definitions = []
        self.resource_conflict_resolutions = []
        self.resource_analysis = None
        self._resource_timer = QTimer(self)
        self._resource_timer.setSingleShot(True)
        self._resource_timer.setInterval(120)
        self._resource_timer.timeout.connect(self.recheck_resource_conflicts)

        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.layout = QVBoxLayout(self.central_widget)

        # Outer tab widget — one tab per project.
        self.project_tabs = QTabWidget()
        self.project_tabs.setTabsClosable(True)
        self.project_tabs.setMovable(True)
        self.project_tabs.tabCloseRequested.connect(self.close_project_tab)
        self.project_tabs.currentChanged.connect(self.on_project_tab_changed)
        self.project_tabs.tabBarDoubleClicked.connect(self.rename_project_tab)
        self.project_tabs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.project_tabs.customContextMenuRequested.connect(self.on_tab_context_menu)
        self.layout.addWidget(self.project_tabs)

        self.setup_menu()
        self._setup_global_shortcuts()
        self.statusBar().addPermanentWidget(QLabel(AppConstants.REVISION_LABEL))
        self._apply_saved_zoom()

        if not self._restore_startup_state():
            self._add_project_from_data("Project 1", {}, [])
            self._seed_test_data(self.project_tabs.widget(0))
        self._loading_startup = False
        self._update_overdue_action()
        self.recheck_resource_conflicts()

        # Autosave: every 3 minutes, if a file path exists and there are
        # unsaved changes, save silently. A crash costs minutes, not a day.
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self._autosave)
        self._autosave_timer.start(3 * 60 * 1000)

        # Repo-local telemetry snapshot: AppData stays canonical. A delayed
        # start recovers a previous crash that skipped closeEvent; the 15-minute
        # timer covers long-lived sessions without touching the 3-minute save path.
        # Both timers are children of this window and are stopped in closeEvent
        # so a quit within 2.5s cannot invoke a destroyed QObject.
        self._telemetry_export_timer = QTimer(self)
        self._telemetry_export_timer.timeout.connect(self._sync_repo_telemetry_background)
        self._telemetry_export_timer.start(15 * 60 * 1000)
        self._startup_telemetry_timer = QTimer(self)
        self._startup_telemetry_timer.setSingleShot(True)
        self._startup_telemetry_timer.timeout.connect(self._sync_repo_telemetry_background)
        self._startup_telemetry_timer.start(2500)

    def _setup_global_shortcuts(self):
        self._notepad_shortcut = QShortcut(QKeySequence("Ctrl+Space"), self)
        self._notepad_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._notepad_shortcut.activated.connect(lambda: self._open_notes_pad(via="ctrl_space"))

    def _autosave(self):
        if not self.unsaved_changes:
            return
        if self.current_filepath and self.file_guard.changed_on_disk():
            usage_logger.log("file_conflict", where="autosave")
            self.statusBar().showMessage(
                "Autosave paused for file: changed on disk; emergency recovery snapshot saved", 10000)
            try:
                from utils.vpmt_io import save_projects
                recovery_target = self._recovery_path()
                save_projects(
                    [p.to_persistable() for p in self.all_projects()],
                    recovery_target,
                    rotate_backups=False,
                    resource_definitions=self.resource_definitions,
                    conflict_resolutions=self.resource_conflict_resolutions,
                )
            except Exception as exc:
                usage_logger.log("recovery_save_failed", type=type(exc).__name__, manual=False)
                self.statusBar().showMessage(
                    "Autosave paused: changed on disk; recovery snapshot failed", 10000)
            return
        try:
            from utils.vpmt_io import save_projects
            target = self.current_filepath or self._recovery_path()
            save_projects(
                [p.to_persistable() for p in self.all_projects()],
                target,
                rotate_backups=False,
                resource_definitions=self.resource_definitions,
                conflict_resolutions=self.resource_conflict_resolutions,
            )
            if self.current_filepath:
                self.file_guard.refresh()
                usage_logger.log("file_save", manual=False)
                self.unsaved_changes = False
                self.update_title()
                self.statusBar().showMessage("Autosaved", 2000)
            else:
                self.statusBar().showMessage("Recovery copy saved", 2000)
        except Exception as exc:
            usage_logger.log("save_failed", type=type(exc).__name__, manual=False)
            self.statusBar().showMessage("Autosave failed; changes remain unsaved", 10000)

    def _recovery_path(self) -> str:
        folder = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.join(os.path.expanduser("~"), ".vpm_tracker")),
            "VPMTracker", "recovery")
        os.makedirs(folder, exist_ok=True)
        if not self._session_recovery_path:
            basis = self.current_filepath or "|".join(
                p.project_id for p in self.all_projects()) or "new"
            key = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]
            self._session_recovery_path = os.path.join(
                folder, f"recovery-{key}-{os.getpid()}.vpmt")
        return self._session_recovery_path

    def _recovery_candidates(self):
        folder = os.path.dirname(self._recovery_path())
        candidates = []
        for name in os.listdir(folder):
            if name.startswith("recovery-") and name.endswith(".vpmt"):
                candidates.append(os.path.join(folder, name))
        legacy = os.path.join(QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation), "recovery.vpmt")
        if os.path.exists(legacy):
            candidates.append(legacy)
        return sorted(set(candidates), key=os.path.getmtime, reverse=True)

    def _handled_recoveries(self) -> dict:
        raw = self.settings.value("recovery_handled_map", None)
        if isinstance(raw, dict):
            return dict(raw)
        return {}

    def _mark_recovery_handled(self, recovery_path: str, mtime: float):
        handled = self._handled_recoveries()
        key = os.path.basename(recovery_path)
        handled[key] = float(mtime)
        self.settings.setValue("recovery_handled_map", handled)

    def _restore_startup_state(self) -> bool:
        handled = self._handled_recoveries()
        for recovery in self._recovery_candidates():
            key = os.path.basename(recovery)
            last_handled_mtime = float(handled.get(key, 0) or 0)
            try:
                recovery_mtime = os.path.getmtime(recovery)
            except OSError:
                continue
            if recovery_mtime > last_handled_mtime:
                reply = QMessageBox.question(
                    self, "Restore Recovery File?",
                    "A recovery copy from an unsaved file was found.\n\n"
                    "Restore it now?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                usage_logger.log("autosave_recovery_offered")
                if reply == QMessageBox.StandardButton.Yes:
                    usage_logger.log("autosave_recovery_accepted")
                    if self._load_path(recovery, prompt_unsaved=False, is_recovery=True):
                        self.current_filepath = None
                        self.unsaved_changes = True
                        self._mark_recovery_handled(recovery, recovery_mtime)
                        self.update_title()
                        return True
                else:
                    self._mark_recovery_handled(recovery, recovery_mtime)

        for path in self._recent_files():
            if self._load_path(path, prompt_unsaved=False):
                return True
        return False

    # ---------------- project lifecycle ----------------
    def _add_project_from_data(self, name: str, metadata: dict, roots: list,
                               journal: list = None,
                               notes: list = None,
                               notepad_html: str = None,
                               is_vave: bool = False,
                               project_id: str = None,
                               reviewed_through: str = None,
                               note_tabs: list = None,
                               resources: dict = None) -> ProjectWidget:
        metadata = dict(metadata or {})
        if reviewed_through:
            metadata["reviewed_through"] = reviewed_through
        proj = ProjectWidget(name=name, metadata=metadata, roots=roots,
                             journal=journal, notes=notes,
                             notepad_html=notepad_html,
                             is_vave=is_vave, project_id=project_id,
                             note_tabs=note_tabs, resources=resources)
        proj.project_changed.connect(self.on_data_changed)
        proj.tree_view.resource_assignment_committed.connect(
            lambda node, old, project=proj:
            self._resource_assignment_changed(project, node, old))
        proj.tree_view.identity_story_requested.connect(self.open_identity_story)
        index = self.project_tabs.addTab(proj, self._project_tab_label(proj))
        self.project_tabs.setCurrentIndex(index)
        proj.activate()
        if hasattr(proj.tree_view, "apply_zoom"):
            proj.tree_view.apply_zoom(getattr(self, "_zoom_factor", 1.0))
        return proj

    def _project_tab_label(self, proj: ProjectWidget) -> str:
        suffix = " [VAVE]" if getattr(proj, "is_vave", False) else ""
        return f"{proj.name}{suffix}"

    def _refresh_project_tab_labels(self):
        for i in range(self.project_tabs.count()):
            proj = self.project_tabs.widget(i)
            if isinstance(proj, ProjectWidget):
                self.project_tabs.setTabText(i, self._project_tab_label(proj))

    def _seed_test_data(self, proj: ProjectWidget):
        """Give a brand-new blank project something to look at."""
        if proj.tree_view.root_nodes:
            return
        root = TaskNode("Project Alpha")
        phase1 = TaskNode("Phase 1", parent=root)
        root.add_child(phase1)
        task1 = TaskNode("Task 1.1", parent=phase1)
        task1.status = "Completed"
        phase1.add_child(task1)
        phase1.add_child(TaskNode("Task 1.2", parent=phase1))
        proj.tree_view.load_project([root])
        # Re-seed history so the test data is the baseline.
        proj.reset_history_baseline()

    def add_new_project(self):
        if self.project_tabs.count() >= MAX_PROJECTS:
            QMessageBox.information(
                self, "Project Limit",
                f"Maximum of {MAX_PROJECTS} projects per file."
            )
            return
        default_name = f"Project {self.project_tabs.count() + 1}"
        name, ok = QInputDialog.getText(self, "New Project", "Project name:", text=default_name)
        if not ok or not name.strip():
            return
        self._add_project_from_data(name.strip(), {}, [])
        self.on_data_changed()

    def close_project_tab(self, index: int):
        if self.project_tabs.count() <= 1:
            QMessageBox.information(
                self, "Cannot Close",
                "A file must contain at least one project."
            )
            return
        proj = self.project_tabs.widget(index)
        if isinstance(proj, ProjectWidget):
            reply = QMessageBox.question(
                self, "Close Project",
                f"Remove project '{proj.name}' from this file?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            proj.close_project()
        self.project_tabs.removeTab(index)
        self.on_data_changed()

    def rename_project_tab(self, index: int):
        if index < 0:
            return
        proj = self.project_tabs.widget(index)
        if not isinstance(proj, ProjectWidget):
            return
        new_name, ok = QInputDialog.getText(self, "Rename Project", "Project name:", text=proj.name)
        if ok and new_name.strip():
            proj.name = new_name.strip()
            self.project_tabs.setTabText(index, self._project_tab_label(proj))
            self.on_data_changed()

    def on_tab_context_menu(self, pos):
        index = self.project_tabs.tabBar().tabAt(pos)
        if index < 0:
            return
        menu = QMenu(self)
        menu.addAction("Rename…", lambda: self.rename_project_tab(index))
        menu.addAction("Close Project", lambda: self.close_project_tab(index))
        menu.addSeparator()
        menu.addAction("Add New Project…", self.add_new_project)
        menu.exec(self.project_tabs.tabBar().mapToGlobal(pos))

    def on_project_tab_changed(self, index: int):
        proj = self.project_tabs.widget(index)
        if isinstance(proj, ProjectWidget):
            proj.activate()
            usage_logger.set_project(proj.project_id)
        else:
            usage_logger.set_project(None)
        self._update_vave_action_state()
        self._update_overdue_action()
        self._announce_data_health()
        usage_logger.log("tab_switch", to=f"project:{index}")

    def active_project(self) -> ProjectWidget:
        w = self.project_tabs.currentWidget()
        return w if isinstance(w, ProjectWidget) else None

    def all_projects(self):
        return [
            self.project_tabs.widget(i)
            for i in range(self.project_tabs.count())
            if isinstance(self.project_tabs.widget(i), ProjectWidget)
        ]

    def _resource_projects(self):
        from utils.config_manager import ConfigManager
        return [{
            "id": project.project_id,
            "name": project.name,
            "roots": project.tree_view.root_nodes,
            "metadata": ConfigManager.snapshot_project(project.project_id),
        } for project in self.all_projects()]

    def _metadata_resource_definitions(self):
        """Definitions for every configured metadata option, assigned or not."""
        from utils.resource_allocation import ResourceDefinition
        from utils.template_catalog import load_templates
        definitions = []
        for item in load_templates():
            kind = str(item.get("kind") or "resource")
            if (item.get("record_type") == "header" or
                    kind.casefold() in {"campaign", "header"}):
                continue
            label = str(item.get("name") or "").strip()
            if not label:
                continue
            definitions.append(ResourceDefinition(
                str(item.get("id")), kind, label,
                max(1, int(item.get("capacity", 1) or 1)),
                bool(item.get("flag_overlaps", False)),
                bool(item.get("active", True)),
                str(item.get("home_location") or ""),
                str(item.get("notes") or ""),
                str(item.get("policy") or "warning")).to_dict())
        return definitions

    def recheck_resource_conflicts(self, announce=False):
        """Rebuild all assignments/conflicts for every loaded project tab."""
        from utils.resource_allocation import (
            ResourceDefinition, analyze_projects, definition_map,
            derive_definitions)
        projects = self._resource_projects()
        combined = definition_map(self.resource_definitions)
        metadata = definition_map([
            ResourceDefinition.from_dict(value)
            for value in self._metadata_resource_definitions()])
        for resource_id, value in metadata.items():
            combined.setdefault(resource_id, value)

        # Every identity remains auditable, but warnings are opt-in and the
        # checkbox beside the Metadata option is the single visible authority.
        # Old documents generated conflict_enabled=True automatically, so those
        # legacy values must not silently override an unchecked Metadata row.
        derived = definition_map(derive_definitions(
            projects, list(combined.values())))
        authoritative = {}
        for resource_id, current in derived.items():
            configured = metadata.get(resource_id)
            saved = combined.get(resource_id)
            if configured is not None:
                conflict_enabled = configured.conflict_enabled
                item_type = configured.type
                item_label = configured.label
            elif saved is not None:
                conflict_enabled = saved.conflict_enabled
                item_type = saved.type
                item_label = saved.label
            else:
                conflict_enabled = current.conflict_enabled
                item_type = current.type
                item_label = current.label
            authoritative[resource_id] = ResourceDefinition(
                resource_id,
                item_type,
                item_label,
                current.capacity,
                conflict_enabled,
                current.active,
                current.home_location,
                current.notes,
                current.policy)
        result = analyze_projects(
            projects, list(authoritative.values()),
            self.resource_conflict_resolutions)
        self.resource_analysis = result
        self.resource_definitions = [value.to_dict() for value in result.definitions]
        for project in self.all_projects():
            project.tree_view.resource_definitions = {
                value.id: value for value in result.definitions}
            project.tree_view.refresh_entire_tree()
            project.timeline.refresh()
        action = getattr(self, "resource_usage_action", None)
        if action is not None:
            count = len(result.unresolved)
            action.setText("Resource Usage & Conflicts…" +
                           (f" ({count})" if count else ""))
            if getattr(self, "resources_menu", None) is not None:
                self.resources_menu.setTitle(
                    "Resources" + (f" ({count})" if count else ""))
        if announce:
            self.statusBar().showMessage(
                f"Checked {len(result.assignments)} resource assignments · "
                f"{len(result.unresolved)} unresolved conflicts", 7000)
        usage_logger.log("resource_analysis", assignments=len(result.assignments),
                         conflicts=len(result.conflicts),
                         unresolved=len(result.unresolved))
        return result

    def _resource_assignment_changed(self, project, node, previous):
        result = self.recheck_resource_conflicts()
        conflicts = [c for c in result.conflicts_for_task(node.id)
                     if c.resolution_state != "accepted"]
        assigned = result.assignments_for_task(node.id)
        if not conflicts:
            if assigned:
                self.statusBar().showMessage(
                    f"{assigned[-1].resource_label} assigned · available for "
                    "the scheduled period", 4000)
            return
        self._show_assignment_conflict(project, node, previous, conflicts)

    def _show_assignment_conflict(self, project, node, previous, conflicts):
        from PyQt6.QtWidgets import QMessageBox
        text = self._resource_conflict_warning_text(conflicts)
        assignment_by_id = {a.id: a for a in self.resource_analysis.assignments}
        box = QMessageBox(self)
        box.setWindowTitle("Resource Conflict")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(text)
        keep = box.addButton("Keep overlap…", QMessageBox.ButtonRole.AcceptRole)
        jump = box.addButton("Jump to conflict", QMessageBox.ButtonRole.ActionRole)
        another = box.addButton("Use another resource", QMessageBox.ButtonRole.ActionRole)
        move = box.addButton("Move to next available…", QMessageBox.ButtonRole.ActionRole)
        cancel = box.addButton("Cancel assignment", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()

        # All button branches below operate on the same immutable analysis.
        if clicked is keep:
            reason, ok = QInputDialog.getText(
                self, "Accept Resource Overlap",
                "Explanation required for keeping this overlap:")
            if ok and reason.strip():
                from utils.resource_allocation import accept_conflict
                replacements = {c.id: accept_conflict(c, reason) for c in conflicts}
                self.resource_conflict_resolutions = [
                    value for value in self.resource_conflict_resolutions
                    if value.get("conflict_id") not in replacements]
                self.resource_conflict_resolutions.extend(replacements.values())
                self.unsaved_changes = True
                self.recheck_resource_conflicts()
                usage_logger.log("resource_conflict_action", action="accepted",
                                 count=len(conflicts))
            else:
                QMessageBox.information(
                    self, "Explanation Required",
                    "The overlap remains unresolved until an explanation is saved.")
        elif clicked is jump:
            current = node.id
            target = next((a for c in conflicts for a_id in c.assignment_ids
                           for a in [assignment_by_id[a_id]]
                           if a.task_id != current), None)
            if target:
                self._jump_to_resource_assignment(target)
            usage_logger.log("resource_conflict_action", action="jump")
        elif clicked in (another, cancel):
            self._restore_resource_assignment(project, node, previous,
                                              choose_another=clicked is another)
            usage_logger.log("resource_conflict_action",
                             action="choose_another" if clicked is another else "cancel")
        elif clicked is move:
            self._move_resource_to_next_available(project, node, conflicts[0])

    def _resource_conflict_warning_text(self, conflicts):
        assignment_by_id = {a.id: a for a in self.resource_analysis.assignments}
        lines = []
        for conflict in conflicts:
            lines.append(
                f"{conflict.resource_label} is already in use for "
                f"{conflict.overlap_workdays} overlapping workday(s):")
            for assignment_id in conflict.assignment_ids:
                assignment = assignment_by_id[assignment_id]
                lines.append(
                    f"• {assignment.task_path} · {assignment.location_label} · "
                    f"{assignment.start_date} through {assignment.end_date}")
        return "\n".join(lines)

    def _jump_to_resource_assignment(self, assignment):
        self._jump_to_project_task(assignment.project_id, assignment.task_id)

    def _jump_to_project_task(self, project_id, task_id):
        for index, project in enumerate(self.all_projects()):
            if project.project_id == project_id:
                self.project_tabs.setCurrentIndex(index)
                project.inner_tabs.setCurrentIndex(0)
                project.tree_view.jump_to_node_id(task_id)
                return

    def _identity_flag_overlaps(self, identity_id):
        """Metadata checkbox is the authority for Story overlap drawing."""
        from utils.identity_story import identity_flag_overlaps
        from utils.template_catalog import load_templates
        return identity_flag_overlaps(
            identity_id, load_templates(), self.resource_definitions)

    def open_identity_story(self, identity_id, label, kind):
        """Open the reusable Story view from an identity chip."""
        from ui.identity_story_panel import IdentityStoryDialog
        from utils.identity_story import build_identity_story
        story = build_identity_story(
            self._resource_projects(), identity_id,
            identity_label=label, identity_kind=kind,
            include_overlaps=self._identity_flag_overlaps(identity_id))
        dialog = IdentityStoryDialog(story, self)
        dialog.jumpRequested.connect(self._jump_to_project_task)
        usage_logger.timed_exec(dialog, "identity_story")

    def _restore_resource_assignment(self, project, node, previous,
                                     choose_another=False):
        old_tokens = [dict(value) for value in previous.get("tokens", [])]
        node.task_tokens = old_tokens
        node.resources = {}
        for token in old_tokens:
            kind = (token.get("kind") or "").casefold()
            if kind in {"case", "room", "article", "cassette", "test article"}:
                key = "article" if kind in {"article", "cassette", "test article"} else kind
                # Legacy dictionary keeps one value; task_tokens retains all.
                node.resources[key] = token.get("label", "")
        node.name = previous.get("name") or (
            old_tokens[0].get("label", "") if old_tokens else node.name)
        project.tree_view.refresh_entire_tree()
        project.tree_view.item_changed_signal.emit(node)
        self.recheck_resource_conflicts()
        if choose_another:
            item = project.tree_view._find_item_by_id(node.id)
            if item:
                project.tree_view.setCurrentItem(item, 0)
                project.tree_view._open_lookup_with_editor = True
                QTimer.singleShot(0, lambda: project.tree_view.editItem(item, 0))

    def _move_resource_to_next_available(self, project, node, conflict):
        from ui.dialogs import ImpactReviewDialog
        from utils.resource_allocation import next_available_block
        from utils.workday_calculator import WorkdayCalculator

        subtree_nodes = [node]
        def collect_nodes(n):
            for c in n.children:
                subtree_nodes.append(c)
                collect_nodes(c)
        collect_nodes(node)
        subtree_ids = {n.id for n in subtree_nodes}

        # Check F13: are any descendant tasks locked by external dependencies?
        for n in subtree_nodes:
            if n == node:
                continue
            for rule in (n.start_rule, n.end_rule):
                mode = rule.get("mode")
                if mode in ("same_as", "continue_after"):
                    target_id = rule.get("task_id")
                    if target_id and target_id not in subtree_ids:
                        target_item = project.tree_view._find_item_by_id(target_id)
                        target_name = target_item.node.name if target_item else "another task"
                        QMessageBox.warning(
                            self, "External Dependency Prevents Move",
                            f"Cannot move '{node.name}' because child task '{n.name}' depends on external task '{target_name}'.\n"
                            "Remove or adjust this dependency before moving."
                        )
                        return

        subtree_assignments = [a for a in self.resource_analysis.assignments
                               if a.task_id in subtree_ids and a.project_id == project.project_id]
        assignment = next((a for a in subtree_assignments
                           if a.task_id == node.id and
                           a.resource_id == conflict.resource_id), None)
        if not assignment:
            return
        metadata = project.to_persistable().get("metadata", {})
        preview = next_available_block(
            subtree_assignments, self.resource_analysis.assignments,
            self.resource_analysis.definitions,
            metadata.get("holidays", []), metadata.get("exclude_weekends", True))
        if not preview:
            QMessageBox.information(self, "No Availability",
                                    "No available block was found in the search range.")
            return
        new_start, new_end = preview
        rule_note = (f" This replaces the current {node.start_rule.get('mode')} "
                     "start rule." if node.start_rule.get("mode") != "automatic" else "")
        if QMessageBox.question(
                self, "Preview Next Available Move",
                f"Move '{node.name}' to {new_start} through {new_end}?{rule_note}\n\n"
                "No dates will change until you confirm.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        impact = project.tree_view._simulate_impact(
            node, new_start=new_start, new_end=new_end)
        if impact is not None:
            dialog = ImpactReviewDialog(impact, self)
            if not usage_logger.timed_exec(dialog, "resource_move_impact"):
                return

        project.begin_batch()
        try:
            if node.children:
                old_start = node.start_date
                if old_start and new_start != old_start:
                    def shift_descendant(child):
                        if child.start_date:
                            dur = WorkdayCalculator.calculate_duration(child.start_date, child.end_date)
                            k = max(0, WorkdayCalculator.calculate_duration(old_start, child.start_date) - 1) if child.start_date >= old_start else 0
                            c_start = WorkdayCalculator.add_workdays(new_start, k + 1)
                            c_end = WorkdayCalculator.add_workdays(c_start, dur)
                            if child.start_rule.get("mode") == "fixed":
                                child.set_start_rule({"mode": "fixed", "date": c_start})
                            if child.end_rule.get("mode") == "fixed":
                                child.set_end_rule({"mode": "fixed", "date": c_end})
                            elif child.end_rule.get("mode") == "duration":
                                child.set_end_rule({"mode": "duration", "days": dur})
                            child.start_date = c_start
                            child.end_date = c_end
                        for subchild in child.children:
                            shift_descendant(subchild)

                    for child in node.children:
                        shift_descendant(child)

            node.set_start_rule({"mode": "fixed", "date": new_start})
            if not node.children:
                node.set_end_rule({"mode": "duration", "days": assignment.workdays})
            project.tree_view.commit_structure_change(node)
        finally:
            project.end_batch()
        usage_logger.log("resource_conflict_action", action="move_next")

    def show_resource_usage(self, focus_resource_id=None):
        from ui.resource_usage_dialog import ResourceUsageDialog
        result = self.recheck_resource_conflicts()
        dialog = ResourceUsageDialog(result, focus_resource_id, self)

        def jump(project_id, task_id):
            assignment = next((a for a in result.assignments
                               if a.project_id == project_id and
                               a.task_id == task_id), None)
            if assignment:
                self._jump_to_resource_assignment(assignment)

        dialog.jumpRequested.connect(jump)
        usage_logger.timed_exec(dialog, "resource_usage")

    def show_selected_resource(self):
        project = self.active_project()
        item = project.tree_view.currentItem() if project else None
        node = getattr(item, "node", None)
        tokens = node.resource_tokens() if node else []
        if not tokens:
            QMessageBox.information(
                self, "No Resource Selected",
                "Select a task with a Case, Room, Lab Testing, article, or "
                "custom metadata resource first.")
            return
        selected = tokens[0]
        if len(tokens) > 1:
            labels = [value.get("label", "Resource") for value in tokens]
            label, ok = QInputDialog.getItem(
                self, "Show Selected Resource", "Resource:", labels, 0, False)
            if not ok:
                return
            selected = tokens[labels.index(label)]
        self.show_resource_usage(selected.get("id"))

    def open_resource_settings(self):
        from ui.resource_usage_dialog import ResourceSettingsDialog
        result = self.recheck_resource_conflicts()
        dialog = ResourceSettingsDialog(result.definitions, self)
        while usage_logger.timed_exec(dialog, "resource_settings"):
            try:
                definitions = dialog.result_definitions()
            except ValueError as exc:
                QMessageBox.warning(self, "Invalid Resource Settings", str(exc))
                continue
            self.resource_definitions = [value.to_dict() for value in definitions]
            self._sync_overlap_flags_to_metadata_catalog(definitions)
            self.unsaved_changes = True
            self.update_title()
            self.recheck_resource_conflicts(announce=True)
            usage_logger.log("resource_settings_save", count=len(definitions))
            break

    @staticmethod
    def _sync_overlap_flags_to_metadata_catalog(definitions):
        """Keep the legacy settings screen consistent with Metadata checkboxes."""
        from utils.template_catalog import load_templates, save_templates
        flags = {value.id: bool(value.conflict_enabled) for value in definitions}
        items = load_templates()
        changed = False
        for item in items:
            identity_id = str(item.get("id") or "")
            if (item.get("record_type") == "header" or
                    identity_id not in flags):
                continue
            enabled = flags[identity_id]
            if bool(item.get("flag_overlaps", False)) != enabled:
                item["flag_overlaps"] = enabled
                changed = True
        if changed:
            save_templates(items)

    # ---------------- menu ----------------
    def _wire_action(self, action: QAction, slot, name: str = None):
        label = name or action.text()
        action.triggered.connect(lambda *args: (usage_logger.log("menu_action", name=label), slot()))

    def setup_menu(self):
        menu = self.menuBar()
        file_menu = menu.addMenu("File")

        new_action = QAction("New Project Tab", self)
        new_action.setShortcut("Ctrl+T")
        self._wire_action(new_action, self.add_new_project)
        file_menu.addAction(new_action)

        file_menu.addSeparator()

        load_action = QAction("Load…", self)
        load_action.setShortcut("Ctrl+O")
        self._wire_action(load_action, self.load_project_file)
        file_menu.addAction(load_action)

        self.recent_menu = file_menu.addMenu("Open Recent")
        self.recent_menu.aboutToShow.connect(self._populate_recent_menu)

        save_action = QAction("Save", self)
        save_action.setShortcut("Ctrl+S")
        self._wire_action(save_action, self.save_project_file)
        file_menu.addAction(save_action)

        save_as_action = QAction("Save As…", self)
        save_as_action.setShortcut("Ctrl+Shift+S")
        self._wire_action(save_as_action, self.save_project_file_as)
        file_menu.addAction(save_as_action)

        file_menu.addSeparator()

        export_action = QAction("Export to Excel…", self)
        self._wire_action(export_action, self.export_to_excel_prompt)
        file_menu.addAction(export_action)

        file_menu.addSeparator()

        exit_action = QAction("Exit", self)
        self._wire_action(exit_action, self.close)
        file_menu.addAction(exit_action)

        # Edit menu — undo/redo forwarded to the active project.
        edit_menu = menu.addMenu("Edit")
        undo_action = QAction("Undo", self)
        undo_action.setShortcut("Ctrl+Z")
        undo_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self._wire_action(undo_action, self._undo_active)
        edit_menu.addAction(undo_action)

        redo_action = QAction("Redo", self)
        redo_action.setShortcut("Ctrl+Y")
        redo_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self._wire_action(redo_action, self._redo_active)
        edit_menu.addAction(redo_action)

        edit_menu.addSeparator()
        refresh_action = QAction("Refresh All", self)
        refresh_action.setShortcut("F5")
        self._wire_action(refresh_action, self._refresh_all)
        edit_menu.addAction(refresh_action)

        edit_menu.addSeparator()
        delay_summary_action = QAction("View Delay Summary…", self)
        self._wire_action(delay_summary_action, self._show_delay_summary)
        edit_menu.addAction(delay_summary_action)

        self.catch_up_action = QAction("Review Project...", self)
        self._wire_action(self.catch_up_action, self._show_catch_up, "Review Project")
        edit_menu.addAction(self.catch_up_action)

        health_action = QAction("Project Data Health...", self)
        self._wire_action(health_action, self._show_data_health, "Project Data Health")
        edit_menu.addAction(health_action)

        waiting_action = QAction("View Waiting-On List...", self)
        self._wire_action(waiting_action, self._show_waiting_list)
        edit_menu.addAction(waiting_action)

        journal_action = QAction("View Activity Journal…", self)
        self._wire_action(journal_action, self._show_journal)
        edit_menu.addAction(journal_action)

        edit_menu.addSeparator()
        search_action = QAction("Search Everything…", self)
        search_action.setShortcut("Ctrl+F")
        self._wire_action(search_action, self._show_search)
        edit_menu.addAction(search_action)

        capture_action = QAction("Open Notes Pad…", self)
        capture_action.setShortcuts(["Ctrl+Shift+N", "Ctrl+Shift+Space"])
        capture_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self._wire_action(capture_action, lambda: self._open_notes_pad(via="menu"), "Open Notes Pad")
        edit_menu.addAction(capture_action)

        edit_menu.addSeparator()
        zoom_in_action = QAction("Zoom In", self)
        self._wire_action(zoom_in_action, lambda: self._adjust_zoom(0.1))
        edit_menu.addAction(zoom_in_action)

        zoom_out_action = QAction("Zoom Out", self)
        self._wire_action(zoom_out_action, lambda: self._adjust_zoom(-0.1))
        edit_menu.addAction(zoom_out_action)

        zoom_reset_action = QAction("Reset Zoom", self)
        self._wire_action(zoom_reset_action, lambda: self._set_zoom(1.0))
        edit_menu.addAction(zoom_reset_action)

        edit_menu.addSeparator()
        set_bl_action = QAction("Set Baseline", self)
        self._wire_action(set_bl_action, self._set_baseline)
        edit_menu.addAction(set_bl_action)

        clear_bl_action = QAction("Clear All Baselines", self)
        self._wire_action(clear_bl_action, self._clear_baseline)
        edit_menu.addAction(clear_bl_action)

        # Options menu (operates on the active project's config).
        options_menu = menu.addMenu("Options")
        metadata_action = QAction("Metadata Lists…", self)
        metadata_action.setToolTip(
            "Edit project phases, rooms, timelines, and custom lists")
        self._wire_action(metadata_action, self.open_template_manager)
        menu.addAction(metadata_action)
        resources_menu = menu.addMenu("Resources")
        self.resources_menu = resources_menu
        self.resource_usage_action = QAction("Resource Usage & Conflicts…", self)
        self._wire_action(self.resource_usage_action, self.show_resource_usage)
        resources_menu.addAction(self.resource_usage_action)
        selected_resource_action = QAction("Show Selected Resource…", self)
        self._wire_action(selected_resource_action, self.show_selected_resource)
        resources_menu.addAction(selected_resource_action)
        recheck_resource_action = QAction("Recheck Resource Conflicts", self)
        self._wire_action(recheck_resource_action,
                          lambda: self.recheck_resource_conflicts(announce=True))
        resources_menu.addAction(recheck_resource_action)
        resources_menu.addSeparator()
        settings_resource_action = QAction("Resource Settings…", self)
        self._wire_action(settings_resource_action, self.open_resource_settings)
        resources_menu.addAction(settings_resource_action)
        font_bigger_action = QAction("Font Bigger", self)
        font_bigger_action.setShortcut("Ctrl+=")
        font_bigger_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self._wire_action(font_bigger_action, lambda: self._adjust_zoom(0.1))
        options_menu.addAction(font_bigger_action)

        font_smaller_action = QAction("Font Smaller", self)
        font_smaller_action.setShortcut("Ctrl+-")
        font_smaller_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self._wire_action(font_smaller_action, lambda: self._adjust_zoom(-0.1))
        options_menu.addAction(font_smaller_action)

        font_reset_action = QAction("Reset Font Size", self)
        font_reset_action.setShortcut("Ctrl+0")
        font_reset_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self._wire_action(font_reset_action, lambda: self._set_zoom(1.0))
        options_menu.addAction(font_reset_action)

        options_menu.addSeparator()

        manage_waiting_action = QAction("Manage Owners / Waiting People…", self)
        self._wire_action(manage_waiting_action, self.open_waiting_people_manager)
        options_menu.addAction(manage_waiting_action)

        calendar_action = QAction("Calendar Settings…", self)
        self._wire_action(calendar_action, self.open_calendar_settings)
        options_menu.addAction(calendar_action)

        templates_action = QAction("Metadata Lists…", self)
        self._wire_action(templates_action, self.open_template_manager)
        options_menu.addAction(templates_action)

        options_menu.addSeparator()
        self.vave_project_action = QAction("VAVE Project", self)
        self.vave_project_action.setCheckable(True)
        self.vave_project_action.toggled.connect(self._toggle_active_project_vave)
        options_menu.addAction(self.vave_project_action)
        self._update_vave_action_state()

        options_menu.addSeparator()

        usage_action = QAction("Record usage statistics", self)
        usage_action.setCheckable(True)
        usage_action.setChecked(usage_logger.is_enabled())
        usage_action.setToolTip(
            "Keeps a local log of which app features you use in AppData, and "
            "copies a sanitized snapshot into the repo telemetry/ folder for review."
        )
        usage_action.toggled.connect(usage_logger.set_enabled)
        options_menu.addAction(usage_action)

        diagnostics_action = QAction("Telemetry Diagnostics…", self)
        self._wire_action(diagnostics_action, self._show_telemetry_diagnostics)
        options_menu.addAction(diagnostics_action)

        export_action = QAction("Export usage logs to repo folder", self)
        export_action.setToolTip(
            "Copy the latest sanitized AppData telemetry into telemetry/ "
            "next to the app (does not replace the AppData store)."
        )
        self._wire_action(export_action, self._export_repo_telemetry)
        options_menu.addAction(export_action)

    def _sync_repo_telemetry_background(self):
        usage_logger.sync_repo_telemetry(background=True)

    def _cancel_repo_telemetry_timers(self):
        for name in ("_startup_telemetry_timer", "_telemetry_export_timer"):
            timer = getattr(self, name, None)
            if timer is not None:
                timer.stop()

    def _export_repo_telemetry(self):
        dest = usage_logger.sync_repo_telemetry(background=False)
        if dest:
            QMessageBox.information(
                self, "Usage logs exported",
                "A sanitized snapshot was written to:\n"
                f"{dest}\n\n"
                "The live store remains in AppData.")
        else:
            QMessageBox.information(
                self, "Usage logs not exported",
                "Usage logging is off, or the repo folder could not be written.")

    def _show_telemetry_diagnostics(self):
        info = usage_logger.usage.diagnostics()
        QMessageBox.information(
            self, "Telemetry Diagnostics",
            "\n".join([
                f"Logging: {'On' if info['enabled'] else 'Off'}",
                f"Session: {info['environment']}",
                f"Last event: {info['last_event']} ({info['last_event_name']})",
                f"Last detected gap: {info['last_gap_days']} day(s)",
                f"Legacy logs imported: {'Yes' if info['legacy_imported'] else 'No'}",
                f"Storage (canonical): {info['storage']}",
                f"Repo snapshot: {info.get('repo_export', '')}",
            ]),
        )

    def _update_vave_action_state(self):
        action = getattr(self, "vave_project_action", None)
        if action is None:
            return
        proj = self.active_project()
        action.blockSignals(True)
        action.setEnabled(proj is not None)
        action.setChecked(bool(getattr(proj, "is_vave", False)) if proj else False)
        action.blockSignals(False)

    def _toggle_active_project_vave(self, checked: bool):
        proj = self.active_project()
        if not proj:
            return
        proj.set_vave_enabled(checked)
        self._refresh_project_tab_labels()
        self.on_data_changed()

    def _undo_active(self):
        proj = self.active_project()
        if proj:
            proj.undo()
            usage_logger.log("undo")

    def _redo_active(self):
        proj = self.active_project()
        if proj:
            proj.redo()
            usage_logger.log("redo")

    def _full_path(self, node):
        parts = []
        n = node
        while n:
            parts.append(n.name)
            n = n.parent
        return " > ".join(reversed(parts))

    def _overdue_leaves(self, proj=None):
        from datetime import datetime
        proj = proj or self.active_project()
        if not proj:
            return []
        today = datetime.now().date()
        overdue = []
        for node in proj.tree_view.get_all_nodes_flat():
            if node.children or node.status == "Completed" or not node.end_date:
                continue
            try:
                end = datetime.strptime(node.end_date, "%Y-%m-%d").date()
            except ValueError:
                continue
            if end < today:
                overdue.append((node, end, (today - end).days))
        overdue.sort(key=lambda row: row[1])
        return overdue

    def _update_overdue_action(self):
        action = getattr(self, "catch_up_action", None)
        if not action:
            return
        count = len(self._overdue_leaves())
        action.setText(f"Review Project... ({count} overdue)" if count else "Review Project...")

    def _data_health_issues(self, proj=None):
        """Return warnings only; never alter project data."""
        from datetime import datetime
        proj = proj or self.active_project()
        if not proj:
            return []
        today = datetime.now().date()
        issues = []
        reviewed = getattr(proj, "reviewed_through", None)
        try:
            reviewed_date = datetime.strptime(reviewed, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            reviewed_date = None
        if reviewed_date is None:
            issues.append(("Project has never been reviewed", None))
        elif (today - reviewed_date).days > 7:
            issues.append((f"Project review is {(today - reviewed_date).days} days old", None))
        all_nodes = proj.tree_view.get_all_nodes_flat()
        all_task_ids = {n.id for n in all_nodes}
        for node in all_nodes:
            # F24: validate rule targets against current project tasks
            start_target = node.start_rule.get("task_id")
            if node.start_rule.get("mode") in ("same_as", "continue_after") and start_target:
                if start_target not in all_task_ids:
                    issues.append(("Task start depends on a missing task", node))
            end_target = node.end_rule.get("task_id")
            if node.end_rule.get("mode") == "same_as" and end_target:
                if end_target not in all_task_ids:
                    issues.append(("Task end depends on a missing task", node))

            if node.children:
                continue
            try:
                start = datetime.strptime(node.start_date, "%Y-%m-%d").date() if node.start_date else None
                end = datetime.strptime(node.end_date, "%Y-%m-%d").date() if node.end_date else None
            except ValueError:
                start = end = None
            if node.status == "In Progress" and end and end < today:
                issues.append(("Overdue task is still In Progress", node))
            if node.status == "Not Started" and start and start < today:
                issues.append(("Task is Not Started after its start date", node))
            if node.status == "Completed" and end and end > today:
                issues.append(("Completed task has a future end date", node))
            if any(not str(rev.get("reason", "")).strip() for rev in node.revisions):
                issues.append(("Delay history has a missing reason", node))
            for detail in getattr(node, "resource_conflict_details", []):
                issues.append((
                    f"Resource conflict ({detail.get('state', 'unresolved')}): "
                    f"{detail.get('resource_label', '')}", node))
            # F23: only demand savings disposition when savings were actually proposed
            if (getattr(proj, "is_vave", False) and node.status == "Completed"
                    and getattr(node, "vave_potential", None) is not None
                    and getattr(node, "vave_realized", None) is None
                    and not getattr(node, "savings_disposition", "")):
                issues.append(("Completed VAVE work has no savings disposition", node))
        return issues

    def _announce_data_health(self):
        issues = self._data_health_issues()
        stale = next((text for text, node in issues if node is None), None)
        if stale:
            self.statusBar().showMessage(f"Data may be stale: {stale}. Use Review Project.", 8000)

    def _show_data_health(self):
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QListWidget, QListWidgetItem, QDialogButtonBox
        proj = self.active_project()
        if not proj:
            return
        issues = self._data_health_issues(proj)
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Project Data Health — {proj.name}")
        dialog.resize(700, 440)
        layout = QVBoxLayout(dialog)
        listing = QListWidget()
        if not issues:
            listing.addItem("No staleness or basic contradictions found.")
        for text, node in issues:
            item = QListWidgetItem(text if node is None else f"{text}: {self._full_path(node)}")
            item.setData(Qt.ItemDataRole.UserRole, node.id if node else "")
            listing.addItem(item)
        def jump(item):
            node_id = item.data(Qt.ItemDataRole.UserRole)
            if node_id:
                dialog.accept()
                proj.inner_tabs.setCurrentIndex(0)
                proj.tree_view.jump_to_node_id(node_id)
        listing.itemActivated.connect(jump)
        layout.addWidget(listing)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        usage_logger.timed_exec(dialog, "data_health")

    def _show_catch_up(self):
        """Review open leaf tasks without confusing replanning with delay."""
        from datetime import datetime
        from PyQt6.QtWidgets import (
            QDialog, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
            QComboBox, QDateEdit, QLineEdit, QDialogButtonBox, QHeaderView,
        )
        from PyQt6.QtCore import QDate

        proj = self.active_project()
        if not proj:
            return
        candidates = [n for n in proj.tree_view.get_all_nodes_flat()
                      if not n.children and n.status != "Completed"]
        if not candidates:
            QMessageBox.information(self, "Review Project", "No open leaf tasks to review.")
            return
        usage_logger.log("review_open", count=len(candidates))
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Review Project — {proj.name}")
        dialog.resize(1180, 650)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(
            "Review each task. Changing a date is normal replanning; only "
            "Record Real Delay adds delay history."))
        table = QTableWidget(len(candidates), 5)
        table.setHorizontalHeaderLabels(["Task", "Current end", "Action", "New end", "Reason"])
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        rows = []
        choices = ["Skip", "Done", "Still on track", "Change date normally", "Record Real Delay"]
        for row, node in enumerate(candidates):
            table.setItem(row, 0, QTableWidgetItem(self._full_path(node)))
            table.setItem(row, 1, QTableWidgetItem(node.end_date or ""))
            action = QComboBox()
            action.addItems(choices)
            date = QDateEdit()
            date.setCalendarPopup(True)
            date.setDisplayFormat("yyyy-MM-dd")
            parsed = QDate.fromString(node.end_date or "", "yyyy-MM-dd")
            date.setDate(parsed if parsed.isValid() else QDate.currentDate())
            reason = QLineEdit()
            reason.setPlaceholderText("Required only for a real delay")
            date.setEnabled(False)
            reason.setEnabled(False)
            action.currentTextChanged.connect(
                lambda choice, date=date, reason=reason: (
                    date.setEnabled(choice == "Change date normally"),
                    reason.setEnabled(choice == "Record Real Delay")))
            table.setCellWidget(row, 2, action)
            table.setCellWidget(row, 3, date)
            table.setCellWidget(row, 4, reason)
            rows.append((node, action, date, reason))
        layout.addWidget(table, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)

        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if not usage_logger.timed_exec(dialog, "project_review"):
            usage_logger.log("review_apply", outcome="canceled")
            return
        missing = [node.name for node, action, _, reason in rows
                   if action.currentText() == "Record Real Delay"
                   and not reason.text().strip()]
        if missing:
            usage_logger.log("review_apply", outcome="failed", reason="missing_delay_reason")
            QMessageBox.warning(self, "Reason Required",
                                "Enter a reason for every recorded real delay.")
            return
        invalid_delay = []
        for node, action, _, _ in rows:
            if action.currentText() != "Record Real Delay":
                continue
            try:
                variance = int(node.duration) - node.baseline_duration
                uncovered = variance - node.logged_slip()
            except (ValueError, TypeError):
                variance = uncovered = 0
            if variance <= 0 or uncovered <= 0:
                invalid_delay.append(node.name)
        if invalid_delay:
            usage_logger.log("review_apply", outcome="failed", reason="nonpositive_delay")
            QMessageBox.warning(
                self, "No Positive Delay",
                "A selected task has no unrecorded positive variance. Choose "
                "Still on track or update its baseline instead.")
            return

        counts = {choice: 0 for choice in choices}
        touched = []
        today = datetime.now().strftime("%Y-%m-%d")
        proj.begin_batch()
        try:
            for node, action, date, reason in rows:
                choice = action.currentText()
                counts[choice] += 1
                if choice == "Done":
                    node.set_status("Completed")
                    touched.append(node)
                elif choice == "Change date normally":
                    new_end = date.date().toString("yyyy-MM-dd")
                    node.set_end_rule({"mode": "fixed", "date": new_end})
                    node.set_date("end", new_end)
                    touched.append(node)
                elif choice == "Record Real Delay":
                    variance = int(node.duration) - node.baseline_duration
                    uncovered = variance - node.logged_slip()
                    node.log_delay_revision({
                        "rev": chr(ord("B") + len(node.revisions)),
                        "date": today, "end": node.end_date or "",
                        "slip": uncovered, "variance": variance,
                        "reason": reason.text().strip(),
                    })
                    touched.append(node)
                    usage_logger.log("record_real_delay", outcome="applied", via="review")
            proj.reviewed_through = today
            proj.tree_view.recalculate_all_dates()
            proj.tree_view.refresh_entire_tree()
            target = touched[0] if touched else candidates[0]
            proj.tree_view.item_changed_signal.emit(target)
        finally:
            proj.end_batch()
        usage_logger.log("review_apply", outcome="applied",
                         done=counts["Done"], on_track=counts["Still on track"],
                         replanned=counts["Change date normally"],
                         delayed=counts["Record Real Delay"], skipped=counts["Skip"])
        proj.tree_view.journal_event.emit(f"Project reviewed through {today}")
        self.statusBar().showMessage(f"Reviewed through {today}", 4000)
        self._update_overdue_action()

    def _show_waiting_list(self):
        from datetime import datetime
        from PyQt6.QtWidgets import (
            QDialog, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
            QDialogButtonBox, QHeaderView,
        )
        from PyQt6.QtGui import QColor

        proj = self.active_project()
        if not proj:
            return
        today = datetime.now().date()
        rows = []
        for node in proj.tree_view.get_all_nodes_flat():
            if not node.waiting_on:
                continue
            days = 0
            if node.waiting_since:
                try:
                    since = datetime.strptime(node.waiting_since, "%Y-%m-%d").date()
                    days = max(0, (today - since).days)
                except ValueError:
                    pass
            rows.append((node, days))
        rows.sort(key=lambda item: item[1], reverse=True)

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Waiting-On List - {proj.name}")
        dialog.resize(760, 420)
        layout = QVBoxLayout(dialog)
        if not rows:
            layout.addWidget(QLabel("No tasks are marked waiting."))
        else:
            table = QTableWidget(len(rows), 4)
            table.setHorizontalHeaderLabels(["Task", "Waiting on", "Since", "Days"])
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.verticalHeader().setVisible(False)
            for row, (node, days) in enumerate(rows):
                values = [self._full_path(node), node.waiting_on, node.waiting_since or "", str(days)]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setData(Qt.ItemDataRole.UserRole, node.id)
                    if days >= 5:
                        item.setForeground(QColor("#D50000"))
                    table.setItem(row, col, item)

            def open_row(item):
                node_id = item.data(Qt.ItemDataRole.UserRole)
                if node_id:
                    dialog.accept()
                    proj.inner_tabs.setCurrentIndex(0)
                    proj.tree_view.jump_to_node_id(node_id)

            table.itemDoubleClicked.connect(open_row)
            layout.addWidget(table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _set_baseline(self):
        proj = self.active_project()
        if proj:
            proj.tree_view.set_baseline()
            self.on_data_changed()

    def _clear_baseline(self):
        proj = self.active_project()
        if proj:
            proj.tree_view.clear_baseline()
            self.on_data_changed()

    def _refresh_all(self):
        """Run scheduling, allocation analysis, and every dependent view."""
        projects = self.all_projects()
        if not projects:
            return
        active_before = self.active_project()
        task_count = 0
        try:
            for proj in projects:
                proj.activate()
                proj.tree_view.recalculate_all_dates()
                proj.tree_view.refresh_entire_tree()
                proj.tree_view.update_filter_options()
                proj.timeline.refresh()
                proj.gantt_view.load_nodes(proj.tree_view.root_nodes)
                proj._update_vave_totals()
                task_count += len(proj.tree_view.get_all_nodes_flat())
        finally:
            if active_before:
                active_before.activate()
        result = self.recheck_resource_conflicts()
        summary = (f"Refreshed {len(projects)} projects · {task_count} tasks · "
                   f"{len(result.assignments)} resource assignments · "
                   f"{len(result.unresolved)} conflicts")
        self.statusBar().showMessage(summary, 8000)
        usage_logger.log("refresh_all", projects=len(projects), tasks=task_count,
                         assignments=len(result.assignments),
                         conflicts=len(result.unresolved))

    def _open_notes_pad(self, via="ctrl_space"):
        """Open the project's Notes pad (big floating notepad) with the cursor
        in its multi-line capture box. Ctrl+Space / Ctrl+Shift+N / the Edit menu
        all land here — one place to jot notes AND see everything already jotted."""
        proj = self.active_project()
        if not proj:
            return
        if via == "ctrl_space":
            proj.show_contextual_notes()
        else:
            proj.show_notes_and_capture()
        usage_logger.log("quick_capture", via=via, kind="open_pad")

    def _apply_saved_zoom(self):
        self._zoom_factor = float(self.settings.value("ui_zoom", 1.0) or 1.0)
        self._set_zoom(self._zoom_factor, persist=False, announce=False)

    def _adjust_zoom(self, delta: float):
        self._set_zoom(getattr(self, "_zoom_factor", 1.0) + delta)

    def _set_zoom(self, factor: float, persist: bool = True, announce: bool = True):
        from PyQt6.QtWidgets import QApplication
        factor = max(0.7, min(3.0, round(factor, 1)))
        self._zoom_factor = factor
        app = QApplication.instance()
        if app:
            base = app.property("vpm_base_font")
            if base is None:
                base = app.font()
                app.setProperty("vpm_base_font", base)
            font = QFont(base)
            font.setPointSizeF(max(1.0, base.pointSizeF() * factor))
            app.setFont(font)
        for proj in self.all_projects():
            if hasattr(proj.tree_view, "apply_zoom"):
                proj.tree_view.apply_zoom(factor)
        if persist:
            self.settings.setValue("ui_zoom", factor)
        if announce:
            self.statusBar().showMessage(f"Zoom {int(factor * 100)}%", 1500)
            usage_logger.log("zoom", pct=int(factor * 100))

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._adjust_zoom(0.1 if event.angleDelta().y() > 0 else -0.1)
            event.accept()
            return
        super().wheelEvent(event)

    # ---------------- global search ----------------
    def _show_search(self):
        from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QLineEdit,
                                     QListWidget, QListWidgetItem, QLabel)
        if self._search_dialog is not None:
            self._search_dialog.show()
            self._search_dialog.raise_()
            self._search_dialog.activateWindow()
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Search Everything")
        dlg.resize(640, 420)
        dlg.setModal(False)
        layout = QVBoxLayout(dlg)
        box = QLineEdit()
        box.setPlaceholderText("Search task names, notes, delay reasons, journal…")
        layout.addWidget(box)
        hint = QLabel("Type at least 2 characters. Click a result to jump to it.")
        hint.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(hint)
        results = QListWidget()
        layout.addWidget(results)

        def full_path(node):
            parts, n = [], node
            while n:
                parts.append(n.name)
                n = n.parent
            return " > ".join(reversed(parts))

        def run_search(query: str):
            results.clear()
            q = query.strip().lower()
            if len(q) < 2:
                return
            for tab_idx in range(self.project_tabs.count()):
                proj = self.project_tabs.widget(tab_idx)
                if not isinstance(proj, ProjectWidget):
                    continue
                tag = f"[{proj.name}] " if self.project_tabs.count() > 1 else ""
                for node in proj.tree_view.get_all_nodes_flat():
                    hits = []
                    if q in node.name.lower():
                        hits.append("name")
                    if node.notes and q in node.notes.lower():
                        hits.append("notes")
                    if getattr(node, "waiting_on", "") and q in node.waiting_on.lower():
                        hits.append("waiting on")
                    delay_text = (node.delay_notes + " " + " ".join(
                        r.get("reason", "") for r in node.revisions)).lower()
                    if q in delay_text:
                        hits.append("delay log")
                    if hits:
                        it = QListWidgetItem(
                            f"{tag}{full_path(node)}   — {', '.join(hits)}")
                        it.setData(Qt.ItemDataRole.UserRole,
                                   (proj.project_id, node.id))
                        results.addItem(it)
                for e in proj.journal:
                    if q in e.get("text", "").lower():
                        it = QListWidgetItem(
                            f"{tag}Journal {e.get('ts', '')}: {e.get('text', '')}")
                        it.setData(Qt.ItemDataRole.UserRole,
                                   (proj.project_id, None))
                        results.addItem(it)
                for line in proj.notes_panel.get_notes():
                    if q in line.lower():
                        it = QListWidgetItem(f"{tag}📝 Note: {line}")
                        it.setData(Qt.ItemDataRole.UserRole,
                                   (proj.project_id, "__note__"))
                        results.addItem(it)
                for panel in getattr(proj, "custom_note_panels", []):
                    tab_name = proj.notes_tabs.tabText(proj.notes_tabs.indexOf(panel))
                    if q in panel.plain_text().lower():
                        it = QListWidgetItem(f"{tag}📝 {tab_name}")
                        it.setData(Qt.ItemDataRole.UserRole,
                                   (proj.project_id, f"__note_tab__:{tab_name}"))
                        results.addItem(it)
            usage_logger.log("search", chars=len(q), hits=results.count())

        def open_result(it):
            usage_logger.log("search_result_opened")
            project_id, node_id = it.data(Qt.ItemDataRole.UserRole)
            tab_idx = next((index for index in range(self.project_tabs.count())
                            if getattr(self.project_tabs.widget(index),
                                       "project_id", None) == project_id), -1)
            if tab_idx < 0:
                return
            self.project_tabs.setCurrentIndex(tab_idx)
            proj = self.project_tabs.widget(tab_idx)
            if node_id == "__note__":
                proj.show_notes_and_capture()
            elif isinstance(node_id, str) and node_id.startswith("__note_tab__:"):
                proj.show_notes_and_capture()
                wanted = node_id.split(":", 1)[1]
                for index in range(proj.notes_tabs.count()):
                    if proj.notes_tabs.tabText(index) == wanted:
                        proj.notes_tabs.setCurrentIndex(index)
                        break
            elif node_id:
                proj.inner_tabs.setCurrentIndex(0)
                proj.tree_view.jump_to_node_id(node_id)
            else:
                self._show_journal()

        box.textChanged.connect(run_search)
        results.itemClicked.connect(open_result)
        results.itemActivated.connect(open_result)

        self._search_dialog = dlg
        dlg.finished.connect(lambda _: setattr(self, "_search_dialog", None))
        dlg.show()
        box.setFocus()

    def _show_digest_since_last_visit(self):
        """'Since you were here' — journal entries from the last 7 days,
        shown once right after a file is opened."""
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
        lines = []
        for proj in self.all_projects():
            recent = [e for e in proj.journal if e.get("ts", "") >= cutoff]
            for e in recent[-15:]:
                prefix = f"[{proj.name}] " if self.project_tabs.count() > 1 else ""
                lines.append(f"{e['ts']}  {prefix}{e['text']}")
        if not lines:
            return
        lines.sort()
        body = "\n".join(lines[-25:])
        QMessageBox.information(
            self, "Since you were here",
            f"Activity in the last 7 days:\n\n{body}")

    def _show_journal(self):
        """Full activity journal for the active project."""
        from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QPlainTextEdit,
                                     QDialogButtonBox, QLabel)
        proj = self.active_project()
        if not proj:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Activity Journal — {proj.name}")
        dialog.resize(560, 440)
        layout = QVBoxLayout(dialog)
        if proj.journal:
            text = "\n".join(
                f"{e.get('ts', '?')}  {e.get('text', '')}"
                for e in reversed(proj.journal))  # newest first
            view = QPlainTextEdit(text)
            view.setReadOnly(True)
            layout.addWidget(view)
        else:
            layout.addWidget(QLabel(
                "No activity recorded yet. The journal fills in "
                "automatically as you work."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _show_delay_summary(self):
        from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QLabel,
                                     QTableWidget, QTableWidgetItem,
                                     QHeaderView, QDialogButtonBox)
        from PyQt6.QtGui import QColor
        from PyQt6.QtCore import Qt

        proj = self.active_project()
        if not proj:
            return

        def full_path(node):
            parts, n = [], node
            while n:
                parts.append(n.name)
                n = n.parent
            return " > ".join(reversed(parts))

        # Collect delayed tasks
        delayed = []
        for node in proj.tree_view.get_all_nodes_flat():
            if node.children:  # skip parents — their delay is a rollup of children
                continue
            if node.baseline_duration is None:
                continue
            try:
                diff = int(node.duration) - node.baseline_duration
            except (ValueError, TypeError):
                continue
            if diff > 0:
                delayed.append((node, diff))

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Delay Summary — {proj.name}")
        dialog.resize(620, 400)
        layout = QVBoxLayout(dialog)

        if not delayed:
            layout.addWidget(QLabel("No delays detected. All tasks are on track!"))
        else:
            total = sum(d for _, d in delayed)
            summary_lbl = QLabel(f"  {len(delayed)} task(s) delayed  |  Total slip: +{total}d")
            summary_lbl.setStyleSheet("font-weight: bold; padding: 6px;")
            layout.addWidget(summary_lbl)

            table = QTableWidget(len(delayed), 3)
            table.setHorizontalHeaderLabels(["Task", "Delay", "Log"])
            table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setWordWrap(True)
            table.verticalHeader().setVisible(False)

            for row, (node, diff) in enumerate(delayed):
                name_item = QTableWidgetItem(full_path(node))
                delay_item = QTableWidgetItem(f"+{diff}d")
                delay_item.setForeground(QColor("#FF0000"))
                delay_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                has_log = bool(node.revisions or node.delay_notes)
                log_text = (node.revision_trail() if has_log
                            else "(no reason logged — double-click Delay cell to add)")
                log_item = QTableWidgetItem(log_text)
                if not has_log:
                    log_item.setForeground(QColor("#999999"))
                table.setItem(row, 0, name_item)
                table.setItem(row, 1, delay_item)
                table.setItem(row, 2, log_item)
                table.resizeRowToContents(row)

            layout.addWidget(table)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    # ---------------- options dialogs ----------------
    def open_waiting_people_manager(self):
        proj = self.active_project()
        if not proj:
            return
        proj.activate()
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QListWidget, QHBoxLayout, QPushButton, QInputDialog, QDialogButtonBox
        from utils.config_manager import ConfigManager
        config = ConfigManager()
        dialog = QDialog(self)
        dialog.setWindowTitle("Manage Owners / Waiting People")
        dialog.resize(360, 420)
        layout = QVBoxLayout(dialog)
        people = QListWidget()
        for name in config.get_owners():
            if name:
                people.addItem(name)
        layout.addWidget(people)
        row = QHBoxLayout()
        add_btn = QPushButton("Add")
        remove_btn = QPushButton("Remove")
        row.addWidget(add_btn)
        row.addWidget(remove_btn)
        layout.addLayout(row)

        def add_person():
            name, ok = QInputDialog.getText(dialog, "Add Waiting Person", "Name/team:")
            if ok and name.strip():
                existing = [people.item(i).text() for i in range(people.count())]
                if name.strip() not in existing:
                    people.addItem(name.strip())

        def remove_person():
            row_idx = people.currentRow()
            if row_idx >= 0:
                people.takeItem(row_idx)

        add_btn.clicked.connect(add_person)
        remove_btn.clicked.connect(remove_person)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if usage_logger.timed_exec(dialog, "waiting_people_manager"):
            config.set_owners([
                people.item(i).text().strip()
                for i in range(people.count())
                if people.item(i).text().strip()
            ])
            self.on_data_changed()

    def open_calendar_settings(self):
        proj = self.active_project()
        if not proj:
            return
        proj.activate()
        from ui.calendar_dialog import CalendarSettingsDialog
        dialog = CalendarSettingsDialog(self)
        if usage_logger.timed_exec(dialog, "calendar_settings"):
            node = proj.tree_view.root_nodes[0] if proj.tree_view.root_nodes else None
            proj.tree_view.commit_structure_change(node)
            self.on_data_changed()

    def open_template_manager(self):
        """Edit headers and options in one Excel-like table."""
        from ui.metadata_editor import MetadataEditorDialog
        from utils.template_catalog import load_templates, save_templates
        templates = load_templates()
        known_ids = {str(item.get("id")) for item in templates}
        for definition in self.resource_definitions:
            resource_id = str(definition.get("id") or "")
            if not resource_id or resource_id in known_ids or not definition.get("active", True):
                continue
            kind = str(definition.get("type") or "resource")
            templates.append({
                "id": resource_id,
                "name": str(definition.get("label") or "Resource"),
                "kind": kind,
                "header": kind.replace("_", " ").title(),
                "flag_overlaps": bool(definition.get("conflict_enabled", False)),
                "version": 1,
            })
        dialog = MetadataEditorDialog(
            templates, parent=self, projects=self._resource_projects())
        dialog.jumpRequested.connect(self._jump_to_project_task)
        while usage_logger.timed_exec(dialog, "metadata_lists"):
            try:
                saved = dialog.result_items()
            except ValueError as exc:
                QMessageBox.warning(self, "Invalid Metadata Option", str(exc))
                continue
            break
        else:
            return
        save_templates(saved)
        self._sync_resource_definitions_to_metadata(saved)
        updated_tasks = self._sync_tasks_to_metadata(saved)
        self.recheck_resource_conflicts()
        self.on_data_changed()
        usage_logger.log("metadata_lists_save", count=len(saved),
                         updated_tasks=updated_tasks)
        if updated_tasks:
            self.statusBar().showMessage(
                f"Metadata saved; {updated_tasks} scheduled task(s) updated", 5000)

    def _sync_resource_definitions_to_metadata(self, saved):
        """Rename by stable ID and retain capacity/history for deleted options."""
        from utils.resource_allocation import ResourceDefinition, definition_map
        current = definition_map(self.resource_definitions)
        present = set()
        for item in saved:
            kind = str(item.get("kind") or "resource")
            resource_id = item.get("id")
            if (not resource_id or item.get("record_type") == "header" or
                    kind.casefold() in {"campaign", "header"}):
                continue
            present.add(str(resource_id))
            old = current.get(str(resource_id))
            label = str(item.get("name") or "Resource")
            current[str(resource_id)] = ResourceDefinition(
                str(resource_id), kind, label,
                old.capacity if old else 1,
                bool(item.get("flag_overlaps", False)),
                old.active if old else True,
                old.home_location if old else "",
                old.notes if old else "",
                old.policy if old else "warning")
        # Deleted options stay identifiable for already-scheduled historical
        # assignments but become inactive for future capacity warnings.
        for resource_id, old in list(current.items()):
            if resource_id not in present and not resource_id.startswith("legacy:"):
                current[resource_id] = ResourceDefinition(
                    old.id, old.type, old.label, old.capacity,
                    old.conflict_enabled, False, old.home_location,
                    old.notes, old.policy)
        self.resource_definitions = [value.to_dict() for value in current.values()]

    @staticmethod
    def _metadata_duration(tokens):
        """Return the last duration-bearing token, matching task entry rules."""
        duration = None
        for token in tokens:
            if token.get("duration") not in (None, ""):
                try:
                    duration = max(1, int(token["duration"]))
                except (TypeError, ValueError):
                    continue
        return duration

    def _sync_tasks_to_metadata(self, saved):
        """Propagate renamed options/default timelines to placed tasks.

        Tasks are linked to metadata by stable option id. A task whose Duration
        still equals its old metadata default follows future default changes;
        an explicitly overridden task duration remains untouched.
        """
        options = {
            item.get("id"): item for item in saved
            if item.get("id") and item.get("record_type") != "header"
            and item.get("kind") != "campaign"
        }
        total = 0
        for project in self.all_projects():
            tree = project.tree_view
            changed_nodes = []
            for node in tree.get_all_nodes_flat():
                if not node.task_tokens:
                    continue
                old_tokens = [dict(token) for token in node.task_tokens]
                old_duration = self._metadata_duration(old_tokens)
                try:
                    rule_days = int(node.end_rule.get("days"))
                except (TypeError, ValueError):
                    rule_days = None
                follows_default = (
                    node.end_rule.get("mode") == "duration"
                    and old_duration is not None
                    and rule_days == old_duration)

                new_tokens = []
                changed = False
                for token in old_tokens:
                    current = options.get(token.get("id"))
                    if current is None:
                        new_tokens.append(token)
                        continue
                    updated = dict(token)
                    for source, target in (("name", "label"), ("kind", "kind"),
                                           ("duration", "duration"),
                                           ("header", "header")):
                        updated[target] = current.get(source)
                    if updated != token:
                        changed = True
                    if node.name == token.get("label"):
                        node.name = updated.get("label") or node.name
                    new_tokens.append(updated)

                # ``resources`` is the legacy one-value-per-kind projection
                # used by older files.  Once a kind has stable tokens, those
                # tokens are authoritative; otherwise a renamed token leaves
                # its old label behind as a second physical assignment.
                resource_kinds = {"room", "case", "cassette", "article",
                                  "test article"}

                def resource_key(token):
                    kind = str(token.get("kind") or "").casefold()
                    if kind not in resource_kinds:
                        return None
                    return ("article" if kind in
                            {"cassette", "article", "test article"} else kind)

                projected = dict(node.resources)
                token_keys = {key for key in
                              (resource_key(token) for token in old_tokens + new_tokens)
                              if key}
                for key in token_keys:
                    projected.pop(key, None)
                for token in new_tokens:
                    key = resource_key(token)
                    label = str(token.get("label") or "").strip()
                    if key and label:
                        projected[key] = label
                if projected != node.resources:
                    changed = True

                if not changed:
                    continue
                node.task_tokens = new_tokens
                node.resources = projected
                new_duration = self._metadata_duration(new_tokens)
                if follows_default and new_duration is not None:
                    node.end_rule = {"mode": "duration", "days": new_duration}
                snapshot_id = node.template_snapshot.get("id")
                if snapshot_id in options:
                    node.template_snapshot = dict(options[snapshot_id])
                changed_nodes.append(node)

            if changed_nodes:
                tree.recalculate_all_dates()
                tree.refresh_entire_tree()
                tree.update_filter_options()
                tree.journal_event.emit(
                    f"Metadata changes updated {len(changed_nodes)} scheduled task(s)")
                tree.item_changed_signal.emit(changed_nodes[0])
                total += len(changed_nodes)
        return total

    # ---------------- data change tracking ----------------
    def on_data_changed(self):
        if not self.unsaved_changes:
            self.unsaved_changes = True
        self.update_title()
        self._refresh_project_tab_labels()
        self._update_vave_action_state()
        self._update_overdue_action()
        self._resource_timer.start()

    def update_title(self):
        title = f"{AppConstants.APP_NAME} - {AppConstants.REVISION_LABEL}"
        title += f" - {self.current_filepath}" if self.current_filepath else " - New File"
        if self.unsaved_changes:
            title += " *"
        self.setWindowTitle(title)

    def closeEvent(self, event):
        if self.unsaved_changes:
            reply = QMessageBox.question(
                self, "Unsaved Changes",
                "You have unsaved changes. Save before closing?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.save_project_file()
                if self.unsaved_changes:
                    event.ignore()
                    return
                event.accept()
            elif reply == QMessageBox.StandardButton.No:
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()
        if event.isAccepted():
            try:
                self._cancel_repo_telemetry_timers()
                usage_logger.log("app_end", secs=usage_logger.usage.session_secs())
                usage_logger.usage.write_summary()
                # Bound so a periodic export holding the lock cannot hang quit.
                usage_logger.sync_repo_telemetry(
                    background=False,
                    timeout=usage_logger.CLOSE_EXPORT_TIMEOUT_SECONDS)
            except Exception:
                pass
            finally:
                self.file_guard.release()

    # ---------------- file I/O ----------------
    def save_project_file(self):
        if not self.current_filepath:
            self.save_project_file_as()
            return
        if self.file_guard.changed_on_disk():
            usage_logger.log("file_conflict", where="manual_save")
            QMessageBox.warning(
                self, "Project Changed on Disk",
                "This project was changed by another app window, computer, or "
                "OneDrive after you opened it. Your changes were not saved.\n\n"
                "Use Save As to preserve your version, then compare the files.")
            return
        try:
            from utils.vpmt_io import save_projects
            save_projects(
                [p.to_persistable() for p in self.all_projects()],
                self.current_filepath,
                resource_definitions=self.resource_definitions,
                conflict_resolutions=self.resource_conflict_resolutions,
            )
            self.statusBar().showMessage(f"Saved to {self.current_filepath}", 3000)
            self.file_guard.refresh()
            usage_logger.log("file_save", manual=True)
            self.unsaved_changes = False
            self.update_title()
            self._remember_recent(self.current_filepath)
            recovery = self._session_recovery_path
            if recovery and os.path.exists(recovery):
                try:
                    self._mark_recovery_handled(recovery, os.path.getmtime(recovery))
                    os.remove(recovery)
                except OSError:
                    pass
        except Exception as e:
            usage_logger.log("save_failed", type=type(e).__name__)
            usage_logger.log("warning_shown", name="save_failed")
            QMessageBox.critical(self, "Error", f"Could not save file: {e}")

    def save_project_file_as(self):
        filename, _ = QFileDialog.getSaveFileName(
            self, "Save File As", "", f"VPM Files (*{AppConstants.FILE_EXT})"
        )
        if filename:
            if os.path.abspath(filename) == self.file_guard.path:
                self.save_project_file()
                return
            candidate = ProjectFileGuard()
            if not candidate.acquire(filename):
                owner = candidate.last_lock_owner or {}
                usage_logger.log("file_conflict", where="save_as_lock")
                QMessageBox.warning(
                    self, "Project Already Open",
                    "That project is already open in another app instance"
                    + (f" (process {owner.get('pid')})." if owner.get('pid') else "."))
                return
            try:
                from utils.vpmt_io import save_projects
                save_projects(
                    [p.to_persistable() for p in self.all_projects()], filename,
                    resource_definitions=self.resource_definitions,
                    conflict_resolutions=self.resource_conflict_resolutions)
            except Exception as exc:
                candidate.release()
                usage_logger.log("save_failed", type=type(exc).__name__)
                QMessageBox.critical(self, "Error", f"Could not save file: {exc}")
                return
            self.file_guard.release()
            self.file_guard = candidate
            self.file_guard.refresh()
            self.current_filepath = filename
            recovery = self._session_recovery_path
            if recovery and os.path.exists(recovery):
                try:
                    self._mark_recovery_handled(recovery, os.path.getmtime(recovery))
                    os.remove(recovery)
                except OSError:
                    pass
            self._session_recovery_path = None
            self.unsaved_changes = False
            self.update_title()
            self._remember_recent(filename)
            usage_logger.log("file_save", manual=True, save_as=True)
            self.statusBar().showMessage(f"Saved to {filename}", 3000)

    def load_project_file(self):
        filename, _ = QFileDialog.getOpenFileName(
            self, "Open File", "", f"VPM Files (*{AppConstants.FILE_EXT})"
        )
        if not filename:
            return
        self._load_path(filename)

    def _confirm_discard_unsaved(self) -> bool:
        if not self.unsaved_changes:
            return True
        reply = QMessageBox.question(
            self, "Unsaved Changes",
            "You have unsaved changes. Save before loading another file?",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.save_project_file()
            return not self.unsaved_changes
        if reply == QMessageBox.StandardButton.No:
            return True
        return False

    def _load_path(self, filename: str, prompt_unsaved: bool = True,
                   is_recovery: bool = False) -> bool:
        if prompt_unsaved and not self._confirm_discard_unsaved():
            return False
        candidate_guard = None
        if (not is_recovery
                and os.path.abspath(filename) != self.file_guard.path):
            candidate_guard = ProjectFileGuard()
            if not candidate_guard.acquire(filename):
                owner = candidate_guard.last_lock_owner or {}
                usage_logger.log("file_conflict", where="open_lock")
                QMessageBox.warning(
                    self, "Project Already Open",
                    "This project is already open in another app instance"
                    + (f" (process {owner.get('pid')})." if owner.get('pid') else ".")
                    + "\n\nThe file was not opened to prevent conflicting saves.")
                return False
        try:
            from utils.vpmt_io import load_projects, read_version, CURRENT_VERSION
            projects = load_projects(filename)
            file_version = read_version(filename)
        except Exception as e:
            if candidate_guard:
                candidate_guard.release()
            usage_logger.log("load_failed", type=type(e).__name__)
            usage_logger.log("warning_shown", name="load_failed")
            QMessageBox.critical(self, "Error", f"Could not load file: {e}")
            return False

        if candidate_guard:
            self.file_guard.release()
            self.file_guard = candidate_guard

        # Seatbelt for much older/unknown files. Small expected upgrades are quiet.
        if self._should_warn_file_version(file_version, CURRENT_VERSION):
            usage_logger.log("warning_shown", name="older_file_version")
            QMessageBox.information(
                self, "Older file version",
                f"This file was saved by an older version of the app "
                f"(file: {file_version}, current: {CURRENT_VERSION}).\n\n"
                "Newer data such as baselines, delay logs, or the activity "
                "journal may be missing from it. Saving will upgrade the "
                "file to the current format.")

        # Wipe existing tabs. Each project's close_project releases ConfigManager state.
        while self.project_tabs.count():
            w = self.project_tabs.widget(0)
            if isinstance(w, ProjectWidget):
                w.close_project()
            self.project_tabs.removeTab(0)

        shared = projects[0] if projects else {}
        self.resource_definitions = list(
            shared.get("resource_definitions", []) or [])
        self.resource_conflict_resolutions = list(
            shared.get("resource_conflict_resolutions", []) or [])

        for proj_dict in projects:
            self._add_project_from_data(
                proj_dict["name"], proj_dict.get("metadata", {}),
                proj_dict.get("roots", []),
                proj_dict.get("journal", []),
                proj_dict.get("notes", []),
                proj_dict.get("notepad_html", ""),
                proj_dict.get("is_vave", False),
                proj_dict.get("id"),
                proj_dict.get("reviewed_through"),
                proj_dict.get("note_tabs", []),
                proj_dict.get("resources", {}),
            )

        self.current_filepath = None if is_recovery else filename
        if is_recovery:
            self.file_guard.release()
        else:
            self.file_guard.refresh()
        self._session_recovery_path = None
        self.unsaved_changes = False
        self.update_title()
        self.statusBar().showMessage(f"Loaded {filename}", 3000)
        if not is_recovery:
            self._remember_recent(filename)
            self._warn_duplicate_basename(filename)
        if not self._loading_startup:
            self._show_digest_since_last_visit()
        self._set_zoom(getattr(self, "_zoom_factor", 1.0), persist=False, announce=False)
        self._update_overdue_action()
        self._show_holiday_tip_if_needed()
        result = self.recheck_resource_conflicts()
        if result.unresolved:
            self.statusBar().showMessage(
                f"Loaded with {len(result.unresolved)} unresolved resource "
                "conflict(s); open Resources for details", 9000)
        usage_logger.log(
            "file_open",
            file_type=os.path.splitext(filename)[1].lower(),
            projects=len(projects),
            tasks=sum(len(p.tree_view.get_all_nodes_flat()) for p in self.all_projects()),
        )
        return True

    def _should_warn_file_version(self, file_version: str, current_version: str) -> bool:
        if file_version == current_version:
            return False
        try:
            file_major, file_minor = [int(part) for part in str(file_version).split(".")[:2]]
            current_major, current_minor = [int(part) for part in str(current_version).split(".")[:2]]
        except (TypeError, ValueError):
            return True
        if file_major != current_major:
            return True
        return current_minor - file_minor > 1

    # ---------------- recent files ----------------
    def _recent_files(self) -> list:
        files = self.settings.value("recent_files", []) or []
        if isinstance(files, str):
            files = [files]
        return [f for f in files if os.path.exists(f)]

    def _remember_recent(self, path: str):
        files = self._recent_files()
        if path in files:
            files.remove(path)
        files.insert(0, path)
        self.settings.setValue("recent_files", files[:8])

    def _populate_recent_menu(self):
        self.recent_menu.clear()
        files = self._recent_files()
        if not files:
            empty = QAction("(no recent files)", self)
            empty.setEnabled(False)
            self.recent_menu.addAction(empty)
            return
        for path in files:
            try:
                from datetime import datetime
                mod = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
            except OSError:
                mod = "unknown date"
            act = QAction(f"{path}  ({mod})", self)
            act.setToolTip(path)
            act.triggered.connect(lambda _, p=path: self._load_path(p))
            self.recent_menu.addAction(act)

    def _warn_duplicate_basename(self, opened_path: str):
        opened_abs = os.path.abspath(opened_path)
        opened_base = os.path.basename(opened_abs).lower()
        try:
            opened_mtime = os.path.getmtime(opened_abs)
        except OSError:
            return
        for path in self._recent_files():
            candidate = os.path.abspath(path)
            if candidate == opened_abs:
                continue
            if os.path.basename(candidate).lower() != opened_base:
                continue
            try:
                candidate_mtime = os.path.getmtime(candidate)
            except OSError:
                continue
            if candidate_mtime > opened_mtime:
                usage_logger.log("warning_shown", name="newer_duplicate_basename")
                QMessageBox.warning(
                    self, "Newer File With Same Name",
                    f"A newer file with the same name exists at:\n\n{candidate}\n\n"
                    "Are you sure this is the right one?")
                return

    def _show_holiday_tip_if_needed(self):
        from utils.config_manager import ConfigManager
        default_holidays = ConfigManager.default_snapshot().get("holidays", []) or []
        if not default_holidays:
            return
        for proj in self.all_projects():
            md = proj.to_persistable().get("metadata", {}) or {}
            if not (md.get("holidays") or []):
                self.statusBar().showMessage(
                    "Tip: this project has no holidays set - Options > Calendar Settings.",
                    8000,
                )
                return

    # ---------------- excel export ----------------
    def export_to_excel_prompt(self):
        """One Export menu entry; ask what to export only when it matters.
        With a single project open there is nothing to ask — just export."""
        projects = self.all_projects()
        if not projects:
            return
        if len(projects) == 1:
            self._export_to_excel(projects)
            return
        proj = self.active_project()
        box = QMessageBox(self)
        box.setWindowTitle("Export to Excel")
        box.setText("What do you want to export?")
        current_btn = box.addButton(
            f"Current project ({proj.name})" if proj else "Current project",
            QMessageBox.ButtonRole.AcceptRole)
        all_btn = box.addButton(
            f"All {len(projects)} projects", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked == current_btn and proj:
            self._export_to_excel([proj])
        elif clicked == all_btn:
            self._export_to_excel(projects)

    def export_all_to_excel(self):
        self._export_to_excel(self.all_projects())

    def export_active_to_excel(self):
        proj = self.active_project()
        if proj:
            self._export_to_excel([proj])

    def _export_to_excel(self, projects):
        if not projects:
            return
        default_name = "export.xlsx"
        if self.current_filepath:
            stem = os.path.splitext(os.path.basename(self.current_filepath))[0]
            default_name = f"{stem}.xlsx"
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export to Excel", default_name, "Excel Workbook (*.xlsx)"
        )
        if not filename:
            return
        try:
            from utils.excel_export import export_projects
            export_projects(
                [p.to_persistable() for p in projects], filename,
                self.resource_definitions,
                self.resource_conflict_resolutions)
            usage_logger.log("excel_export", projects=len(projects))
            self.statusBar().showMessage(f"Exported to {filename}", 4000)
        except ImportError:
            usage_logger.log("warning_shown", name="excel_missing_dependency")
            QMessageBox.critical(
                self, "Missing Dependency",
                "Excel export requires the 'openpyxl' package.\n\n"
                "Install it with:\n    pip install openpyxl",
            )
        except Exception as e:
            usage_logger.log("error", where="excel_export", type=type(e).__name__)
            QMessageBox.critical(self, "Export Failed", f"Could not export: {e}")
