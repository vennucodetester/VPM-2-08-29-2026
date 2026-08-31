"""
TimelinePane v3 — Gantt welded to the Tracker grid, redesigned around
two ideas after v2's hierarchy cues failed on deep trees:

1. Outline-level buttons (1 / 2 / 3 / All) — one click collapses the
   grid (and therefore the chart) to that depth. The readable
   "management chart" is the level-2/3 view, exactly like MS Project's
   Outline Level dropdown.

2. Groups draw as nested CONTAINER BOXES, not summary brackets:
   a light rounded outline wrapping the group's children rows,
   inset a few px per depth — real visual indentation. A COLLAPSED
   group draws as one fat slate bar (the clean phase bar).

Alignment contract: every paint asks Qt where the grid's viewport is
on screen (mapToGlobal), so bars sit pixel-exact beside their rows.
The controls strip lives BELOW the chart.
"""
from datetime import datetime, date, timedelta

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLabel, QScrollArea, QFrame, QFileDialog,
                             QToolTip)
from PyQt6.QtCore import Qt, QRect, QSize, QPoint, QPointF
from PyQt6.QtGui import QPainter, QColor, QPen, QPixmap, QPolygonF, QFont

DATE_FMT = "%Y-%m-%d"

C_DONE = QColor("#43A047")
C_PROGRESS = QColor("#FFB300")
C_NOT_STARTED = QColor("#64B5F6")
C_OVERDUE = QColor("#E53935")
C_SUMMARY = QColor("#37474F")     # collapsed group bar
C_BOX = QColor("#90A4AE")         # expanded group outline
C_BOX_FILL = QColor(55, 71, 79, 6)
C_MILESTONE = QColor("#37474F")
C_TODAY = QColor("#FF6D00")
C_LINK = QColor("#78909C")
C_LABEL = QColor("#37474F")
C_SLIP = QColor(120, 120, 120, 150)   # ghost baseline bar (H3)
C_SLIP_LATE = QColor("#C62828")       # +Nd text when a task slipped later
C_CRITICAL = QColor("#B71C1C")        # critical-chain outline (H4)
C_BAND = QColor(0, 0, 0, 9)
C_MONTH_LINE = QColor(0, 0, 0, 38)
C_WEEK_LINE = QColor(0, 0, 0, 14)

LEFT_PAD = 18
RIGHT_PAD = 170  # room for labels after the last bar


def _parse(s):
    try:
        return datetime.strptime(s, DATE_FMT).date()
    except (ValueError, TypeError):
        return None


def _is_inbox(node):
    """True if this node is the Inbox notes group or lives inside it.

    The Inbox is a dateless root named 'inbox' used purely for quick notes.
    It is skipped everywhere in the timeline so notes never draw as bars.
    """
    n = node
    while n is not None:
        if n.parent is None and (n.name or "").strip().lower() == "inbox":
            return True
        n = n.parent
    return False


def _depth(node):
    d = 0
    n = node.parent
    while n is not None:
        d += 1
        n = n.parent
    return d


def _is_late(node, today):
    """Red means exactly one thing: past its end date and not Completed.
    Slip vs. baseline is PRIVATE (opt-in via the Slips toggle, H3) and must
    never surface through color — house rule #3."""
    e = _parse(node.end_date)
    return node.status != "Completed" and e is not None and e < today


def _progress_parts(node):
    """Duration-weighted completed leaf work for node's dated descendants."""
    if _is_inbox(node):
        return 0, 0
    if not node.children:
        if not _parse(node.start_date) or not _parse(node.end_date):
            return 0, 0
        try:
            weight = max(int(node.duration), 1)
        except (TypeError, ValueError):
            weight = 1
        return (weight if node.status == "Completed" else 0), weight
    done = total = 0
    for child in node.children:
        d, t = _progress_parts(child)
        done += d
        total += t
    return done, total


def progress_fraction(node):
    """Completed work fraction, weighted by leaf workday duration."""
    done, total = _progress_parts(node)
    if total <= 0:
        return None
    return done / total


def _progress_fraction_for_roots(roots):
    done = total = 0
    for root in roots:
        d, t = _progress_parts(root)
        done += d
        total += t
    if total <= 0:
        return None
    return done / total


def _status_color(node, today):
    e = _parse(node.end_date)
    if node.status == "Completed":
        return C_DONE
    if _is_late(node, today):
        return C_OVERDUE
    if node.status == "In Progress":
        return C_PROGRESS
    return C_NOT_STARTED


