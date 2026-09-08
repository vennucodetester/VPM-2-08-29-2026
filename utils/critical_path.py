"""
Critical Path Analysis Utility

Implements Critical Path Method (CPM) algorithm for project scheduling.
Calculates Early Start, Early Finish, Late Start, Late Finish, and Slack for all tasks.
Identifies the critical path (tasks with zero slack).
"""

from datetime import datetime, timedelta
from typing import List, Dict, Set, Optional, Tuple
from models.task_node import TaskNode

DATE_FMT = "%Y-%m-%d"


class CriticalPathAnalyzer:
    """
    Analyzes task dependencies and calculates the critical path.

    The critical path is the longest sequence of dependent tasks that determines
    the minimum project duration. Any delay in critical path tasks delays the entire project.
    """

    def __init__(self, root_nodes: List[TaskNode], holidays=None, exclude_weekends=None):
        """
        Initialize analyzer with root task nodes.

        Args:
            root_nodes: List of root-level TaskNode objects
            holidays: Optional set of holiday date strings (YYYY-MM-DD)
            exclude_weekends: Optional bool for excluding weekends
        """
        self.root_nodes = root_nodes
        self.all_nodes = self._flatten_nodes(root_nodes)
        self.node_map = {node.id: node for node in self.all_nodes}

        # ONLY include LEAF tasks (no children) in critical path calculation
        self.leaf_nodes = [node for node in self.all_nodes if not node.children]

        from utils.config_manager import ConfigManager
        config = ConfigManager()
        self.holidays = set(holidays if holidays is not None else config.get_holidays())
        self.exclude_weekends = bool(exclude_weekends if exclude_weekends is not None else config.get_exclude_weekends())

        # Results storage
        self.early_start: Dict[str, datetime] = {}
        self.early_finish: Dict[str, datetime] = {}
        self.late_start: Dict[str, datetime] = {}
        self.late_finish: Dict[str, datetime] = {}
        self.slack: Dict[str, int] = {}
        self.critical_path_ids: Set[str] = set()  # Critical LEAF task IDs
        self.critical_parent_ids: Set[str] = set()  # Parent IDs with critical descendants

    def _flatten_nodes(self, nodes: List[TaskNode]) -> List[TaskNode]:
        """Flatten hierarchical task structure into a flat list."""
        result = []
        for node in nodes:
            result.append(node)
            if node.children:
                result.extend(self._flatten_nodes(node.children))
        return result

    def _get_duration_days(self, node: TaskNode) -> int:
        """Calculate duration in workdays between start and end date."""
        if not node.start_date or not node.end_date:
            return 0
        from utils.workday_calculator import WorkdayCalculator
        return max(1, WorkdayCalculator.calculate_duration(
            node.start_date, node.end_date,
            holidays=self.holidays, exclude_weekends=self.exclude_weekends
        ))

    def _get_prev_workday(self, date_str: str) -> str:
        from utils.workday_calculator import WorkdayCalculator
        dt = datetime.strptime(date_str, DATE_FMT)
        while True:
            dt -= timedelta(days=1)
            if WorkdayCalculator.is_workday(dt, self.holidays, self.exclude_weekends):
                return dt.strftime(DATE_FMT)

    def _subtract_workdays(self, end_date_str: str, days: int) -> str:
        from utils.workday_calculator import WorkdayCalculator
        if days <= 1:
            return end_date_str
        dt = datetime.strptime(end_date_str, DATE_FMT)
        added = 0
        target = days - 1
        while added < target:
            dt -= timedelta(days=1)
            if WorkdayCalculator.is_workday(dt, self.holidays, self.exclude_weekends):
                added += 1
        return dt.strftime(DATE_FMT)

    def _resolve_leaf_predecessors(self, pred_node: TaskNode, prefer_start: bool = False) -> List[TaskNode]:
        if not pred_node.children:
            return [pred_node]
        leaves = [n for n in self._flatten_nodes(pred_node.children) if not n.children]
        if not leaves:
            return []
        if prefer_start:
            return [min(leaves, key=lambda c: c.start_date if c.start_date else "9999-99-99")]
        return [max(leaves, key=lambda c: c.end_date if c.end_date else "0000-00-00")]

    def _get_predecessors(self, node: TaskNode) -> List[TaskNode]:
        """
        Get all predecessors for a LEAF task (both explicit and implicit).
        Respects start_rule (fixed tasks have no predecessor).
        """
        predecessors = []
        s_mode = node.start_rule.get("mode", "automatic")

        # 1. Explicit start rule dependency (continue_after, same_as)
        if s_mode in ("same_as", "continue_after"):
            target_id = node.start_rule.get("task_id")
            if target_id and target_id in self.node_map:
                pred = self.node_map[target_id]
                prefer_start = (s_mode == "same_as" and node.start_rule.get("field") == "start")
                predecessors.extend(self._resolve_leaf_predecessors(pred, prefer_start=prefer_start))

        # 2. Sequential sibling predecessor (only for automatic sequential mode)
        elif s_mode == "automatic":
            if node.parent and not node.is_parallel:
                siblings = node.parent.children
                try:
                    idx = siblings.index(node)
                    if idx > 0:
                        prev_sibling = siblings[idx - 1]
                        predecessors.extend(self._resolve_leaf_predecessors(prev_sibling))
                except ValueError:
                    pass
            elif not node.parent:
                try:
                    idx = self.root_nodes.index(node)
                    if idx > 0:
                        prev_root = self.root_nodes[idx - 1]
                        predecessors.extend(self._resolve_leaf_predecessors(prev_root))
                except ValueError:
                    pass

        # 3. Explicit end rule dependency (same_as)
        e_mode = node.end_rule.get("mode")
        if e_mode == "same_as":
            target_id = node.end_rule.get("task_id")
            if target_id and target_id in self.node_map:
                pred = self.node_map[target_id]
                predecessors.extend(self._resolve_leaf_predecessors(pred))

        unique_preds = []
        for p in predecessors:
            if p and p.id != node.id and p.id in self.node_map and p not in unique_preds:
                unique_preds.append(p)
        return unique_preds

    def _get_successors(self, node: TaskNode) -> List[TaskNode]:
        """Get all leaf successors for a task."""
        return [other for other in self.leaf_nodes if node in self._get_predecessors(other)]

    def _topological_sort(self) -> List[TaskNode]:
        """
        Sort LEAF tasks in topological order (predecessors before successors).
        Uses Kahn's algorithm for cycle detection.
        Only processes leaf tasks (tasks with no children).
        """
        in_degree = {node.id: 0 for node in self.leaf_nodes}
        for node in self.leaf_nodes:
            for succ in self._get_successors(node):
                if succ.id in in_degree:
                    in_degree[succ.id] += 1

        queue = [node for node in self.leaf_nodes if in_degree[node.id] == 0]
        result = []

        while queue:
            node = queue.pop(0)
            result.append(node)

            for succ in self._get_successors(node):
                if succ.id in in_degree:
                    in_degree[succ.id] -= 1
                    if in_degree[succ.id] == 0:
                        queue.append(succ)

        if len(result) != len(self.leaf_nodes):
            print("WARNING: Circular dependency detected in leaf tasks!")
            return self.leaf_nodes

        return result

    def forward_pass(self):
        """
        Calculate Early Start (ES) and Early Finish (EF) for all leaf tasks.
        ES = max(EF of all predecessors converted to workdays)
        EF = add_workdays(ES, duration)
        """
        from utils.workday_calculator import WorkdayCalculator
        sorted_nodes = self._topological_sort()

        for node in sorted_nodes:
            if not node.start_date:
                continue

            try:
                node_start = datetime.strptime(node.start_date, DATE_FMT)
            except ValueError:
                continue

            s_mode = node.start_rule.get("mode", "automatic")
            predecessors = self._get_predecessors(node)

            if s_mode == "fixed":
                self.early_start[node.id] = node_start
            elif predecessors:
                max_pred_es = None
                for pred in predecessors:
                    if pred.id not in self.early_finish:
                        continue
                    pred_ef_str = self.early_finish[pred.id].strftime(DATE_FMT)
                    if s_mode == "same_as":
                        field = node.start_rule.get("field", "start")
                        if field == "start" and pred.id in self.early_start:
                            base_date_str = self.early_start[pred.id].strftime(DATE_FMT)
                        else:
                            base_date_str = pred_ef_str
                        offset = int(node.start_rule.get("offset", 0) or 0)
                        if offset > 0:
                            base_date_str = WorkdayCalculator.add_workdays(
                                base_date_str, offset + 1,
                                holidays=self.holidays, exclude_weekends=self.exclude_weekends
                            )
                        pred_req_date = datetime.strptime(base_date_str, DATE_FMT)
                    else:
                        next_wd = WorkdayCalculator.get_next_workday(
                            pred_ef_str,
                            holidays=self.holidays, exclude_weekends=self.exclude_weekends
                        )
                        offset = int(node.start_rule.get("offset", 0) or 0) if s_mode == "continue_after" else 0
                        if offset > 0:
                            next_wd = WorkdayCalculator.add_workdays(
                                next_wd, offset + 1,
                                holidays=self.holidays, exclude_weekends=self.exclude_weekends
                            )
                        pred_req_date = datetime.strptime(next_wd, DATE_FMT)

                    if max_pred_es is None or pred_req_date > max_pred_es:
                        max_pred_es = pred_req_date

                self.early_start[node.id] = max_pred_es if max_pred_es else node_start
            else:
                self.early_start[node.id] = node_start

            # Calculate Early Finish
            duration = self._get_duration_days(node)
            es_str = self.early_start[node.id].strftime(DATE_FMT)
            ef_str = WorkdayCalculator.add_workdays(
                es_str, max(1, duration),
                holidays=self.holidays, exclude_weekends=self.exclude_weekends
            )
            self.early_finish[node.id] = datetime.strptime(ef_str, DATE_FMT)

    def backward_pass(self):
        """
        Calculate Late Start (LS) and Late Finish (LF) for all leaf tasks.
        LF = min(LS of all successors converted to workdays)
        LS = subtract_workdays(LF, duration)
        """
        from utils.workday_calculator import WorkdayCalculator
        sorted_nodes = list(reversed(self._topological_sort()))

        if not self.early_finish:
            return

        project_end = max(self.early_finish.values())

        for node in sorted_nodes:
            if node.id not in self.early_finish:
                continue

            successors = self._get_successors(node)
            duration = self._get_duration_days(node)

            if successors:
                min_succ_lf = project_end
                min_succ_ls = None
                for succ in successors:
                    if succ.id not in self.late_start:
                        continue
                    s_mode = succ.start_rule.get("mode", "automatic")
                    offset = int(succ.start_rule.get("offset", 0) or 0)

                    if s_mode == "same_as" and succ.start_rule.get("field") == "start":
                        succ_target_dt = self.late_start[succ.id]
                        succ_target_str = succ_target_dt.strftime(DATE_FMT)
                        if offset > 0:
                            succ_target_str = self._subtract_workdays(succ_target_str, offset + 1)
                        target_ls_dt = datetime.strptime(succ_target_str, DATE_FMT)
                        if min_succ_ls is None or target_ls_dt < min_succ_ls:
                            min_succ_ls = target_ls_dt
                    elif s_mode == "same_as" and succ.start_rule.get("field") == "end":
                        succ_target_dt = self.late_finish[succ.id]
                        succ_target_str = succ_target_dt.strftime(DATE_FMT)
                        if offset > 0:
                            succ_target_str = self._subtract_workdays(succ_target_str, offset + 1)
                        target_lf_dt = datetime.strptime(succ_target_str, DATE_FMT)
                        if min_succ_lf is None or target_lf_dt < min_succ_lf:
                            min_succ_lf = target_lf_dt
                    else:
                        # Finish-to-Start (continue_after or sequential sibling)
                        succ_ls_str = self.late_start[succ.id].strftime(DATE_FMT)
                        target_str = succ_ls_str
                        if offset > 0:
                            target_str = self._subtract_workdays(target_str, offset + 1)
                        prev_wd = self._get_prev_workday(target_str)
                        target_lf_dt = datetime.strptime(prev_wd, DATE_FMT)
                        if min_succ_lf is None or target_lf_dt < min_succ_lf:
                            min_succ_lf = target_lf_dt

                # From LF constraint, calculate corresponding LS
                ls_from_lf_str = self._subtract_workdays(min_succ_lf.strftime(DATE_FMT), max(1, duration))
                ls_from_lf_dt = datetime.strptime(ls_from_lf_str, DATE_FMT)

                if min_succ_ls is not None:
                    self.late_start[node.id] = min(ls_from_lf_dt, min_succ_ls)
                else:
                    self.late_start[node.id] = ls_from_lf_dt

                lf_str = WorkdayCalculator.add_workdays(
                    self.late_start[node.id].strftime(DATE_FMT), max(1, duration),
                    holidays=self.holidays, exclude_weekends=self.exclude_weekends
                )
                self.late_finish[node.id] = datetime.strptime(lf_str, DATE_FMT)
            else:
                self.late_finish[node.id] = project_end
                ls_str = self._subtract_workdays(project_end.strftime(DATE_FMT), max(1, duration))
                self.late_start[node.id] = datetime.strptime(ls_str, DATE_FMT)

    def calculate_slack(self):
        """
        Calculate slack (float) in workdays for LEAF tasks only.
        Slack = workdays between early_start and late_start (0 if same).
        """
        from utils.workday_calculator import WorkdayCalculator
        for node in self.leaf_nodes:
            if node.id in self.early_start and node.id in self.late_start:
                es = self.early_start[node.id]
                ls = self.late_start[node.id]
                es_str = es.strftime(DATE_FMT)
                ls_str = ls.strftime(DATE_FMT)
                if ls >= es:
                    count = WorkdayCalculator.calculate_duration(
                        es_str, ls_str,
                        holidays=self.holidays, exclude_weekends=self.exclude_weekends
                    )
                    self.slack[node.id] = max(0, count - 1)
                else:
                    count = WorkdayCalculator.calculate_duration(
                        ls_str, es_str,
                        holidays=self.holidays, exclude_weekends=self.exclude_weekends
                    )
                    self.slack[node.id] = -(count - 1)

    def identify_critical_path(self):
        """
        Identify LEAF tasks on the critical path.
        Critical tasks have zero slack.
        """
        self.critical_path_ids = {
            node_id for node_id, slack_val in self.slack.items()
            if slack_val == 0
        }

    def identify_critical_parents(self):
        """
        Identify parent tasks that have critical descendants.
        Marks all ancestors of critical leaf tasks.
        """
        self.critical_parent_ids.clear()
        for critical_id in self.critical_path_ids:
            if critical_id in self.node_map:
                node = self.node_map[critical_id]
                current = node.parent
                while current:
                    self.critical_parent_ids.add(current.id)
                    current = current.parent

    def analyze(self) -> Dict[str, any]:
        """
        Perform complete critical path analysis on LEAF tasks only.

        Returns:
            Dictionary containing:
            - critical_path_ids: Set of LEAF task IDs on critical path
            - critical_parent_ids: Set of parent task IDs with critical descendants
            - slack: Dict mapping task ID to slack days
            - early_start/early_finish: Dict mapping task ID to dates
            - late_start/late_finish: Dict mapping task ID to dates
            - project_duration: Total project duration in workdays
        """
        from utils.workday_calculator import WorkdayCalculator
        self.forward_pass()
        self.backward_pass()
        self.calculate_slack()
        self.identify_critical_path()
        self.identify_critical_parents()

        project_duration = 0
        if self.early_finish:
            project_start_dt = min(self.early_start.values()) if self.early_start else datetime.now()
            project_end_dt = max(self.early_finish.values())
            project_duration = WorkdayCalculator.calculate_duration(
                project_start_dt.strftime(DATE_FMT),
                project_end_dt.strftime(DATE_FMT),
                holidays=self.holidays, exclude_weekends=self.exclude_weekends
            )

        return {
            'critical_path_ids': self.critical_path_ids,
            'critical_parent_ids': self.critical_parent_ids,
            'slack': self.slack,
            'early_start': self.early_start,
            'early_finish': self.early_finish,
            'late_start': self.late_start,
            'late_finish': self.late_finish,
            'project_duration': project_duration,
            'project_start': min(self.early_start.values()) if self.early_start else None,
            'project_end': max(self.early_finish.values()) if self.early_finish else None
        }

    def is_critical(self, node: TaskNode) -> bool:
        """Check if a task is on the critical path."""
        return node.id in self.critical_path_ids

    def get_slack_days(self, node: TaskNode) -> int:
        """Get slack (float) in days for a task."""
        return self.slack.get(node.id, 0)

    def get_critical_path_tasks(self) -> List[TaskNode]:
        """Get list of tasks on the critical path."""
        return [node for node in self.all_nodes if node.id in self.critical_path_ids]