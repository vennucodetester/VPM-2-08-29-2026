"""
ConfigManager — per-project settings (owners, holidays, exclude_weekends).

Multi-project support (file format v2.0) means each project keeps its own
metadata. The scheduler and workday calculator still resolve settings via
`ConfigManager()` — we keep that call pattern but back it with a swappable
active profile, so the currently-active project tab is the source of truth.

Call `ConfigManager.set_active_project(project_id)` whenever the user
switches tabs or before running recalculations for a specific project.
Global defaults persist in `vpm_config.json` as before, and are used as the
initial values for every new project.
"""
import json
import os

_CONFIG_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.join(os.path.expanduser("~"), ".vpm_tracker")),
    "VPMTracker")
CONFIG_FILE = os.path.join(_CONFIG_DIR, "vpm_config.json")
LEGACY_CONFIG_FILES = [
    os.path.join(os.getcwd(), "vpm_config.json"),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "vpm_config.json")),
]


class _ProjectConfig:
    """Plain holder — no disk I/O, one per project tab."""
    __slots__ = ("owners", "holidays", "exclude_weekends")

    def __init__(self, owners=None, holidays=None, exclude_weekends=True):
        self.owners = list(owners) if owners else ["Unassigned", "Me"]
        self.holidays = list(holidays) if holidays else []
        self.exclude_weekends = bool(exclude_weekends)

    def snapshot(self) -> dict:
        return {
            "owners": list(self.owners),
            "holidays": list(self.holidays),
            "exclude_weekends": self.exclude_weekends,
        }


class ConfigManager:
    _instance = None
    # Registry of all per-project configs, keyed by a caller-chosen project id.
    _projects: dict = {}
    # The project whose config is "live" for scheduler calls right now.
    _active_id: str = "__default__"

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConfigManager, cls).__new__(cls)
            cls._instance._load_disk_defaults()
        return cls._instance

    # ---- disk-backed global defaults (used when no project has been loaded) ----
    def _load_disk_defaults(self):
        owners = ["Unassigned", "Me"]
        holidays = []
        exclude_weekends = True
        source = CONFIG_FILE if os.path.exists(CONFIG_FILE) else next(
            (path for path in LEGACY_CONFIG_FILES if os.path.exists(path)), None)
        if source:
            try:
                with open(source, "r") as f:
                    data = json.load(f)
                    owners = data.get("owners", owners)
                    holidays = data.get("holidays", holidays)
                    exclude_weekends = data.get("exclude_weekends", exclude_weekends)
            except Exception:
                pass
        # Seed the default project so every accessor has something to read.
        ConfigManager._projects["__default__"] = _ProjectConfig(
            owners=owners, holidays=holidays, exclude_weekends=exclude_weekends
        )
        ConfigManager._active_id = "__default__"
        if source and source != CONFIG_FILE:
            self._save_disk_defaults()

    def _save_disk_defaults(self):
        """Only the __default__ profile is persisted to vpm_config.json.
        Project-specific values live inside the .vpmt file."""
        cfg = ConfigManager._projects.get("__default__")
        if cfg is None:
            return
        try:
            os.makedirs(_CONFIG_DIR, exist_ok=True)
            with open(CONFIG_FILE, "w") as f:
                json.dump(cfg.snapshot(), f, indent=4)
        except Exception:
            pass

    # ---- project registry ----
    @classmethod
    def register_project(cls, project_id: str, metadata: dict = None):
        """Create (or reset) a project profile from loaded metadata dict."""
        ConfigManager()
        md = metadata or {}
        base = cls._projects["__default__"].snapshot()
        loaded_owners = md.get("owners")
        defaultish = not loaded_owners or set(loaded_owners) <= {"", "Unassigned", "Me"}
        base_owners = [owner for owner in base.get("owners", []) if str(owner).strip()]
        if defaultish and set(base_owners) - {"Unassigned", "Me"}:
            loaded_owners = base_owners
        merged = {
            "owners": loaded_owners if loaded_owners else base.get("owners"),
            "holidays": md["holidays"] if "holidays" in md else base.get("holidays"),
            "exclude_weekends": (
                md["exclude_weekends"] if "exclude_weekends" in md
                else base.get("exclude_weekends", True)
            ),
        }
        cls._projects[project_id] = _ProjectConfig(
            owners=merged.get("owners"),
            holidays=merged.get("holidays"),
            exclude_weekends=merged.get("exclude_weekends", True),
        )

    @classmethod
    def default_snapshot(cls) -> dict:
        ConfigManager()
        return cls._projects["__default__"].snapshot()

    @classmethod
    def unregister_project(cls, project_id: str):
        cls._projects.pop(project_id, None)
        if cls._active_id == project_id:
            cls._active_id = "__default__"

    @classmethod
    def set_active_project(cls, project_id: str):
        if project_id not in cls._projects:
            cls.register_project(project_id)
        cls._active_id = project_id

    @classmethod
    def active_project_id(cls) -> str:
        return cls._active_id

    @classmethod
    def snapshot_project(cls, project_id: str = None) -> dict:
        pid = project_id or cls._active_id
        cfg = cls._projects.get(pid) or cls._projects["__default__"]
        return cfg.snapshot()

    # ---- active-project accessors (legacy API used by scheduler/IO/UI) ----
    def _active(self) -> _ProjectConfig:
        return ConfigManager._projects.get(ConfigManager._active_id) \
            or ConfigManager._projects["__default__"]

    def get_owners(self):
        return self._active().owners

    def set_owners(self, owners):
        self._active().owners = list(owners)
        if ConfigManager._active_id == "__default__":
            self._save_disk_defaults()

    def get_holidays(self):
        return self._active().holidays

    def set_holidays(self, holidays):
        self._active().holidays = list(holidays)
        if ConfigManager._active_id == "__default__":
            self._save_disk_defaults()

    def get_exclude_weekends(self):
        return self._active().exclude_weekends

    def set_exclude_weekends(self, exclude):
        self._active().exclude_weekends = bool(exclude)
        if ConfigManager._active_id == "__default__":
            self._save_disk_defaults()
