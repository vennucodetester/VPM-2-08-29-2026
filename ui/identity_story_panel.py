"""Reusable graphical timeline and connections view for Identity Stories."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QScrollArea, QTabWidget, QToolTip,
    QVBoxLayout, QWidget,
)

from utils.identity_story import (
    IdentityStory, event_context_label, shortest_unique_event_labels,
)


DATE_FMT = "%Y-%m-%d"


def _date(value):
    return datetime.strptime(value, DATE_FMT).date()


class IdentityTimeline(QWidget):
    eventSelected = pyqtSignal(object)
    jumpRequested = pyqtSignal(str, str)
    overlapSelected = pyqtSignal(object)

    LEFT = 330
    RIGHT = 24
    TOP = 60
    LANE = 62
    UNSCHEDULED_LANE = 42
    empty_message = "No scheduled usage for this identity"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.story = IdentityStory("", "Identity", "identity")
        self._bar_hits = []
        self._unscheduled_hits = []
        self._overlap_hits = []
        self._selected_event_ids = set()
        self._event_labels = {}
        self.setMouseTracking(True)
        self.setMinimumWidth(620)

    def set_story(self, story):
        self.story = story
        self._selected_event_ids.clear()
        self._event_labels = shortest_unique_event_labels(story.all_events)
        height = (self.TOP + max(1, len(story.events)) * self.LANE + 45 +
                  max(1, len(story.unscheduled_events)) *
                  self.UNSCHEDULED_LANE)
        self.setMinimumHeight(max(260, height))
        self.updateGeometry()
        self.update()

    def _date_range(self):
        if not self.story.events:
            today = date.today()
            return today - timedelta(days=7), today + timedelta(days=21)
        first = min(_date(value.start_date) for value in self.story.events)
        last = max(_date(value.end_date) for value in self.story.events)
        padding = max(2, min(10, (last - first).days // 12))
        return first - timedelta(days=padding), last + timedelta(days=padding)

    def geometry_snapshot(self, width=None):
        """Pure geometry used by paint and headless interaction tests."""
        width = max(width or self.width(), self.LEFT + 220)
        first, last = self._date_range()
        span = max(1, (last - first).days + 1)
        chart_width = max(1, width - self.LEFT - self.RIGHT)

        def x_of(value):
            return self.LEFT + int((_date(value) - first).days / span * chart_width)

        bars = []
        event_rows = {}
        for index, event in enumerate(self.story.events):
            y = self.TOP + index * self.LANE + 24
            x1 = x_of(event.start_date)
            x2 = self.LEFT + int(((_date(event.end_date) - first).days + 1)
                                 / span * chart_width)
            rect = QRect(x1, y, max(5, x2 - x1), 18)
            bars.append((rect, event))
            event_rows[event.event_id] = (rect, event)
        overlaps = []
        for overlap in self.story.overlaps:
            first_row = event_rows.get(overlap.first_event_id)
            second_row = event_rows.get(overlap.second_event_id)
            if not first_row or not second_row:
                continue
            x1 = x_of(overlap.overlap_start)
            x2 = self.LEFT + int(((_date(overlap.overlap_end) - first).days + 1)
                                 / span * chart_width)
            top = min(first_row[0].top(), second_row[0].top()) - 5
            bottom = max(first_row[0].bottom(), second_row[0].bottom()) + 5
            band = QRect(x1, top, max(4, x2 - x1), bottom - top)
            overlaps.append((band, overlap))
        unscheduled = []
        y = self.TOP + max(1, len(self.story.events)) * self.LANE + 42
        for index, event in enumerate(self.story.unscheduled_events):
            unscheduled.append((QRect(
                18, y + index * self.UNSCHEDULED_LANE,
                width - 36, self.UNSCHEDULED_LANE - 4), event))
        return {"range": (first, last), "bars": bars,
                "overlaps": overlaps, "unscheduled": unscheduled}

    def paintEvent(self, event):
        snapshot = self.geometry_snapshot()
        self._bar_hits = snapshot["bars"]
        self._overlap_hits = snapshot["overlaps"]
        self._unscheduled_hits = snapshot["unscheduled"]
        first, last = snapshot["range"]
        span = max(1, (last - first).days + 1)
        chart_width = max(1, self.width() - self.LEFT - self.RIGHT)

        def x_of(value):
            current = value if isinstance(value, date) else _date(value)
            return self.LEFT + int((current - first).days / span * chart_width)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        painter.setPen(QColor("#344054"))
        painter.setFont(QFont(self.font().family(), 9, QFont.Weight.Bold))
        painter.drawText(12, 24, f"Story: {self.story.identity_label}")
        painter.setFont(QFont(self.font().family(), 8))
        painter.setPen(QColor("#667085"))
        painter.drawText(self.LEFT, 26, first.strftime("%b %d, %Y"))
        painter.drawText(self.width() - 120, 26, 105, 18,
                         Qt.AlignmentFlag.AlignRight, last.strftime("%b %d, %Y"))

        if not self.story.events:
            painter.setPen(QColor("#475467"))
            painter.drawText(QRect(12, 55, self.width() - 24, 70),
                             Qt.AlignmentFlag.AlignCenter,
                             self.empty_message)
        else:
            for day_offset in range(span):
                current = first + timedelta(days=day_offset)
                if current.weekday() == 0:
                    x = x_of(current)
                    painter.setPen(QPen(QColor("#eaecf0"), 1))
                    painter.drawLine(x, 35, x,
                                     self.TOP + len(self.story.events) * self.LANE)
            for index, (rect, event_value) in enumerate(snapshot["bars"]):
                lane_top = self.TOP + index * self.LANE
                if index % 2:
                    painter.fillRect(0, lane_top, self.width(), self.LANE,
                                     QColor("#f8fafc"))
                label_rect = QRect(12, lane_top + 5, self.LEFT - 24, 20)
                context_rect = QRect(12, lane_top + 27, self.LEFT - 24, 17)
                painter.setPen(QColor("#344054"))
                painter.setFont(QFont(
                    self.font().family(), 8, QFont.Weight.DemiBold))
                label = self._event_labels.get(
                    event_value.event_id, event_value.task_name)
                label = painter.fontMetrics().elidedText(
                    label, Qt.TextElideMode.ElideMiddle, label_rect.width())
                painter.drawText(label_rect, Qt.AlignmentFlag.AlignVCenter, label)
                painter.setFont(QFont(self.font().family(), 7))
                painter.setPen(QColor("#667085"))
                context = painter.fontMetrics().elidedText(
                    event_context_label(event_value),
                    Qt.TextElideMode.ElideRight, context_rect.width())
                painter.drawText(context_rect, Qt.AlignmentFlag.AlignVCenter,
                                 context)
                color = (QColor("#6b9f73") if event_value.status == "Completed"
                         else QColor("#60a5fa"))
                painter.setPen(QPen(color.darker(125), 1))
                painter.setBrush(color)
                painter.drawRoundedRect(rect, 3, 3)
                if event_value.event_id in self._selected_event_ids:
                    painter.setPen(QPen(QColor("#111827"), 3))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRoundedRect(rect.adjusted(-2, -2, 2, 2), 4, 4)

            # Bands first communicate which lanes share the interval; opaque
            # red segments then make the exact date range unmistakable.
            for band, overlap in snapshot["overlaps"]:
                painter.fillRect(band, QColor(239, 68, 68, 35))
                involved = {overlap.first_event_id, overlap.second_event_id}
                for rect, event_value in snapshot["bars"]:
                    if event_value.event_id not in involved:
                        continue
                    segment = QRect(band.left(), rect.top(), band.width(), rect.height())
                    painter.fillRect(segment, QColor("#dc2626"))
                painter.setPen(QColor("#991b1b"))
                painter.drawText(band.left() + 3, band.top() - 2,
                                 f"Overlap {overlap.overlap_start}–{overlap.overlap_end}")

        today = date.today()
        if first <= today <= last:
            x = x_of(today)
            painter.setPen(QPen(QColor("#111827"), 1, Qt.PenStyle.DashLine))
            painter.drawLine(x, 32, x,
                             self.TOP + max(1, len(self.story.events)) * self.LANE)
            painter.drawText(x + 3, 45, "Today")

        heading_y = self.TOP + max(1, len(self.story.events)) * self.LANE + 30
        painter.setFont(QFont(self.font().family(), 8, QFont.Weight.Bold))
        painter.setPen(QColor("#344054"))
        painter.drawText(14, heading_y, "Unscheduled")
        painter.setFont(QFont(self.font().family(), 8))
        if not self.story.unscheduled_events:
            painter.setPen(QColor("#98a2b3"))
            painter.drawText(108, heading_y, "None")
        for rect, event_value in snapshot["unscheduled"]:
            painter.fillRect(rect, QColor("#f2f4f7"))
            painter.setPen(QColor("#475467"))
            painter.setFont(QFont(
                self.font().family(), 8, QFont.Weight.DemiBold))
            label_rect = QRect(rect.left() + 8, rect.top() + 2,
                               rect.width() - 13, 17)
            label = self._event_labels.get(
                event_value.event_id, event_value.task_name)
            label = painter.fontMetrics().elidedText(
                label, Qt.TextElideMode.ElideMiddle, label_rect.width())
            painter.drawText(label_rect, Qt.AlignmentFlag.AlignVCenter, label)
            painter.setFont(QFont(self.font().family(), 7))
            painter.setPen(QColor("#667085"))
            context_rect = QRect(rect.left() + 8, rect.top() + 19,
                                 rect.width() - 13, 16)
            context = painter.fontMetrics().elidedText(
                event_context_label(event_value),
                Qt.TextElideMode.ElideRight, context_rect.width())
            painter.drawText(context_rect, Qt.AlignmentFlag.AlignVCenter,
                             context)
        painter.end()

    def _event_at(self, point):
        for rect, event_value in self._bar_hits + self._unscheduled_hits:
            if rect.contains(point):
                return event_value
        return None

    def mouseMoveEvent(self, event):
        value = self._event_at(event.position().toPoint())
        if value:
            connections = ", ".join(
                f"{item.kind}: {item.label}" for item in value.connected_tokens)
            text = (f"{value.project_name}\n{value.task_path}\n"
                    f"{value.start_date or 'Unscheduled'} → "
                    f"{value.end_date or 'Unscheduled'}\n{value.status}")
            if connections:
                text += f"\nConnected: {connections}"
            QToolTip.showText(event.globalPosition().toPoint(), text, self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        point = event.position().toPoint()
        for rect, overlap in self._overlap_hits:
            if rect.contains(point):
                self._selected_event_ids = {
                    overlap.first_event_id, overlap.second_event_id}
                self.overlapSelected.emit(overlap)
                self.update()
                event.accept()
                return
        value = self._event_at(point)
        if value:
            self._selected_event_ids = {value.event_id}
            self.eventSelected.emit(value)
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        value = self._event_at(event.position().toPoint())
        if value:
            self.jumpRequested.emit(value.project_id, value.task_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class IdentityConnectionsView(QWidget):
    """Focused selected-identity → activity → connected-identity graph."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.story = IdentityStory("", "Identity", "identity")
        self._event_labels = {}
        self.setMinimumHeight(280)

    def set_story(self, story):
        self.story = story
        self._event_labels = shortest_unique_event_labels(story.all_events)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        center = QPoint(max(120, self.width() // 2), 50)
        painter.setPen(QPen(QColor("#2563eb"), 2))
        painter.setBrush(QColor("#dbeafe"))
        painter.drawRoundedRect(QRect(center.x() - 90, 24, 180, 42), 8, 8)
        painter.setPen(QColor("#1e3a8a"))
        painter.drawText(QRect(center.x() - 84, 27, 168, 36),
                         Qt.AlignmentFlag.AlignCenter,
                         self.story.identity_label)
        events = self.story.all_events
        if not events:
            painter.setPen(QColor("#667085"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "No activity connections")
            return
        event_y = 105
        event_gap = max(44, min(70, (self.height() - 130) // max(1, len(events))))
        identity_positions = {}
        connections = self.story.connected_identities
        right_x = max(center.x() + 170, self.width() - 220)
        for index, connected in enumerate(connections):
            y = 105 + index * max(40, min(62, (self.height() - 130) //
                                               max(1, len(connections))))
            identity_positions[connected.identity_id] = QPoint(right_x, y + 16)
            painter.setPen(QPen(QColor("#2563eb"), 1))
            painter.setBrush(QColor("#eff6ff"))
            painter.drawRoundedRect(QRect(right_x - 85, y, 170, 32), 6, 6)
            painter.setPen(QColor("#1e3a8a"))
            painter.drawText(QRect(right_x - 80, y + 2, 160, 28),
                             Qt.AlignmentFlag.AlignCenter,
                             f"{connected.kind}: {connected.label}")
        for index, activity in enumerate(events):
            y = event_y + index * event_gap
            left_x = min(center.x() - 190, 180)
            rect = QRect(left_x - 78, y, 156, 32)
            painter.setPen(QPen(QColor("#64748b"), 1))
            painter.setBrush(QColor("#f1f5f9"))
            painter.drawRoundedRect(rect, 6, 6)
            painter.setPen(QColor("#334155"))
            label = self._event_labels.get(activity.event_id,
                                           activity.task_name)
            label = painter.fontMetrics().elidedText(
                label, Qt.TextElideMode.ElideMiddle, rect.width() - 8)
            painter.drawText(rect.adjusted(4, 1, -4, -1),
                             Qt.AlignmentFlag.AlignCenter, label)
            painter.setPen(QPen(QColor("#94a3b8"), 1))
            painter.drawLine(center.x() - 70, 66, rect.center().x(), rect.top())
            for connected in activity.connected_tokens:
                target = identity_positions.get(connected.identity_id)
                if target:
                    painter.drawLine(rect.right(), rect.center().y(),
                                     target.x() - 85, target.y())
        painter.end()


class IdentityStoryPanel(QWidget):
    jumpRequested = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.heading = QLabel(
            "Double-click a case, room, cassette, or other identity to see its story.")
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)
        self.tabs = QTabWidget()
        self.timeline = IdentityTimeline()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.timeline)
        self.connections = IdentityConnectionsView()
        self.tabs.addTab(scroll, "Timeline")
        self.tabs.addTab(self.connections, "Connections")
        self.tabs.hide()
        layout.addWidget(self.tabs, 1)
        self.details = QLabel("")
        self.details.setWordWrap(True)
        self.details.setStyleSheet(
            "QLabel { background:#f8fafc; border-top:1px solid #d0d5dd; padding:6px; }")
        self.details.hide()
        layout.addWidget(self.details)
        self.timeline.eventSelected.connect(self._show_event)
        self.timeline.overlapSelected.connect(self._show_overlap)
        self.timeline.jumpRequested.connect(self.jumpRequested)

    def set_story(self, story):
        self.heading.setText(
            f"Story: {story.identity_label} · {len(story.events)} scheduled · "
            f"{len(story.unscheduled_events)} unscheduled · "
            f"{len(story.overlaps)} overlap(s)")
        self.timeline.set_story(story)
        self.connections.set_story(story)
        self.tabs.show()
        self.details.hide()

    def _show_event(self, event):
        connected = ", ".join(
            f"{value.kind}: {value.label}" for value in event.connected_tokens)
        context = (f" · {len(event.child_context)} child row(s)" if
                   event.child_context else "")
        self.details.setText(
            f"{event.project_name} · {event.task_path}{context}\n"
            f"{event.start_date or 'Unscheduled'} → "
            f"{event.end_date or 'Unscheduled'} · {event.status}"
            + (f"\nConnected: {connected}" if connected else ""))
        self.details.show()

    def _show_overlap(self, overlap):
        self.details.setText(
            f"Overlap: {overlap.overlap_start} through {overlap.overlap_end}\n"
            "Both involved activities are highlighted.")
        self.details.show()


class IdentityStoryDialog(QDialog):
    jumpRequested = pyqtSignal(str, str)

    def __init__(self, story, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Identity Story — {story.identity_label}")
        self.resize(1050, 680)
        layout = QVBoxLayout(self)
        self.panel = IdentityStoryPanel()
        self.panel.set_story(story)
        self.panel.jumpRequested.connect(self.jumpRequested)
        layout.addWidget(self.panel, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
