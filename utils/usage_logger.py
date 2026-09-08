"""Private, upgrade-safe workflow telemetry stored outside the app folder."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from PyQt6.QtCore import QSettings

from utils.version_info import install_dir


SCHEMA_VERSION = 2
SESSION_IDLE_SECONDS = 30 * 60
REPO_TELEMETRY_DIRNAME = "telemetry"
REPO_EXPORT_MONTHS = 2
SENSITIVE_KEYS = {
    "path", "file", "filename", "task", "task_name", "note", "text",
    "dollar", "dollars", "amount", "value",
}


def _default_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "VPMTracker" / "telemetry"
    return Path.home() / ".vpm_tracker" / "telemetry"


def repo_telemetry_dir(dest=None) -> Path:
    """Repo-local snapshot folder. AppData remains the canonical store."""
    if dest:
        return Path(dest)
    return Path(install_dir()) / REPO_TELEMETRY_DIRNAME


def _safe_details(details: dict) -> dict:
    clean = {}
    for key, value in (details or {}).items():
        low_key = str(key).lower()
        if low_key in SENSITIVE_KEYS:
            if low_key in {"path", "file", "filename"}:
                clean["file_type"] = Path(str(value)).suffix.lower()
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[key] = value
    return clean


def _atomic_write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _sanitize_event(row: dict) -> dict | None:
    if not isinstance(row, dict) or not row.get("ev"):
        return None
    clean = {
        "schema": row.get("schema", SCHEMA_VERSION),
        "eid": row.get("eid"),
        "ts": row.get("ts"),
        "sid": row.get("sid"),
        "install": row.get("install"),
        "project": row.get("project"),
        "env": row.get("env"),
        "ev": str(row.get("ev")),
        "d": _safe_details(row.get("d") or {}),
    }
    return clean


def _recent_month_stamps(count=REPO_EXPORT_MONTHS):
    now = datetime.now()
    year, month = now.year, now.month
    stamps = []
    for _ in range(max(1, count)):
        stamps.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year -= 1
            month = 12
    return stamps


class UsageLogger:
    def __init__(self, root=None, enabled=None, import_legacy=True):
        self.sid = uuid.uuid4().hex
        self.started = time.time()
        self.last_activity = self.started
        self.project_id = None
        self.root = Path(root) if root else _default_root()
        self.state_path = self.root / "state.json"
        self.state = self._read_state()
        self.install_id = self.state.get("install_id") or secrets.token_hex(12)
        self.state["install_id"] = self.install_id
        is_test = (
            os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen"
            or os.environ.get("VPM_TELEMETRY", "") == "0"
            or "pytest" in sys.modules
        )
        configured = bool(QSettings("VPM", "VPMTracker").value(
            "usage_logging", True, type=bool))
        # Tests are disabled by default, but an explicitly constructed logger
        # with enabled=True may write into its caller-supplied temporary root.
        self.enabled = bool(configured if enabled is None else enabled) \
            and (not is_test or enabled is True)
        self.environment = "test" if is_test else "production"
        self._export_lock = threading.Lock()
        self._export_thread = None
        if not self.enabled:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        if import_legacy and not self.state.get("legacy_import_v1"):
            self._import_legacy_logs()
            self.state["legacy_import_v1"] = datetime.now().isoformat(timespec="seconds")
        self.prune()
        self._write_state()

    def _read_state(self):
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_state(self):
        if not self.enabled:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def set_project(self, project_id):
        self.project_id = str(project_id) if project_id else None

    def set_enabled(self, enabled: bool):
        QSettings("VPM", "VPMTracker").setValue("usage_logging", bool(enabled))
        self.enabled = bool(enabled) and self.environment != "test"
        if self.enabled:
            self.root.mkdir(parents=True, exist_ok=True)
            self._write_state()

    def path(self, stamp=None):
        stamp = stamp or datetime.now().strftime("%Y-%m")
        return self.root / f"usage-{stamp}.jsonl"

    def prune(self):
        cutoff = datetime.now() - timedelta(days=366)
        try:
            for path in self.root.glob("usage-*.jsonl"):
                if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                    path.unlink()
        except OSError:
            pass

    def log(self, event: str, **details):
        if not self.enabled:
            return
        try:
            now_epoch = time.time()
            if now_epoch - self.last_activity >= SESSION_IDLE_SECONDS:
                self.sid = uuid.uuid4().hex
                self.started = now_epoch
            self.last_activity = now_epoch
            now = datetime.now()
            line = {
                "schema": SCHEMA_VERSION,
                "eid": uuid.uuid4().hex,
                "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
                "sid": self.sid,
                "install": self.install_id,
                "project": self.project_id,
                "env": self.environment,
                "ev": str(event),
                "d": _safe_details(details),
            }
            self.root.mkdir(parents=True, exist_ok=True)
            with self.path().open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, separators=(",", ":")) + "\n")
            previous = self.state.get("last_event")
            if previous:
                try:
                    gap = (now - datetime.fromisoformat(previous)).days
                    if gap >= 7 and event == "app_start":
                        self.state["last_gap_days"] = gap
                except ValueError:
                    pass
            self.state["last_event"] = now.isoformat(timespec="seconds")
            self.state["last_event_name"] = str(event)
            self._write_state()
            try:
                last_summary = datetime.fromisoformat(
                    self.state.get("last_summary", "1970-01-01"))
            except ValueError:
                last_summary = datetime(1970, 1, 1)
            if now - last_summary >= timedelta(days=7):
                self.write_summary()
        except Exception:
            pass

    def session_secs(self) -> int:
        return max(0, int(time.time() - self.started))

    def _legacy_candidates(self):
        install = Path(install_dir()).resolve()
        roots = [install, install / ".git-helper" / "recovery"]
        if install.parent != install:
            roots.append(install.parent / ".git-helper" / "recovery")
        found = set()
        for root in roots:
            if not root.exists():
                continue
            try:
                for path in root.rglob("usage-*.jsonl"):
                    if self.root not in path.parents:
                        found.add(path.resolve())
            except OSError:
                continue
        return sorted(found)

    def _import_legacy_logs(self):
        """Merge snapshot-style logs using maximum line multiplicity."""
        sources = []
        for path in self._legacy_candidates():
            try:
                sources.append(Counter(
                    line for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ))
            except OSError:
                pass
        if not sources:
            return
        merged = Counter()
        for source in sources:
            for line, count in source.items():
                merged[line] = max(merged[line], count)
        by_month = {}
        for line, count in merged.items():
            try:
                old = json.loads(line)
                month = str(old.get("ts", ""))[:7]
                if len(month) != 7:
                    continue
            except Exception:
                continue
            for occurrence in range(count):
                seed = f"{line}\0{occurrence}".encode("utf-8")
                record = {
                    "schema": SCHEMA_VERSION,
                    "eid": "legacy-" + hashlib.sha256(seed).hexdigest()[:24],
                    "ts": old.get("ts"),
                    "sid": old.get("sid"),
                    "install": self.install_id,
                    "project": None,
                    "env": "legacy-unknown",
                    "ev": old.get("ev"),
                    "d": _safe_details(old.get("d", {})),
                }
                by_month.setdefault(month, []).append(record)
        for month, records in by_month.items():
            target = self.path(month)
            known = set()
            if target.exists():
                for line in target.read_text(encoding="utf-8").splitlines():
                    try:
                        known.add(json.loads(line).get("eid"))
                    except Exception:
                        pass
            with target.open("a", encoding="utf-8") as handle:
                for record in sorted(records, key=lambda row: (row["ts"], row["eid"])):
                    if record["eid"] not in known:
                        handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    def write_summary(self):
        if not self.enabled:
            return
        counts = Counter()
        sessions = set()
        first = last = None
        cutoff = datetime.now() - timedelta(days=7)
        for path in self.root.glob("usage-*.jsonl"):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                try:
                    if datetime.strptime(row.get("ts", ""), "%Y-%m-%d %H:%M:%S") < cutoff:
                        continue
                except ValueError:
                    continue
                event_name = row.get("ev")
                if event_name == "file_save":
                    event_name = ("file_save_manual" if row.get("d", {}).get("manual")
                                  else "file_save_automatic")
                counts[event_name] += 1
                if row.get("sid"):
                    sessions.add(row.get("sid"))
                ts = row.get("ts")
                if ts:
                    first = ts if first is None or ts < first else first
                    last = ts if last is None or ts > last else last
        summary = {
            "generated": datetime.now().isoformat(timespec="seconds"),
            "period_days": 7,
            "first_event": first,
            "last_event": last,
            "sessions": len(sessions),
            "events": sum(counts.values()),
            "counts": dict(counts.most_common()),
            "last_gap_days": self.state.get("last_gap_days", 0),
        }
        try:
            (self.root / "summary-latest.json").write_text(
                json.dumps(summary, indent=2), encoding="utf-8")
            self.state["last_summary"] = datetime.now().isoformat(timespec="seconds")
            self._write_state()
        except OSError:
            pass

    def diagnostics(self):
        dest = repo_telemetry_dir()
        return {
            "enabled": self.enabled,
            "environment": self.environment,
            "storage": str(self.root),
            "repo_export": str(dest),
            "last_event": self.state.get("last_event", "never"),
            "last_event_name": self.state.get("last_event_name", "none"),
            "last_gap_days": self.state.get("last_gap_days", 0),
            "legacy_imported": bool(self.state.get("legacy_import_v1")),
        }

    def _blocks_default_repo_export(self, dest_root: Path) -> bool:
        """Never let pytest / offscreen sessions write into the real repo folder."""
        if self.environment != "test":
            return False
        default = Path(install_dir()) / REPO_TELEMETRY_DIRNAME
        try:
            return dest_root.resolve() == default.resolve()
        except OSError:
            return True

    def _copy_sanitized_jsonl(self, source: Path, dest: Path) -> int:
        kept = []
        try:
            lines = source.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            clean = _sanitize_event(row)
            if clean is None:
                continue
            if clean.get("env") == "test" and self._blocks_default_repo_export(dest.parent):
                continue
            kept.append(json.dumps(clean, separators=(",", ":")))
        _atomic_write_text(dest, ("\n".join(kept) + ("\n" if kept else "")))
        return len(kept)

    def export_repo_telemetry(self, dest=None, include_report=True):
        """Copy sanitized recent AppData telemetry into the repo-local folder.

        AppData (`self.root`) stays the canonical store. This writes a snapshot
        for Creative / Git Helper: current-month JSONL (plus previous month when
        present), summary-latest.json, and optionally report-latest.md.
        """
        if not self.enabled:
            return None
        dest_root = repo_telemetry_dir(dest)
        if self._blocks_default_repo_export(dest_root):
            return None
        try:
            dest_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        try:
            for stamp in _recent_month_stamps():
                source = self.path(stamp)
                if source.exists():
                    self._copy_sanitized_jsonl(source, dest_root / source.name)
            summary_src = self.root / "summary-latest.json"
            if not summary_src.exists():
                self.write_summary()
            if summary_src.exists():
                try:
                    _atomic_write_text(
                        dest_root / "summary-latest.json",
                        summary_src.read_text(encoding="utf-8"),
                    )
                except OSError:
                    pass
            if include_report:
                try:
                    from utils.usage_report import build_report
                    report = build_report(months=3, root=self.root)
                    _atomic_write_text(dest_root / "report-latest.md", report)
                except Exception:
                    pass
            return dest_root
        except OSError:
            return None

    def sync_repo_telemetry(self, dest=None, background=False, include_report=True):
        """Export to the repo folder, optionally on a daemon thread."""
        if not self.enabled:
            return None
        dest_root = repo_telemetry_dir(dest)
        if self._blocks_default_repo_export(dest_root):
            return None
        if background:
            thread = threading.Thread(
                target=self._export_repo_telemetry_safe,
                args=(dest, include_report),
                daemon=True,
                name="telemetry-repo-export",
            )
            self._export_thread = thread
            thread.start()
            return dest_root
        with self._export_lock:
            return self.export_repo_telemetry(dest=dest, include_report=include_report)

    def _export_repo_telemetry_safe(self, dest, include_report):
        try:
            with self._export_lock:
                self.export_repo_telemetry(dest=dest, include_report=include_report)
        except Exception:
            pass


usage = UsageLogger()


def log(event: str, **details):
    usage.log(event, **details)


def set_enabled(enabled: bool):
    usage.set_enabled(enabled)


def set_project(project_id):
    usage.set_project(project_id)


def is_enabled() -> bool:
    return bool(usage.enabled)


def sync_repo_telemetry(dest=None, background=False, include_report=True):
    return usage.sync_repo_telemetry(
        dest=dest, background=background, include_report=include_report)


def timed_exec(dialog, name: str):
    start = time.time()
    try:
        result = dialog.exec()
        usage.log("dialog", name=name, outcome="ok" if result else "cancel",
                  ms_open=int((time.time() - start) * 1000))
        return result
    except Exception as exc:
        usage.log("error", where=f"dialog:{name}", type=type(exc).__name__)
        raise
