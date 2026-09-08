"""
Gantt Chart Widget — Simplified Classic View

Two panels:
  Left  (260 px)  : TracePanel — shows the predecessor chain from the clicked
                    task, with slack annotations and BOTTLENECK labels.
  Right (scrollable): GanttTimeline — task names in a 200-px left margin;
                    horizontal status-coloured bars on the timeline to the right.

Controls
  • Zoom slider / +/- buttons
  • "Show Links" toggle  (dependency arrows, off by default)
  • "Clear Trace" button
  • Status legend (4 colours)

Click any task name or bar → trace panel fills with the predecessor chain.
Double-click any bar → jumps to Tracker tab and selects that task.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

from PyQt6.QtCore import Qt, QPoint, QPointF, QRect, QSize, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QLinearGradient,
                         QPainter, QPainterPath, QPalette, QPen, QPolygonF)
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                             QScrollArea, QSlider, QSplitter,
                             QTreeWidgetItemIterator, QVBoxLayout, QWidget)

from models.task_node import TaskNode
from utils.critical_path import CriticalPathAnalyzer

DATE_FMT = "%Y-%m-%d"

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
_C_COMPLETED   = QColor("#4CAF50")   # green
_C_IN_PROGRESS = QColor("#FFC107")   # amber
_C_NOT_STARTED = QColor("#5C8DB8")   # steel blue
_C_OVERDUE     = QColor("#E53935")   # red
_C_PARENT      = QColor("#546E7A")   # blue-grey, thin summary bar
_C_TRACE       = QColor("#FF9800")   # orange trace highlight
_C_TODAY       = QColor(255, 120, 0, 200)
_C_DEPEND      = QColor(110, 110, 120)
_C_NAME_BG     = QColor(26, 26, 32)
_C_NAME_ALT    = QColor(32, 32, 40)
_C_HEADER      = QColor(36, 36, 44)
_C_CANVAS      = QColor(28, 28, 35)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flatten(nodes: List[TaskNode]) -> List[TaskNode]:
    """Pre-order flatten of a task forest."""
    out: List[TaskNode] = []
    for n in nodes:
        out.append(n)
        if n.children:
            out.extend(_flatten(n.children))
    return out


def _depth(task: TaskNode) -> int:
    d, p = 0, task.parent
    while p:
        d += 1
        p = p.parent
    return d


def _is_overdue(task: TaskNode) -> bool:
    if task.status == "Completed" or not task.end_date:
        return False
    try:
        return datetime.strptime(task.end_date, DATE_FMT).date() < datetime.now().date()
    except ValueError:
        return False


def _bar_color(task: TaskNode) -> QColor:
    if _is_overdue(task):
        return QColor(_C_OVERDUE)
    if task.status == "Completed":
        return QColor(_C_COMPLETED)
    if task.status == "In Progress":
        return QColor(_C_IN_PROGRESS)
    return QColor(_C_NOT_STARTED)


# ---------------------------------------------------------------------------
# TracePanel
# ---------------------------------------------------------------------------

class TracePanel(QWidget):
    """
    Left panel: displays the predecessor chain trace for a clicked task.

    chain format (list of dicts):
        task         : TaskNode
        slack        : int (workdays of float)
        duration     : int (calendar days)
        is_bottleneck: bool (slack == 0 and it's a leaf)
    chain[0] = the clicked target; chain[-1] = the furthest upstream ancestor.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(260)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 10, 8, 8)
        layout.setSpacing(6)

        self._title = QLabel("TRACE PATH")
        self._title.setStyleSheet(
            "font-size: 10pt; font-weight: bold; color: #FFC107; padding: 2px 0;"
        )
        layout.addWidget(self._title)

        self._hint = QLabel(
            "Click any task name or bar\nto trace its predecessor chain."
        )
        self._hint.setStyleSheet("color: #777; font-size: 9pt;")
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background: #444; max-height: 1px;")
        layout.addWidget(sep)

        # Scroll area for chain items
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(scroll, 1)

        self._inner = QWidget()
        self._inner_layout = QVBoxLayout(self._inner)
        self._inner_layout.setContentsMargins(0, 0, 0, 0)
        self._inner_layout.setSpacing(3)
        self._inner_layout.addStretch()   # keeps items top-aligned
        scroll.setWidget(self._inner)

    # ------------------------------------------------------------------

    def update_trace(self, chain: List[Dict], target_name: str):
        self._title.setText(f"▶ {target_name}")
        self._hint.setVisible(False)
        self._clear_items()

        for entry in chain:
            task        = entry["task"]
            slack       = entry["slack"]
            dur         = entry["duration"]
            bottleneck  = entry["is_bottleneck"]

            icon  = "⚠" if bottleneck else "✓"
            sub   = "← BOTTLENECK" if bottleneck else f"{slack}d slack"
            color = "#E53935" if bottleneck else "#4CAF50"
            bg    = "#2d1010" if bottleneck else "#102010"
            border= "#E53935" if bottleneck else "#4CAF50"

            html = (
                f"<span style='font-size:10pt;color:{color};'>{icon}</span> "
                f"<b style='font-size:9pt;color:#ddd;'>{task.name}</b><br>"
                f"<span style='font-size:8pt;color:#999;'>{dur}d &nbsp;·&nbsp; {sub}</span>"
            )
            lbl = QLabel(html)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(
                f"background:{bg}; border-left:3px solid {border}; "
                f"padding:5px 6px; border-radius:2px;"
            )
            # Insert before the trailing stretch
            self._inner_layout.insertWidget(self._inner_layout.count() - 1, lbl)

    def clear_trace(self):
        self._title.setText("TRACE PATH")
        self._hint.setVisible(True)
        self._clear_items()

    def _clear_items(self):
        while self._inner_layout.count() > 1:          # keep the stretch
            item = self._inner_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()


