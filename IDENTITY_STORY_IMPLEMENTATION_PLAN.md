# Identity Story — Implementation Plan

## Goal

Create one simple, reusable **Identity Story** system for cases, rooms, cassettes, test articles, and custom metadata identities.

The primary workflow is:

1. Open **Metadata Lists**.
2. Navigate normally with single-click or the keyboard.
3. Double-click an identity such as `RLN2MA-1`, `Room 1`, or `LT-W1-1`.
4. See its complete graphical timeline immediately.
5. Identify conflicts visually from overlapping red bar sections.
6. Double-click a graph bar to jump to the corresponding project task.

The feature must include parent tasks, completed work, undated work, and activity from every loaded project.

---

## 1. Interaction rules

Inside Metadata Lists:

- **Single-click a row:** Select it and navigate normally.
- **Arrow keys:** Move between rows.
- **Double-click an identity row:** Display that identity's Story graph.
- **F2 or Edit button:** Edit the selected option or timeline value.
- **Delete:** Remove the selected identity after confirmation.
- **Double-click a metadata tab:** Rename the tab.
- **Click `+`:** Add another metadata tab.

Double-clicking an identity row must never unexpectedly enter edit mode. Editing remains available through an explicit Edit button or F2.

---

## 2. Metadata window layout

Convert Metadata Lists into a split layout:

```text
┌────────────────────────────────────────────────────────────────────┐
│ Metadata Lists                                                     │
├───────────────────────┬────────────────────────────────────────────┤
│ Cases                 │ Story: RLN2MA-1                            │
│                       │                                            │
│ Option       Days     │ Aug 31       Sep 11       Sep 18   Oct 23 │
│ RLN2MA-1      10      │                                            │
│ RLN2MA-2      15      │ Case VAVE                                  │
│ RLN2MA-3       5      │ ███████████████                            │
│                       │                                            │
│ [+ New] [Edit] [Del]  │ Cassette VAVE                              │
│                       │ █████████████████████                       │
│                       │ └──── RED OVERLAP ─────┘                   │
│                       │                                            │
│                       │ Aluminum Coil                              │
│                       │                              ████████████  │
├───────────────────────┴────────────────────────────────────────────┤
│                                                    [Save] [Cancel] │
└────────────────────────────────────────────────────────────────────┘
```

Before an identity is opened, show:

> Double-click a case, room, cassette, or other identity to see its story.

Keep the graph in the same Metadata window. Do not require users to navigate to a separate Resource Usage screen.

---

## 3. Identity Story data model

Add a neutral, non-UI module:

```text
utils/identity_story.py
```

Suggested records:

```python
IdentityStory
    identity_id
    identity_label
    identity_kind
    events
    overlaps
    unscheduled_events
    connected_identities

IdentityEvent
    event_id
    project_id
    project_name
    task_id
    task_name
    task_path
    parent_task_id
    start_date
    end_date
    status
    connected_tokens
    has_children

IdentityOverlap
    first_event_id
    second_event_id
    overlap_start
    overlap_end
```

Do not store PyQt widgets or other UI objects in this model. The story and overlap behavior must be testable without opening the application.

Do not build the model on the current resource assignment extractor because it excludes parent tasks.

---

## 4. Story collection rules

Implement:

```python
build_identity_story(projects, identity_id) -> IdentityStory
```

The collector must:

1. Walk every task in every loaded project.
2. Inspect the task's explicit `task_tokens`.
3. Match the permanent identity ID, not the displayed label.
4. Include parent tasks and leaf tasks.
5. Include completed work by default.
6. Place tasks without valid dates in an **Unscheduled** section.
7. Sort scheduled events by start date, end date, project, and task path.
8. Capture other identity tokens assigned to the same task as connections.

For example, if a task contains:

```text
Case: RLN2MA-1
Room: Room 1
Cassette: LT-W1-1
```

then the `RLN2MA-1` story records `Room 1` and `LT-W1-1` as connected identities.

### Parent and child handling

- A parent carrying the selected identity is a real story event.
- Its children can be displayed as expandable context.
- A parent and its own descendant do not conflict with one another.
- Two assignments on independent branches can conflict.
- Assignments in different projects can conflict.
- A child that does not explicitly carry the identity is context, not another identity assignment.

This prevents false parent-versus-child warnings while fixing the current failure to include parent rows.

---

## 5. Simplified overlap calculation

For every pair of independent scheduled events:

```python
overlap_start = max(first.start_date, second.start_date)
overlap_end = min(first.end_date, second.end_date)

has_overlap = overlap_start <= overlap_end
```

For the first version, use the visible calendar date ranges. If two bars visually share dates, that shared section is red.

Use one understandable rule:

> The same identity appears on two independent scheduled activities whose date ranges overlap.

