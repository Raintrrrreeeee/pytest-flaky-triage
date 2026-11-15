"""Backward-compatible facade for AST heuristics, call-graph inspection, and cause disambiguation."""

from report.ast_utils import (
    MAX_CALL_DEPTH, RANDOM_FUNCTIONS, RANDOM_MODULES, SETUP_METHODS, TEARDOWN_METHODS, TMP_FIXTURE_NAMES,
    _call_head_name, _collect_imports_and_globals, _collect_local_imports, _dedup_preserve_order,
    _extract_assign_targets_and_value, _find_enclosing_class, _find_method_in_class, _find_target_node,
    _find_top_def, _iter_class_hierarchy, _parse_file, _parse_file_cached, _rel_path,
    _resolve_call_parts, _resolve_module_file, _resolve_root_name, _split_test_id,
)
from report.disambiguator import (
    ALL_CAUSE_CATEGORIES, _AST_CATEGORY_REASONS, _is_weak_sleep_only,
    _is_weak_uuid_only, _with_polluter_suffix, disambiguate_causes,
)
from report.inspector import (
    _bind_call_arg_sets, _extract_caller_context, _find_fixture_defs,
    _infer_returned_class, _is_test_side_file, _tag_via_helper, inspect_test_source,
)
from report.signals import (
    _collect_class_self_attrs, _collect_local_var_context, _collect_teardown_clear_calls,
    _fixture_returns_safe_temp, _has_fixed_seed_call, _has_network_mock_in_nodes,
    _has_time_freeze_or_tz_override, _has_tmp_fixture_in_nodes, _is_autouse_fixture,
    _is_fixture_func, _is_hardcoded_path_expr, _is_safe_temp_expr, _is_worker_spawn_call,
    _scan_class_shared_state, _scan_function_signals, _scan_shared_globals_in_func,
    _tree_uses_network_mock,
)

__all__ = [
    "ALL_CAUSE_CATEGORIES", "MAX_CALL_DEPTH", "RANDOM_FUNCTIONS", "RANDOM_MODULES",
    "SETUP_METHODS", "TEARDOWN_METHODS", "TMP_FIXTURE_NAMES", "_AST_CATEGORY_REASONS",
    "_bind_call_arg_sets", "_call_head_name", "_collect_class_self_attrs",
    "_collect_imports_and_globals", "_collect_local_imports", "_collect_local_var_context",
    "_collect_teardown_clear_calls", "_dedup_preserve_order", "_extract_assign_targets_and_value",
    "_extract_caller_context", "_find_enclosing_class", "_find_fixture_defs",
    "_find_method_in_class", "_find_target_node", "_find_top_def", "_fixture_returns_safe_temp",
    "_has_fixed_seed_call", "_has_network_mock_in_nodes", "_has_time_freeze_or_tz_override",
    "_has_tmp_fixture_in_nodes", "_infer_returned_class", "_is_autouse_fixture",
    "_is_fixture_func", "_is_hardcoded_path_expr", "_is_safe_temp_expr", "_is_test_side_file",
    "_is_weak_sleep_only", "_is_weak_uuid_only", "_is_worker_spawn_call", "_iter_class_hierarchy",
    "_parse_file", "_parse_file_cached", "_rel_path", "_resolve_call_parts",
    "_resolve_module_file", "_resolve_root_name", "_scan_class_shared_state",
    "_scan_function_signals", "_scan_shared_globals_in_func", "_split_test_id",
    "_tag_via_helper", "_tree_uses_network_mock", "_with_polluter_suffix",
    "disambiguate_causes", "inspect_test_source",
]
