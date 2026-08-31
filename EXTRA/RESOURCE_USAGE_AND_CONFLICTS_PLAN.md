# Resource Usage and Conflict Management Plan

Status: implemented and acceptance-verified on 2026-08-30.

Date: 2026-08-29

## 1. Purpose

The tracker must answer two related questions whenever a conflict-enabled
metadata option, such as Case `RLN2-1`, is placed on scheduled work:

1. **Is this resource already being used somewhere else during the same time?**
2. **Where is this resource being used, for what work, in which location, and
   for how long?**

The implementation must detect conflicts without silently changing the user's
plan. It must also provide a resource rundown that is useful before, during,
and after scheduling.

Conflict analysis applies to options from every metadata list except **Project
Phases**. Cases, Rooms, Cassettes/Test Articles, Lab Testing options, and options
from user-created metadata tabs are conflict-enabled by default. Project Phases
remain reusable workflow/classification options and never consume capacity.

The design uses one generic engine rather than adding a separate scheduler for
every metadata list. Each conflict-enabled option is capacity 1 by default.

## 2. Confirmed pre-implementation gap (closed)

Before this implementation, the scheduler treated metadata selections as descriptive scheduling
tokens. It does not calculate availability and does not populate
`TaskNode.schedule_conflicts` for overlapping resource assignments.

The UI already has partial display hooks for conflict text, but there is no
resource allocation analyzer feeding those hooks. There is also no consolidated
resource-usage window. `Refresh All` recalculates task dates and repaints the
views; it does not currently recompute resource conflicts.

Closing that gap required a real allocation analyzer and a dedicated
usage interface. It cannot be completed by adding another warning message to
the existing scheduler.

## 3. User-facing outcome

### 3.1 Assigning a Case

When a user chooses `=case` and selects `RLN2-1`, the row should immediately
retain the Case connection and run resource analysis after the selection is
committed.

If no overlap exists, no blocking dialog should interrupt entry. A small status
message may say:

> RLN2-1 assigned · available for the scheduled period

If an overlap exists, show a compact, actionable warning:

> **RLN2-1 is already in use for 8 overlapping workdays**
>
> DOE Type 1 · Room 1 · 2026-09-08 through 2026-10-02  
> DOE Type 2 · Room 3 · 2026-09-15 through 2026-10-10

Available actions:

- **Keep overlap** — requires an optional or required explanation, depending on
  the decision in Section 15.
- **Jump to conflict** — selects and scrolls to the other task.
- **Use another Case** — returns focus to the Task cell lookup.
- **Move to next available date** — previews the proposed move and its schedule
  impact; it never applies silently.
- **Cancel assignment** — restores the previous Case connection.

Warnings must not automatically alter fixed dates, dependency rules, baselines,
or delay history.

### 3.2 Conflict indication in the tracker

A conflicted task should remain readable and should not turn the entire row
bright red. Recommended presentation:

- Show a small amber/red conflict icon beside the relevant metadata chip.
- Add a clear tooltip containing the resource, conflicting task, location,
  overlap dates, and overlapping workdays.
- Add `Resource conflict` to Project Data Health.
- Show the number of unresolved resource conflicts in the Resources menu.
- Use accessible text and icons in addition to color.

Resolved/accepted conflicts should remain visible but visually distinct from
unresolved conflicts.

### 3.3 Resource Usage & Conflicts window

Add a top-level **Resources** menu next to **Metadata Lists** with:

- **Resource Usage & Conflicts…**
- **Show Selected Resource…**
- **Recheck Resource Conflicts**
- **Resource Settings…**

Also add **Show Resource Usage…** to the task context menu when the task has a
conflict-enabled metadata token.

The main window should have three views:

#### Summary view

| Resource | Type | Scheduled uses | Planned workdays | Conflicts | Next available |
|---|---|---:|---:|---:|---|
| RLN2-1 | Case | 4 | 63 | 1 | 2026-10-12 |

#### Detailed usage view

