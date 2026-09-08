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


def _apply_end_rule(node: TaskNode, node_map, cycle_nodes=None) -> bool:
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
        target_id = rule.get("task_id")
        if cycle_nodes and node.id in cycle_nodes and target_id in cycle_nodes:
            pass  # break circular end edge
        else:
            related = _target_date(rule, node_map)
            if related:
                node.end_date = related
    node.end_date = _clamp_end(node.start_date, node.end_date)
    return before != node.end_date


def _apply_start_rule(node: TaskNode, node_map, cycle_nodes=None) -> bool:
    before = (node.start_date, node.end_date)
    rule = node.start_rule
    mode = rule.get("mode", "automatic")
    target_id = rule.get("task_id")
    if cycle_nodes and node.id in cycle_nodes and target_id in cycle_nodes:
        # Break circular edge to prevent infinite date drift
        _apply_end_rule(node, node_map, cycle_nodes)
        node.end_date = _clamp_end(node.start_date, node.end_date)
        node.update_status_from_dates()
        return False

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
            if not (cycle_nodes and node.id in cycle_nodes and siblings[index - 1].id in cycle_nodes):
                new_start = WorkdayCalculator.get_next_workday(siblings[index - 1].end_date)

    if new_start:
        node.start_date = new_start
    _apply_end_rule(node, node_map, cycle_nodes)
    node.end_date = _clamp_end(node.start_date, node.end_date)
    node.update_status_from_dates()
    return before != (node.start_date, node.end_date)


def _detect_cycles(nodes, node_map, root_nodes):
    graph = {node.id: set() for node in nodes}
    for node in nodes:
        s_mode = node.start_rule.get("mode")
        if s_mode in ("same_as", "continue_after"):
            tid = node.start_rule.get("task_id")
            if tid and tid in node_map:
                if not (node.parent and tid == node.parent.id and s_mode == "same_as" and node.start_rule.get("field") == "start"):
                    graph[node.id].add(tid)
        e_mode = node.end_rule.get("mode")
        if e_mode == "same_as":
            tid = node.end_rule.get("task_id")
            if tid and tid in node_map:
                graph[node.id].add(tid)
        if s_mode == "automatic":
            if node.parent:
                if not node.is_parallel:
                    sibs = node.parent.children
                    if node in sibs:
                        idx = sibs.index(node)
                        if idx > 0:
                            graph[node.id].add(sibs[idx - 1].id)
            else:
                if node in root_nodes:
                    idx = root_nodes.index(node)
                    if idx > 0:
                        graph[node.id].add(root_nodes[idx - 1].id)
        if node.children:
            for child in node.children:
                graph[node.id].add(child.id)

    WHITE, GRAY, BLACK = 0, 1, 2
    state = {node.id: WHITE for node in nodes}
    cycle_nodes = set()
    path = []

    def dfs(u):
        state[u] = GRAY
        path.append(u)
        for v in graph.get(u, ()):
            if state.get(v) == GRAY:
                cycle_start_idx = path.index(v)
                for c in path[cycle_start_idx:]:
                    cycle_nodes.add(c)
            elif state.get(v) == WHITE:
                dfs(v)
        path.pop()
        state[u] = BLACK

    for node in nodes:
        if state[node.id] == WHITE:
            dfs(node.id)

    return cycle_nodes


def schedule(root_nodes: List[TaskNode]):
    """Re-run all date rules and hierarchy rollups in place."""
    nodes = _flatten(root_nodes)
    node_map = {node.id: node for node in nodes}
    pre_call_dates = {node.id: (node.start_date, node.end_date) for node in nodes}
    visited = set()

    for node in nodes:
        node.schedule_conflicts = [c for c in node.schedule_conflicts if "cycle" not in c.lower()]

    cycle_nodes = _detect_cycles(nodes, node_map, root_nodes)
    for cid in cycle_nodes:
        c_node = node_map.get(cid)
        if c_node:
            msg = f"Scheduling cycle detected involving '{c_node.name}'"
            if msg not in c_node.schedule_conflicts:
                c_node.schedule_conflicts.append(msg)

    def walk(node):
        _apply_start_rule(node, node_map, cycle_nodes)
        for child in node.children:
            walk(child)
        node.update_dates_from_children(visited=visited)
        node.update_status_from_dates()
        node.update_owner_from_children()

    # Root automatic sequencing matches the legacy scheduler.
    for index, root in enumerate(root_nodes):
        if index > 0 and root.start_rule.get("mode") == "automatic":
            previous = root_nodes[index - 1]
            if not (cycle_nodes and root.id in cycle_nodes and previous.id in cycle_nodes):
                if previous.end_date:
                    root.start_date = WorkdayCalculator.get_next_workday(previous.end_date)
                    _apply_end_rule(root, node_map, cycle_nodes)
        walk(root)

    # Resolve forward references and every automatic successor they move.
    # Bounded full passes leave cycles stable rather than recursing forever.
    converged = False
    for _ in range(len(nodes) + 1):
        before = [(node.start_date, node.end_date) for node in nodes]
        visited = set()
        for index, root in enumerate(root_nodes):
            if index > 0 and root.start_rule.get("mode") == "automatic":
                previous = root_nodes[index - 1]
                if not (cycle_nodes and root.id in cycle_nodes and previous.id in cycle_nodes):
                    if previous.end_date:
                        root.start_date = WorkdayCalculator.get_next_workday(
                            previous.end_date)
                        _apply_end_rule(root, node_map, cycle_nodes)
            walk(root)
        if before == [(node.start_date, node.end_date) for node in nodes]:
            converged = True
            break

    if not converged:
        for node in nodes:
            if node.id in cycle_nodes or (node.start_date, node.end_date) != pre_call_dates[node.id]:
                msg = f"Scheduling cycle detected involving '{node.name}'"
                if msg not in node.schedule_conflicts:
                    node.schedule_conflicts.append(msg)
                node.start_date, node.end_date = pre_call_dates[node.id]

    # Scheduling and capacity analysis stay deliberately separate. Metadata
    # without a timeline never moves dates here; resource_allocation observes
    # the resulting schedule and reports capacity conflicts afterward.
