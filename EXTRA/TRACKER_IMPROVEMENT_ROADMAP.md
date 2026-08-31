# VPM Tracker Improvement Roadmap

Status: implemented in the current worktree and covered by the roadmap test
suite. The later confirmed interaction design in
`TELEMETRY_ANALYSIS_FINDINGS.md` is authoritative where it differs here:
Task `=` lookup is inline, `=`/`+` date shortcuts use direct row picking,
metadata uses generic Option/Timeline lists, and Room/Case are not separate
tracker tabs. Capacity scheduling remains deferred until usage proves it.

This document preserves the user's workflow decisions from the August 29,
2026 review. The reusable-template/resource-scheduling idea is important, but
must not displace the everyday editing improvements that led to it.

The implementation should reuse one context-aware cell picker for Task, Start,
End, and Depends On cells. Building four unrelated dialogs would duplicate
work and create inconsistent behavior. The picker changes its choices based on
the active cell, and always supports both mouse and keyboard use.

## Priority order

### Priority A — fix everyday planning friction first

1. **Protect the project file.** Detect another open instance and detect when
   the file changed on disk after it was loaded. Use a recovery file per
   project/instance and warn before replacing a newer copy. OneDrive and a
   second app window must not silently produce last-save-wins data loss.
2. **Install the telemetry foundation before changing workflows.** Store it
   outside the install folder, preserve it through upgrades, exclude tests,
   assign unique event IDs and stable anonymous install/project IDs, and never
   commit it to Git. Import and deduplicate recoverable legacy logs once,
   remove already-tracked telemetry from Git, detect logging gaps, and generate
   a small automatic weekly summary. Add each new workflow's outcome events as
   that workflow is implemented so the next review is based on complete
   evidence.
3. **Separate ordinary schedule edits from real delays.** Editing Start, End,
   or Duration is normal planning and must not create a delay entry. A delay
   is recorded only through a deliberate `Record Real Delay` action, which is
   the extra step that asks for a reason and adds to delay history.
4. **Put common scheduling choices directly on the row.** Predecessor,
   same-start-as another task, automatic sequencing, manual date, and clear
   link must not be buried in context-menu submenus. Start, End, and Depends On
   cells use the unified picker described below; the existing calendar remains
   a first-class choice.
5. **Allow structure before typing a new task.** Enter creates a blank row;
   Tab/Shift+Tab sets its indent level before the user types the name. Typing
   then begins editing, and Enter finishes the row and creates the next one.
6. **Make Ctrl+Space contextual.** With a task selected it opens that task's
   notes. With no task selected (or the project header selected) it opens the
   overall Notes Center.
7. **Create a useful Notes Center.** It includes an automatic All Task Notes
   view grouped by task/project, supports jumping back to the source task, and
   allows user-created overall-note tabs such as Meetings, Risks, Ideas, and
   Lab Notes. Task notes remain attached to their tasks; the overall view does
   not duplicate them.
8. **Complete workflow telemetry.** Log the workflows that were previously
   invisible: Enter-created rows, indent/outdent, predecessor/equal/manual
   choices, date/duration/status edits, task-note opens, menu depth, completion,
   cancellation, elapsed time, conflicts, and undo. Never log task names, note
   text, dollar values, or full file paths. Provide a small diagnostics view
   showing whether logging is active, whether the session is production or
   test, when the last event was written, and whether unexplained gaps exist.

### Priority B — make project updating easier

9. Add a simple Review flow with `Done`, `Still on track`, `Change date
   normally`, `Record Real Delay`, and `Skip`. A normal date change remains
   separate from a confirmed delay. Track a separate `reviewed through` date
   so an autosave cannot make stale project data appear current. Show a stale
   project warning and flag basic contradictions: overdue In Progress tasks,
   past-start Not Started tasks, future-dated Completed tasks, missing delay
   reasons, and completed VAVE work without a savings disposition.
10. Keep owner/waiting-on functionality available and easy to fill, but do not
   make it mandatory or prioritize it above the editing work. Fix project-owner
   configuration persistence and migrate the existing configured owner list so
   attempted setup cannot leave every task and project with blank/default
   ownership choices.