| Resource | Project | Task path | Location | Start | End | Workdays | Status | Conflict |
|---|---|---|---|---|---|---:|---|---|
| RLN2-1 | Project A | Lab Testing > DOE Type 1 | Room 1 | 2026-09-08 | 2026-10-02 | 20 | Planned | 8d overlap |
| RLN2-1 | Project A | Lab Testing > DOE Type 2 | Room 3 | 2026-09-15 | 2026-10-10 | 20 | Planned | 8d overlap |

Double-clicking a row must jump directly to the source task and project tab.

#### Allocation timeline view

Display one horizontal lane per physical resource, with separate bars for each
task. Overlapping portions should be highlighted and labeled. Room/location
should appear on or beside each bar.

Filters:

- Resource type
- Specific resource
- Project
- Room/location
- Date range
- Conflicts only
- Include completed work

The window should initially focus the resource selected in the tracker when it
was opened from a task.

## 4. Definitions

### 4.1 Resource

A physical or capacity-constrained item attached to a task through metadata:

- `case`
- `room`
- `article` (Cassettes/Test Articles)
- `activity` (Lab Testing options)
- options in user-created metadata lists
- future configured metadata/resource types

`phase` (Project Phases) is explicitly excluded.

### 4.2 Assignment

One resource used by one scheduled leaf task over the task's occupied workdays.

Recommended normalized representation:

```python
ResourceAssignment(
    project_id,
    task_id,
    resource_type,
    resource_id,
    resource_label,
    location_label,
    start_date,
    end_date,
    occupied_dates,
    capacity_units,
)
```

### 4.3 Conflict

A conflict exists when assignments for the same resource exceed its configured
capacity on one or more occupied dates.

For any conflict-enabled option with capacity 1, any shared occupied workday is
a conflict. For a unique Case, this remains true even when both tasks list the
same Room. A single physical Case cannot be used twice merely because the
location labels match.

### 4.4 Location

The task's Room token is the primary location. If no Room is assigned, display
`Location not assigned`; missing location does not suppress Case conflict
detection.

### 4.5 Scheduled workdays

The analyzer should use the actual occupied dates generated from the task's
start/end span and the applicable project work calendar. It should not use a
simple calendar-day subtraction.

If one assignment ends on a workday when another begins, that shared day counts
as an overlap because both ranges are inclusive in the tracker.

## 5. Scope rules

### 5.1 Include

- Leaf tasks with valid start and end dates.
- Tasks with one or more conflict-enabled metadata tokens/resources.
- All project tabs in the currently loaded `.vpmt` document.
- Fixed, automatic, same-as, and dependency-driven tasks.
- Completed tasks when included by the view filter; completed historical work
  should not create a current/future warning unless its dates overlap another
  assignment under review.

### 5.2 Exclude

- Parent/group rows whose dates are rollups from children. Counting them would
  double-count the same resource usage.
- Inbox/dateless tasks.
- Deleted tasks.
- Metadata options that are defined but not assigned to a task.
- Project Phase tokens. They classify/structure work and do not represent a
  capacity-constrained assignment.
- Separate `.vpmt` files that are not loaded. Cross-file checking is a later
  shared-registry feature.

### 5.3 Multiple resources on one task

The analyzer should read `task_tokens`, not only the legacy single-value
`node.resources` dictionary. A task may use multiple options from the same
conflict-enabled list, including multiple Cases, without silently overwriting
the earlier assignment.

### 5.4 Project Phases (deferred workflow discussion)

Project Phase options currently serve as reusable task/workflow classifications
with editable default timelines. Selecting a phase places a stable phase token
on the task and can supply its default duration. A phase does not identify a
unique physical item, so the same phase may legitimately appear on many tasks
at overlapping times.

For this feature:

- Project Phases are excluded from capacity and conflict calculations.
- Phase tokens continue to supply names/default timelines as they do today.
- The broader question of whether phases should create groups, enforce a
  lifecycle, or remain simple reusable classifications is explicitly deferred
  for a separate discussion and does not block resource-conflict work.

## 6. Stable resource identity

Conflict matching must use a stable resource/option ID whenever available, not
only the displayed label. Renaming `RLN2-1` must not break its allocation
history or make it appear to be a different Case.

