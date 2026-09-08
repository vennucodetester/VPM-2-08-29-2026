
import uuid
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from utils.workday_calculator import WorkdayCalculator

DATE_FMT = "%Y-%m-%d"


def _clamp_end(start: Optional[str], end: Optional[str]) -> Optional[str]:
    """Guarantee end >= start. Returns a valid end string or the original if inputs are bad."""
    if not start or not end:
        return end
    try:
        s = datetime.strptime(start, DATE_FMT)
        e = datetime.strptime(end, DATE_FMT)
    except ValueError:
        return end
    if e < s:
        return start
    return end


class TaskNode:
    def __init__(self, name: str = "New Task", parent: Optional['TaskNode'] = None):
        self.id: str = str(uuid.uuid4())
        self.name: str = name
        self.start_date: Optional[str] = datetime.now().strftime("%Y-%m-%d")
        self.end_date: Optional[str] = datetime.now().strftime("%Y-%m-%d")
        self.status: str = "Not Started"
        self.status_manual: bool = False
        self.owner: str = ""
        self.waiting_on: str = ""
        self.waiting_since: Optional[str] = None
        self.notes: str = ""
        self.is_parallel: bool = False
        self.vave_potential: Optional[float] = None
        self.vave_realized: Optional[float] = None
        self.vave_stage: str = "Idea"
        self.savings_disposition: str = ""
        self.template_snapshot: Dict[str, Any] = {}
        self.task_tokens: List[Dict[str, Any]] = []
        self.resources: Dict[str, str] = {}
        self.resource_capacities: Dict[str, int] = {}
        self.schedule_conflicts: List[str] = []
        # Derived by the document-wide analyzer; intentionally not serialized.
        self.resource_conflict_details: List[Dict[str, Any]] = []
        self.schedule_reason: str = ""

        # Hierarchy
        self.parent: Optional[TaskNode] = parent
        self.children: List[TaskNode] = []

        # Meta
        self.expanded: bool = True
        # One controlling rule per date field. Compatibility properties below
        # expose the old dates_locked/predecessor_id API while UI migration is
        # completed in small releases.
        self.start_rule: Dict[str, Any] = {"mode": "automatic"}
        self.end_rule: Dict[str, Any] = {"mode": "duration", "days": 1}
        self.baseline_duration: Optional[int] = None  # Set by "Set Baseline" (Rev A)
        self.baseline_end: Optional[str] = None       # End date at baseline time
        self.delay_notes: str = ""  # Legacy free-text log (pre-revision files)
        # Revision trail: Rev A = baseline; each slip confirmed by the user
        # appends {"rev","date","end","slip","reason"}. See DelayDelegate.
        self.revisions: list = []
        # True on a freshly inserted row whose baseline was auto-stamped from
        # placeholder dates. The FIRST real end/duration the user types
        # re-stamps the baseline silently (a new line is on track by
        # definition); only edits after that count as delay.
        self.baseline_provisional: bool = False

    def resource_tokens(self):
        """All assigned metadata resources, retaining multiple same-kind values."""
        from utils.resource_allocation import canonical_resource_kind, legacy_resource_id
        tokens = [dict(token) for token in self.task_tokens
                  if (token.get("kind") or "").casefold()
                  not in {"phase", "campaign", "header"}]
        represented = {(canonical_resource_kind(t.get("kind")),
                        (t.get("label") or "").strip().casefold())
                       for t in tokens}
        for kind, label in self.resources.items():
            key = (canonical_resource_kind(kind), str(label).strip().casefold())
            if label and key not in represented:
                tokens.append({"kind": kind, "label": label,
                               "id": legacy_resource_id(kind, label)})
        return tokens

    @staticmethod
    def _normalize_start_rule(rule) -> Dict[str, Any]:
        rule = dict(rule or {})
        mode = rule.get("mode", "automatic")
        if mode not in {"automatic", "fixed", "same_as", "continue_after"}:
            mode = "automatic"
        clean = {"mode": mode}
        if mode == "fixed":
            clean["date"] = rule.get("date")
        elif mode in {"same_as", "continue_after"}:
            clean.update({
                "task_id": rule.get("task_id"),
                "field": rule.get("field", "start" if mode == "same_as" else "end"),
                "offset": max(0, int(rule.get("offset", 0) or 0)),
                "offset_unit": rule.get("offset_unit", "workdays"),
            })
        return clean

    @staticmethod
    def _normalize_end_rule(rule, fallback_days=1) -> Dict[str, Any]:
        rule = dict(rule or {})
        mode = rule.get("mode", "duration")
        if mode not in {"duration", "fixed", "same_as"}:
            mode = "duration"
        if mode == "duration":
            return {"mode": "duration", "days": max(1, int(rule.get("days", fallback_days) or 1))}
        if mode == "fixed":
            return {"mode": "fixed", "date": rule.get("date")}
        return {
            "mode": "same_as",
            "task_id": rule.get("task_id"),
            "field": rule.get("field", "end"),
        }

    @property
    def dates_locked(self) -> bool:
        return self.start_rule.get("mode") == "fixed" or self.end_rule.get("mode") == "fixed"

    @dates_locked.setter
    def dates_locked(self, locked: bool):
        if locked:
            self.start_rule = {"mode": "fixed", "date": self.start_date}
            self.end_rule = {"mode": "fixed", "date": self.end_date}
        else:
            days = 1
            if self.start_date and self.end_date:
                days = max(1, WorkdayCalculator.calculate_duration(self.start_date, self.end_date))
            self.start_rule = {"mode": "automatic"}
            self.end_rule = {"mode": "duration", "days": days}

    @property
    def predecessor_id(self) -> Optional[str]:
        if self.start_rule.get("mode") == "continue_after":
            return self.start_rule.get("task_id")
        return None

    @predecessor_id.setter
    def predecessor_id(self, node_id: Optional[str]):
        if node_id:
            self.start_rule = {
                "mode": "continue_after", "task_id": node_id,
                "field": "end", "offset": 0, "offset_unit": "workdays",
            }
        elif self.start_rule.get("mode") == "continue_after":
            self.start_rule = {"mode": "automatic"}

    def set_start_rule(self, rule):
        self.start_rule = self._normalize_start_rule(rule)

    def set_end_rule(self, rule):
        fallback = max(1, int(self.duration or 1))
        self.end_rule = self._normalize_end_rule(rule, fallback)

    def rule_explanation(self, field: str) -> str:
        rule = self.start_rule if field == "start" else self.end_rule
        mode = rule.get("mode")
        if mode == "automatic":
            return "Automatic"
        if mode == "duration":
            return f"Duration: {rule.get('days', 1)} workday(s)"
        if mode == "fixed":
            return f"Fixed: {rule.get('date') or 'unset'}"
        relation = "Same as" if mode == "same_as" else "Continue after"
        return f"{relation} {rule.get('task_id') or 'missing'} · {rule.get('field', field).title()}"

    def add_child(self, child: 'TaskNode'):
        child.parent = self
        self.children.append(child)

        if len(self.children) == 1 and self.start_date:
            duration = 1
            if child.start_date and child.end_date:
                duration = WorkdayCalculator.calculate_duration(child.start_date, child.end_date)
            child.start_date = self.start_date
            child.end_date = WorkdayCalculator.add_workdays(child.start_date, duration)

        if len(self.children) > 1:
            child.update_from_previous_sibling(self.children[-2])

        self.update_dates_from_children()
        self.update_owner_from_children()

    def remove_child(self, child: 'TaskNode'):
        if child in self.children:
            idx = self.children.index(child)
            self.children.remove(child)
            child.parent = None

            if 0 < idx < len(self.children):
                prev = self.children[idx - 1]
                next_sib = self.children[idx]
                next_sib.update_from_previous_sibling(prev)
                # (further ripple is the scheduler's job — callers recalc)

            if not self.children:
                self.is_parallel = False
                if self.parent:
                    siblings = self.parent.children
                    if self in siblings:
                        my_idx = siblings.index(self)
                        if my_idx > 0:
                            self.update_from_previous_sibling(siblings[my_idx - 1])

            self.update_dates_from_children()

    # ------------------------------------------------------------------
    # Core setter — single point of truth for date mutation
    # ------------------------------------------------------------------

    def set_date(self, date_type: str, date_str: str,
                 force: bool = False,
                 is_rollup: bool = False,
                 visited: Optional[set] = None):
        """
        Set a date and (optionally) cascade to siblings + rollup to parent.

        force=True   - bypass dates_locked (manual UI override only)
        is_rollup=True - called from update_dates_from_children; skip cascade to avoid loops
        visited      - set of node ids already touched in this cascade (recursion guard)
        """
        if self.start_rule.get("mode") == "fixed" and not force and date_type == 'start':
            if not (is_rollup and self.children):
                return

        changed = False
        if date_type == 'start':
            if self.start_date != date_str:
                self.start_date = date_str
                changed = True
        elif date_type == 'end':
            if self.end_date != date_str:
                self.end_date = date_str
                changed = True

        # F5: enforce end >= start invariant — ALWAYS, even if the caller's
        # update was a no-op. Guards against any upstream code that mutates
        # start_date/end_date directly.
        fixed_end = _clamp_end(self.start_date, self.end_date)
        if fixed_end != self.end_date:
            self.end_date = fixed_end
            changed = True

        if not changed:
            return

        self.update_status_from_dates()

        if is_rollup:
            # Rollup only touches its own parent chain; do NOT cascade to siblings/children
            if self.parent:
                self.parent.update_dates_from_children(visited=visited)
            return

        if visited is None:
            visited = set()
        if self.id in visited:
            return
        visited.add(self.id)

        if self.parent:
            self.parent.update_dates_from_children(visited=visited)

        # NOTE: no sibling cascade here. Propagating a date change to other
        # tasks is the scheduler's job (utils/scheduler.py) — every UI call
        # site follows a mutation with recalculate_all_dates(). Having a
        # second propagation path in the model was the source of the
        # "toggle it twice" / stale-date bugs.

    # ------------------------------------------------------------------
    # Sibling / predecessor propagation
    # ------------------------------------------------------------------

    def update_from_previous_sibling(self, prev_sibling: 'TaskNode'):
        """Start = Prev.End + 1 workday, shift End to preserve duration."""
        if self.start_rule.get("mode") != "automatic":
            return
        if not prev_sibling.end_date:
            return

        # is_parallel ON = snap to parent.start_date (parallel with parent);
        # OFF = auto-chain to prev sibling's end + 1 workday.
        if self.is_parallel and self.parent and self.parent.start_date:
            new_start = self.parent.start_date
        else:
            new_start = WorkdayCalculator.get_next_workday(prev_sibling.end_date)

        if new_start != self.start_date:
            duration = 1
            if self.start_date and self.end_date:
                duration = WorkdayCalculator.calculate_duration(self.start_date, self.end_date)
            self.start_date = new_start
            self.end_date = WorkdayCalculator.add_workdays(new_start, duration)
            self.end_date = _clamp_end(self.start_date, self.end_date)

    def update_first_child_from_parent(self, parent: 'TaskNode'):
        """First child with is_parallel=OFF anchors to parent.start_date.
        Duration is preserved; end shifts to match."""
        if self.start_rule.get("mode") != "automatic":
            return
        if not parent.start_date:
            return
        new_start = parent.start_date
        if new_start != self.start_date:
            duration = 1
            if self.start_date and self.end_date:
                duration = WorkdayCalculator.calculate_duration(self.start_date, self.end_date)
            self.start_date = new_start
            self.end_date = WorkdayCalculator.add_workdays(new_start, duration)
            self.end_date = _clamp_end(self.start_date, self.end_date)

    def update_from_predecessor(self, predecessor: 'TaskNode'):
        """Start = Predecessor.End + 1 workday (manual cross-tree link)."""
        if self.start_rule.get("mode") != "continue_after":
            return
        if not predecessor.end_date:
            return

        new_start = WorkdayCalculator.get_next_workday(predecessor.end_date)
        if new_start != self.start_date:
            duration = 1
            if self.start_date and self.end_date:
                duration = WorkdayCalculator.calculate_duration(self.start_date, self.end_date)
            self.start_date = new_start
            self.end_date = WorkdayCalculator.add_workdays(new_start, duration)
            self.end_date = _clamp_end(self.start_date, self.end_date)
            self.update_status_from_dates()

    # cascade_updates() was removed: it was a second, competing date-
    # propagation engine alongside utils/scheduler.py. The scheduler is
    # now the only code that ripples date changes between tasks.

    # ------------------------------------------------------------------
    # Shift children by explicit delta (F2 — no stale min-start math)
    # ------------------------------------------------------------------

    def shift_children(self, delta_days: int):
        """Shift every descendant by delta_days (calendar days)."""
        if not self.children or delta_days == 0:
            return
        for child in self.children:
            if child.start_rule.get("mode") != "fixed" and child.start_date:
                child.start_date = self._add_days(child.start_date, delta_days)
            if child.end_rule.get("mode") != "fixed" and child.end_date:
                child.end_date = self._add_days(child.end_date, delta_days)
                child.end_date = _clamp_end(child.start_date, child.end_date)
            if child.children:
                child.shift_children(delta_days)

    @staticmethod
    def _add_days(date_str: str, delta_days: int) -> str:
        try:
            dt = datetime.strptime(date_str, DATE_FMT)
        except ValueError:
            return date_str
        return (dt + timedelta(days=delta_days)).strftime(DATE_FMT)

    @staticmethod
    def _days_between(a: Optional[str], b: Optional[str]) -> int:
        if not a or not b:
            return 0
        try:
            da = datetime.strptime(a, DATE_FMT)
            db = datetime.strptime(b, DATE_FMT)
        except ValueError:
            return 0
        return (db - da).days

    # ------------------------------------------------------------------
    # Duration helpers
    # ------------------------------------------------------------------

    @property
    def duration(self) -> str:
        if self.start_date and self.end_date:
            return str(WorkdayCalculator.calculate_duration(self.start_date, self.end_date))
        return "0"

    def set_duration(self, days: int):
        if days < 1:
            days = 1
        if not self.start_date:
            return
        self.end_rule = {"mode": "duration", "days": days}
        new_end = WorkdayCalculator.add_workdays(self.start_date, days)
        self.set_date('end', new_end)

    # ------------------------------------------------------------------
    # Delay / revision helpers
    # ------------------------------------------------------------------

    def logged_slip(self) -> int:
        """Total slip already recorded across all revisions."""
        return sum(r.get("slip", 0) for r in self.revisions)

    def revision_trail(self) -> str:
        """Human-readable Rev A..N trail (falls back to legacy delay_notes)."""
        lines = []
        if self.baseline_duration is not None:
            base = f"Rev A  (baseline)  {self.baseline_duration}d"
            if self.baseline_end:
                base += f"  end {self.baseline_end}"
            lines.append(base)
        for r in self.revisions:
            slip = r.get("slip", 0)
            sign = f"+{slip}d" if slip > 0 else f"{slip}d"
            line = f"Rev {r.get('rev', '?')}  {r.get('date', '')}  {sign}"
            if r.get("end"):
                line += f"  end {r['end']}"
            if r.get("reason"):
                line += f"  — {r['reason']}"
            lines.append(line)
        if not self.revisions and self.delay_notes:
            # Old files logged free text before revisions existed
            lines.append(self.delay_notes)
        return "\n".join(lines)

    def log_delay_revision(self, rev: Dict[str, Any]):
        """Append a delay revision and prepend the matching dated note line."""
        self.revisions.append(dict(rev))
        slip = rev.get("slip", 0)
        date = rev.get("date") or datetime.now().strftime(DATE_FMT)
        end = rev.get("end") or self.end_date or ""
        reason = rev.get("reason") or ""
        sign = f"+{slip}d" if slip > 0 else f"{slip}d"
        note = f"[{date}]: Delayed {sign}, end -> {end}."
        if reason:
            note += f" {reason}"
        existing = self.notes or ""
        if existing.startswith(note):
            return
        self.notes = f"{note}\n{existing}" if existing else note

    # ------------------------------------------------------------------
    # Rollup (F4 — pass is_rollup=True so set_date doesn't re-cascade)
    # ------------------------------------------------------------------

    def update_dates_from_children(self, visited: Optional[set] = None):
        if not self.children:
            return
        starts = [c.start_date for c in self.children if c.start_date]
        ends = [c.end_date for c in self.children if c.end_date]
        if not starts or not ends:
            return

        min_start = min(starts)
        max_end = max(ends)

        if self.start_date != min_start:
            self.set_date('start', min_start, is_rollup=True, visited=visited)
        if self.end_date != max_end:
            self.set_date('end', max_end, is_rollup=True, visited=visited)
        if self.start_date and self.end_date and self.start_date > self.end_date:
            self.start_date = min_start
            self.end_date = max_end

    def update_owner_from_children(self):
        if not self.children:
            return
        owners = set()
        for child in self.children:
            if child.owner:
                for o in child.owner.split('/'):
                    if o.strip():
                        owners.add(o.strip())
        self.owner = "/".join(sorted(owners)) if owners else ""

    def set_owner(self, new_owner: str):
        if self.owner != new_owner:
            self.owner = new_owner
            if self.parent:
                self.parent.update_owner_from_children()

    def set_waiting_on(self, who: str):
        who = (who or "").strip()
        if who:
            self.waiting_on = who
            if not self.waiting_since:
                self.waiting_since = datetime.now().strftime(DATE_FMT)
        else:
            self.waiting_on = ""
            self.waiting_since = None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def update_status_from_dates(self):
        if not self.start_date:
            return
        try:
            start_dt = datetime.strptime(self.start_date, DATE_FMT).date()
            today = datetime.now().date()
            if self.children:
                if all(c.status == "Completed" for c in self.children):
                    self.status = "Completed"
                    return
                if self.status == "Completed":
                    self.status = "In Progress"
                self.status_manual = False
            elif self.status_manual and self.status in ("Not Started", "In Progress"):
                return
            if self.status == "Completed" and not self.children:
                return
            self.status = "Not Started" if start_dt > today else "In Progress"
        except ValueError:
            pass

    def set_status(self, new_status: str):
        if self.status != new_status:
            self.status = new_status
            self.status_manual = not self.children
            if self.parent:
                self.parent.update_status_from_dates()

    # ------------------------------------------------------------------
    # VAVE dollars
    # ------------------------------------------------------------------

    def vave_display_potential(self) -> Optional[float]:
        return self._vave_display_value("vave_potential")

    def vave_display_realized(self) -> Optional[float]:
        return self._vave_display_value("vave_realized")

    def _vave_display_value(self, attr: str) -> Optional[float]:
        manual = getattr(self, attr, None)
        if manual is not None:
            return manual
        rolled = sum(
            float(value)
            for child in self.children
            for value in [child._vave_display_value(attr)]
            if value is not None
        )
        return rolled if rolled else None

    def vave_total_potential(self) -> float:
        return self._vave_total_value("vave_potential")

    def vave_total_realized(self) -> float:
        return self._vave_total_value("vave_realized")

    def _vave_total_value(self, attr: str) -> float:
        manual = getattr(self, attr, None)
        if manual is not None:
            return float(manual)
        return sum(child._vave_total_value(attr) for child in self.children)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "status": self.status,
            "status_manual": self.status_manual,
            "owner": self.owner,
            "waiting_on": self.waiting_on,
            "waiting_since": self.waiting_since,
            "notes": self.notes,
            "is_parallel": self.is_parallel,
            "vave_potential": self.vave_potential,
            "vave_realized": self.vave_realized,
            "vave_stage": self.vave_stage,
            "savings_disposition": self.savings_disposition,
            "template_snapshot": dict(self.template_snapshot),
            "task_tokens": [dict(token) for token in self.task_tokens],
            "resources": dict(self.resources),
            "resource_capacities": dict(self.resource_capacities),
            "children": [c.to_dict() for c in self.children],
            "expanded": self.expanded,
            "dates_locked": self.dates_locked,
            "predecessor_id": self.predecessor_id,
            "start_rule": dict(self.start_rule),
            "end_rule": dict(self.end_rule),
            "baseline_duration": self.baseline_duration,
            "baseline_end": self.baseline_end,
            "delay_notes": self.delay_notes,
            "revisions": self.revisions,
            "baseline_provisional": self.baseline_provisional,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], parent: Optional['TaskNode'] = None) -> 'TaskNode':
        node = cls(name=data.get("name", "New Task"), parent=parent)
        node.id = data.get("id", str(uuid.uuid4()))
        node.start_date = data.get("start_date")
        node.end_date = data.get("end_date")
        # Repair any inverted span from a previous buggy save
        node.end_date = _clamp_end(node.start_date, node.end_date)
        node.status = data.get("status", "Not Started")
        node.status_manual = data.get("status_manual", False)
        node.owner = data.get("owner", "")
        node.waiting_on = data.get("waiting_on", "")
        node.waiting_since = data.get("waiting_since")
        node.notes = data.get("notes", "")
        node.vave_potential = cls._money_or_none(data.get("vave_potential"))
        node.vave_realized = cls._money_or_none(data.get("vave_realized"))
        node.vave_stage = data.get("vave_stage", "Idea")
        node.savings_disposition = data.get("savings_disposition", "")
        node.template_snapshot = dict(data.get("template_snapshot") or {})
        node.task_tokens = [dict(token) for token in (data.get("task_tokens") or [])]
        node.resources = dict(data.get("resources") or {})
        node.resource_capacities = {
            str(key): max(1, int(value or 1))
            for key, value in (data.get("resource_capacities") or {}).items()
        }
        node.expanded = data.get("expanded", True)
        # v2.4 scheduling-rule migration. Existing files retain their displayed
        # dates; no scheduler run is triggered during deserialization.
        if "start_rule" in data:
            node.start_rule = cls._normalize_start_rule(data.get("start_rule"))
        elif data.get("dates_locked", False):
            node.start_rule = {"mode": "fixed", "date": node.start_date}
        elif data.get("predecessor_id"):
            node.start_rule = cls._normalize_start_rule({
                "mode": "continue_after", "task_id": data.get("predecessor_id"),
                "field": "end",
            })
        elif data.get("is_parallel", False) and parent is not None:
            node.start_rule = cls._normalize_start_rule({
                "mode": "same_as", "task_id": parent.id, "field": "start",
            })
        else:
            node.start_rule = {"mode": "automatic"}
        fallback_days = 1
        if node.start_date and node.end_date:
            fallback_days = max(1, WorkdayCalculator.calculate_duration(
                node.start_date, node.end_date))
        if "end_rule" in data:
            node.end_rule = cls._normalize_end_rule(data.get("end_rule"), fallback_days)
        elif data.get("dates_locked", False):
            node.end_rule = {"mode": "fixed", "date": node.end_date}
        else:
            node.end_rule = {"mode": "duration", "days": fallback_days}
        node.baseline_duration = data.get("baseline_duration")
        node.baseline_end = data.get("baseline_end")
        node.delay_notes = data.get("delay_notes", "")
        node.revisions = data.get("revisions", [])
        node.baseline_provisional = data.get("baseline_provisional", False)

        if "is_parallel" in data:
            node.is_parallel = data["is_parallel"]
        else:
            node.is_parallel = len(data.get("children", [])) > 0

        for child_data in data.get("children", []):
            child_node = cls.from_dict(child_data, parent=node)
            node.children.append(child_node)

        node.update_status_from_dates()
        return node

    @staticmethod
    def _money_or_none(value) -> Optional[float]:
        if value in ("", None):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Dependency traversal (used by simulate / CPM / UI)
    # ------------------------------------------------------------------

    def collect_dependencies(self) -> List['TaskNode']:
        deps = []
        if self.parent:
            siblings = self.parent.children
            try:
                idx = siblings.index(self)
                for i in range(idx + 1, len(siblings)):
                    sib = siblings[i]
                    deps.append(sib)
                    deps.extend(sib.get_all_descendants())
            except ValueError:
                pass
        return deps

    def get_all_descendants(self) -> List['TaskNode']:
        out = []
        for c in self.children:
            out.append(c)
            out.extend(c.get_all_descendants())
        return out

    def simulate_date_change(self, new_start_date: str) -> List[Dict[str, Any]]:
        impacts = []
        if not self.start_date:
            return impacts
        try:
            current_dt = datetime.strptime(self.start_date, DATE_FMT)
            new_dt = datetime.strptime(new_start_date, DATE_FMT)
            delta_days = (new_dt - current_dt).days
            if delta_days == 0:
                return impacts
            for node in self.collect_dependencies():
                if node.start_date:
                    try:
                        s = datetime.strptime(node.start_date, DATE_FMT)
                        impacts.append({
                            'node': node,
                            'old_start': node.start_date,
                            'new_start': (s + timedelta(days=delta_days)).strftime(DATE_FMT),
                            'delta': delta_days,
                        })
                    except ValueError:
                        pass
        except ValueError:
            pass
        return impacts
