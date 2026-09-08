"""Neutral, UI-free history model for stable metadata identities."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, List, Tuple


DATE_FMT = "%Y-%m-%d"
NON_IDENTITY_KINDS = {"phase", "campaign", "header"}


@dataclass(frozen=True)
class ConnectedIdentity:
    identity_id: str
    label: str
    kind: str


@dataclass(frozen=True)
class IdentityContext:
    task_id: str
    task_name: str
    task_path: str


@dataclass(frozen=True)
class IdentityEvent:
    event_id: str
    project_id: str
    project_name: str
    task_id: str
    task_name: str
    task_path: str
    parent_task_id: str
    start_date: str
    end_date: str
    status: str
    connected_tokens: Tuple[ConnectedIdentity, ...]
    has_children: bool
    ancestor_task_ids: Tuple[str, ...] = ()
    child_context: Tuple[IdentityContext, ...] = ()

    @property
    def scheduled(self):
        return _valid_span(self.start_date, self.end_date)


@dataclass(frozen=True)
class IdentityOverlap:
    first_event_id: str
    second_event_id: str
    overlap_start: str
    overlap_end: str


@dataclass
class IdentityStory:
    identity_id: str
    identity_label: str
    identity_kind: str
    events: List[IdentityEvent] = field(default_factory=list)
    overlaps: List[IdentityOverlap] = field(default_factory=list)
    unscheduled_events: List[IdentityEvent] = field(default_factory=list)
    connected_identities: List[ConnectedIdentity] = field(default_factory=list)

    @property
    def all_events(self):
        return list(self.events) + list(self.unscheduled_events)


@dataclass(frozen=True)
class DuplicateIdentityGroup:
    kind: str
    normalized_label: str
    label: str
    identity_ids: Tuple[str, ...]


def _parse(value):
    try:
        return datetime.strptime(str(value or ""), DATE_FMT)
    except ValueError:
        return None


def _valid_span(start, end):
    first, last = _parse(start), _parse(end)
    return first is not None and last is not None and first <= last


def _normal(value):
    return " ".join(str(value or "").split()).casefold()


def _token_id(token):
    return str(token.get("id") or "")


def _identity_tokens(node):
    """Stable tokens plus resources-only assignments from older documents."""
    from utils.resource_allocation import canonical_resource_kind, legacy_resource_id
    tokens = [dict(value) for value in
              (getattr(node, "task_tokens", None) or [])]
    represented = {(canonical_resource_kind(value.get("kind")),
                    str(value.get("label") or "").strip().casefold())
                   for value in tokens}
    for kind, label in (getattr(node, "resources", None) or {}).items():
        key = (canonical_resource_kind(kind), str(label).strip().casefold())
        if label and key not in represented:
            tokens.append({"id": legacy_resource_id(kind, label),
                           "kind": kind, "label": label})
    return tokens


def _walk(roots):
    def descend(node, path, ancestors):
        current_path = f"{path} > {node.name}" if path else node.name
        yield node, current_path, tuple(ancestors)
        for child in node.children:
            yield from descend(child, current_path, (*ancestors, node.id))
    for root in roots or []:
        yield from descend(root, "", ())


def _context_for(node, path):
    context = []

    def descend(child, prefix):
        child_path = f"{prefix} > {child.name}"
        context.append(IdentityContext(child.id, child.name, child_path))
        for grandchild in child.children:
            descend(grandchild, child_path)
    for child in node.children:
        descend(child, path)
    return tuple(context)


def _connections(tokens, selected_id):
    found = {}
    for token in tokens or []:
        kind = str(token.get("kind") or "identity")
        identity_id = _token_id(token)
        label = str(token.get("label") or "").strip()
        if (not identity_id or not label or identity_id == selected_id or
                kind.casefold() in NON_IDENTITY_KINDS):
            continue
        found[identity_id] = ConnectedIdentity(identity_id, label, kind)
    return tuple(sorted(found.values(), key=lambda value:
                        (value.kind.casefold(), value.label.casefold(),
                         value.identity_id)))


def _independent(first, second):
    return (first.task_id not in second.ancestor_task_ids and
            second.task_id not in first.ancestor_task_ids)


_CHIP_LABEL = re.compile(r"\[([^\[\]]+)\]")


def _path_parts(event):
    """Return the visible hierarchy for an event, including its task name."""
    parts = [value.strip() for value in str(event.task_path or "").split(">")
             if value.strip()]
    task_name = str(event.task_name or "Task").strip() or "Task"
    if not parts:
        return [task_name]
    if _normal(parts[-1]) != _normal(task_name):
        parts.append(task_name)
    return parts


def _part_key(part):
    """Comparable key for one breadcrumb segment.

    Chip-prefixed names such as ``[Lab Testing] - no clear timeline yet``
    belong with sibling ``Lab Testing`` rows, so uniqueness still walks
    up to the shared parent instead of treating the leaf as already unique.
    """
    text = str(part or "").strip()
    chip = _CHIP_LABEL.search(text)
    if chip:
        return _normal(chip.group(1))
    return _normal(text)


def shortest_unique_event_labels(events):
    """Return the shortest recognizable breadcrumb for every story event.

    The project is displayed separately in the timeline, so path candidates
    only have to be unique among equally named projects. If two records still
    have the exact same full path, connected identities and dates are used as
    meaningful discriminators before a deterministic sequence number is added.

    A parent is always kept when the event has one. A uniquely worded leaf
    such as ``[Lab Testing] - no clear timeline yet`` must not drop the
    parent prefix that sibling Lab Testing rows still show.
    """
    values = list(events or [])
    parts_by_id = {value.event_id: _path_parts(value) for value in values}
    keys_by_id = {event_id: tuple(_part_key(part) for part in parts)
                  for event_id, parts in parts_by_id.items()}
    candidates = {}
    for value in values:
        parts = parts_by_id[value.event_id]
        chosen = " › ".join(parts)
        min_depth = 2 if len(parts) > 1 else 1
        for depth in range(min_depth, len(parts) + 1):
            suffix = keys_by_id[value.event_id][-depth:]
            matching = [other for other in values
                        if (_normal(other.project_name) ==
                            _normal(value.project_name) and
                            keys_by_id[other.event_id][-depth:] == suffix)]
            if len(matching) == 1:
                chosen = " › ".join(parts[-depth:])
                break
        candidates[value.event_id] = chosen

    # Exact duplicate paths can exist in imported data. Make the visible pair
    # of breadcrumb + project meaningful before falling back to an ordinal.
    duplicate_groups = {}
    for value in values:
        key = (_normal(value.project_name), _normal(candidates[value.event_id]))
        duplicate_groups.setdefault(key, []).append(value)
    for group in duplicate_groups.values():
        if len(group) < 2:
            continue
        contextual = {}
        for value in group:
            connections = " · ".join(token.label for token in
                                      value.connected_tokens[:2])
            contextual[value.event_id] = connections
        if len({_normal(text) for text in contextual.values() if text}) == len(group):
            for value in group:
                candidates[value.event_id] += f" · {contextual[value.event_id]}"
            continue
        dated = {}
        for value in group:
            dated[value.event_id] = (
                f"{value.start_date or 'Unscheduled'}–"
                f"{value.end_date or 'Unscheduled'}")
        if len({_normal(text) for text in dated.values()}) == len(group):
            for value in group:
                candidates[value.event_id] += f" · {dated[value.event_id]}"
            continue
        for number, value in enumerate(sorted(group, key=lambda item: item.event_id), 1):
            candidates[value.event_id] += f" · occurrence {number}"
    return candidates


def event_context_label(event, connection_limit=2):
    """Compact second line for a lane: project plus useful identities."""
    labels = [value.label for value in event.connected_tokens]
    visible = labels[:max(0, connection_limit)]
    parts = [event.project_name, *visible]
    remaining = len(labels) - len(visible)
    if remaining:
        parts.append(f"+{remaining} more")
    return " · ".join(str(value) for value in parts if str(value).strip())


def build_identity_story(projects, identity_id, identity_label=None,
                         identity_kind=None) -> IdentityStory:
    """Collect the complete cross-project story for one permanent option ID."""
    identity_id = str(identity_id or "")
    scheduled, unscheduled = [], []
    inferred_label, inferred_kind = "", "identity"
    connections = {}
    for project in projects or []:
        project_id = str(project.get("id") or project.get("project_id") or "project")
        project_name = str(project.get("name") or "Project")
        for node, path, ancestors in _walk(project.get("roots", [])):
            tokens = _identity_tokens(node)
            match = next((token for token in tokens
                          if _token_id(token) == identity_id), None)
            if match is None:
                continue
            inferred_label = str(match.get("label") or inferred_label)
            inferred_kind = str(match.get("kind") or inferred_kind)
            connected = _connections(tokens, identity_id)
            for value in connected:
                connections[value.identity_id] = value
            event = IdentityEvent(
                event_id=f"{project_id}:{node.id}",
                project_id=project_id,
                project_name=project_name,
                task_id=node.id,
                task_name=node.name,
                task_path=path,
                parent_task_id=node.parent.id if node.parent else "",
                start_date=node.start_date or "",
                end_date=node.end_date or "",
                status=node.status,
                connected_tokens=connected,
                has_children=bool(node.children),
                ancestor_task_ids=ancestors,
                child_context=_context_for(node, path),
            )
            (scheduled if event.scheduled else unscheduled).append(event)

    scheduled.sort(key=lambda event: (
        event.start_date, event.end_date, event.project_name.casefold(),
        event.task_path.casefold(), event.event_id))
    unscheduled.sort(key=lambda event: (
        event.project_name.casefold(), event.task_path.casefold(), event.event_id))
    overlaps = []
    for index, first in enumerate(scheduled):
        for second in scheduled[index + 1:]:
            if not _independent(first, second):
                continue
            start = max(first.start_date, second.start_date)
            end = min(first.end_date, second.end_date)
            if start <= end:
                overlaps.append(IdentityOverlap(
                    first.event_id, second.event_id, start, end))
    overlaps.sort(key=lambda value: (
        value.overlap_start, value.overlap_end,
        value.first_event_id, value.second_event_id))
    return IdentityStory(
        identity_id=identity_id,
        identity_label=str(identity_label or inferred_label or "Identity"),
        identity_kind=str(identity_kind or inferred_kind or "identity"),
        events=scheduled,
        overlaps=overlaps,
        unscheduled_events=unscheduled,
        connected_identities=sorted(
            connections.values(), key=lambda value:
            (value.kind.casefold(), value.label.casefold(), value.identity_id)),
    )


def find_duplicate_identities(options: Iterable[dict]):
    """Diagnose same-list duplicate labels without merging stable IDs."""
    grouped = {}
    labels = {}
    for option in options or []:
        if option.get("record_type") == "header":
            continue
        identity_id = str(option.get("id") or "")
        label = str(option.get("name") or option.get("label") or "").strip()
        kind = str(option.get("kind") or "identity")
        if not identity_id or not label:
            continue
        key = (kind.casefold(), _normal(label))
        grouped.setdefault(key, set()).add(identity_id)
        labels[key] = label
    return [DuplicateIdentityGroup(kind, normalized, labels[(kind, normalized)],
                                   tuple(sorted(identity_ids)))
            for (kind, normalized), identity_ids in sorted(grouped.items())
            if len(identity_ids) > 1]


def merge_identity_ids(projects, source_ids, target_id, confirmed=False):
    """Confirmation-gated merge primitive for a future backed-up repair UI.

    This intentionally changes only explicit task tokens. Callers must create
    the normal document backup before passing ``confirmed=True``.
    """
    if not confirmed:
        raise PermissionError("Identity merge requires explicit confirmation")
    sources = {str(value) for value in source_ids if str(value) != str(target_id)}
    changed = 0
    for project in projects or []:
        for node, _path, _ancestors in _walk(project.get("roots", [])):
            for token in getattr(node, "task_tokens", None) or []:
                if _token_id(token) in sources:
                    token["id"] = str(target_id)
                    changed += 1
    return changed
