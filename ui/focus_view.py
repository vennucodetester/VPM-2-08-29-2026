"""
FocusView â€” replaces the Gantt chart in the Visuals tab.

Two stacked panels, both rebuilt from the task tree every time the tab
is shown:

1. "What drives my end date" â€” the driver chain (critical path) rendered
   as a sentence of pills, the single task to push on, and a watch list
   of near-critical tasks with little slack.
2. "This week" board â€” Overdue / Due this week / Starting next week,
   each with waiting-on names, so the user knows who to chase.
"""
from datetime import datetime, timedelta

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QFrame, QScrollArea, QComboBox, QMenu)
from PyQt6.QtCore import Qt, pyqtSignal

from utils.workday_calculator import WorkdayCalculator

DATE_FMT = "%Y-%m-%d"


def _parse(date_str):
    try:
        return datetime.strptime(date_str, DATE_FMT).date()
    except (ValueError, TypeError):
        return None


def _fmt(date_str):
    d = _parse(date_str)
    return d.strftime("%b %d") if d else "?"


def _is_inbox(node):
    """True if this node is the Inbox notes group or lives inside it.

    The Inbox is a dateless root named 'inbox' used purely for quick notes
    (Ctrl+Space capture). It must never appear in any schedule visual.
    """
    n = node
    while n is not None:
        if n.parent is None and (n.name or "").strip().lower() == "inbox":
            return True
        n = n.parent
    return False


def _flatten(roots):
    out = []

    def walk(nodes):
        for n in nodes:
            out.append(n)
            walk(n.children)
    walk(roots)
    return out


def _link(node):
    """Task name as a clickable anchor carrying the node id."""
    return (f"<a href='{node.id}' style='color:inherit; "
            f"text-decoration:none;'>{node.name}</a>")