11. Extend VAVE with a lightweight stage progression:
   `Idea -> Testing -> Approved -> Implemented -> Savings Verified`. Only the
   final verified stage prompts for realized savings. Money is never inferred.

### Priority C — reusable templates and resource scheduling

12. Add reusable project-phase templates such as Idea, Design, Prototype,
    Sourcing, Lab Testing, and Implementation.
13. Add reusable activity/test templates with standard durations and sequence
    rules. Examples: Instrumentation = 1 week, DOE = 2 weeks, NSF Type 2 = its
    configured duration.
14. Add project resources: uniquely named physical cases, rooms, cassettes/test
    articles, and later other constrained resources only when needed.
15. Generate schedule activities from the chosen template and resources, then
    provide Project, Room, and Case views over the same underlying tasks.

## Context-aware `=` picker in the current grid

The picker should feel like an extension of editing a normal cell, not a
separate application or a large form. One shared picker shell supplies search,
keyboard navigation, recent choices, and cancel/undo behavior; each cell type
supplies only its valid choices.

### Basic interaction

- Double-click the Task cell and type `=` to open a compact picker.
- In a blank/root task, the first picker offers meaningful activity templates:
  `Lab Testing`, `Design`, `Prototype`, `Sourcing`, `Implementation`, etc.
- Selecting an option inserts a structured chip/token rather than ordinary
  text. The row can still contain normal free text.
- Type `=` again after the first choice to add another relevant selection.
- The choices are contextual. After `Lab Testing`, the picker offers fields
  such as Case, Room, Cassette/Test Article, or a saved Test Campaign—not an
  unrelated master list.

Example parent row:

```text
[Lab Testing] [Case: RLN2-01] [Room: 1] [Cassette: LT-MB-1]
```

Example child rows:

```text
    [Instrumentation]
    [DOE]
    [NSF - Type 2]
```

The parent selections are inherited by the children. A child may override a
case, room, or cassette when that specific test is different.

### Meaning of `=` depends on the active cell

- In the **Task/Name** cell, `=` opens the template/resource picker described
  above.
- In a **Start/End** cell, `=` means match a date from another task, `+` means
  continue after another task, and the calendar selects a fixed date.
- In the existing **Depends On** cell, clicking or typing `=` opens the same
  relationship picker as a convenient summary/shortcut. Do not add a separate
  Schedule column merely to duplicate Start/End controls.

The active cell makes the meaning clear. The app must also show a clickable
button/icon for every symbol action; symbols speed up expert use but are never
required knowledge.

**Discoverability requirement:** `=` and `+` are optional shortcuts only.
Every date editor must visibly offer the clickable choices `Calendar`, `Same
As`, `Continue After`, and `Automatic`. A user must be able to discover and use
the complete feature without knowing or typing either symbol. Brief tooltips or
first-use guidance may explain the matching shortcuts.

## Date-cell relationship language

The same selection approach works directly in Start and End cells. Opening the
cell shows one compact date editor with four visible modes:

```text
[Calendar / Fixed Date] [= Same as] [+ Continue after] [Automatic]
```

The existing calendar picker remains available in the first mode. Typing `=`
or `+` jumps directly to the matching mode; the user never has to type a full
formula.

### Core meanings

- In a **Start** cell, type `=` and select another task to start on the same
  date as that task. The picker may also offer that task's End when needed.
- In a **Start** cell, type `+` and select another task to continue after it:
  start on the next workday after the selected task ends. This is the normal
  predecessor relationship.
- An optional offset can extend the continuation, for example `+ 3 workdays`
  after the selected task ends.
- In a **Start** or **End** cell, typing/picking a literal calendar date creates
  a fixed manual value for that field and shows a visible pin/lock indicator.
- A small cell menu also exposes the same choices for users who do not want to
  type symbols: `Same date as...`, `Continue after...`, `Fixed date...`, and
  `Return to automatic`.

The grid always displays the calculated/selected date first, followed by a
small mode indicator. The formula explanation appears in the open editor and
tooltip, so the schedule remains readable.

Examples as displayed in cells and explained when opened:

```text
Start: Sep 15, 2026  =    (= DOE Trial 1 · Start)
Start: Sep 16, 2026  +    (+ DOE Trial 1 · End, next workday)
Start: Sep 15, 2026  📌   (fixed from calendar)
End:   Sep 30, 2026  =    (= Validation · End)
End:   Sep 30, 2026  📌   (fixed from calendar)
```

