"""Document-wide resource assignment and capacity-conflict analysis.

Scheduling decides task dates.  This module only observes those dates; it
never mutates scheduling rules, baselines, delay history, or displayed dates.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import hashlib
import re
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple


DATE_FMT = "%Y-%m-%d"
NON_RESOURCE_KINDS = {"phase", "campaign", "header"}


def _normal(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def canonical_resource_kind(kind: str) -> str:
    k = _normal(kind)
    if k in {"article", "cassette", "test article", "test_article"}:
        return "cassette"
    return k


def legacy_resource_id(kind, label) -> str:
    return f"legacy:{_normal(canonical_resource_kind(kind))}:{_normal(label)}"


@dataclass(frozen=True)
class ResourceDefinition:
    id: str
    type: str
    label: str
    capacity: int = 1
    conflict_enabled: bool = True
    active: bool = True
    home_location: str = ""
    notes: str = ""
    policy: str = "warning"

    @classmethod
    def from_dict(cls, value):
        value = dict(value or {})
        kind = str(value.get("type") or value.get("kind") or "resource")
        label = str(value.get("label") or value.get("name") or "Resource")
        return cls(
            id=str(value.get("id") or legacy_resource_id(kind, label)),
            type=kind, label=label,
            capacity=max(1, int(value.get("capacity", 1) or 1)),
            conflict_enabled=bool(value.get(
                "conflict_enabled", kind.casefold() != "phase")),
            active=bool(value.get("active", True)),
            home_location=str(value.get("home_location") or ""),
            notes=str(value.get("notes") or ""),
            policy=str(value.get("policy") or "warning"),
        )

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ResourceAssignment:
    id: str
    project_id: str
    project_name: str
    task_id: str
    task_name: str
    task_path: str
    resource_type: str
    resource_id: str
    resource_label: str
    location_label: str
    start_date: str
    end_date: str
    occupied_dates: Tuple[str, ...]
    capacity_units: int = 1
    status: str = "Not Started"
    ancestor_task_ids: Tuple[str, ...] = ()

    @property
    def workdays(self):
        return len(self.occupied_dates)


@dataclass(frozen=True)
class ResourceConflict:
    id: str
    resource_type: str
    resource_id: str
    resource_label: str
    assignment_ids: Tuple[str, ...]
    task_ids: Tuple[str, ...]
    project_ids: Tuple[str, ...]
    overlap_start: str
    overlap_end: str
    overlap_workdays: int
    locations: Tuple[str, ...]
    capacity: int
    peak_usage: int
    assignment_signature: str = ""
    resolution_state: str = "unresolved"
    resolution_reason: str = ""
    resolution_timestamp: str = ""


@dataclass
class AnalysisResult:
    assignments: List[ResourceAssignment] = field(default_factory=list)
    conflicts: List[ResourceConflict] = field(default_factory=list)
    definitions: List[ResourceDefinition] = field(default_factory=list)

    @property
    def unresolved(self):
        return [c for c in self.conflicts if c.resolution_state != "accepted"]

    def conflicts_for_task(self, task_id):
        return [c for c in self.conflicts if task_id in c.task_ids]

    def assignments_for_task(self, task_id):
        return [a for a in self.assignments if a.task_id == task_id]


def definition_map(values: Iterable[Mapping]) -> Dict[str, ResourceDefinition]:
    result = {}
    for value in values or []:
        definition = (value if isinstance(value, ResourceDefinition)
                      else ResourceDefinition.from_dict(value))
        result[definition.id] = definition
    return result


def derive_definitions(projects, existing=()) -> List[ResourceDefinition]:
    """Migrate definitions from assigned tokens/resources without changing tasks."""
    by_id = definition_map(existing)
    for project in projects or []:
        for node, _path in _walk_project(project.get("roots", [])):
            for token in _resource_tokens(node):
                kind = str(token.get("kind") or "resource")
                label = str(token.get("label") or "").strip()
                if not label or kind.casefold() in NON_RESOURCE_KINDS:
                    continue
                resource_id = str(token.get("id") or legacy_resource_id(kind, label))
                if resource_id not in by_id:
                    by_id[resource_id] = ResourceDefinition(
                        resource_id, kind, label,
                        conflict_enabled=kind.casefold() != "phase")
                elif by_id[resource_id].label != label:
                    old = by_id[resource_id]
                    by_id[resource_id] = ResourceDefinition(
                        old.id, old.type, label, old.capacity,
                        old.conflict_enabled, old.active, old.home_location,
                        old.notes, old.policy)
    return list(by_id.values())


def _walk_project(roots):
    def walk(node, prefix):
        path = f"{prefix} > {node.name}" if prefix else node.name
        yield node, path
        for child in node.children:
            yield from walk(child, path)
    for root in roots or []:
        yield from walk(root, "")


def _resource_tokens(node):
    """Return all stable token assignments, plus legacy resources if absent."""
    tokens = [dict(token) for token in (getattr(node, "task_tokens", None) or [])]
    represented = {
        (canonical_resource_kind(token.get("kind")), _normal(token.get("label")))
        for token in tokens
    }
    for kind, label in (getattr(node, "resources", None) or {}).items():
        if not label or (canonical_resource_kind(kind), _normal(label)) in represented:
            continue
        tokens.append({"kind": kind, "label": label,
                       "id": legacy_resource_id(kind, label)})
    # A duplicate token must not consume the same physical resource twice.
    unique = []
    seen = set()
    for token in tokens:
        kind = str(token.get("kind") or "resource")
        label = str(token.get("label") or "").strip()
        resource_id = str(token.get("id") or legacy_resource_id(kind, label))
        key = (canonical_resource_kind(kind), resource_id)
        if label and key not in seen:
            seen.add(key)
            unique.append({**token, "kind": kind, "label": label,
                           "id": resource_id})
    return unique


def _calendar(project):
    metadata = project.get("metadata", {}) or {}
    return set(metadata.get("holidays", []) or []), bool(
        metadata.get("exclude_weekends", True))


def occupied_dates(start, end, holidays=(), exclude_weekends=True) -> Tuple[str, ...]:
    try:
        current = datetime.strptime(start, DATE_FMT)
        finish = datetime.strptime(end, DATE_FMT)
    except (TypeError, ValueError):
        return ()
    if current > finish:
        return ()
    holidays = set(holidays or [])
    dates = []
    while current <= finish:
        text = current.strftime(DATE_FMT)
        if text not in holidays and (not exclude_weekends or current.weekday() < 5):
            dates.append(text)
        current += timedelta(days=1)
    return tuple(dates)


def extract_assignments(projects, definitions=()) -> List[ResourceAssignment]:
    defs = definition_map(definitions)
    assignments = []
    for project in projects or []:
        project_id = str(project.get("id") or project.get("project_id") or "project")
        project_name = str(project.get("name") or "Project")
        holidays, exclude_weekends = _calendar(project)
        for node, path in _walk_project(project.get("roots", [])):
            if not node.start_date or not node.end_date:
                continue
            occupied = occupied_dates(node.start_date, node.end_date,
                                      holidays, exclude_weekends)
            if not occupied:
                continue
            tokens = _resource_tokens(node)
            rooms = [token["label"] for token in tokens
                     if token["kind"].casefold() == "room"]
            location = ", ".join(rooms) if rooms else "Location not assigned"
            for token in tokens:
                kind = token["kind"]
                if kind.casefold() in NON_RESOURCE_KINDS:
                    continue
                resource_id = token["id"]
                assignment_id = hashlib.sha256(
                    f"{project_id}|{node.id}|{resource_id}".encode()).hexdigest()[:20]
                ancestors = []
                current = node.parent
                while current is not None:
                    ancestors.append(current.id)
                    current = current.parent
                assignments.append(ResourceAssignment(
                    assignment_id, project_id, project_name, node.id, node.name,
                    path, kind, resource_id, token["label"], location,
                    node.start_date, node.end_date, occupied, 1, node.status,
                    tuple(ancestors)))
    return assignments


def _conflict_id(resource_id, assignment_ids, start, end):
    raw = "|".join([resource_id, *sorted(assignment_ids), start, end])
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def analyze_projects(projects, definitions=(), resolutions=(), attach=True):
    definitions = derive_definitions(projects, definitions)
    defs = definition_map(definitions)
    assignments = extract_assignments(projects, definitions)
    by_resource = {}
    for assignment in assignments:
        by_resource.setdefault(assignment.resource_id, []).append(assignment)
    resolutions = {str(value.get("conflict_id") or value.get("id")): dict(value)
                   for value in (resolutions or [])}
    conflicts = []
    for resource_id, group in by_resource.items():
        definition = defs.get(resource_id) or ResourceDefinition(
            resource_id, group[0].resource_type, group[0].resource_label)
        # Usage/history is collected for every identity. Only explicitly
        # enabled identities participate in overlap-conflict detection.
        if not definition.conflict_enabled:
            continue
        capacity = definition.capacity
        occupancy = {}
        for assignment in group:
            for date in assignment.occupied_dates:
                occupancy.setdefault(date, []).append(assignment)
        conflict_days = []
        for date in sorted(occupancy):
            raw_present = occupancy[date]
            # A child explicitly carrying its parent's same identity describes
            # the same reservation, not another unit of capacity.
            present = [assignment for assignment in raw_present
                       if not any(
                           other.project_id == assignment.project_id and
                           other.task_id in assignment.ancestor_task_ids
                           for other in raw_present)]
            occupancy[date] = present
            if sum(a.capacity_units for a in present) > capacity:
                conflict_days.append((date, tuple(sorted(
                    (a.id for a in present)))))
        # Merge adjacent occupied conflict days with the same participants.
        start = 0
        while start < len(conflict_days):
            ids = conflict_days[start][1]
            end = start
            while end + 1 < len(conflict_days) and conflict_days[end + 1][1] == ids:
                end += 1
            dates = [row[0] for row in conflict_days[start:end + 1]]
            involved = [a for a in group if a.id in ids]
            conflict_id = _conflict_id(resource_id, ids, dates[0], dates[-1])
            resolution = resolutions.get(conflict_id, {})
            signature = hashlib.sha256("|".join(sorted(
                f"{a.id}:{a.start_date}:{a.end_date}" for a in involved
            )).encode()).hexdigest()[:20]
            accepted = (resolution.get("state") == "accepted" and
                        resolution.get("assignment_signature") == signature)
            conflicts.append(ResourceConflict(
                conflict_id, definition.type, resource_id, definition.label,
                ids, tuple(sorted({a.task_id for a in involved})),
                tuple(sorted({a.project_id for a in involved})),
                dates[0], dates[-1], len(dates),
                tuple(sorted({a.location_label for a in involved})), capacity,
                max(sum(a.capacity_units for a in occupancy[d]) for d in dates),
                signature, "accepted" if accepted else "unresolved",
                str(resolution.get("reason") or "") if accepted else "",
                str(resolution.get("timestamp") or "") if accepted else ""))
            start = end + 1
    result = AnalysisResult(assignments, conflicts, definitions)
    if attach:
        attach_analysis(projects, result)
    return result


def attach_analysis(projects, result):
    nodes = {}
    assignments = {a.id: a for a in result.assignments}
    for project in projects or []:
        project_id = str(project.get("id") or project.get("project_id") or "project")
        for node, _path in _walk_project(project.get("roots", [])):
            nodes[(project_id, node.id)] = node
            node.schedule_conflicts = [c for c in node.schedule_conflicts if "cycle" in c.lower()]
            node.resource_conflict_details = []
    for conflict in result.conflicts:
        conflict_assignments = [assignments[a_id] for a_id in conflict.assignment_ids if a_id in assignments]
        for a in conflict_assignments:
            node = nodes.get((a.project_id, a.task_id))
            if node is None:
                continue
            others = [o for o in conflict_assignments
                      if not (o.project_id == a.project_id and o.task_id == a.task_id)]
            other_text = "; ".join(
                f"{o.task_path} · {o.location_label}" for o in others)
            state = "accepted" if conflict.resolution_state == "accepted" else "unresolved"
            text = (f"{conflict.resource_label}: {conflict.overlap_workdays} "
                    f"overlapping workday(s), {conflict.overlap_start} through "
                    f"{conflict.overlap_end} · {other_text} · {state}")
            if text not in node.schedule_conflicts:
                node.schedule_conflicts.append(text)
                node.resource_conflict_details.append({
                    "conflict_id": conflict.id, "resource_id": conflict.resource_id,
                    "resource_label": conflict.resource_label,
                    "state": conflict.resolution_state, "text": text,
                })


def accept_conflict(conflict, reason, timestamp=None):
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("An explanation is required")
    return {
        "conflict_id": conflict.id, "state": "accepted", "reason": reason,
        "assignment_signature": conflict.assignment_signature,
        "timestamp": timestamp or datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def next_available_start(assignment, all_assignments, capacity=1,
                         holidays=(), exclude_weekends=True, max_days=3650):
    """Find the first later continuous work block; never applies the result."""
    others_by_date = {}
    for other in all_assignments:
        if other.resource_id != assignment.resource_id or other.id == assignment.id:
            continue
        # Ancestor/descendant in same project sharing same reservation do not compete
        if other.project_id == assignment.project_id and (
            other.task_id in assignment.ancestor_task_ids or
            assignment.task_id in getattr(other, "ancestor_task_ids", ())
        ):
            continue
        for date in other.occupied_dates:
            others_by_date.setdefault(date, []).append(other)

    occupied_by_others = {}
    for date, raw_others in others_by_date.items():
        present = [o for o in raw_others
                   if not any(o2.project_id == o.project_id and o2.task_id in o.ancestor_task_ids
                              for o2 in raw_others)]
        occupied_by_others[date] = sum(o.capacity_units for o in present)

    needed = max(1, assignment.workdays)
    candidate = datetime.strptime(assignment.start_date, DATE_FMT) + timedelta(days=1)
    holidays = set(holidays or [])
    for _ in range(max_days):
        candidate_text = candidate.strftime(DATE_FMT)
        if (candidate_text in holidays or
                (exclude_weekends and candidate.weekday() >= 5)):
            candidate += timedelta(days=1)
            continue
        start_text = candidate.strftime(DATE_FMT)
        block = []
        cursor = candidate
        while len(block) < needed:
            text = cursor.strftime(DATE_FMT)
            if text not in holidays and (not exclude_weekends or cursor.weekday() < 5):
                block.append(text)
            cursor += timedelta(days=1)
        if all(occupied_by_others.get(date, 0) + assignment.capacity_units <= capacity
               for date in block):
            return start_text, block[-1]
        candidate += timedelta(days=1)
    return None


def next_available_block(task_assignments, all_assignments, definitions=(),
                         holidays=(), exclude_weekends=True, max_days=3650):
    """Preview the first block available for every resource on one task or moved subtree."""
    task_assignments = list(task_assignments)
    if not task_assignments:
        return None
    defs = definition_map(definitions)
    from utils.workday_calculator import WorkdayCalculator

    moved_ids = {a.id for a in task_assignments}
    moved_project_ids = {a.project_id for a in task_assignments}
    relevant_resource_ids = {a.resource_id for a in task_assignments}

    # Group external assignments by (resource_id, date) with ancestor deduplication
    external_by_res_date = {r_id: {} for r_id in relevant_resource_ids}
    for other in all_assignments:
        if other.id in moved_ids or other.resource_id not in relevant_resource_ids:
            continue
        if other.project_id in moved_project_ids and any(
            other.task_id in a.ancestor_task_ids or a.task_id in getattr(other, "ancestor_task_ids", ())
            for a in task_assignments if a.resource_id == other.resource_id
        ):
            continue
        res_dict = external_by_res_date[other.resource_id]
        for date in other.occupied_dates:
            res_dict.setdefault(date, []).append(other)

    external_occupancy = {r_id: {} for r_id in relevant_resource_ids}
    for r_id, date_dict in external_by_res_date.items():
        for date, raw_others in date_dict.items():
            present = [o for o in raw_others
                       if not any(o2.project_id == o.project_id and o2.task_id in o.ancestor_task_ids
                                  for o2 in raw_others)]
            external_occupancy[r_id][date] = sum(o.capacity_units for o in present)

    anchor_start = min(a.start_date for a in task_assignments)
    candidate = datetime.strptime(anchor_start, DATE_FMT) + timedelta(days=1)
    holidays = set(holidays or [])

    assignment_offsets = []
    for a in task_assignments:
        if a.start_date >= anchor_start:
            k = max(0, WorkdayCalculator.calculate_duration(
                anchor_start, a.start_date, holidays=holidays, exclude_weekends=exclude_weekends) - 1)
        else:
            k = 0
        assignment_offsets.append((a, k, a.workdays))

    for _ in range(max_days):
        candidate_text = candidate.strftime(DATE_FMT)
        if (candidate_text in holidays or
                (exclude_weekends and candidate.weekday() >= 5)):
            candidate += timedelta(days=1)
            continue

        available = True
        moved_on_date = {}
        candidate_dates_all = []

        for a, k, dur in assignment_offsets:
            a_start = WorkdayCalculator.add_workdays(
                candidate_text, k + 1, holidays=holidays, exclude_weekends=exclude_weekends)
            a_block = []
            cursor = datetime.strptime(a_start, DATE_FMT)
            while len(a_block) < dur:
                text = cursor.strftime(DATE_FMT)
                if text not in holidays and (not exclude_weekends or cursor.weekday() < 5):
                    a_block.append(text)
                cursor += timedelta(days=1)
            candidate_dates_all.extend(a_block)
            for d in a_block:
                moved_on_date.setdefault((a.resource_id, d), []).append(a)

        for (r_id, date), raw_moved in moved_on_date.items():
            cap = defs.get(r_id, ResourceDefinition(r_id, raw_moved[0].resource_type, raw_moved[0].resource_label)).capacity
            present_moved = [m for m in raw_moved
                             if not any(m2.project_id == m.project_id and m2.task_id in m.ancestor_task_ids
                                        for m2 in raw_moved)]
            moved_load = sum(m.capacity_units for m in present_moved)
            ext_load = external_occupancy[r_id].get(date, 0)
            if ext_load + moved_load > cap:
                available = False
                break

        if available:
            min_block_date = min(candidate_dates_all) if candidate_dates_all else candidate_text
            max_block_date = max(candidate_dates_all) if candidate_dates_all else candidate_text
            return min_block_date, max_block_date

        candidate += timedelta(days=1)

    return None