class TimelineCanvas(QWidget):
    def __init__(self, tree, parent=None):
        super().__init__(parent)
        self._tree = tree
        self.px_per_day = 0.0     # 0 = fit to width
        self.show_labels = True
        self.show_links = True
        self.show_slips = False   # H3 — off by default, never persisted on
        self.show_critical = False  # H4 — off by default
        self._critical_ids = set()  # populated only while show_critical
        self._hits = []           # [(QRect, node)]
        self.setMouseTracking(True)
        self.setMinimumWidth(160)

    # ---------------- time range ----------------
    def _date_range(self):
        lo = hi = None
        stack = [r for r in self._tree.root_nodes if not _is_inbox(r)]
        while stack:
            n = stack.pop()
            stack.extend(n.children)
            s, e = _parse(n.start_date), _parse(n.end_date)
            if s and (lo is None or s < lo):
                lo = s
            if e and (hi is None or e > hi):
                hi = e
        if lo is None:
            today = date.today()
            return today - timedelta(days=7), today + timedelta(days=21)
        return lo - timedelta(days=4), hi + timedelta(days=10)

    def _effective_ppd(self, days):
        if self.px_per_day > 0:
            return self.px_per_day
        avail = max(self.parentWidget().width() if self.parentWidget()
                    else self.width(), 240) - LEFT_PAD - RIGHT_PAD
        return max(0.5, avail / max(days, 1))

    def content_width(self):
        t0, t1 = self._date_range()
        days = (t1 - t0).days + 1
        return int(LEFT_PAD + days * self._effective_ppd(days) + RIGHT_PAD)

    def sizeHint(self):
        return QSize(self.content_width(), 240)

    def refresh(self):
        self._recompute_critical()
        self.setMinimumWidth(self.content_width())
        self.updateGeometry()
        self.update()

    # ---------------- critical chain (H4) ----------------
    def _recompute_critical(self):
        """Populate self._critical_ids only while the toggle is on — it's
        O(n), so don't pay for it when the outline is hidden (plan H4)."""
        self._critical_ids = set()
        if not self.show_critical:
            return
        try:
            from utils.critical_path import CriticalPathAnalyzer
            roots = [r for r in self._tree.root_nodes if not _is_inbox(r)]
            res = CriticalPathAnalyzer(roots).analyze()
            self._critical_ids = (set(res.get("critical_path_ids", set()))
                                  | set(res.get("critical_parent_ids", set())))
        except Exception:
            self._critical_ids = set()

    def _is_critical(self, node):
        return self.show_critical and node.id in self._critical_ids

    # ---------------- VAVE dollars (H1) ----------------
    def _vave_on(self):
        """VAVE visibility follows the grid's Potential column, which
        set_vave_enabled drives — no separate timeline setting (plan H)."""
        try:
            from vpm_tracker_core import Columns
            return not self._tree.isColumnHidden(Columns.POTENTIAL)
        except Exception:
            return False

    def _vave_suffix(self, node):
        """' ($realized / $potential)' for a node with rolled-up $, else ''.
        Whole-dollar format (user chose this over the grid's .1f for bars)."""
        if not self._vave_on():
            return ""
        pot = node.vave_display_potential()
        rea = node.vave_display_realized()
        if pot is None and rea is None:
            return ""
        return f" (${(rea or 0):,.0f} / ${(pot or 0):,.0f})"

    def _bar_label(self, node, limit):
        """Truncated name plus group progress and VAVE suffix."""
        name = node.name if len(node.name) <= limit else node.name[:limit - 3] + "…"
        if node.children:
            fraction = progress_fraction(node)
            if fraction is not None:
                name += f" · {round(fraction * 100)}%"
        return name + self._vave_suffix(node)

    # ---------------- shared chart renderer ----------------
    def _render_header(self, p, t0, t1, ppd, x_of, header_h, total_h, width):
        p.fillRect(QRect(0, 0, width, header_h), QColor("#eceae3"))
        f = QFont(p.font())
        f.setPointSize(8)
        f.setBold(True)
        p.setFont(f)
        d = date(t0.year, t0.month, 1)
        while d <= t1:
            nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
            x = int(x_of(d))
            x_next = int(x_of(min(nxt, t1 + timedelta(days=1))))
            p.setPen(QPen(C_MONTH_LINE, 1))
            p.drawLine(x, 0, x, total_h)
            p.setPen(QPen(QColor("#444444")))
            label = d.strftime("%b %Y") if ppd >= 1.4 else d.strftime("%b")
            p.drawText(QRect(x, 0, max(x_next - x, 30), header_h),
                       Qt.AlignmentFlag.AlignCenter, label)
            d = nxt
        if ppd >= 4:
            d = t0 - timedelta(days=t0.weekday())
            while d <= t1:
                x = int(x_of(d))
                p.setPen(QPen(C_WEEK_LINE, 1))
                p.drawLine(x, header_h, x, total_h)
                d += timedelta(days=7)
        p.setPen(QPen(C_MONTH_LINE, 1))
        p.drawLine(0, header_h, width, header_h)

    @staticmethod
    def _last_descendant_idx(rows, i):
        """Index of the last row that is a descendant of rows[i], or None."""
        node = rows[i][0]
        last = None
        j = i + 1
        while j < len(rows):
            anc = rows[j][0].parent
            ok = False
            while anc is not None:
                if anc is node:
                    ok = True
                    break
                anc = anc.parent
            if not ok:
                break
            last = j
            j += 1
        return last

    def _render_rows(self, p, rows, x_of, width, today, register_hits=False):
        """rows: list of (node, y, row_h) in visual order — shared by the
        live paint and the PNG export."""
        geom = {}  # node.id -> (x1, x2, y_center) for link arrows
        f = QFont(p.font())
        f.setPointSize(8)

        for idx, (node, y, row_h) in enumerate(rows):
            if idx % 2 == 1:
                p.fillRect(QRect(0, y, width, row_h), C_BAND)

        # --- groups first (boxes sit behind everything) ---
        for i, (node, y, row_h) in enumerate(rows):
            if not node.children:
                continue
            s, e = _parse(node.start_date), _parse(node.end_date)
            if not s or not e:
                continue
            x1 = int(x_of(s))
            x2 = int(x_of(e + timedelta(days=1)))
            yc = y + row_h // 2
            geom[node.id] = (x1, x2, yc)
            last = self._last_descendant_idx(rows, i)

            if last is None:
                # Collapsed group → one fat slate bar (management view).
                # Green shows completed work; slate is the remaining phase.
                bh = max(14, int(row_h * 0.55))
                bar = QRect(x1, yc - bh // 2, max(x2 - x1, 4), bh)
                p.setPen(QPen(C_SUMMARY.darker(125), 1))
                p.setBrush(C_SUMMARY)
                p.drawRoundedRect(bar, 3, 3)
                fraction = progress_fraction(node)
                if fraction is not None and fraction > 0:
                    fill_w = min(bar.width(), max(1, int(round(bar.width() * fraction))))
                    p.save()
                    p.setClipRect(QRect(bar.left(), bar.top(), fill_w, bar.height()))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(C_DONE)
                    p.drawRoundedRect(bar, 3, 3)
                    p.restore()
                    if 0 < fill_w < bar.width():
                        bx = bar.left() + fill_w
                        p.setPen(QPen(C_SUMMARY.darker(135), 1))
                        p.drawLine(bx, bar.top() + 1, bx, bar.bottom() - 1)
                if self._is_critical(node):
                    p.setPen(QPen(C_CRITICAL, 2))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawRoundedRect(bar, 3, 3)
                if register_hits:
                    self._hits.append((bar.adjusted(-2, -4, 2, 4), node))
                if self.show_labels:
                    fb = QFont(f)
                    fb.setBold(True)
                    p.setFont(fb)
                    p.setPen(QPen(C_LABEL))
                    p.drawText(x2 + 8, yc + 4, self._bar_label(node, 40))
            else:
                # Expanded group → container box wrapping its children,
                # inset per depth so nesting reads as indentation.
                m = max(12 - _depth(node) * 3, 2)
                bx1, bx2 = x1 - m, x2 + m
                top = y + 2
                bottom = rows[last][1] + rows[last][2] - 2
                box = QRect(bx1, top, max(bx2 - bx1, 6), bottom - top)
                # Neutral gray outline / near-white fill (not status-colored).
                p.setPen(QPen(C_BOX, 1.4))
                p.setBrush(C_BOX_FILL)
                p.drawRoundedRect(box, 6, 6)
                if self._is_critical(node):
                    p.setPen(QPen(C_CRITICAL, 2))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawRoundedRect(box, 6, 6)
                if register_hits:
                    self._hits.append((QRect(bx1, y, box.width(), row_h),
                                       node))
                if self.show_labels:
                    fb = QFont(f)
                    fb.setBold(True)
                    p.setFont(fb)
                    p.setPen(QPen(C_SUMMARY))
                    p.drawText(bx1 + 8, yc + 4, self._bar_label(node, 44))

        # --- leaf bars & milestones on top ---
        p.setFont(f)
        for node, y, row_h in rows:
            if node.children:
                continue
            s, e = _parse(node.start_date), _parse(node.end_date)
            if not s or not e:
                continue
            x1 = int(x_of(s))
            x2 = int(x_of(e + timedelta(days=1)))
            yc = y + row_h // 2
            is_milestone = (e - s).days <= 0
            conflict_details = getattr(node, "resource_conflict_details", [])
            unresolved_resource = any(
                value.get("state") != "accepted" for value in conflict_details)
            accepted_resource = bool(conflict_details) and not unresolved_resource

            if is_milestone:
                size = min(row_h - 10, 16)
                cx = x1 + max((x2 - x1) // 2, 2)
                tri = QPolygonF([QPointF(cx, yc - size / 2),
                                 QPointF(cx + size / 2, yc),
                                 QPointF(cx, yc + size / 2),
                                 QPointF(cx - size / 2, yc)])
                p.setPen(QPen(C_MILESTONE.darker(120), 1))
                p.setBrush(C_MILESTONE)
                p.drawPolygon(tri)
                if unresolved_resource or accepted_resource:
                    p.setPen(QPen(QColor("#dc2626") if unresolved_resource
                                  else QColor("#f59e0b"), 2))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawPolygon(tri)
                hit = QRect(int(cx - size), int(yc - size),
                            int(size * 2), int(size * 2))
                x2 = int(cx + size / 2)
            else:
                color = _status_color(node, today)
                bh = max(14, int(row_h * 0.55))
                # H3 — baseline slip ghost (leaves only), drawn UNDER the bar.
                be = _parse(node.baseline_end) if node.baseline_end else None
                slipped = self.show_slips and be is not None and be != e
                if slipped:
                    gx2 = int(x_of(be + timedelta(days=1)))
                    gh = max(6, int(row_h * 0.40))
                    ghost = QRect(x1, yc - gh // 2, max(gx2 - x1, 3), gh)
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(C_SLIP)
                    p.drawRoundedRect(ghost, 2, 2)
                bar = QRect(x1, yc - bh // 2, max(x2 - x1, 4), bh)
                p.setPen(QPen(color.darker(135), 1))
                p.setBrush(color)
                p.drawRoundedRect(bar, 3, 3)
                if self._is_critical(node):
                    p.setPen(QPen(C_CRITICAL, 2))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawRoundedRect(bar, 3, 3)
                if unresolved_resource or accepted_resource:
                    p.setPen(QPen(QColor("#dc2626") if unresolved_resource
                                  else QColor("#f59e0b"), 2))
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.drawRoundedRect(bar, 3, 3)
                hit = bar.adjusted(-2, -4, 2, 4)
                if slipped:
                    n = (e - be).days   # calendar-day slip vs baseline
                    fs = QFont(f)
                    fs.setPointSize(7)
                    p.setFont(fs)
                    p.setPen(QPen(C_SLIP_LATE if n > 0 else C_LABEL))
                    p.drawText(x2 + 4, yc - bh // 2 - 1,
                               f"+{n}d" if n > 0 else f"{n}d")
                    p.setFont(f)

            geom[node.id] = (x1, x2, yc)
            if register_hits:
                self._hits.append((hit, node))
            if self.show_labels:
                p.setPen(QPen(C_LABEL))
                p.drawText(x2 + 8, yc + 4, self._bar_label(node, 40))

        # --- dependency arrows (explicit links, both ends visible) ---
        if self.show_links:
            p.setPen(QPen(C_LINK, 1.4))
            p.setBrush(C_LINK)
            for node, y, row_h in rows:
                pid = node.predecessor_id
                if not pid or pid not in geom or node.id not in geom:
                    continue
                px1, px2, pyc = geom[pid]
                sx1, sx2, syc = geom[node.id]
                elbow_x = px2 + 7
                p.drawLine(px2 + 1, pyc, elbow_x, pyc)
                p.drawLine(elbow_x, pyc, elbow_x, syc)
                p.drawLine(elbow_x, syc, max(sx1 - 3, elbow_x), syc)
                ax = max(sx1 - 3, elbow_x)
                arrow = QPolygonF([QPointF(ax, syc - 4),
                                   QPointF(ax, syc + 4),
                                   QPointF(ax + 5, syc)])
                p.drawPolygon(arrow)
        return geom

    def _render_today(self, p, x_of, today, y0, y1):
        tx = int(x_of(today))
        p.setPen(QPen(C_TODAY, 2))
        p.drawLine(tx, y0, tx, y1)
        f = QFont(p.font())
        f.setPointSize(7)
        p.setFont(f)
        p.setPen(QPen(C_TODAY))
        p.drawText(tx + 3, y0 + 10, "Today")

    # ---------------- live painting ----------------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#fbfaf7"))
        self._hits = []

        t0, t1 = self._date_range()
        days = (t1 - t0).days + 1
        ppd = self._effective_ppd(days)

        def x_of(d):
            return LEFT_PAD + (d - t0).days * ppd

        try:
            tree_top = self._tree.viewport().mapToGlobal(QPoint(0, 0)).y()
            my_top = self.mapToGlobal(QPoint(0, 0)).y()
            offset = tree_top - my_top
        except RuntimeError:
            offset = 24
        header_h = max(offset, 20)

        self._render_header(p, t0, t1, ppd, x_of, header_h,
                            self.height(), self.width())

        from ui.tree_grid_view import TaskTreeWidgetItem
        from PyQt6.QtWidgets import QTreeWidgetItemIterator
        rows = []
        it = QTreeWidgetItemIterator(self._tree)
        while it.value():
            item = it.value()
            it += 1
            if not isinstance(item, TaskTreeWidgetItem):
                continue
            # Inbox is notes, not schedule — never draw it on the chart.
            if _is_inbox(item.node):
                continue
            r = self._tree.visualItemRect(item)
            if r.height() <= 0:
                continue
            y = offset + r.y()
            # generous margins: an off-screen parent must still box its
            # visible children
            if y + r.height() < header_h - 800 or y > self.height() + 800:
                continue
            rows.append((item.node, y, r.height()))

        today = date.today()
        # Clip row drawing to below the header so boxes/labels from rows
        # scrolled upward can't paint over the month bar.
        p.save()
        p.setClipRect(QRect(0, header_h, self.width(),
                            self.height() - header_h))
        self._render_rows(p, rows, x_of, self.width(), today,
                          register_hits=True)
        p.restore()
        self._render_today(p, x_of, today, header_h, self.height())
        p.end()

    # ---------------- interaction ----------------
    def _node_at(self, pos):
        # later entries are drawn on top — search from the end
        for rect, node in reversed(self._hits):
            if rect.contains(pos):
                return node
        return None

    def mouseMoveEvent(self, event):
        node = self._node_at(event.pos())
        if node:
            tip = (f"{node.name}\n{node.start_date} → {node.end_date}"
                   f"  ·  {node.status}")
            if node.owner:
                tip += f"\n{node.owner}"
            if self._vave_on():
                pot = node.vave_display_potential()
                rea = node.vave_display_realized()
                if pot is not None or rea is not None:
                    tip += (f"\nPotential ${(pot or 0):,.0f}"
                            f" · Realized ${(rea or 0):,.0f}")
            if self.show_slips:
                trail = node.revision_trail()
                if trail:
                    tip += f"\n{trail}"
            if node.schedule_conflicts:
                tip += "\nRESOURCE CONFLICT:\n" + "\n".join(
                    node.schedule_conflicts)
            QToolTip.showText(event.globalPosition().toPoint(), tip, self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        node = self._node_at(event.pos())
        if node:
            self._tree.jump_to_node_id(node.id)
        super().mousePressEvent(event)

    def wheelEvent(self, event):
        """Vertical wheel over the chart scrolls the GRID — the chart
        mirrors the grid's rows, so they move together regardless of
        which side the mouse is over. Shift+wheel (or a sideways wheel)
        still pans the chart horizontally via the scroll area."""
        if (abs(event.angleDelta().y()) > abs(event.angleDelta().x())
                and not (event.modifiers()
                         & Qt.KeyboardModifier.ShiftModifier)):
            from PyQt6.QtWidgets import QApplication
            QApplication.sendEvent(self._tree.viewport(), event)
            event.accept()
            return
        super().wheelEvent(event)

    # ---------------- export ----------------
    def _export_header_lines(self, ordered, today, project_name):
        """H5 — the title band's lines as (kind, text). Counts leaf tasks
        only; respects the slips/critical toggles and the VAVE flag."""
        tasks = [n for _, n in ordered if not n.children]
        Y = len(tasks)
        X = sum(1 for n in tasks if n.status == "Completed")
        late = [n for n in tasks
                if n.status != "Completed"
                and _parse(n.end_date) and _parse(n.end_date) < today]
        dt = today.strftime("%b %d, %Y")
        title = (project_name or "").strip()
        line1 = f"{title} — {dt}" if title else dt
        line2 = f"{X} of {Y} tasks done · {len(late)} late"
        if self.show_critical:
            nlate = sum(1 for n in late if n.id in self._critical_ids)
            line2 += f" · {nlate} on critical chain late"
        ms = []
        for n in tasks:
            s, e = _parse(n.start_date), _parse(n.end_date)
            if s and e and (e - s).days <= 0:
                ms.append((e, n))
        upcoming = sorted((m for m in ms if m[0] >= today), key=lambda m: m[0])
        if upcoming:
            e, n = upcoming[0]
            line2 += f" · Next milestone: {n.name} {e.strftime('%b %d')}"
        lines = [("title", line1), ("normal", line2)]
        roots = [r for r in self._tree.root_nodes if not _is_inbox(r)]
        work_fraction = _progress_fraction_for_roots(roots)
        work_text = (f"{round(work_fraction * 100)}% of work complete"
                     if work_fraction is not None else None)
        if self._vave_on():
            A = B = 0.0
            for r in roots:
                A += r.vave_total_realized()
                B += r.vave_total_potential()
            P = int(round(A / B * 100)) if B else 0
            prefix = f"{work_text} · " if work_text else ""
            lines.append(("vave",
                          f"VAVE: {prefix}Realized ${A:,.0f} of ${B:,.0f} ({P}%)"))
        elif work_text:
            lines.append(("normal", f"Progress: {work_text}"))
        return lines

    def _draw_export_title(self, p, lines, width, title_h):
        y = 18
        for kind, text in lines:
            f = QFont(p.font())
            if kind == "title":
                f.setPointSize(11)
                f.setBold(True)
                p.setPen(QPen(QColor("#222222")))
            elif kind == "vave":
                f.setPointSize(9)
                f.setBold(True)
                p.setPen(QPen(C_DONE.darker(115)))
            else:
                f.setPointSize(9)
                f.setBold(False)
                p.setPen(QPen(QColor("#444444")))
            p.setFont(f)
            p.drawText(10, y, text)
            y += 18
        p.setPen(QPen(C_MONTH_LINE, 1))
        p.drawLine(0, title_h - 1, width, title_h - 1)

    def export_image(self, path, project_name=""):
        """Full chart, every expanded row, name column + legend strip."""
        from ui.tree_grid_view import TaskTreeWidgetItem
        ordered = []

        def walk(item, depth):
            if isinstance(item, TaskTreeWidgetItem):
                if _is_inbox(item.node):
                    return  # exclude the Inbox notes group from exports too
                ordered.append((depth, item.node))
                if item.isExpanded():
                    for i in range(item.childCount()):
                        walk(item.child(i), depth + 1)

        for i in range(self._tree.topLevelItemCount()):
            walk(self._tree.topLevelItem(i), 0)
        if not ordered:
            return False

        NAME_W, ROW_H, HEAD_H, LEGEND_H = 250, 28, 30, 30
        today = date.today()
        title_lines = self._export_header_lines(ordered, today, project_name)
        TITLE_H = 12 + len(title_lines) * 18

        t0, t1 = self._date_range()
        days = (t1 - t0).days + 1
        ppd = max(self._effective_ppd(days), 2.5)
        chart_w = int(LEFT_PAD + days * ppd + RIGHT_PAD)
        pm = QPixmap(NAME_W + chart_w,
                     TITLE_H + HEAD_H + len(ordered) * ROW_H + LEGEND_H)
        pm.fill(QColor("#ffffff"))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # H5 — title band, drawn before we shift the chart down beneath it.
        self._draw_export_title(p, title_lines, pm.width(), TITLE_H)
        p.translate(0, TITLE_H)

        def x_of(d):
            return NAME_W + LEFT_PAD + (d - t0).days * ppd

        chart_bottom = HEAD_H + len(ordered) * ROW_H
        self._render_header(p, t0, t1, ppd, x_of, HEAD_H,
                            chart_bottom, pm.width())

        f = QFont(p.font())
        f.setPointSize(8)
        for idx, (depth, node) in enumerate(ordered):
            y = HEAD_H + idx * ROW_H
            f.setBold(bool(node.children))
            p.setFont(f)
            p.setPen(QPen(QColor("#222222")))
            name = node.name
            if node.children:
                fraction = progress_fraction(node)
                if fraction is not None:
                    name += f" · {round(fraction * 100)}%"
            limit = 38 - depth * 2
            if len(name) > limit:
                name = name[:limit - 2] + "…"
            p.drawText(8 + depth * 12, y + ROW_H - 9, name)
        p.setPen(QPen(C_MONTH_LINE, 1))
        p.drawLine(NAME_W - 6, 0, NAME_W - 6, chart_bottom)

        rows = [(node, HEAD_H + idx * ROW_H, ROW_H)
                for idx, (_, node) in enumerate(ordered)]
        saved_labels = self.show_labels
        self.show_labels = False  # names live in the name column
        self._render_rows(p, rows, x_of, pm.width(), today)
        self.show_labels = saved_labels
        self._render_today(p, x_of, today, HEAD_H, chart_bottom)

        y = chart_bottom + 8
        f.setBold(False)
        f.setPointSize(8)
        p.setFont(f)
        x = 10
        entries = [(C_DONE, "Completed"), (C_PROGRESS, "In progress"),
                   (C_NOT_STARTED, "Not started"), (C_OVERDUE, "Overdue"),
                   (C_SUMMARY, "Group"), (C_MILESTONE, "◆ Milestone")]
        if self.show_slips:
            entries.append((C_SLIP, "Slip (baseline)"))
        for color, text in entries:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawRect(QRect(x, y, 12, 12))
            p.setPen(QPen(QColor("#333333")))
            p.drawText(x + 16, y + 11, text)
            x += 16 + 10 + len(text) * 6 + 14
        if self.show_critical:
            # outline swatch matches the on-chart 2px dark-red outline
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(C_CRITICAL, 2))
            p.drawRect(QRect(x, y, 12, 12))
            p.setPen(QPen(QColor("#333333")))
            p.drawText(x + 16, y + 11, "Critical chain")
        p.end()
        return pm.save(path)


class DollarBar(QWidget):
    """H2 — slim stacked VAVE bar shown above the controls strip.

    Green = realized (banked), amber = remaining potential, same color
    language as the task bars. Visible only when VAVE is on and there is
    a non-zero potential total. Inbox roots are excluded like everywhere."""

    def __init__(self, tree, vave_check, parent=None):
        super().__init__(parent)
        self._tree = tree
        self._vave_on = vave_check
        self.setFixedHeight(24)

    def _totals(self):
        realized = potential = 0.0
        for r in self._tree.root_nodes:
            if _is_inbox(r):
                continue
            realized += r.vave_total_realized()
            potential += r.vave_total_potential()
        return realized, potential

    def is_active(self):
        if not self._vave_on():
            return False
        _, potential = self._totals()
        return potential > 0

    def refresh(self):
        self.setVisible(self.is_active())
        self.update()

    def paintEvent(self, event):
        if not self.is_active():
            return
        realized, potential = self._totals()
        pct = int(round(realized / potential * 100)) if potential else 0
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        h = self.height()
        track_x = 10
        track_w = max(int(self.width() * 0.42), 60)
        bar_h = 12
        by = (h - bar_h) // 2
        # amber base = whole potential; green overlay = realized portion
        p.setBrush(C_PROGRESS)
        p.drawRoundedRect(QRect(track_x, by, track_w, bar_h), 3, 3)
        gw = int(track_w * (realized / potential)) if potential else 0
        if gw > 0:
            p.setBrush(C_DONE)
            p.drawRoundedRect(QRect(track_x, by, min(max(gw, 3), track_w),
                                    bar_h), 3, 3)
        f = QFont(p.font())
        f.setPointSize(9)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QPen(C_LABEL))
        txt = f"Realized ${realized:,.0f} of ${potential:,.0f} ({pct}%)"
        p.drawText(track_x + track_w + 12, h - 7, txt)
        p.end()


class TimelineContainer(QWidget):
    """Chart on top, controls strip at the BOTTOM (keeps the canvas top
    edge aligned with the grid's top edge)."""

    def __init__(self, tree, parent=None):
        super().__init__(parent)
        self._tree = tree

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.canvas = TimelineCanvas(tree)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.canvas)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(self.scroll, 1)

        # H2 — overall dollar bar, directly above the controls strip.
        self.dollar_bar = DollarBar(tree, self.canvas._vave_on)
        root.addWidget(self.dollar_bar)

        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 2)
        bar.setSpacing(4)

        def btn(label, tip, slot, checkable=False, checked=False, width=None):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.setFixedHeight(22)
            if width:
                b.setFixedWidth(width)
            b.setCheckable(checkable)
            if checkable:
                b.setChecked(checked)
            b.clicked.connect(slot)
            bar.addWidget(b)
            return b

        self.toggle_btn = btn("Hide", "Show / hide the timeline",
                              self._toggle, checkable=True, checked=True)

        lv = QLabel("Levels:")
        lv.setStyleSheet("font-size: 10px; color: #555; margin-left: 6px;")
        bar.addWidget(lv)
        for label, level in (("1", 1), ("2", 2), ("3", 3), ("All", None)):
            btn(label,
                f"Collapse the plan to outline level {label} "
                "(grid and chart together)",
                lambda _, l=level: self._set_level(l), width=34)

        btn("Fit", "Fit whole project in view", self._fit)
        btn("−", "Zoom out", lambda: self._zoom(0.7), width=28)
        btn("+", "Zoom in", lambda: self._zoom(1.4), width=28)
        self.labels_btn = btn("Labels", "Show task names next to bars",
                              self._set_flags, checkable=True, checked=True)
        self.links_btn = btn("Links", "Show dependency arrows",
                             self._set_flags, checkable=True, checked=True)
        # Slips are private: this starts OFF every session (never persisted on)
        # so slippage is never shown to leadership unless he opts in.
        self.slips_btn = btn(
            "Slips",
            "Show baseline slip shadows (private — starts off each session; "
            "included in exports only while on)",
            self._set_flags, checkable=True, checked=False)
        self.critical_btn = btn(
            "Critical", "Outline the critical chain (same tasks the Visuals "
            "tab flags)", self._set_flags, checkable=True, checked=False)
        btn("Export…", "Save the full chart as a PNG image", self._export)

        self._base_legend = (
            "<span style='color:#43A047'>■</span> done "
            "<span style='color:#FFB300'>■</span> active "
            "<span style='color:#64B5F6'>■</span> planned "
            "<span style='color:#E53935'>■</span> late "
            "<span style='color:#37474F'>■</span> group "
            "<span style='color:#90A4AE'>▢</span> open group")
        self.legend = QLabel(self._base_legend)
        self.legend.setStyleSheet("font-size: 10px; color: #555;")
        bar.addWidget(self.legend)
        bar.addStretch()
        root.addLayout(bar)

        tree.verticalScrollBar().valueChanged.connect(
            lambda _: self.canvas.update())
        tree.itemExpanded.connect(lambda _: self.canvas.update())
        tree.itemCollapsed.connect(lambda _: self.canvas.update())
        tree.item_changed_signal.connect(lambda _: self.refresh())
        self.dollar_bar.refresh()

    def refresh(self):
        self.canvas.refresh()
        self.dollar_bar.refresh()

    def _toggle(self):
        visible = self.toggle_btn.isChecked()
        self.scroll.setVisible(visible)
        self.toggle_btn.setText("Hide" if visible else "Show timeline")

    def _set_level(self, level):
        self._tree.set_outline_level(level)
        self.canvas.refresh()

    def _set_flags(self):
        self.canvas.show_labels = self.labels_btn.isChecked()
        self.canvas.show_links = self.links_btn.isChecked()
        self.canvas.show_slips = self.slips_btn.isChecked()
        self.canvas.show_critical = self.critical_btn.isChecked()
        self.canvas._recompute_critical()
        self._update_legend()
        self.canvas.update()

    def _update_legend(self):
        extra = ("  <span style='color:#B71C1C'>▭</span> critical chain"
                 if self.critical_btn.isChecked() else "")
        self.legend.setText(self._base_legend + extra)

    def _fit(self):
        self.canvas.px_per_day = 0.0
        self.canvas.refresh()

    def _zoom(self, factor):
        t0, t1 = self.canvas._date_range()
        days = (t1 - t0).days + 1
        current = self.canvas._effective_ppd(days)
        self.canvas.px_per_day = max(0.5, min(30.0, current * factor))
        self.canvas.refresh()

    def _project_name(self):
        """Read the live project name off the enclosing ProjectWidget
        (which owns a `.name`), so renames are always reflected. QWidget
        has no `.name`, so this only matches the project widget."""
        w = self.parent()
        while w is not None:
            n = getattr(w, "name", None)
            if isinstance(n, str) and n:
                return n
            w = w.parent()
        return ""

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export timeline image", "timeline.png", "PNG image (*.png)")
        if path:
            self.canvas.export_image(path, project_name=self._project_name())