Identity resolution order:

1. Metadata option ID from the task token.
2. Persisted resource ID from a migrated assignment.
3. Legacy fallback key: normalized type plus normalized label.

Labels remain editable display text. IDs remain internal and stable.

Duplicate display labels within one resource list should be rejected or clearly
flagged in Metadata Lists because they are ambiguous to users even when IDs are
technically different.

## 7. Capacity model

Confirmed defaults:

- Every conflict-enabled metadata option: capacity 1
- Project Phase options: conflict checking disabled; capacity does not apply

Store capacity at the document resource-definition level, not separately on
each task. The existing `TaskNode.resource_capacities` field may be migrated or
retained only for backward compatibility; per-task capacity is not a reliable
source of truth for a shared physical resource.

Resource/list settings should allow:

- Capacity
- Conflict checking enabled/disabled (Project Phases locked off by default)
- Active/inactive state
- Optional location/home location
- Optional notes
- Whether conflicts are warnings or prohibited without an override

Capacity greater than 1 should be calculated per occupied date. If three tasks
use a resource with capacity 2 on the same day, all assignments involved in the
excess allocation should be identified.

## 8. Conflict-analysis engine

Create a separate module, recommended path:

`utils/resource_allocation.py`

The ordinary scheduler remains responsible for dates. The allocation analyzer
reads the scheduler's results and reports conflicts. Keeping these concerns
separate prevents availability warnings from unexpectedly rewriting schedule
rules.

### 8.1 Pipeline

1. Flatten every project tree.
2. Extract assignments from eligible leaf tasks.
3. Resolve stable resource IDs and display labels.
4. Generate occupied workday sets using each project's calendar.
5. Group assignments by resource ID.
6. Build a date-to-assignment occupancy map for each resource.
7. Compare occupancy with capacity.
8. Merge consecutive conflicting dates into readable overlap spans.
9. Produce immutable conflict records.
10. Attach task-specific summaries to the view model and refresh the UI.

### 8.2 Conflict record

```python
ResourceConflict(
    id,
    resource_type,
    resource_id,
    resource_label,
    assignment_ids,
    task_ids,
    project_ids,
    overlap_start,
    overlap_end,
    overlap_workdays,
    locations,
    capacity,
    peak_usage,
    resolution_state,
    resolution_reason,
)
```

Conflict IDs should be deterministic from resource ID, involved task IDs, and
the overlap interval so accepted conflicts remain associated after save/load.

### 8.3 Performance

For current project sizes, grouping assignments and indexing occupied dates is
more than sufficient. Avoid pairwise comparison across every task in the file.
The analyzer should operate only within each resource group.

## 9. Recalculation triggers

Resource analysis must rerun after any operation that can change assignments or
occupied dates:

- Selecting, replacing, or clearing a conflict-enabled metadata token
- Editing Start, End, or Duration
- Changing a scheduling rule or dependency
- Indenting, outdenting, moving, adding, or deleting a task
- Changing project holidays or weekend rules
- Changing resource capacity
- Renaming or deleting a metadata resource option
- Loading or restoring a project
- Undo and redo
- Applying Excel-imported changes in a future round-trip feature
- Choosing Refresh All

Use a short single-shot debounce for rapid edits so multiple internal date
changes produce one analysis and one repaint.

## 10. Refresh All contract

`Refresh All` should become a defined orchestration command rather than a
generic repaint.

Required order:

1. Activate each project's calendar/configuration.
2. Recalculate scheduling rules.
3. Roll up parent dates and owners.
4. Rebuild resource assignments across all project tabs.
5. Recompute resource conflicts.
6. Refresh tree rows, filters, attached timelines, Visuals, totals, and conflict
   counts.
7. Display a completion summary such as:

   > Refreshed 2 projects · 251 tasks · 18 resource assignments · 2 conflicts

Refresh must not silently modify fixed dates or convert a warning into an
automatic scheduling decision.

## 11. Conflict resolution behavior

### 11.1 Warn first

The initial implementation should be warning-first. It should not automatically
move tasks merely because a Case is occupied.

