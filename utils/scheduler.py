"""Single scheduling engine for automatic, fixed and task-related dates."""
from datetime import datetime, timedelta
from typing import Iterable, List

from models.task_node import TaskNode, DATE_FMT, _clamp_end
from utils.workday_calculator import WorkdayCalculator


def _flatten(roots: Iterable[TaskNode]) -> List[TaskNode]:
    out = []

    def walk(nodes):
        for node in nodes:
            out.append(node)
            walk(node.children)
    walk(roots)
    return out


def _rollup_only(node: TaskNode, visited: set):
    for child in node.children:
        _rollup_only(child, visited)
    node.update_dates_from_children(visited=visited)


def _target_date(rule, node_map):
    target = node_map.get(rule.get("task_id"))
    if target is None:
        return None
    return target.start_date if rule.get("field") == "start" else target.end_date


def _add_offset(date_str, offset, unit):
    if not date_str or not offset:
        return date_str
    if unit == "calendar_days":
        try:
            return (datetime.strptime(date_str, DATE_FMT) + timedelta(days=offset)).strftime(DATE_FMT)
        except ValueError:
            return date_str
    # add_workdays is inclusive, so N extra workdays requires N+1 duration.
    return WorkdayCalculator.add_workdays(date_str, int(offset) + 1)


def _apply_end_rule(node: TaskNode, node_map) -> bool:
    if node.children:
        return False  # group dates are child rollups
    before = node.end_date
    rule = node.end_rule
    mode = rule.get("mode", "duration")
    if mode == "duration" and node.start_date:
        node.end_date = WorkdayCalculator.add_workdays(
            node.start_date, max(1, int(rule.get("days", 1) or 1)))
    elif mode == "fixed" and rule.get("date"):
        node.end_date = rule["date"]
    elif mode == "same_as":
        related = _target_date(rule, node_map)
        if related:
            node.end_date = related
    node.end_date = _clamp_end(node.start_date, node.end_date)
    return before != node.end_date


def _apply_start_rule(node: TaskNode, node_map) -> bool:
    before = (node.start_date, node.end_date)
    rule = node.start_rule
    mode = rule.get("mode", "automatic")
    new_start = None
    if mode == "fixed":
        new_start = rule.get("date")
    elif mode == "same_as":
        new_start = _target_date(rule, node_map)
        new_start = _add_offset(new_start, rule.get("offset", 0),
                                rule.get("offset_unit", "workdays"))
    elif mode == "continue_after":
        related = _target_date(rule, node_map)
        if related:
            new_start = WorkdayCalculator.get_next_workday(related)
            new_start = _add_offset(new_start, rule.get("offset", 0),
                                    rule.get("offset_unit", "workdays"))
    elif node.parent:
        # Existing automatic sibling behavior remains the default.
        siblings = node.parent.children
        try:
            index = siblings.index(node)
        except ValueError:
            index = -1
        if node.is_parallel and node.parent.start_date:
            new_start = node.parent.start_date
        elif index == 0:
            new_start = node.parent.start_date
        elif index > 0 and siblings[index - 1].end_date:
            new_start = WorkdayCalculator.get_next_workday(siblings[index - 1].end_date)

    if new_start:
        node.start_date = new_start
    _apply_end_rule(node, node_map)
    node.update_status_from_dates()
    return before != (node.start_date, node.end_date)


def schedule(root_nodes: List[TaskNode]):
    """Re-run all date rules and hierarchy rollups in place."""
    nodes = _flatten(root_nodes)
    node_map = {node.id: node for node in nodes}
    visited = set()

    def walk(node):
        _apply_start_rule(node, node_map)
        for child in node.children:
            walk(child)
        node.update_dates_from_children(visited=visited)
        node.update_status_from_dates()
        node.update_owner_from_children()

    # Root automatic sequencing matches the legacy scheduler.
    for index, root in enumerate(root_nodes):
        if index > 0 and root.start_rule.get("mode") == "automatic":
            previous = root_nodes[index - 1]
            if previous.end_date:
                root.start_date = WorkdayCalculator.get_next_workday(previous.end_date)
                _apply_end_rule(root, node_map)
        walk(root)

    # Resolve forward references. Bounded iteration leaves cycles stable rather
    # than recursing forever; the UI conflict layer reports them to the user.
    for _ in range(len(nodes) + 1):
        changed = False
        for node in nodes:
            if node.start_rule.get("mode") in {"same_as", "continue_after"}:
                changed = _apply_start_rule(node, node_map) or changed
            if node.end_rule.get("mode") == "same_as":
                changed = _apply_end_rule(node, node_map) or changed
        for root in root_nodes:
            _rollup_only(root, visited)
        if not changed:
            break

    # Scheduling and capacity analysis stay deliberately separate. Metadata
    # without a timeline never moves dates here; resource_allocation observes
    # the resulting schedule and reports capacity conflicts afterward.