These are live relationships. If DOE Trial 1 moves, a row using `=` or `+`
moves with it. A pinned literal date does not move.

### Start and End are controlled separately

Replace the current single `dates_locked` behavior with explicit rules for
each field:

- **Start fixed, End by duration:** the task begins on the chosen date and its
  end follows its duration.
- **Start automatic/related, End fixed:** the task begins from its scheduling
  rule and ends on the fixed deadline; displayed duration is derived.
- **Both fixed:** the task occupies the exact chosen window; duration is
  derived from those dates.
- **Neither fixed:** the scheduler controls Start and Duration controls End.

If the user chooses a predecessor/continuation rule while Start is fixed, the
app must clearly ask to replace the fixed Start rule. It must never silently
keep two conflicting rules.

Likewise, picking a calendar date for a cell that currently has an `=` or `+`
relationship asks to replace that relationship with the fixed date. One field
has one controlling rule.

### Scheduling and resource guardrails

- `+ Task` means the first workday after that task's End, not merely the same
  date as its End.
- `= Task` defaults to the matching field (`Start = Start`, `End = End`) while
  still allowing the user to choose the other field from the picker.
- `+ Task` is an earliest-start relationship: room/case availability may push
  it later, and the app explains why.
- `= Task` is an exact equality relationship. If the selected room/case is not
  available on that exact date, show a conflict; do not silently break the
  equality by moving the task.
- A fixed manual date is not silently moved for a resource conflict; the app
  shows the conflict and asks the user to resolve it.
- Parent/group dates should normally remain rollups of their children. A parent
  target date, if needed later, should be a separate deadline rather than a
  hidden override of the child rollup.
- Creating or changing any relationship is ordinary scheduling, not a real
  delay. Only the explicit `Record Real Delay` action writes delay history.

### How a lab-testing row becomes a schedule

1. Create/select a parent row and choose `= Lab Testing`.
2. Add Case `RLN2-01`, Room `1`, and Cassette `LT-MB-1` on that row.
3. Add child rows and choose tests with `=`: Instrumentation, DOE, NSF Type 2,
   etc.; alternatively select several tests in one campaign picker.
4. Show a schedule preview before generation.
5. On confirmation, populate durations and dates from the saved test-template
   definitions.
6. A later campaign using the same case starts on the first date when the case,
   chosen room, and project prerequisites are all available.

## Build order that avoids rework

Implement in small releases; do not attempt the template/resource system in
the same release as the core editing changes.

1. **Safety and measurement:** file-conflict protection plus upgrade-safe
   telemetry storage, legacy-log import/deduplication, automatic summaries,
   gap detection, and diagnostics.
2. **Scheduling foundation:** replace the single manual-date flag internally
   with explicit Start and End rules, migrate old files, and add scheduler
   tests. Preserve the current visible behavior before changing the UI.
3. **Unified date editor:** calendar, `=`, `+`, Automatic, visible mode markers,
   Depends On shortcut, conflict messages, and one-step undo.
4. **Normal edit versus real delay:** remove automatic delay creation from date
   and duration edits; add the deliberate Record Real Delay action and clean
   Review flow.
5. **Fast row creation and notes:** structure-before-name behavior, contextual
   Ctrl+Space, All Task Notes, and user-created overall-note tabs.
6. **Task-cell picker:** add the optional `=` template/token editor while plain
   text tasks remain unchanged.
7. **Templates first, resources second:** phase/test templates and generated
   child rows before room/case capacity scheduling.
8. **Resource views and VAVE stages:** Room/Case views, conflict handling, then
   the VAVE stage/verified-savings layer.

Every multi-row generation or schedule relationship change must be one Undo
operation. A generated campaign that creates ten rows must not require ten
undos.

## Release acceptance criteria

- Two app instances or an externally changed OneDrive file cannot silently
  overwrite newer work.
- Upgrading or replacing the app folder retains telemetry, while tests never
  enter production telemetry.
- Existing telemetry is imported without duplicate events, and telemetry is
  not tracked by Git.
- Opening and saving an existing `.vpmt` file without edits preserves its
  displayed dates, relationships, notes, VAVE values, and project settings.