### 11.2 Move to next available

Offer an explicit command that:

1. Searches forward for the first continuous available block of the required
   occupied workdays.
2. Respects holidays/weekends.
3. Produces an impact preview showing affected dependent and parent tasks.
4. Requires user confirmation.
5. Applies through the task's existing scheduling rule rather than writing
   disconnected display dates.

If the task has a fixed rule, the preview must explain that applying the move
will replace that fixed rule.

### 11.3 Accepted overlap

If intentional overlap is allowed, persist an explicit resolution record with:

- Conflict ID
- User-entered reason
- Timestamp
- Resolution state

Accepted conflicts remain in the rundown and Data Health but do not repeatedly
interrupt normal editing unless their dates or participants change.

## 12. Persistence and migration

### 12.1 New document-level data

Recommended `.vpmt` additions:

```json
{
  "resource_definitions": [
    {
      "id": "case-rln2-1",
      "type": "case",
      "label": "RLN2-1",
      "capacity": 1,
      "active": true
    }
  ],
  "resource_conflict_resolutions": []
}
```

Assignments themselves can remain derived from task tokens and task dates;
they should not be duplicated as independently editable saved records.

### 12.2 Legacy migration

- Derive resource definitions from existing metadata options and task tokens.
- Reuse existing token IDs.
- Fall back to normalized labels only when old data has no token ID.
- Default capacities to 1.
- Do not change any task dates during migration.
- Run analysis after load and show a non-blocking summary if existing overlaps
  are discovered.
- Preserve backward-compatible reading of `node.resources`.

### 12.3 Save safety

- Include new fields in normal atomic `.vpmt` saves and backups.
- Include them in undo/redo project snapshots where appropriate.
- Never store computed conflict text as the only source of truth; recompute from
  assignments, capacity, and resolution records.

## 13. UI integration points

Expected code areas:

- `models/task_node.py`
  - Assignment helpers or resource-token accessors
  - Backward-compatible serialization only where task-specific state is needed
- `utils/resource_allocation.py`
  - Assignment extraction, occupancy, conflict detection, next-available search
- `utils/vpmt_io.py`
  - Document resource definitions and resolution persistence
- `ui/main_window.py`
  - Resources menu, rundown dialog, all-project orchestration, Refresh All
- `ui/tree_grid_view.py`
  - Immediate analysis triggers, chip conflict indicator, jump actions
- `ui/project_widget.py`
  - Snapshot/history integration and timeline refresh
- `ui/timeline_pane.py`
  - Resource allocation lanes or conflict overlays
- `ui/metadata_editor.py`
  - Stable IDs, capacity editing, safe resource deletion/rename behavior
- `utils/excel_export.py`
  - Optional Resource Usage and Resource Conflicts sheets

Avoid putting conflict detection directly inside a paint method or dialog.
There must be one analyzer used by assignment warnings, Refresh All, rundown,
Data Health, timeline overlays, tests, and future Excel import.

## 14. Testing plan

### 14.1 Unit tests

- Same Case, non-overlapping ranges: no conflict.
- Same Case, one shared workday: one-day conflict.
- Same Case, partial overlap: correct first/last date and workday count.
- Same Case, identical spans: full-span conflict.
- Same label but different stable IDs: no conflict.
- Renamed Case with same ID: conflict remains.
- Different Cases, identical spans: no conflict.
- Same Case in different Rooms: conflict includes both locations.
- Same Case with missing Room: conflict still detected.
- Capacity 2 with two assignments: no conflict.
- Capacity 2 with three assignments: conflict with peak usage 3.
- Weekend/holiday intersection: only occupied workdays count.
- Parent and child share token: parent excluded to prevent double count.
- Dateless/Inbox tasks: excluded.
- Deleted task: conflict disappears.
- Accepted conflict: persists and is associated after reload.
- Changed dates invalidate an outdated accepted resolution.

### 14.2 Integration tests