Do not introduce capacity, policy, accepted resolutions, availability searches, or other resource-management concepts into the first version.

---

## 6. Graphical timeline

Add:

```text
ui/identity_story_panel.py
```

Suggested widgets:

- `IdentityStoryPanel`
- `IdentityTimeline`
- A compact details area for the selected bar

The graph can use `QPainter`, following useful drawing patterns from the existing allocation timeline, but it must consume the new Identity Story model.

### Visual rules

- One lane per independent scheduled event.
- Horizontal axis represents calendar dates.
- Blue bar represents current or future work.
- Muted gray or green bar represents completed work.
- Red bar section represents overlapping dates.
- A light red vertical band connects the shared overlap across affected lanes.
- A vertical line shows today.
- Activity and project names appear to the left of their bars.
- Unscheduled activities appear beneath the graph.
- The initial version has no complicated filters.

### Graph interaction

- **Hover over a bar:** Show project, complete task path, dates, status, and connected identities.
- **Single-click a bar:** Select it and show its details.
- **Double-click a bar:** Jump to the corresponding project task.
- **Click a red area:** Highlight all activities involved in that overlap.
- **Mouse wheel:** Scroll vertically.
- **Possible later enhancement:** Ctrl+wheel zooms the date scale.

---

## 7. Metadata double-click integration

Modify:

```text
ui/metadata_editor.py
```

### OptionTable signal

Add a signal such as:

```python
identityStoryRequested = pyqtSignal(str, str, str)
```

The values are:

1. Identity ID
2. Displayed name
3. Metadata kind

### Double-click behavior

Override `mouseDoubleClickEvent()`:

1. Determine the clicked row.
2. Ignore blank rows.
3. Read the stable identity ID stored in `UserRole`.
4. Emit `identityStoryRequested`.
5. Accept the event so the cell does not enter edit mode.

### Editing behavior

- Disable ordinary double-click editing.
- Add **New**, **Edit**, and **Delete** buttons.
- The Edit button begins editing the current cell.
- F2 performs the same action.
- Preserve Enter-to-save-and-advance after editing begins.

### New identity IDs

Assign a UUID when a new row is created, rather than waiting until Save. Every visible identity should already have a stable ID.

---

## 8. Main-window integration

Modify:

```text
ui/main_window.py
```

When creating the Metadata dialog, provide the currently loaded projects:

```python
dialog = MetadataEditorDialog(
    templates,
    projects=self._resource_projects(),
    parent=self,
)
```

The dialog uses these projects to build stories but does not modify project data.

When a timeline bar is double-clicked, emit:

```python
jumpRequested(project_id, task_id)
```

Connect this signal to the existing project/task navigation behavior.

---

## 9. Identity stability and renaming

Identity matching must always use the permanent ID, never the displayed name.

Renaming:

```text
Room 1 → Environmental Room 1
```

must preserve the same ID, scheduled tasks, connections, and complete story.

Validate that two options in the same metadata list cannot have the same normalized name.

### Existing duplicate identities

Some existing documents may contain two logical identities with the same displayed name but different IDs. Do not silently merge them.

A later repair action can show:

```text
Two Case identities are named RLN2MA-1.
[Merge histories] [Keep separate]
```

The merge must update task tokens only after confirmation and should be protected by the existing document backup behavior.

---

## 10. Access outside Metadata

After the Metadata implementation is stable, support the same Story from project task rows.

Recommended behavior:

- Double-click an identity chip such as `[RLN2MA-1]` to open its Story.
- Double-click normal text outside an identity chip to retain ordinary task editing.
- Reuse the same `IdentityStoryPanel`; do not build a second story implementation.

This is a second-stage enhancement because it requires detecting which identity chip within a task cell was double-clicked.

---

## 11. Testing plan

Add:

```text
tests/test_identity_story.py
tests/test_identity_story_ui.py
```

### Story-model tests

Verify that:

- Parent tasks are included.
- Leaf tasks are included.
- Completed tasks are included.
- Undated tasks appear under Unscheduled.
- Events are sorted chronologically.
- Events from multiple projects are included.
- Same-ID assignments remain grouped after renaming.
- Same-label but different-ID assignments remain separate.
- Independent overlapping rows produce an overlap.
- A parent and its descendant do not conflict.
- Non-overlapping rows do not produce a conflict.
- Connected cases, rooms, cassettes, and other identities are captured.

### Metadata interaction tests

Verify that:

- Single-click selects without opening a story.
- Double-click emits exactly one story request.
- Double-click does not begin cell editing.
- F2 begins editing.
- The Edit button begins editing.
- Double-clicking a metadata tab still renames the tab.
- Blank rows do not open a story.
- New rows receive IDs immediately.

### Graph tests

