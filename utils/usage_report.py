import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from statistics import mean, median
from utils.usage_logger import usage


KNOWN_FEATURES = [
    "quick_capture", "search", "zoom", "task_add", "task_delete", "indent",
    "outdent", "bulk_edit", "filter_apply", "undo", "redo", "baseline_set",
    "baseline_update_node", "catchup_open", "catchup_apply", "waiting_set",
    "waiting_clear", "notes_pad_open", "note_promoted", "excel_export",
]


def _read_events(months: int, root=None):
    root = str(root if root is not None else usage.root)
    if not os.path.isdir(root):
        return []
    files = sorted(
        f for f in os.listdir(root)
        if f.startswith("usage-") and f.endswith(".jsonl")
    )[-max(1, months):]
    events = []
    for name in files:
        path = os.path.join(root, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            pass
    return events


def build_report(months: int = 3, root=None) -> str:
    events = _read_events(months, root=root)
    counts = Counter(e.get("ev") for e in events)
    sessions = defaultdict(dict)
    weekdays = Counter()
    hours = Counter()
    warnings = Counter()
    errors = Counter()
    dialog_stats = defaultdict(lambda: {"count": 0, "cancel": 0, "ms": 0})
    by_month = defaultdict(Counter)

    for e in events:
        ev = e.get("ev")
        d = e.get("d", {}) or {}
        sid = e.get("sid")
        ts = e.get("ts", "")
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            weekdays[dt.strftime("%A")] += 1
            hours[dt.hour] += 1
            by_month[dt.strftime("%Y-%m")][ev] += 1
        except Exception:
            pass
        if ev == "app_start":
            sessions[sid]["start"] = ts
        elif ev == "app_end":
            sessions[sid]["secs"] = int(d.get("secs", 0) or 0)
        elif ev == "warning_shown":
            warnings[d.get("name", "?")] += 1
        elif ev == "error":
            errors[(d.get("where", "?"), d.get("type", "?"))] += 1
        elif ev == "dialog":
            name = d.get("name", "?")
            dialog_stats[name]["count"] += 1
            dialog_stats[name]["cancel"] += 1 if d.get("outcome") == "cancel" else 0
            dialog_stats[name]["ms"] += int(d.get("ms_open", 0) or 0)

    lengths = [s.get("secs", 0) for s in sessions.values() if s.get("secs")]
    lines = ["# VPM Tracker Usage Report", ""]
    lines.append(f"Events analyzed: {len(events)} over last {months} month(s)")
    lines.append(f"Sessions: {len(sessions)}")
    if lengths:
        lines.append(f"Average session: {int(mean(lengths))}s")
        lines.append(f"Median session: {int(median(lengths))}s")
    lines.append("")
    lines.append("## Sessions")
    lines.append("Weekdays: " + ", ".join(f"{k}: {v}" for k, v in weekdays.most_common()))
    lines.append("Hours: " + ", ".join(f"{h:02d}:00={c}" for h, c in sorted(hours.items())))
    lines.append("")
    lines.append("## Feature Leaderboard")
    for ev, count in counts.most_common():
        lines.append(f"- {ev}: {count}")
    never = [f for f in KNOWN_FEATURES if counts.get(f, 0) == 0]
    lines.append("")
    lines.append("Never used: " + (", ".join(never) if never else "none"))
    lines.append("")
    lines.append("## Friction")
    lines.append("Warnings:")
    for name, count in warnings.most_common(10):
        lines.append(f"- {name}: {count}")
    lines.append("Dialogs:")
    ranked = sorted(dialog_stats.items(), key=lambda kv: (kv[1]["cancel"] / max(1, kv[1]["count"]), kv[1]["ms"]), reverse=True)
    for name, s in ranked[:10]:
        rate = int(100 * s["cancel"] / max(1, s["count"]))
        lines.append(f"- {name}: {s['count']} shown, {rate}% cancel, {s['ms']}ms total")
    lines.append("")
    lines.append("## Errors")
    for (where, typ), count in errors.most_common():
        lines.append(f"- {where} / {typ}: {count}")
    lines.append("")
    lines.append("## Trend")
    months_sorted = sorted(by_month)
    if len(months_sorted) >= 2:
        prev, cur = months_sorted[-2], months_sorted[-1]
        all_events = sorted(set(by_month[prev]) | set(by_month[cur]))
        for ev in all_events:
            lines.append(f"- {ev}: {prev}={by_month[prev][ev]}, {cur}={by_month[cur][ev]}")
    else:
        lines.append("Need at least two months for trend data.")
    return "\n".join(lines) + "\n"


def main():
    months = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    report = build_report(months)
    print(report)
    root = str(usage.root)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "report-latest.md"), "w", encoding="utf-8") as f:
        f.write(report)


if __name__ == "__main__":
    main()