- Selecting a conflicting Case produces a warning after token commit.
- Clearing/replacing the Case removes/recomputes the warning.
- Duration edit changes the conflict interval immediately.
- Metadata timeline change recomputes actual dates and conflicts.
- Refresh All recomputes conflicts and updates its completion summary.
- Undo/redo restores conflict state.
- Save/load produces the same unresolved and accepted results.
- Clicking a rundown row jumps to the correct project and task.
- Resource timeline highlights the exact overlap.
- Exported Resource Usage sheet reconciles to the in-app rundown.

### 14.3 Regression tests

- Resource analysis never moves fixed dates by itself.
- Ordinary date changes remain ordinary replanning, not delay entries.
- Assignment warnings do not clear Task cell tokens.
- Duplicate task names do not confuse jumps or conflicts because IDs are used.
- Multiple project tabs are analyzed together.
- Existing files without resource definitions load unchanged.

### 14.4 Acceptance scenario

1. Assign `RLN2-1` to Task A in Room 1 for 20 workdays.
2. Assign `RLN2-1` to Task B in Room 3 with an 8-workday overlap.
3. Confirm the assignment warning names both tasks, both rooms, and 8 days.
4. Open Resource Usage and verify both rows and the overlap lane.
5. Jump from the rundown to Task A and then Task B.
6. Move Task B beyond Task A and confirm the conflict disappears.
7. Undo and confirm the conflict returns.
8. Save, reopen, Refresh All, and confirm the same result.

## 15. Implementation phases

Implementation status: Phases 1 through 6 are complete. The ordinary scheduler
remains date-focused; every resource surface is fed by the shared analyzer in
`utils/resource_allocation.py`.

### Phase 1 — Domain contract and analyzer

- Finalize decisions in Section 17.
- Add stable resource-definition model.
- Implement assignment extraction and conflict detection.
- Add unit tests before UI work.

Exit criterion: the analyzer correctly reports the acceptance scenario from
in-memory project data without changing dates.

### Phase 2 — Persistence and migration

- Persist resource definitions and accepted resolutions.
- Migrate legacy task resources/tokens.
- Add save/load and undo/redo tests.

Exit criterion: analysis results and accepted resolutions survive save/reopen.

### Phase 3 — Immediate warnings and tracker indicators

- Trigger analysis after task/resource/date changes.
- Add accessible conflict indicator and tooltip.
- Add warning actions: keep, jump, choose another, cancel.
- Add conflicts to Data Health.

Exit criterion: a user cannot unknowingly create an overlapping unique Case
assignment in the loaded document.

### Phase 4 — Resource Usage & Conflicts window

- Add Resources menu.
- Build summary and detailed tables.
- Implement filters and jump-to-task behavior.
- Show total workdays, next available date, and resolution state.

Exit criterion: every use of a selected Case can be found and audited in one
place.

### Phase 5 — Allocation timeline and next-available action

- Add resource lanes and overlap highlighting.
- Implement next-available search.
- Reuse schedule-impact preview before moving a task.

Exit criterion: users can understand and deliberately resolve conflicts without
manually comparing dates.

### Phase 6 — Refresh, export, telemetry, and hardening

- Implement the complete Refresh All contract.
- Add Resource Usage/Conflicts Excel sheets.
- Add privacy-safe outcome telemetry (counts/types only; no Case labels).
- Run the complete regression and acceptance suite.

Exit criterion: Refresh All, rundown, grid, timeline, save/load, and Excel
export all reconcile to the same analyzer results.

## 16. Definition of done

The feature is complete only when:

- Every scheduled use of a conflict-enabled metadata option can be listed with
  task, project, room/location where applicable, dates, and workdays.
- Overlapping use of any capacity-1 enabled option is detected across all
  project tabs in the loaded document; Project Phases are excluded.
- The warning identifies both assignments and the exact overlap.
- No automatic date movement occurs without confirmation.
- Fixed/manual schedule rules remain intact unless explicitly replaced.
- Refresh All recomputes and visibly reports resource-analysis results.
- Conflicts appear consistently in the task, rundown, timeline, Data Health,
  and optional Excel export.
- Rename, delete, replace, undo, redo, save, load, and metadata timeline changes
  all recompute correctly.