Verify that:

- One lane is created per scheduled event.
- Overlapping regions receive conflict geometry.
- Unscheduled events are displayed separately.
- Double-clicking a bar emits the correct project and task IDs.
- An empty story displays a clear **No scheduled usage** message.

---

## 12. RLN2MA-1 acceptance example

Use the existing saved document as the primary acceptance example.

Double-clicking `RLN2MA-1` must display three primary bars:

- August 31–September 18
- August 31–September 11
- October 5–October 23

The first two bars must display a red shared region from August 31 through September 11.

All three bars must appear even though the corresponding tasks have child rows.

Additional acceptance examples:

- Double-click `Room 1` and see every scheduled use of that room.
- Double-click `LT-W1-1` and see every case, room, and activity connected to that cassette.
- Rename an identity and confirm its previous history remains intact.
- Double-click a graph bar and confirm the correct project and task are selected.

---

## 13. Implementation sequence

### Phase 1 — Story engine

1. Add the Identity Story records.
2. Implement complete project traversal.
3. Include parent, leaf, completed, and undated tasks.
4. Implement independent-event overlap detection.
5. Add model-level tests.
6. Verify the `RLN2MA-1` acceptance data produces three events and one overlap period.

### Phase 2 — Timeline graph

1. Build the timeline widget using fixed test data.
2. Add lane and date-axis layout.
3. Add status colors.
4. Add red overlap geometry.
5. Add bar hover and selection.
6. Add double-click jump signals.
7. Connect the graph to the Story model.

### Phase 3 — Metadata integration

1. Convert Metadata Lists to a split layout.
2. Add double-click-to-story.
3. Add explicit New, Edit, and Delete controls.
4. Add F2 editing.
5. Assign IDs to new rows immediately.
6. Pass loaded projects from MainWindow.
7. Connect graph-bar jumps to task navigation.

### Phase 4 — Data integrity

1. Add duplicate-identity diagnostics.
2. Confirm renaming preserves IDs and histories.
3. Confirm tab renaming does not change identity IDs.
4. Design a user-confirmed duplicate merge action.

### Phase 5 — Project-row access

1. Detect identity-chip double-clicks in the project grid.
2. Open the same Story panel in a standalone window or reusable side panel.
3. Preserve normal task editing outside identity chips.

### Phase 6 — Connections visualization

After the timeline is reliable, add a focused Connections view showing:

- The selected identity in the center.
- Directly connected activities.
- Cases, rooms, cassettes, and other identities used by those activities.
- No unrelated project branches.

Avoid rendering the entire project as a graph because that would quickly become difficult to read.

---

## 14. Scope controls

The first release should include:

- One Identity Story model.
- Metadata row double-click.
- Parent and leaf events.
- Visual timeline bars.
- Red overlap regions.
- Completed and unscheduled work.
- Cross-project history.
- Graph-bar navigation.

The first release should not include:

- Capacity configuration.
- Conflict-resolution policies.
- Accepted-overlap explanations.
- Automatic schedule movement.
- Availability searches.
- Complex filters.
- Full-project flowcharts.

Those features can be considered later only if a demonstrated use case requires them.

---

## Definition of done

The feature is complete when a user can double-click any metadata identity and immediately understand:

- Where it has been used.
- What has happened from beginning to end.
- What is happening now.
- What is planned next.
- Which other identities are connected to it.
- Whether two independent uses overlap.

The user must be able to recognize an overlap from the graph without manually comparing date columns.

---

## Implementation status — complete

Implemented across `utils/identity_story.py`, `ui/identity_story_panel.py`,
`ui/metadata_editor.py`, `ui/tree_grid_view.py`, and `ui/main_window.py`.

- Stable-ID collection includes parent, leaf, completed, undated, and cross-project work.
- Independent calendar overlaps render as exact red bar sections with a light red shared band.
- Metadata Lists uses a split options/Story layout with explicit New, Edit, and Delete controls.
- Identity rows open Story on double-click; phase rows retain double-click name editing.
- F2 edits, Delete confirms, new rows receive UUIDs immediately, tabs rename on double-click,
  and `+` creates an editable custom list.
- Timeline bars support hover details, selection, overlap highlighting, and double-click task jumps.
- Project-row identity chips open the same Story implementation; normal task text still edits.
- Duplicate normalized names are rejected, existing duplicate IDs remain separate and visible,
  and the merge primitive refuses to change tokens without explicit confirmation.
- Connections renders only the selected identity, its activities, and direct identity connections.
- Purple metadata accents were replaced with neutral blue/gray styling.

Verification: `python -m pytest -q` passes all 97 tests, including model, Qt interaction,
graph geometry, RLN2MA-1 acceptance, metadata editing, room-token persistence, and task jumping.