class FocusView(QWidget):
    # Emits the node id when the user clicks a task name â†’ jump to Tracker.
    task_activated = pyqtSignal(str)
    task_action_requested = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.root_nodes = []
        self.waiting_filter = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        top.setContentsMargins(12, 8, 12, 0)
        top.addWidget(QLabel("Waiting On:"))
        self.waiting_combo = QComboBox()
        self.waiting_combo.setMinimumWidth(180)
        self.waiting_combo.currentTextChanged.connect(self._on_waiting_changed)
        top.addWidget(self.waiting_combo)
        top.addStretch()
        outer.addLayout(top)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)

        body = QWidget()
        scroll.setWidget(body)
        self.layout_body = QVBoxLayout(body)
        self.layout_body.setContentsMargins(16, 12, 16, 16)
        self.layout_body.setSpacing(14)

        self.digest_label = self._label("", size=12)
        self.layout_body.addWidget(self.digest_label)
        self.chain_panel = self._make_panel()
        self.week_panel = self._make_panel()
        self.layout_body.addWidget(self.chain_panel)
        self.layout_body.addWidget(self.week_panel)
        self.layout_body.addStretch()

    @staticmethod
    def _make_panel():
        frame = QFrame()
        frame.setStyleSheet(
            "QFrame { background: #f7f6f3; border-radius: 10px; }"
            "QLabel { background: transparent; }")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        return frame

    @staticmethod
    def _clear(frame):
        lay = frame.layout()
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

    def _label(self, html, size=13, node=None):
        lbl = QLabel(html)
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setStyleSheet(f"font-size: {size}px; color: #202020;")
        lbl.setOpenExternalLinks(False)
        lbl.linkActivated.connect(self.task_activated.emit)
        if node is not None:
            lbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            lbl.customContextMenuRequested.connect(
                lambda pos, n=node, w=lbl: self._task_menu(n, w, pos))
            tip = self._delay_tooltip(node)
            if tip:
                lbl.setToolTip(tip)
        return lbl

    def _task_menu(self, node, widget, pos):
        menu = QMenu(self)
        menu.addAction("Mark Completed", lambda: self.task_action_requested.emit(node.id, "complete"))
        menu.addAction("Record Real Delay...", lambda: self.task_action_requested.emit(node.id, "delay"))
        menu.addAction("Jump to Tracker", lambda: self.task_activated.emit(node.id))
        menu.exec(widget.mapToGlobal(pos))

    def _on_waiting_changed(self, text):
        self.waiting_filter = "" if text == "All" else text
        self._rebuild()

    def _waiting_from(self, nodes):
        waiting = set()
        for n in nodes:
            if getattr(n, "waiting_on", ""):
                waiting.add(n.waiting_on)
        return sorted(waiting)

    def _waiting_matches(self, node):
        if not self.waiting_filter:
            return True
        return getattr(node, "waiting_on", "") == self.waiting_filter

    @staticmethod
    def _delay_tooltip(node):
        if getattr(node, "revisions", None) or getattr(node, "delay_notes", ""):
            return node.revision_trail()
        return ""

    # ------------------------------------------------------------------
    # Public API (mirrors the old GanttChartWidget)
    # ------------------------------------------------------------------

    def load_nodes(self, root_nodes):
        # Drop the Inbox group entirely â€” it holds dateless notes, not plan
        # tasks, so it must not influence the driver chain or the week board.
        self.root_nodes = [r for r in (root_nodes or []) if not _is_inbox(r)]
        self._refresh_waiting_combo()
        self._rebuild()

    def _refresh_waiting_combo(self):
        current = self.waiting_filter
        waiting = self._waiting_from(_flatten(self.root_nodes))
        self.waiting_combo.blockSignals(True)
        try:
            self.waiting_combo.clear()
            self.waiting_combo.addItem("All")
            self.waiting_combo.addItems(waiting)
            if current and current in waiting:
                self.waiting_combo.setCurrentText(current)
            else:
                self.waiting_filter = ""
                self.waiting_combo.setCurrentText("All")
        finally:
            self.waiting_combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Driver-chain analysis
    # ------------------------------------------------------------------

    def _driver_of(self, node, node_map):
        """The task whose finish dictates this task's start (scheduler mirror)."""
        if node.predecessor_id and node.predecessor_id in node_map:
            return node_map[node.predecessor_id]
        if node.parent:
            sibs = node.parent.children
            try:
                idx = sibs.index(node)
            except ValueError:
                return None
            if not node.is_parallel and idx > 0:
                return sibs[idx - 1]
            return self._driver_of(node.parent, node_map)
        try:
            idx = self.root_nodes.index(node)
        except ValueError:
            return None
        if not node.is_parallel and idx > 0:
            return self.root_nodes[idx - 1]
        return None

    @staticmethod
    def _defining_leaf(node):
        """Descend to the leaf whose end date defines this node's end."""
        while node.children:
            dated = [c for c in node.children if c.end_date]
            if not dated:
                return node
            node = max(dated, key=lambda c: c.end_date)
        return node

    def _compute_chain(self, all_nodes, node_map):
        leaves = [n for n in all_nodes if not n.children and n.end_date]
        if not leaves:
            return []
        end_task = max(leaves, key=lambda n: n.end_date)
        chain = [end_task]
        seen = {end_task.id}
        cur = end_task
        while True:
            d = self._driver_of(cur, node_map)
            if d is None:
                break
            d = self._defining_leaf(d)
            if d.id in seen:
                break
            chain.append(d)
            seen.add(d.id)
            cur = d
        chain.reverse()
        return chain

    def _compute_slack(self, all_nodes, node_map, project_end):
        """slack[node.id] = workdays its downstream terminal end sits before
        the project end. 0 = critical: any slip moves the project."""
        successors = {}
        for n in all_nodes:
            d = self._driver_of(n, node_map)
            if d is not None:
                successors.setdefault(d.id, []).append(n)

        memo = {}

        def terminal_end(n, guard):
            if n.id in memo:
                return memo[n.id]
            if n.id in guard:
                return n.end_date or ""
            guard.add(n.id)
            succ = successors.get(n.id, [])
            best = n.end_date or ""
            for s in succ:
                t = terminal_end(s, guard)
                if t > best:
                    best = t
            memo[n.id] = best
            return best

        slack = {}
        for n in all_nodes:
            t_end = terminal_end(n, set())
            if not t_end or not project_end:
                continue
            if t_end >= project_end:
                slack[n.id] = 0
            else:
                slack[n.id] = max(
                    0, WorkdayCalculator.calculate_duration(t_end, project_end) - 1)
        return slack

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _rebuild(self):
        all_nodes = _flatten(self.root_nodes)
        node_map = {n.id: n for n in all_nodes}
        self._build_digest(all_nodes, node_map)
        self._build_chain_panel(all_nodes, node_map)
        self._build_week_panel(all_nodes, node_map)

    def _build_digest(self, all_nodes, node_map):
        today = datetime.now().date()
        week_end = today - timedelta(days=today.weekday()) + timedelta(days=6)
        leaves = [n for n in all_nodes if not n.children]
        project_end = max((n.end_date or "" for n in leaves), default="")
        overdue = [
            n for n in leaves
            if n.status != "Completed" and _parse(n.end_date) and _parse(n.end_date) < today
        ]
        due_week = [
            n for n in leaves
            if n.status != "Completed" and _parse(n.end_date) and today <= _parse(n.end_date) <= week_end
        ]
        ready = self._ready_to_start(all_nodes, node_map)
        self.digest_label.setText(
            f"Project ends <b>{_fmt(project_end)}</b> Â· "
            f"{len(overdue)} overdue Â· {len(due_week)} due this week Â· "
            f"{len(ready)} ready to start"
        )

    def _build_chain_panel(self, all_nodes, node_map):
        self._clear(self.chain_panel)
        lay = self.chain_panel.layout()

        chain = self._compute_chain(all_nodes, node_map)
        if not chain:
            lay.addWidget(self._label("No scheduled tasks yet."))
            return

        project_end = max((n.end_date or "" for n in all_nodes), default="")
        today = datetime.now().date()

        lay.addWidget(self._label(
            f"Project ends <b>{_fmt(project_end)}</b> because of this chain:",
            size=14))

        # Chain pills (first not-completed task = the active driver, in red)
        push_task = next((t for t in chain if t.status != "Completed"), None)
        pills = []
        for t in chain:
            if t.status == "Completed":
                style = "background:#E1F5EE; color:#085041;"
                tag = "done"
            elif t is push_task:
                style = ("background:#FCEBEB; color:#791F1F; "
                         "border:2px solid #E24B4A;")
                e = _parse(t.end_date)
                if e and e >= today:
                    left = WorkdayCalculator.calculate_duration(
                        today.strftime(DATE_FMT), t.end_date)
                    tag = f"{left}d left"
                else:
                    tag = "overdue"
            else:
                style = "background:#ECEAE4; color:#444441;"
                tag = f"{t.duration}d"
            pills.append(
                f"<span style='{style} padding:3px 8px; border-radius:6px; "
                f"white-space:nowrap;'>{_link(t)} Â· {tag}</span>")
        lay.addWidget(self._label(
            " <span style='color:#888'>â†’</span> ".join(pills)))

        # Push-here card
        if push_task:
            waiting = f" (waiting on {push_task.waiting_on})" if push_task.waiting_on else ""
            lay.addWidget(self._label(
                f"<span style='color:#A32D2D; font-weight:bold;'>Push here:"
                f"</span> <b>{_link(push_task)}</b>{waiting} â€” every workday "
                f"saved here moves the project end a day earlier.", size=13,
                node=push_task))

        # Watch list: near-critical leaves not already in the chain
        slack = self._compute_slack(all_nodes, node_map, project_end)
        chain_ids = {t.id for t in chain}
        watch = sorted(
            (n for n in all_nodes
             if not n.children and n.status != "Completed"
             and n.id not in chain_ids and 0 < slack.get(n.id, 999) <= 5),
            key=lambda n: slack[n.id])[:3]
        if watch:
            rows = "<br>".join(
                f"â€¢ {_link(n)} â€” only <b>{slack[n.id]}d</b> of slack"
                for n in watch)
            lay.addWidget(self._label(
                f"<span style='color:#854F0B; font-weight:bold;'>Watch list:"
                f"</span> if these slip past their slack they join the chain "
                f"and push {_fmt(project_end)}.<br>{rows}", size=13))

        # Permission to ignore the rest
        open_leaves = [n for n in all_nodes
                       if not n.children and n.status != "Completed"]
        relaxed = [n for n in open_leaves
                   if n.id not in chain_ids
                   and n not in watch and slack.get(n.id, 999) > 5]
        if relaxed:
            lay.addWidget(self._label(
                f"<span style='color:#888'>The other {len(relaxed)} open "
                f"task(s) have comfortable slack â€” a few days there won't "
                f"move your end date.</span>", size=12))

    def _ready_to_start(self, all_nodes, node_map):
        ready = []
        for n in all_nodes:
            if n.children or n.status != "Not Started" or not self._waiting_matches(n):
                continue
            driver = self._driver_of(n, node_map)
            if driver is None or driver.status == "Completed":
                ready.append(n)
        return sorted(ready, key=lambda n: n.start_date or "")

    def _build_week_panel(self, all_nodes, node_map):
        self._clear(self.week_panel)
        lay = self.week_panel.layout()

        today = datetime.now().date()
        week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=6)
        next_start = week_start + timedelta(days=7)
        next_end = next_start + timedelta(days=6)

        closed_start = week_start - timedelta(days=14)

        overdue, due_week, starting, closed = [], [], [], []
        for n in all_nodes:
            if n.children or not self._waiting_matches(n):
                continue
            s, e = _parse(n.start_date), _parse(n.end_date)
            if n.status == "Completed":
                if e and closed_start <= e <= today:
                    closed.append(n)
                continue
            if e and e < today:
                overdue.append(n)
            elif e and today <= e <= week_end:
                due_week.append(n)
            elif s and next_start <= s <= next_end:
                starting.append(n)

        lay.addWidget(self._label("<b>This week</b>", size=14))

        ready = self._ready_to_start(all_nodes, node_map)
        cols = QHBoxLayout()
        cols.setSpacing(10)
        for title, color, items, date_of, prefix in (
            ("Ready to start", "#0B5C85", ready,
             lambda n: n.start_date, "starts"),
            ("Overdue", "#A32D2D", sorted(overdue, key=lambda n: n.end_date or ""),
             lambda n: n.end_date, "due"),
            ("Due this week", "#854F0B", sorted(due_week, key=lambda n: n.end_date or ""),
             lambda n: n.end_date, "due"),
            ("Starting next week", "#0F6E56", sorted(starting, key=lambda n: n.start_date or ""),
             lambda n: n.start_date, "starts"),
            (f"Closed since {closed_start.strftime('%b %d')}", "#5F5E5A",
             sorted(closed, key=lambda n: n.end_date or "", reverse=True),
             lambda n: n.end_date, "done"),
        ):
            card = QFrame()
            card.setStyleSheet(
                "QFrame { background: #ffffff; border-radius: 8px; }"
                "QLabel { background: transparent; }")
            clay = QVBoxLayout(card)
            clay.setContentsMargins(10, 8, 10, 8)
            clay.setSpacing(4)
            head = QLabel(title)
            head.setStyleSheet(
                f"font-size:12px; font-weight:bold; color:{color};")
            clay.addWidget(head)
            if items:
                for n in items[:8]:
                    waiting = f" - waiting on {n.waiting_on}" if n.waiting_on else ""
                    date_label = f"{prefix} {_fmt(date_of(n))}"
                    if title == "Due this week" and _parse(date_of(n)) == today:
                        date_label = "<b>today</b>"
                    clay.addWidget(self._label(
                        f"{_link(n)}<br><span style='color:#999; font-size:11px;'>"
                        f"{date_label}{waiting}</span>", size=12, node=n))
                if len(items) > 8:
                    clay.addWidget(self._label(
                        f"<span style='color:#999'>+{len(items) - 8} moreâ€¦</span>",
                        size=11))
            else:
                clay.addWidget(self._label(
                    "<span style='color:#bbb'>none</span>", size=12))
            clay.addStretch()
            cols.addWidget(card, 1)
        lay.addLayout(cols)