- All unit, integration, migration, and acceptance tests pass.

## 17. Final user decisions

Answers recorded from the user on 2026-08-29. All decisions needed for Phase 1
are resolved and binding for implementation.

1. **Scope:** Should conflicts be checked across all project tabs in the
   currently loaded `.vpmt` file?  
   **Confirmed:** Yes. Analyze every project tab in the loaded document.

2. **Case capacity:** Is every named Case such as `RLN2-1` one unique physical
   item that can only be used once at a time?  
   **Confirmed:** Every individually named Case is one physical item with
   capacity 1. It may appear on only one assignment on a given occupied
   workday.

3. **Metadata coverage:** Which metadata options participate in conflict
   detection?  
   **Confirmed:** Everything except Project Phases. Cases, Rooms,
   Cassettes/Test Articles, Lab Testing options, and options from new custom
   metadata tabs are enabled. Each defaults to capacity 1. Project Phase usage
   will be discussed separately later.

4. **Intentional overlaps:** May a user keep an overlap? If yes, must they enter
   a reason?  
   **Confirmed:** Yes. Show a confirmation popup and require an explanation.
   Persist the explanation with the accepted conflict.

5. **Automatic behavior:** Should the tracker ever move a task automatically
   to avoid a conflict?  
   **Confirmed:** No automatic movement. Warn first; a move occurs only through
   a user-confirmed action with impact preview.

6. **Location:** Is the selected Room the correct definition of where a Case is
   being used?  
   **Confirmed:** Yes. Room is the location; display `Location not assigned`
   when absent.

7. **Shared boundary day:** If Task A ends on the same workday Task B begins,
   should that day count as a conflict?  
   **Confirmed:** Yes. The next line item must start on the following workday,
   not the same day. Task date spans are inclusive.

8. **Multiple Cases per task:** Can one task use more than one Case at the same
   time?  
   **Confirmed:** Yes. The data model, picker, analyzer, rundown, and persistence
   must support multiple Cases on one task.

9. **Completed work:** Should past completed assignments remain in the rundown?
   **Approved plan default:** Keep them for history but hide them by default in
   the active-conflict view.

10. **Separate files:** Do you eventually need conflicts detected across Cases
    scheduled in different `.vpmt` files?  
    **Approved plan default:** Defer. That requires a shared resource registry
    and a separate conflict/ownership strategy for OneDrive and concurrent
    editing.

No remaining workflow question blocks implementation. The meaning and future
workflow of Project Phases is deferred as requested; its exclusion from
conflict detection is final for this goal.

## 18. Completion evidence

- Domain/analyzer: immutable resource definitions, assignments, deterministic
  conflicts, capacity-per-workday calculation, accepted-resolution signatures,
  and all-resource next-available search are implemented in
  `utils/resource_allocation.py`.
- Persistence/migration: `.vpmt` v2.6 stores document-level
  `resource_definitions` and `resource_conflict_resolutions`; legacy resources
  receive normalized stable fallback IDs and load without date changes.
- Immediate handling: token commits run analysis and offer Keep overlap (reason
  required), Jump, Use another resource, Move to next available with impact
  preview, and Cancel assignment. No analyzer path writes dates.
- Visibility: unresolved/accepted markers and accessible tooltips appear in the
  grid and attached timeline; Project Data Health and the Resources menu expose
  counts and state.
- Rundown: the Resources menu opens filtered Summary, Detailed Usage, and
  Allocation Timeline views; detailed rows jump by project/task IDs.
- Settings: capacity, conflict-enabled, active, home location, notes, and policy
  are document-level settings. Inactive resources disappear from new lookup
  choices while existing assignments remain auditable.
- Refresh/export: Refresh All processes every project and reports project, task,
  assignment, and conflict totals. Excel Resource Usage and Resource Conflicts
  sheets use the same analyzer result as the UI.
- Verification: 78 automated tests pass, including the complete two-task,
  two-room, 20-workday/8-overlap acceptance scenario, confirmed move, Undo,
  save/reopen, timeline/rundown, migration, and Excel reconciliation.