- Every `=` or `+` action is also available through the visible `Calendar`,
  `Same As`, `Continue After`, and `Automatic` choices.
- The Review flow records a `reviewed through` date and exposes stale or
  contradictory task state without changing it automatically.
- Owner configuration survives save, close, reopen, upgrade, and project-tab
  switching.
- Every relationship edit, generated campaign, and Review application is one
  Undo operation.
- Telemetry can measure workflow opened, completed, canceled, failed, and time
  spent without storing project content or full paths.

### Existing-file migration

- Old `predecessor_id` values become `+ predecessor End` Start rules.
- Existing manually dated tasks are migrated conservatively and keep their
  currently displayed dates. The migration must not reschedule a real file on
  first open.
- Existing implicit sibling sequencing remains Automatic.
- Existing parallel tasks retain their current dates/behavior until the user
  changes the new relationship rule.
- New fields are optional with safe defaults so every current `.vpmt` file and
  backup continues to load.

## Guardrails and open design decisions

- **Keep the normal grid normal.** Plain text tasks continue to work exactly as
  before. Structured `=` choices are optional.
- **Calendar is never removed.** It remains the familiar fixed-date option in
  every Start/End editor; `=` and `+` are additional choices.
- **One rule per field.** Start and End each show exactly what controls them;
  replacing a rule is explicit and undoable.
- **Do not add duplicate columns.** Reuse Task, Start, End, and Depends On. Add
  compact mode/resource indicators or a detail panel instead of widening the
  grid for every new concept.
- **Avoid information overload.** Show compact chips in the row; full details
  belong in a tooltip or focused detail panel.
- **Use unique resource identities.** `RLN2` may be a model; an actual case
  must have an identity such as `RLN2-01` so its schedule is trustworthy.
- **Prevent double booking.** A case cannot be in two places simultaneously;
  a room respects its configured capacity. The next start is the latest of
  case availability, room availability, and task prerequisites.
- **Treat testing as a campaign.** Setup/instrumentation may happen once for a
  group of tests rather than being duplicated before every test.
- **Allow parallel or sequential test rules.** Each template states whether it
  follows the prior activity, may overlap, or requires exclusive room use.
- **Support project overrides.** A standard duration may be changed for one
  project without changing the reusable master template.
- **Snapshot template versions.** Updating a master template affects new work,
  not already-created schedules unless the user explicitly refreshes them.
- **Do not manufacture delays.** Generated dates and ordinary replanning never
  create real-delay records. Only `Record Real Delay` does.
- **Do not auto-calculate money.** VAVE potential and realized values remain
  explicit user entries; only totals/rollups may be calculated.
- **Explain every automatic date.** Opening a date must answer why it is there:
  fixed by user, equal to another date, after a predecessor, limited by a room
  or case, or automatically sequenced.

## Confirmed interaction decisions

- The existing calendar remains available for fixed Start and End dates.
- `=` in a date cell creates an exact live match to a selected task date.
- `+` in Start creates a continuation after a selected task ends.
- Start and End may be fixed independently or together.
- A normal edit/relationship change is not a recorded real delay.
- The template/resource feature stays behind the everyday editing work in the
  implementation order.

## Remaining choices before the relevant release

1. Decide whether the parent row's visible text should be entirely chips or a
   normal task name followed by compact chips. Prefer the latter unless a
   prototype proves it wastes too much width. Treat the Task-cell `=` token
   interaction as a prototype until users can discover and complete it without
   instruction; plain text remains the default fallback.
2. Decide whether tests are usually added one child at a time with `=` or by a
   multi-select campaign picker that creates all children at once. Start with
   one-at-a-time because it matches the described workflow; add multi-select
   only after observing repetition.
3. Define the first small master lists: project phases, tests/durations, rooms,
   cases, and cassettes/test articles. Do not add people/equipment constraints
   until rooms and cases work well.
4. Decide whether offsets entered with `+` use workdays by default (recommended)
   and provide an explicit calendar-days override only if real use requires it.

## Analysis lesson

Future prioritization must combine four sources: telemetry, the latest saved
project state, a click-depth/code-path audit, and direct user review. The old
telemetry did not log several highly used actions and therefore cannot be the
sole source of product decisions.
