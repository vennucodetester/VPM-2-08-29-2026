import unittest

from models.task_node import TaskNode
from utils.identity_story import (
    build_identity_story, event_context_label, find_duplicate_identities,
    identity_flag_overlaps, merge_identity_ids, shortest_unique_event_labels,
)


def token(identity_id, label, kind="case"):
    return {"id": identity_id, "label": label, "kind": kind}


def task(name, start, end, tokens=(), status="Not Started", parent=None):
    node = TaskNode(name, parent=parent)
    node.start_date, node.end_date = start, end
    node.status = status
    node.task_tokens = [dict(value) for value in tokens]
    return node


def project(identity, name, roots):
    return {"id": identity, "name": name, "roots": roots, "metadata": {}}


class IdentityStoryTests(unittest.TestCase):
    def test_duplicate_task_names_use_shortest_unique_hierarchy(self):
        selected = token("case-1", "RLN2MA-1")
        root = task("VAVE activities", "2026-08-01", "2026-10-31")
        cassette = task("Cassette VAVE", "2026-08-01", "2026-10-31",
                        parent=root)
        short_term = task("S/m-2", "2026-08-31", "2026-09-18",
                          parent=cassette)
        first = task("Lab Testing", "2026-08-31", "2026-09-18",
                     [selected], parent=short_term)
        aluminum = task("MT Aluminum Coil", "2026-10-05", "2026-10-23",
                        parent=cassette)
        second = task("Lab Testing", "2026-10-05", "2026-10-23",
                      [selected], parent=aluminum)
        case_vave = task("Case VAVE activities", "2026-08-31", "2026-09-11",
                         parent=root)
        third = task("Lab Testing", "2026-08-31", "2026-09-11",
                     [selected], parent=case_vave)
        root.children = [cassette, case_vave]
        cassette.children = [short_term, aluminum]
        short_term.children = [first]
        aluminum.children = [second]
        case_vave.children = [third]

        story = build_identity_story(
            [project("P", "VAVE-MB 2.0", [root])], "case-1")
        labels = shortest_unique_event_labels(story.events)
        self.assertEqual({
            "S/m-2 › Lab Testing",
            "MT Aluminum Coil › Lab Testing",
            "Case VAVE activities › Lab Testing",
        }, set(labels.values()))
        self.assertEqual(3, len(set(labels.values())))

    def test_repeated_parent_names_expand_until_labels_are_unique(self):
        selected = token("case-1", "Case")
        roots = []
        for branch_name in ("Branch A", "Branch B"):
            branch = task(branch_name, "2026-09-01", "2026-09-10")
            build = task("Build", "2026-09-01", "2026-09-10",
                         parent=branch)
            lab = task("Lab Testing", "2026-09-01", "2026-09-10",
                       [selected], parent=build)
            branch.children = [build]
            build.children = [lab]
            roots.append(branch)
        story = build_identity_story([project("P", "P", roots)], "case-1")
        self.assertEqual({
            "Branch A › Build › Lab Testing",
            "Branch B › Build › Lab Testing",
        }, set(shortest_unique_event_labels(story.events).values()))

    def test_project_and_connected_identities_supply_lane_context(self):
        selected = token("case-1", "Case")
        node = task("Lab Testing", "2026-09-01", "2026-09-10", [
            selected,
            token("room-1", "Room 1", "room"),
            token("cassette-1", "LT-W1-1", "article"),
            token("fixture-1", "Fixture A", "fixture"),
        ])
        story = build_identity_story(
            [project("P", "VAVE-MB 2.0", [node])], "case-1")
        self.assertEqual(
            "VAVE-MB 2.0 · LT-W1-1 · Fixture A · +1 more",
            event_context_label(story.events[0]))

    def test_same_task_path_in_different_projects_uses_project_context(self):
        selected = token("case-1", "Case")
        story = build_identity_story([
            project("P1", "Alpha", [
                task("Lab Testing", "2026-09-01", "2026-09-02", [selected])]),
            project("P2", "Beta", [
                task("Lab Testing", "2026-09-03", "2026-09-04", [selected])]),
        ], "case-1")
        labels = shortest_unique_event_labels(story.events)
        self.assertEqual({"Lab Testing"}, set(labels.values()))
        self.assertEqual({"Alpha", "Beta"},
                         {event_context_label(value) for value in story.events})

    def test_parent_leaf_completed_undated_and_cross_project_collection(self):
        selected = token("case-1", "RLN2MA-1")
        parent = task("Parent", "2026-08-31", "2026-09-18", [selected])
        child = task("Child", "2026-09-01", "2026-09-04", [], parent=parent)
        parent.children = [child]
        leaf = task("Leaf", "2026-10-05", "2026-10-23", [selected],
                    status="Completed")
        undated = task("Inbox work", None, None, [selected])
        story = build_identity_story([
            project("P1", "Alpha", [parent, undated]),
            project("P2", "Beta", [leaf]),
        ], "case-1")
        self.assertEqual(["Parent", "Leaf"],
                         [value.task_name for value in story.events])
        self.assertTrue(story.events[0].has_children)
        self.assertEqual("Child", story.events[0].child_context[0].task_name)
        self.assertEqual("Completed", story.events[1].status)
        self.assertEqual(["Inbox work"],
                         [value.task_name for value in story.unscheduled_events])
        self.assertEqual({"P1", "P2"},
                         {value.project_id for value in story.all_events})

    def test_events_sort_by_dates_project_and_path(self):
        selected = token("case-1", "Case")
        values = [
            task("Later", "2026-09-02", "2026-09-03", [selected]),
            task("Zulu", "2026-09-01", "2026-09-03", [selected]),
            task("Alpha", "2026-09-01", "2026-09-02", [selected]),
        ]
        story = build_identity_story([project("P", "P", values)], "case-1")
        self.assertEqual(["Alpha", "Zulu", "Later"],
                         [value.task_name for value in story.events])

    def test_rename_uses_stable_id_and_same_label_different_id_stays_separate(self):
        old = task("Old", "2026-09-01", "2026-09-02",
                   [token("case-1", "Old Label")])
        same_label_other_id = task(
            "Other", "2026-09-01", "2026-09-02",
            [token("case-2", "Environmental Case")])
        projects = [project("P", "P", [old, same_label_other_id])]
        story = build_identity_story(
            projects, "case-1", "Environmental Case", "case")
        self.assertEqual("Environmental Case", story.identity_label)
        self.assertEqual(["Old"], [value.task_name for value in story.events])
        self.assertFalse(build_identity_story(projects, "missing").events)

    def test_resources_only_legacy_assignment_appears_in_story(self):
        from utils.resource_allocation import legacy_resource_id
        node = task("Legacy room use", "2026-09-01", "2026-09-02")
        node.resources = {"room": "Room 9"}
        story = build_identity_story(
            [project("P", "P", [node])],
            legacy_resource_id("room", "Room 9"))
        self.assertEqual(["Legacy room use"],
                         [value.task_name for value in story.events])
        self.assertEqual("Room 9", story.identity_label)

    def test_independent_overlap_and_nonoverlap_calendar_rule(self):
        selected = token("case-1", "Case")
        first = task("First", "2026-09-01", "2026-09-10", [selected])
        second = task("Second", "2026-09-05", "2026-09-12", [selected])
        later = task("Later", "2026-09-13", "2026-09-14", [selected])
        story = build_identity_story([project("P", "P", [first, second, later])],
                                     "case-1")
        self.assertEqual(1, len(story.overlaps))
        self.assertEqual(("2026-09-05", "2026-09-10"),
                         (story.overlaps[0].overlap_start,
                          story.overlaps[0].overlap_end))

    def test_unchecked_overlap_flag_omits_overlap_records(self):
        selected = token("doe-type-2", "DOE -Type 2", "activity")
        first = task("First", "2026-09-01", "2026-09-10", [selected])
        second = task("Second", "2026-09-05", "2026-09-12", [selected])
        projects = [project("P", "P", [first, second])]
        hidden = build_identity_story(
            projects, "doe-type-2", flag_overlaps=False)
        shown = build_identity_story(
            projects, "doe-type-2", flag_overlaps=True)
        self.assertEqual(2, len(hidden.events))
        self.assertEqual([], hidden.overlaps)
        self.assertEqual(1, len(shown.overlaps))
        self.assertEqual(("2026-09-05", "2026-09-10"),
                         (shown.overlaps[0].overlap_start,
                          shown.overlaps[0].overlap_end))

    def test_identity_flag_overlaps_reads_metadata_checkbox(self):
        options = [
            {"id": "doe-type-2", "name": "DOE -Type 2",
             "flag_overlaps": False},
            {"id": "case-1", "name": "RLN2MA-1", "flag_overlaps": True},
            {"id": "legacy-1", "conflict_enabled": True},
        ]
        self.assertFalse(identity_flag_overlaps(options, "doe-type-2"))
        self.assertTrue(identity_flag_overlaps(options, "case-1"))
        self.assertTrue(identity_flag_overlaps(options, "legacy-1"))
        self.assertFalse(identity_flag_overlaps(options, "missing"))

    def test_parent_and_descendant_do_not_overlap_each_other(self):
        selected = token("case-1", "Case")
        parent = task("Parent", "2026-09-01", "2026-09-10", [selected])
        child = task("Child", "2026-09-02", "2026-09-03", [selected],
                     parent=parent)
        parent.children = [child]
        story = build_identity_story([project("P", "P", [parent])], "case-1")
        self.assertEqual(2, len(story.events))
        self.assertEqual([], story.overlaps)

    def test_connections_capture_other_identity_types(self):
        selected = token("case-1", "Case")
        node = task("Test", "2026-09-01", "2026-09-02", [
            selected,
            token("room-1", "Room 1", "room"),
            token("cassette-1", "LT-W1-1", "article"),
            token("doe", "DOE", "activity"),
            token("fixture", "Fixture A", "custom_fixture"),
            token("phase", "Design", "phase"),
        ])
        story = build_identity_story([project("P", "P", [node])], "case-1")
        self.assertEqual({"room-1", "cassette-1", "doe", "fixture"},
                         {value.identity_id for value in
                          story.connected_identities})

    def test_rln2ma_acceptance_has_three_bars_and_one_red_period(self):
        selected = token("rln2ma-1", "RLN2MA-1")
        nodes = [
            task("Case VAVE", "2026-08-31", "2026-09-18", [selected]),
            task("Cassette VAVE", "2026-08-31", "2026-09-11", [selected]),
            task("Aluminum Coil", "2026-10-05", "2026-10-23", [selected]),
        ]
        for node in nodes:
            node.children = [task("Context", node.start_date, node.end_date,
                                  [], parent=node)]
        story = build_identity_story([project("P", "Acceptance", nodes)],
                                     "rln2ma-1")
        self.assertEqual(3, len(story.events))
        self.assertEqual(1, len(story.overlaps))
        self.assertEqual(("2026-08-31", "2026-09-11"),
                         (story.overlaps[0].overlap_start,
                          story.overlaps[0].overlap_end))

    def test_duplicate_diagnostics_do_not_merge_and_merge_requires_confirmation(self):
        options = [
            {"id": "case-1", "name": " RLN2MA-1 ", "kind": "case"},
            {"id": "case-2", "name": "rln2ma-1", "kind": "case"},
        ]
        groups = find_duplicate_identities(options)
        self.assertEqual(("case-1", "case-2"), groups[0].identity_ids)
        node = task("Use", "2026-09-01", "2026-09-02",
                    [token("case-2", "RLN2MA-1")])
        projects = [project("P", "P", [node])]
        with self.assertRaises(PermissionError):
            merge_identity_ids(projects, ["case-2"], "case-1")
        self.assertEqual("case-2", node.task_tokens[0]["id"])
        self.assertEqual(1, merge_identity_ids(
            projects, ["case-2"], "case-1", confirmed=True))
        self.assertEqual("case-1", node.task_tokens[0]["id"])


if __name__ == "__main__":
    unittest.main()