# ---------------------------------------------------------------------------
# GanttTimeline canvas
# ---------------------------------------------------------------------------

class GanttTimeline(QWidget):
    """
    Scrollable Gantt canvas.

    Layout
    ------
    x ∈ [0, left_margin)          : task-name strip (painted by _draw_name_strip)
    x ∈ [left_margin, width)      : bar area  (painted by _draw_bars / _draw_grid …)
    y ∈ [0, header_height)        : header row (months + days)
    y ∈ [header_height, height)   : task rows
    """

    task_clicked        = pyqtSignal(TaskNode)
    task_double_clicked = pyqtSignal(TaskNode)

    # Geometry constants
    LEFT_MARGIN   = 200     # px reserved for task names
    ROW_HEIGHT    = 32
    HEADER_HEIGHT = 54

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(400)
        self.setMouseTracking(True)

        # Data
        self.tasks: List[TaskNode]          = []
        self.task_y_positions: Dict[str,int]= {}
        self.node_map: Dict[str, TaskNode]  = {}
        self.slack_data: Dict[str, int]     = {}

        # Timeline bounds
        self.start_date: Optional[datetime] = None
        self.end_date:   Optional[datetime] = None

        # Zoom
        self.pixels_per_day: int = 20

        # Interaction state
        self.hovered_task: Optional[TaskNode] = None
        self.traced_ids:  Set[str]            = set()
        self.show_dependencies: bool          = False   # off by default

        # Background fill
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, _C_CANVAS)
        self.setPalette(pal)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_tasks(self, root_nodes: List[TaskNode],
                   slack_data: Optional[Dict[str, int]] = None):
        self.tasks      = _flatten(root_nodes)
        self.node_map   = {t.id: t for t in self.tasks}
        self.slack_data = slack_data or {}

        self.task_y_positions = {}
        y = self.HEADER_HEIGHT + 5
        for task in self.tasks:
            self.task_y_positions[task.id] = y
            y += self.ROW_HEIGHT

        self._calc_bounds()
        self._update_size()
        self.update()

    def _calc_bounds(self):
        starts, ends = [], []
        for t in self.tasks:
            for attr, lst in ((t.start_date, starts), (t.end_date, ends)):
                if attr:
                    try:
                        lst.append(datetime.strptime(attr, DATE_FMT))
                    except ValueError:
                        pass
        if starts and ends:
            self.start_date = min(starts) - timedelta(days=7)
            self.end_date   = max(ends)   + timedelta(days=14)
        else:
            self.start_date = datetime.now()
            self.end_date   = datetime.now() + timedelta(days=30)

    def _update_size(self):
        if not self.start_date or not self.end_date:
            return
        w = self.LEFT_MARGIN + (self.end_date - self.start_date).days * self.pixels_per_day + 80
        h = len(self.tasks) * self.ROW_HEIGHT + self.HEADER_HEIGHT + 40
        self.setMinimumWidth(max(w, 600))
        self.setMinimumHeight(max(h, 400))

    def set_zoom(self, ppd: int):
        self.pixels_per_day = max(4, min(80, ppd))
        self._update_size()
        self.update()

    # ------------------------------------------------------------------
    # Coordinate helpers
    # ------------------------------------------------------------------

    def _x(self, date_str: str) -> Optional[int]:
        """Date string → pixel x.  Returns None if date is invalid."""
        if not self.start_date:
            return None
        try:
            d = datetime.strptime(date_str, DATE_FMT)
            return self.LEFT_MARGIN + int((d - self.start_date).days * self.pixels_per_day)
        except (ValueError, TypeError):
            return None

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        self._draw_grid(painter)
        self._draw_name_strip(painter)
        self._draw_timeline_header(painter)
        self._draw_today_marker(painter)
        self._draw_task_bars(painter)
        if self.show_dependencies:
            self._draw_dependencies(painter)

        # Divider between name strip and bar area
        painter.setPen(QPen(QColor(75, 75, 90), 1))
        painter.drawLine(self.LEFT_MARGIN, 0, self.LEFT_MARGIN, self.height())

    # ---- grid & weekend shading ----------------------------------------

    def _draw_grid(self, painter: QPainter):
        if not self.start_date or not self.end_date:
            return

        # Alternating rows + horizontal separators
        for i, task in enumerate(self.tasks):
            y = self.task_y_positions.get(task.id, 0)
            rect = QRect(self.LEFT_MARGIN, y,
                         self.width() - self.LEFT_MARGIN, self.ROW_HEIGHT)
            painter.fillRect(rect, _C_NAME_ALT if i % 2 == 1 else _C_CANVAS)
            painter.setPen(QPen(QColor(48, 48, 58), 1))
            painter.drawLine(self.LEFT_MARGIN, y + self.ROW_HEIGHT - 1,
                             self.width(),     y + self.ROW_HEIGHT - 1)

        # Weekend column shading
        cur = self.start_date
        while cur <= self.end_date:
            if cur.weekday() in (5, 6):
                x = self._x(cur.strftime(DATE_FMT))
                if x is not None:
                    painter.fillRect(
                        QRect(x, self.HEADER_HEIGHT,
                              self.pixels_per_day, self.height()),
                        QColor(45, 45, 52, 80)
                    )
            cur += timedelta(days=1)

    # ---- task name strip -----------------------------------------------

    def _draw_name_strip(self, painter: QPainter):
        # Column header
        painter.fillRect(0, 0, self.LEFT_MARGIN, self.HEADER_HEIGHT, _C_HEADER)
        painter.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        painter.setPen(QPen(QColor(180, 180, 190)))
        painter.drawText(
            QRect(8, 0, self.LEFT_MARGIN - 12, self.HEADER_HEIGHT),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            "Task"
        )

        name_font_bold   = QFont("Arial", 8, QFont.Weight.Bold)
        name_font_normal = QFont("Arial", 8, QFont.Weight.Normal)
        fm_bold   = QFontMetrics(name_font_bold)
        fm_normal = QFontMetrics(name_font_normal)

        for i, task in enumerate(self.tasks):
            y        = self.task_y_positions.get(task.id, 0)
            row_rect = QRect(0, y, self.LEFT_MARGIN, self.ROW_HEIGHT)

            # Background
            bg = _C_NAME_ALT if i % 2 == 1 else _C_NAME_BG
            painter.fillRect(row_rect, bg)

            # Trace highlight
            if task.id in self.traced_ids:
                painter.fillRect(row_rect, QColor(55, 38, 5))

            # Indentation
            indent = 6 + _depth(task) * 14
            avail  = self.LEFT_MARGIN - indent - 8

            # Collapse triangle hint for parents
            if task.children:
                painter.setFont(name_font_bold)
                fm = fm_bold
                text = fm.elidedText(task.name, Qt.TextElideMode.ElideRight, avail)
                color = QColor(255, 200, 80) if task.id in self.traced_ids \
                        else QColor(200, 200, 210)
            else:
                painter.setFont(name_font_normal)
                fm = fm_normal
                text = fm.elidedText(task.name, Qt.TextElideMode.ElideRight, avail)
                color = QColor(255, 200, 80) if task.id in self.traced_ids \
                        else QColor(190, 190, 195)

            painter.setPen(QPen(color))
            painter.drawText(
                QRect(indent, y, avail, self.ROW_HEIGHT),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                text
            )

            # Row separator
            painter.setPen(QPen(QColor(50, 50, 62), 1))
            painter.drawLine(0, y + self.ROW_HEIGHT - 1,
                             self.LEFT_MARGIN, y + self.ROW_HEIGHT - 1)

    # ---- timeline header -----------------------------------------------

    def _draw_timeline_header(self, painter: QPainter):
        if not self.start_date or not self.end_date:
            return

        painter.fillRect(self.LEFT_MARGIN, 0,
                         self.width() - self.LEFT_MARGIN, self.HEADER_HEIGHT,
                         _C_HEADER)

        cur = self.start_date
        while cur <= self.end_date:
            x = self._x(cur.strftime(DATE_FMT))
            if x is None:
                cur += timedelta(days=1)
                continue

            # Month label at month boundary
            if cur.day == 1 or cur == self.start_date:
                painter.setFont(QFont("Arial", 9, QFont.Weight.Bold))
                painter.setPen(QPen(QColor(200, 200, 210)))
                painter.drawText(QRect(x + 2, 4, 110, 22),
                                 Qt.AlignmentFlag.AlignLeft,
                                 cur.strftime("%b %Y"))

            # Day number (only when zoomed in enough)
            if self.pixels_per_day >= 12:
                painter.setFont(QFont("Arial", 7))
                painter.setPen(QPen(QColor(140, 140, 150)))
                painter.drawText(
                    QRect(x, 28, self.pixels_per_day, 20),
                    Qt.AlignmentFlag.AlignCenter,
                    str(cur.day)
                )

            # Monday tick mark
            if cur.weekday() == 0:
                painter.setPen(QPen(QColor(75, 75, 90), 1))
                painter.drawLine(x, self.HEADER_HEIGHT - 8, x, self.HEADER_HEIGHT)

            cur += timedelta(days=1)

        # Bottom border
        painter.setPen(QPen(QColor(80, 80, 95), 1))
        painter.drawLine(self.LEFT_MARGIN, self.HEADER_HEIGHT,
                         self.width(), self.HEADER_HEIGHT)

    # ---- today line ----------------------------------------------------

    def _draw_today_marker(self, painter: QPainter):
        x = self._x(datetime.now().strftime(DATE_FMT))
        if x is None or x < self.LEFT_MARGIN:
            return
        painter.setPen(QPen(_C_TODAY, 2))
        painter.drawLine(x, self.HEADER_HEIGHT, x, self.height())
        painter.setFont(QFont("Arial", 7, QFont.Weight.Bold))
        painter.setPen(QPen(_C_TODAY))
        painter.drawText(x + 3, self.HEADER_HEIGHT + 14, "TODAY")

    # ---- task bars -----------------------------------------------------

    def _draw_task_bars(self, painter: QPainter):
        fm = QFontMetrics(QFont("Arial", 8))

        for task in self.tasks:
            if not task.start_date or not task.end_date:
                continue

            xs = self._x(task.start_date)
            xe = self._x(task.end_date)
            if xs is None or xe is None:
                continue            # skip bad dates silently

            y    = self.task_y_positions.get(task.id, 0)
            bw   = max(xe - xs, 4)

            if task.children:
                # ── Summary / parent bar: thin, full children span, no text ──
                bh   = max(5, self.ROW_HEIGHT // 5)
                by   = y + (self.ROW_HEIGHT - bh) // 2
                rect = QRect(xs, by, bw, bh)
                painter.fillRect(rect, _C_PARENT)
                painter.setPen(QPen(_C_PARENT.darker(150), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(rect)

            else:
                # ── Leaf bar ──
                bh   = self.ROW_HEIGHT - 10
                by   = y + 5
                rect = QRect(xs, by, bw, bh)

                color = _bar_color(task)
                painter.setBrush(QBrush(color))
                painter.setPen(QPen(color.darker(140), 1))
                painter.drawRoundedRect(rect, 3, 3)

                # Trace amber outline
                if task.id in self.traced_ids:
                    painter.setPen(QPen(_C_TRACE, 2))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRoundedRect(rect.adjusted(-2, -2, 2, 2), 3, 3)

                # Label inside bar (if room)
                if bw > 20:
                    label = fm.elidedText(task.name,
                                          Qt.TextElideMode.ElideRight,
                                          bw - 6)
                    painter.setFont(QFont("Arial", 8))
                    painter.setPen(QPen(QColor(255, 255, 255, 210)))
                    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

    # ---- dependency arrows ---------------------------------------------

    def _draw_dependencies(self, painter: QPainter):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        for task in self.tasks:
            pred = self._find_predecessor(task)
            if not pred or not pred.end_date or not task.start_date:
                continue

            px = self._x(pred.end_date)
            tx = self._x(task.start_date)
            if px is None or tx is None:
                continue

            py = self.task_y_positions.get(pred.id, 0) + self.ROW_HEIGHT // 2
            ty = self.task_y_positions.get(task.id,  0) + self.ROW_HEIGHT // 2

            in_trace = (task.id in self.traced_ids and pred.id in self.traced_ids)
            color    = _C_TRACE if in_trace else _C_DEPEND
            width    = 2 if in_trace else 1

            path = QPainterPath()
            path.moveTo(px, py)
            mx = (px + tx) / 2
            if abs(ty - py) < 4:
                path.lineTo(tx - 8, ty)
            else:
                path.lineTo(mx, py)
                path.lineTo(mx, ty)
                path.lineTo(tx - 8, ty)

            painter.setPen(QPen(color, width))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

            # Arrowhead
            sz  = 7
            pts = QPolygonF([
                QPointF(tx - 8,      ty),
                QPointF(tx - 8 - sz, ty - sz // 2),
                QPointF(tx - 8 - sz, ty + sz // 2),
            ])
            painter.setBrush(QBrush(color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPolygon(pts)

    # ---- predecessor lookup (shared by bars + arrows) ------------------

    def _find_predecessor(self, task: TaskNode) -> Optional[TaskNode]:
        mode = (getattr(task, "start_rule", None) or {}).get("mode", "automatic")
        if mode == "fixed":
            return None
        if mode in ("same_as", "continue_after"):
            target_id = (task.start_rule or {}).get("task_id") or task.predecessor_id
            return self.node_map.get(target_id)
        if task.predecessor_id and task.predecessor_id in self.node_map:
            return self.node_map[task.predecessor_id]
        if task.parent and not task.is_parallel:
            siblings = task.parent.children
            try:
                idx = siblings.index(task)
                if idx > 0:
                    return siblings[idx - 1]
            except ValueError:
                pass
        return None

    # ------------------------------------------------------------------
    # Mouse events
    # ------------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            task = self._task_at(event.pos())
            if task:
                self.task_clicked.emit(task)

    def mouseDoubleClickEvent(self, event):
        task = self._task_at(event.pos())
        if task:
            self.task_double_clicked.emit(task)

    def mouseMoveEvent(self, event):
        task = self._task_at(event.pos())
        if task != self.hovered_task:
            self.hovered_task = task
            self.setToolTip(self._tooltip(task) if task else "")

    def _task_at(self, pos: QPoint) -> Optional[TaskNode]:
        """Return the task under cursor (name strip or bar area)."""
        x, y = pos.x(), pos.y()
        for task in self.tasks:
            ty = self.task_y_positions.get(task.id, 0)
            if not (ty <= y < ty + self.ROW_HEIGHT):
                continue
            # Name strip click — any x in [0, LEFT_MARGIN) hits
            if x < self.LEFT_MARGIN:
                return task
            # Bar area click
            if task.start_date and task.end_date:
                xs = self._x(task.start_date)
                xe = self._x(task.end_date)
                if xs is not None and xe is not None:
                    if xs - 4 <= x <= max(xe, xs + 8):
                        return task
        return None

    def _tooltip(self, task: TaskNode) -> str:
        slack = self.slack_data.get(task.id, 0)
        over  = "  ⚠ Overdue" if _is_overdue(task) else ""
        lines = [
            f"<b>{task.name}</b>",
            f"Status: {task.status}{over}",
            f"Start: {task.start_date or 'N/A'}",
            f"End:   {task.end_date or 'N/A'}",
            f"Duration: {task.duration} days",
            f"Slack: {slack} days",
        ]
        if task.owner:
            lines.append(f"Owner: {task.owner}")
        return "<br>".join(lines)

    # ------------------------------------------------------------------
    # Trace helpers called from GanttChartWidget
    # ------------------------------------------------------------------

    def highlight_trace(self, task_ids: Set[str]):
        self.traced_ids = task_ids
        self.update()

    def clear_trace(self):
        self.traced_ids = set()
        self.update()


# ---------------------------------------------------------------------------
# GanttChartWidget — top-level widget
# ---------------------------------------------------------------------------

class GanttChartWidget(QWidget):
    """
    Public interface (called from project_widget.py):
        load_nodes(root_nodes)   — refresh the whole chart
        main_window              — set by ProjectWidget so double-click can
                                   switch to the Tracker tab
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.main_window = None
        self.root_nodes:  List[TaskNode]    = []
        self.node_map:    Dict[str, TaskNode] = {}
        self.slack_data:  Dict[str, int]    = {}
        self._setup_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Create timeline first so _build_toolbar() can reference it
        self.timeline = GanttTimeline()
        self.timeline.task_clicked.connect(self._on_task_clicked)
        self.timeline.task_double_clicked.connect(self._on_task_double_clicked)

        root.addWidget(self._build_toolbar())

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left panel
        self.trace_panel = TracePanel()
        splitter.addWidget(self.trace_panel)

        # Right panel
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self._scroll.setWidget(self.timeline)
        splitter.addWidget(self._scroll)

        splitter.setSizes([260, 900])
        root.addWidget(splitter)

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(40)
        bar.setStyleSheet(
            "background: #222230; border-bottom: 1px solid #444;"
        )
        h = QHBoxLayout(bar)
        h.setContentsMargins(10, 4, 10, 4)
        h.setSpacing(8)

        # Zoom
        lbl = QLabel("Zoom:")
        lbl.setStyleSheet("color:#ccc;")
        h.addWidget(lbl)

        btn_out = QPushButton("−")
        btn_out.setFixedWidth(28)
        btn_out.setStyleSheet("QPushButton { color:#ccc; background:#333; border:1px solid #555; }")
        btn_out.clicked.connect(lambda: self._zoom(-5))
        h.addWidget(btn_out)

        self._zoom_slider = QSlider(Qt.Orientation.Horizontal)
        self._zoom_slider.setMinimum(4)
        self._zoom_slider.setMaximum(80)
        self._zoom_slider.setValue(20)
        self._zoom_slider.setFixedWidth(140)
        self._zoom_slider.valueChanged.connect(self.timeline.set_zoom)
        h.addWidget(self._zoom_slider)

        btn_in = QPushButton("+")
        btn_in.setFixedWidth(28)
        btn_in.setStyleSheet("QPushButton { color:#ccc; background:#333; border:1px solid #555; }")
        btn_in.clicked.connect(lambda: self._zoom(5))
        h.addWidget(btn_in)

        h.addSpacing(16)

        # Show Links toggle
        self._links_btn = QPushButton("Show Links")
        self._links_btn.setCheckable(True)
        self._links_btn.setChecked(False)
        self._links_btn.setStyleSheet(
            "QPushButton { color:#ccc; background:#333; border:1px solid #555; padding:2px 8px; }"
            "QPushButton:checked { background:#3a3a6a; border-color:#6060aa; }"
        )
        self._links_btn.toggled.connect(self._toggle_links)
        h.addWidget(self._links_btn)

        # Clear Trace button
        clr = QPushButton("Clear Trace")
        clr.setStyleSheet(
            "QPushButton { color:#ccc; background:#333; border:1px solid #555; padding:2px 8px; }"
            "QPushButton:hover { background:#444; }"
        )
        clr.clicked.connect(self._clear_trace)
        h.addWidget(clr)

        h.addStretch()

        # Legend
        h.addWidget(self._build_legend())

        return bar

    @staticmethod
    def _build_legend() -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        for color, label in (
            ("#4CAF50", "Completed"),
            ("#FFC107", "In Progress"),
            ("#5C8DB8", "Not Started"),
            ("#E53935", "Overdue"),
        ):
            dot = QLabel("●")
            dot.setStyleSheet(f"color:{color}; font-size:14px;")
            lbl = QLabel(label)
            lbl.setStyleSheet("color:#bbb; font-size:8pt;")
            h.addWidget(dot)
            h.addWidget(lbl)
        return w

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_nodes(self, root_nodes: List[TaskNode]):
        """Refresh the Gantt chart with the current task forest."""
        if not root_nodes:
            return

        self.root_nodes = root_nodes
        self.node_map   = {n.id: n for n in _flatten(root_nodes)}

        # Critical path analysis for slack data
        try:
            analyzer        = CriticalPathAnalyzer(root_nodes)
            results         = analyzer.analyze()
            self.slack_data = results.get("slack", {})
        except Exception:
            self.slack_data = {}

        self.timeline.load_tasks(root_nodes, self.slack_data)
        self._clear_trace()

    # ------------------------------------------------------------------
    # Toolbar handlers
    # ------------------------------------------------------------------

    def _zoom(self, delta: int):
        v = self._zoom_slider.value() + delta
        self._zoom_slider.setValue(max(4, min(80, v)))

    def _toggle_links(self, checked: bool):
        self.timeline.show_dependencies = checked
        self.timeline.update()

    def _clear_trace(self):
        self.timeline.clear_trace()
        self.trace_panel.clear_trace()

    # ------------------------------------------------------------------
    # Trace — clicked task
    # ------------------------------------------------------------------

    def _on_task_clicked(self, task: TaskNode):
        chain      = self._compute_trace(task)
        traced_ids = {e["task"].id for e in chain}
        self.timeline.highlight_trace(traced_ids)
        self.trace_panel.update_trace(chain, task.name)

    def _compute_trace(self, target: TaskNode) -> List[Dict]:
        """
        Walk backwards through the predecessor chain starting at *target*.

        For each node we record:
            task         TaskNode
            duration     calendar days (end - start + 1)
            slack        workdays of float from CriticalPathAnalyzer
            is_bottleneck True when slack == 0 and the node is a leaf task

        chain[0] = target (the clicked task)
        chain[-1] = furthest upstream anchor
        """
        chain: List[Dict] = []
        visited: Set[str] = set()
        current: Optional[TaskNode] = target

        while current and current.id not in visited:
            visited.add(current.id)

            dur = 0
            if current.start_date and current.end_date:
                try:
                    s   = datetime.strptime(current.start_date, DATE_FMT)
                    e   = datetime.strptime(current.end_date,   DATE_FMT)
                    dur = (e - s).days + 1
                except ValueError:
                    pass

            slack = self.slack_data.get(current.id, 0)
            chain.append({
                "task":         current,
                "duration":     dur,
                "slack":        slack,
                "is_bottleneck": slack == 0 and not current.children,
            })

            # Find upstream predecessor
            pred: Optional[TaskNode] = None
            mode = (getattr(current, "start_rule", None) or {}).get("mode", "automatic")
            if mode == "fixed":
                pred = None
            elif mode in ("same_as", "continue_after"):
                target_id = (current.start_rule or {}).get("task_id") or current.predecessor_id
                pred = self.node_map.get(target_id)
            elif current.predecessor_id and current.predecessor_id in self.node_map:
                pred = self.node_map[current.predecessor_id]
            elif current.parent:
                sibs = current.parent.children
                try:
                    idx = sibs.index(current)
                    if idx > 0 and not current.is_parallel:
                        pred = sibs[idx - 1]
                    elif idx == 0:
                        # First child — walk up to parent (if not already visited)
                        p = current.parent
                        if p.id not in visited:
                            pred = p
                except ValueError:
                    pass

            current = pred

        return chain

    # ------------------------------------------------------------------
    # Double-click → jump to Tracker tab
    # ------------------------------------------------------------------

    def _on_task_double_clicked(self, task: TaskNode):
        if not self.main_window:
            return
        try:
            self.main_window.inner_tabs.setCurrentIndex(0)
        except AttributeError:
            pass
        self._select_in_tree(task)

    def _select_in_tree(self, task: TaskNode):
        if not self.main_window:
            return
        tree = self.main_window.tree_view
        it   = QTreeWidgetItemIterator(tree)
        while it.value():
            item      = it.value()
            item_task = item.data(0, Qt.ItemDataRole.UserRole)
            if item_task and getattr(item_task, "id", None) == task.id:
                tree.setCurrentItem(item)
                tree.scrollToItem(item)
                for col in range(tree.columnCount()):
                    item.setBackground(col, QColor(255, 255, 0, 80))
                break
            it += 1
