# Repo-local telemetry snapshot

These files are a sanitized copy of the canonical AppData store
(`%LOCALAPPDATA%/VPMTracker/telemetry` on Windows, `~/.vpm_tracker/telemetry`
elsewhere) for Creative / Git Helper review.

The live logger still writes only to AppData. The app refreshes this folder
on close, about every 15 minutes, shortly after startup, and from
**Options → Export usage logs to repo folder**.

| File | Contents |
| --- | --- |
| `usage-YYYY-MM.jsonl` | Current month (and previous month when present), re-sanitized |
| `summary-latest.json` | Rolling 7-day event counts |
| `report-latest.md` | Human-readable `usage_report` output |

Do not treat this folder as the source of truth. Paths, note text, task names,
and dollar values are stripped before copy.
