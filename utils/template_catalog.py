"""Upgrade-safe reusable phase/activity templates."""
import json
import os


BUILT_INS = [
    {"id": "phase-idea", "name": "Idea", "kind": "phase", "header": "Project Phases", "duration": 1, "version": 1},
    {"id": "phase-design", "name": "Design", "kind": "phase", "header": "Project Phases", "duration": 10, "version": 1},
    {"id": "phase-prototype", "name": "Prototype", "kind": "phase", "header": "Project Phases", "duration": 10, "version": 1},
    {"id": "phase-sourcing", "name": "Sourcing", "kind": "phase", "header": "Project Phases", "duration": 10, "version": 1},
    {"id": "phase-lab", "name": "Lab Testing", "kind": "phase", "header": "Project Phases", "duration": 1, "version": 1},
    {"id": "phase-implementation", "name": "Implementation", "kind": "phase", "header": "Project Phases", "duration": 10, "version": 1},
    {"id": "test-instrumentation", "name": "Instrumentation", "kind": "activity", "header": "Lab Testing", "duration": 5, "sequence": "sequential", "version": 1},
    {"id": "test-doe", "name": "DOE", "kind": "activity", "header": "Lab Testing", "duration": 10, "sequence": "sequential", "version": 1},
    {"id": "test-nsf2", "name": "NSF Type 2", "kind": "activity", "header": "Lab Testing", "duration": 10, "sequence": "sequential", "version": 1},
    {"id": "campaign-lab", "name": "Lab Test Campaign", "kind": "campaign",
     "children": ["test-instrumentation", "test-doe", "test-nsf2"], "version": 1},
]


def _path():
    root = os.path.join(os.environ.get(
        "LOCALAPPDATA", os.path.join(os.path.expanduser("~"), ".vpm_tracker")),
        "VPMTracker")
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, "templates.json")


def load_templates():
    by_id = {item["id"]: dict(item) for item in BUILT_INS}
    try:
        with open(_path(), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
            replace_built_ins = isinstance(payload, dict) and payload.get(
                "replace_built_ins", False)
            items = payload.get("items", []) if isinstance(payload, dict) else payload
            if replace_built_ins:
                by_id = {}
            for item in items:
                if item.get("id"):
                    by_id[item["id"]] = item
    except (OSError, ValueError, TypeError):
        pass
    return list(by_id.values())


def save_templates(items):
    with open(_path(), "w", encoding="utf-8") as handle:
        # A full replacement is intentional: users can delete or rename the
        # original defaults without those defaults returning on next launch.
        json.dump({"replace_built_ins": True, "items": list(items)},
                  handle, indent=2)
