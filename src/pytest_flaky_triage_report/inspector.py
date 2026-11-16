"""SECTION 3: Call-graph and test source AST inspector (`inspect_test_source`)."""

import ast
import re
from collections import deque
from pathlib import Path

from pytest_flaky_triage_report.ast_utils import (
    MAX_CALL_DEPTH,
    SETUP_METHODS,
    _call_head_name,
    _collect_imports_and_globals,
    _collect_local_imports,
    _dedup_preserve_order,
    _extract_assign_targets_and_value,
    _find_enclosing_class,
    _find_method_in_class,
    _find_target_node,
    _find_top_def,
    _iter_class_hierarchy,
    _parse_file,
    _rel_path,
    _resolve_call_parts,
    _split_test_id,
)
from pytest_flaky_triage_report.signals import (
    _collect_class_self_attrs,
    _collect_local_var_context,
    _expand_expr_nodes,
    _expr_has_float_safe_guard,
    _expr_involves_float_calc,
    _fixture_returns_safe_temp,
    _has_network_mock_in_nodes,
    _has_tmp_fixture_in_nodes,
    _is_autouse_fixture,
    _is_fixture_func,
    _is_hardcoded_path_expr,
    _is_safe_temp_expr,
    _scan_class_shared_state,
    _scan_function_signals,
    _scan_shared_globals_in_func,
    _tree_uses_network_mock,
)


def _infer_returned_class(func_node, imported=None):
    """If a function/fixture returns or yields a Call like `Cart('user')` or a variable assigned one, return its head name."""
    local_assigns = {}
    for node in ast.walk(func_node):
        targets, val = _extract_assign_targets_and_value(node)
        if targets and isinstance(val, ast.Call):
            head = _call_head_name(val, imported=imported)
            if head:
                for t in targets:
                    if isinstance(t, ast.Name):
                        local_assigns[t.id] = head
    for node in ast.walk(func_node):
        if isinstance(node, (ast.Return, ast.Yield)) and node.value is not None:
            if isinstance(node.value, ast.Call):
                head = _call_head_name(node.value, imported=imported)
                if head:
                    return head
            elif isinstance(node.value, ast.Name) and node.value.id in local_assigns:
                return local_assigns[node.value.id]
    return None


def _find_fixture_defs(param_names, test_tree, test_file, project_path, enclosing_cls=None):
    """Find fixture definitions matching `param_names` or `autouse=True` (plus upstream fixture deps up to 3 hops)
    in `test_tree`, `enclosing_cls`, or ancestor `conftest.py` files, supporting same-name parent conftest delegation."""
    wanted = set(param_names or ())
    found = []
    candidate_files = [(test_file, test_tree)]
    cur_dir = test_file.resolve().parent
    proj_resolved = project_path.resolve()
    while True:
        conftest = cur_dir / "conftest.py"
        if conftest.is_file() and conftest != test_file.resolve():
            ct_tree = _parse_file(conftest)
            if ct_tree is not None:
                candidate_files.append((conftest, ct_tree))
        if cur_dir == proj_resolved or cur_dir.parent == cur_dir:
            break
        cur_dir = cur_dir.parent

    all_fixture_nodes = []
    if enclosing_cls is not None:
        for cls_in_mro in _iter_class_hierarchy(enclosing_cls, tree=test_tree):
            for node in getattr(cls_in_mro, "body", []):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_fixture_func(node):
                    all_fixture_nodes.append((node.name, test_file, test_tree, node))
    for f_path, f_tree in candidate_files:
        for node in getattr(f_tree, "body", []):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_fixture_func(node):
                all_fixture_nodes.append((node.name, f_path, f_tree, node))

    def _resolve_from(req_name, min_idx=0):
        for idx in range(min_idx, len(all_fixture_nodes)):
            fix_name, f_path, f_tree, node = all_fixture_nodes[idx]
            if fix_name == req_name:
                return idx, (fix_name, f_path, f_tree, node)
        return None

    def _fixture_arg_requests(fix_name, idx, node):
        reqs = []
        for arg in (
            list(getattr(node.args, "posonlyargs", []))
            + list(getattr(node.args, "args", []))
            + list(getattr(node.args, "kwonlyargs", []))
        ):
            if arg.arg != "self":
                next_min_idx = (idx + 1) if arg.arg == fix_name else 0
                reqs.append((arg.arg, next_min_idx))
        return reqs

    seen_defs = set()
    frontier = []

    initial_matches = []
    for idx, entry in enumerate(all_fixture_nodes):
        fix_name, f_path, f_tree, node = entry
        if _is_autouse_fixture(node):
            initial_matches.append((idx, entry))
    for req_name in wanted:
        res = _resolve_from(req_name, 0)
        if res is not None:
            initial_matches.append(res)

    for idx, (fix_name, f_path, f_tree, node) in initial_matches:
        def_key = (f_path.resolve(), id(node))
        if def_key not in seen_defs:
            seen_defs.add(def_key)
            found.append((fix_name, f_path, f_tree, node))
            frontier.extend(_fixture_arg_requests(fix_name, idx, node))

    for _ in range(MAX_CALL_DEPTH):
        if not frontier:
            break
        next_frontier = []
        for req_name, min_idx in frontier:
            res = _resolve_from(req_name, min_idx)
            if res is None:
                continue
            idx, (fix_name, f_path, f_tree, node) = res
            def_key = (f_path.resolve(), id(node))
            if def_key not in seen_defs:
                seen_defs.add(def_key)
                found.append((fix_name, f_path, f_tree, node))
                next_frontier.extend(_fixture_arg_requests(fix_name, idx, node))
        frontier = next_frontier

    return found


def _bind_call_arg_sets(
    call_node,
    callee_node,
    caller_safe_tmp,
    caller_hardcoded,
    caller_float_vars,
    caller_local_exprs,
    caller_imported,
    is_method=False,
):
    """Map call-site argument classifications (safe_tmp, hardcoded_path, float) to callee parameter names."""
    args_obj = getattr(callee_node, "args", None)
    if args_obj is None:
        return set(), set(), set()
    pos_params = [
        a.arg
        for a in list(getattr(args_obj, "posonlyargs", [])) + list(getattr(args_obj, "args", []))
    ]
    if is_method and pos_params and pos_params[0] in ("self", "cls"):
        pos_params = pos_params[1:]
    kw_params = {a.arg for a in getattr(args_obj, "kwonlyargs", [])}
    all_params = set(pos_params) | kw_params

    bound = {}
    for idx, arg_expr in enumerate(getattr(call_node, "args", [])):
        if idx < len(pos_params):
            bound[pos_params[idx]] = arg_expr
    for kw in getattr(call_node, "keywords", []):
        if kw.arg and kw.arg in all_params:
            bound[kw.arg] = kw.value

    callee_safe = set()
    callee_hard = set()
    callee_float = set()
    for p_name, expr in bound.items():
        if _is_safe_temp_expr(
            expr, caller_safe_tmp, local_exprs=caller_local_exprs, imported=caller_imported
        ):
            callee_safe.add(p_name)
        elif _is_hardcoded_path_expr(
            expr,
            caller_safe_tmp,
            caller_hardcoded,
            local_exprs=caller_local_exprs,
            imported=caller_imported,
        ):
            callee_hard.add(p_name)
        if _expr_involves_float_calc(
            expr, caller_local_exprs, float_vars=caller_float_vars, imported=caller_imported
        ) or (isinstance(expr, ast.Name) and expr.id in caller_float_vars):
            callee_float.add(p_name)
    return callee_safe, callee_hard, callee_float


def _extract_caller_context(
    func_node,
    imported,
    initial_safe_tmp=None,
    initial_hardcoded=None,
    initial_float=None,
    extra_decorators=None,
):
    """Extract a function's effective safe_tmp_vars, hardcoded_path_vars, float_vars, and local_exprs."""
    eff_imported = _collect_local_imports(func_node, imported)
    safe_tmp_vars, hardcoded_vars, float_vars, local_exprs, _ = _collect_local_var_context(
        func_node,
        eff_imported,
        initial_safe_tmp=initial_safe_tmp,
        initial_hardcoded=initial_hardcoded,
        initial_float=initial_float,
        extra_decorators=extra_decorators,
    )
    return safe_tmp_vars, hardcoded_vars, float_vars, local_exprs


def _is_test_side_file(file_path, test_file):
    try:
        resolved = Path(file_path).resolve()
        if resolved == Path(test_file).resolve() or resolved.name == "conftest.py":
            return True
    except OSError:
        pass
    return False


def _tag_via_helper(sig_list, via_helper):
    if not via_helper:
        return sig_list
    suffix = f" (via `{via_helper}(...)`)"
    tagged = []
    for item in sig_list:
        new_item = dict(item)
        if suffix not in new_item.get("detail", ""):
            new_item["detail"] = f"{new_item['detail']}{suffix}"
        tagged.append(new_item)
    return tagged


def _enrich_hash_order_from_set_callees(indicators, set_returning_callees):
    """Post-pass 1: enrich hash_order_sensitivity when a callee returns an unordered set."""
    if set_returning_callees and indicators["hash_order_sensitivity"]:
        code_rel, func_node, desc = set_returning_callees[0]
        first_hit = indicators["hash_order_sensitivity"][0]
        via_suffix = ""
        m_via = re.search(r" \(via `[^`]+`\)$", first_hit["detail"])
        if m_via:
            via_suffix = m_via.group(0)
        first_hit["detail"] = (
            f"unsorted `join(...)` on unordered `set` from "
            f"`{code_rel}:{func_node.lineno}` (`{desc}()`){via_suffix}"
        )


def _link_float_returning_callees(indicators, float_returning_callees, scanned_test_side_funcs):
    """Post-pass 2: link float-returning callees to strict equality assertions on Test Side."""
    if not float_returning_callees or indicators["float_precision"]:
        return
    for code_rel, func_node, desc in float_returning_callees:
        leaf_sym = desc.split(".")[-1]
        for _, ts_rel, ts_func, ts_via, ts_imported in scanned_test_side_funcs:
            ts_local_exprs = {}
            for n in ast.walk(ts_func):
                targets, val = _extract_assign_targets_and_value(n)
                if targets and val is not None:
                    for t in targets:
                        if isinstance(t, ast.Name):
                            ts_local_exprs[t.id] = val
            float_assert_lines = []
            for n in ast.walk(ts_func):
                if isinstance(n, ast.Assert) and not _expr_has_float_safe_guard(
                    n.test, ts_local_exprs, imported=ts_imported
                ):
                    has_eq = any(
                        isinstance(sub, ast.Compare)
                        and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in sub.ops)
                        for sub in ast.walk(n.test)
                    )
                    if has_eq:
                        for sub in _expand_expr_nodes(n.test, ts_local_exprs):
                            if isinstance(sub, ast.Call):
                                _, attr, bare_fn = _resolve_call_parts(sub.func, ts_imported)
                                if (
                                    bare_fn == leaf_sym
                                    or attr == leaf_sym
                                    or (isinstance(sub.func, ast.Name) and sub.func.id == leaf_sym)
                                    or (isinstance(sub.func, ast.Attribute) and sub.func.attr == leaf_sym)
                                ):
                                    float_assert_lines.append(n.lineno)
                                    break
            if float_assert_lines:
                float_assert_lines.sort()
                lines_str = ", ".join(str(ln) for ln in _dedup_preserve_order(float_assert_lines))
                hit = {
                    "side": "Test Side",
                    "location": f"{ts_rel}:{lines_str}",
                    "detail": (
                        f"asserts strict equality (`==`/`!=`) on floating-point return from "
                        f"`{code_rel}:{func_node.lineno}` (`{desc}()`) without `pytest.approx` or `math.isclose`"
                    ),
                }
                indicators["float_precision"].extend(_tag_via_helper([hit], ts_via))
                break
        if indicators["float_precision"]:
            break


def inspect_test_source(test_id, project_dir=None, suspected_polluters=None):
    """Inspect test and called production ASTs to collect heuristic indicators.

    Returns a dictionary with:
      - available (bool): whether the test source function was located and parsed
      - indicators (dict[str, list[dict]]): per-cause list of {side, location, detail}
    """
    empty = {
        "available": False,
        "indicators": {
            "unseeded_randomness": [],
            "race_conditions": [],
            "hash_order_sensitivity": [],
            "shared_state_interference": [],
            "timezone_sensitivity": [],
            "resource_leak": [],
            "external_io": [],
            "float_precision": [],
        },
    }
    if not project_dir:
        return empty
    project_path = Path(project_dir)
    if not project_path.is_dir():
        return empty

    file_part, node_parts = _split_test_id(test_id)
    if not file_part or not node_parts:
        return empty

    test_file = project_path / file_part
    if not test_file.is_file():
        return empty

    test_tree = _parse_file(test_file)
    if test_tree is None:
        return empty

    test_func = _find_target_node(test_tree, node_parts)
    if test_func is None:
        return empty

    imported, module_globals = _collect_imports_and_globals(
        test_tree, project_path, current_file=test_file
    )
    imported = _collect_local_imports(
        test_func, imported, project_dir=project_path, current_file=test_file
    )

    indicators = {
        "unseeded_randomness": [],
        "race_conditions": [],
        "hash_order_sensitivity": [],
        "shared_state_interference": [],
        "timezone_sensitivity": [],
        "resource_leak": [],
        "external_io": [],
        "float_precision": [],
    }

    enclosing_cls = _find_enclosing_class(test_tree, node_parts)
    cls_decorators = getattr(enclosing_cls, "decorator_list", []) if enclosing_cls is not None else []
    param_names = [
        arg.arg
        for arg in (
            list(getattr(test_func.args, "posonlyargs", []))
            + list(getattr(test_func.args, "args", []))
            + list(getattr(test_func.args, "kwonlyargs", []))
        )
        if arg.arg != "self"
    ]
    fixture_defs = _find_fixture_defs(
        param_names, test_tree, test_file, project_path, enclosing_cls=enclosing_cls
    )
    all_test_func_nodes = [test_func] + [fix_node for _, _, _, fix_node in fixture_defs]
    has_tmp_fixture = _has_tmp_fixture_in_nodes(all_test_func_nodes)
    has_net_mock = _has_network_mock_in_nodes(
        all_test_func_nodes, enclosing_cls=enclosing_cls, imported=imported
    )

    base_local_vars = {}
    for g_name, (_, g_call) in module_globals.items():
        if g_call:
            base_local_vars[g_name] = g_call

    base_self_attrs = (
        _collect_class_self_attrs(enclosing_cls, tree=test_tree)
        if enclosing_cls is not None
        else {}
    )
    setup_nodes = []
    if enclosing_cls is not None:
        seen_setup_names = set()
        for cls_in_mro in _iter_class_hierarchy(enclosing_cls, tree=test_tree):
            for item in getattr(cls_in_mro, "body", []):
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name in SETUP_METHODS
                    and item.name not in seen_setup_names
                ):
                    seen_setup_names.add(item.name)
                    setup_nodes.append(item)

    fixture_safe_tmp_names = set()
    for fix_name, fix_file, fix_tree, fix_node in fixture_defs:
        fix_imported, _ = _collect_imports_and_globals(
            fix_tree, project_path, current_file=fix_file
        )
        fix_imported = _collect_local_imports(
            fix_node, fix_imported, project_dir=project_path, current_file=fix_file
        )
        for k, v in fix_imported.items():
            imported.setdefault(k, v)
        if _tree_uses_network_mock(fix_node, imported=fix_imported):
            has_net_mock = True
        if _fixture_returns_safe_temp(fix_node, imported=fix_imported):
            fixture_safe_tmp_names.add(fix_name)
        ret_cls = _infer_returned_class(fix_node)
        if ret_cls:
            base_local_vars[fix_name] = ret_cls

    for node in getattr(test_tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ret_cls = _infer_returned_class(node)
            if ret_cls:
                base_local_vars[f"__factory__:{node.name}"] = ret_cls

    enclosing_hierarchy_names = (
        {"self", "cls"} | {c.name for c in _iter_class_hierarchy(enclosing_cls, tree=test_tree)}
        if enclosing_cls is not None
        else {"self", "cls"}
    )
    reset_class_attrs = set()
    reset_modules = set()
    for root_node in all_test_func_nodes + setup_nodes:
        is_setup_or_teardown = getattr(root_node, "name", "") in SETUP_METHODS
        for node in ast.walk(root_node):
            targets, val = _extract_assign_targets_and_value(node)
            if targets and val is not None:
                for t in targets:
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                        if is_setup_or_teardown or t.value.id not in enclosing_hierarchy_names:
                            reset_class_attrs.add((t.value.id, t.attr))
                    elif (
                        isinstance(t, ast.Subscript)
                        and isinstance(t.value, ast.Attribute)
                        and isinstance(t.value.value, ast.Name)
                    ):
                        if is_setup_or_teardown or t.value.value.id not in enclosing_hierarchy_names:
                            reset_class_attrs.add((t.value.value.id, t.value.attr))
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in imported:
                    fn_name = node.func.id
                    entry = imported[fn_name]
                    if isinstance(entry, tuple):
                        mod_file, remote_name, _ = entry
                        if mod_file is not None and (
                            fn_name.startswith(("reset_", "clear_", "init_"))
                            or (remote_name and remote_name.startswith(("reset_", "clear_", "init_")))
                        ):
                            reset_modules.add(str(mod_file))

    bfs_queue = deque()
    visited_targets = set()
    scanned_test_side_funcs = []

    def enqueue_target(
        t_file,
        t_tree,
        t_func,
        t_side,
        t_desc,
        t_cls=None,
        t_depth=0,
        t_via=None,
        t_safe_tmp=None,
        t_hardcoded=None,
        t_float=None,
    ):
        if t_depth > MAX_CALL_DEPTH or t_func is None or t_tree is None:
            return
        try:
            file_key = str(Path(t_file).resolve())
        except OSError:
            file_key = str(t_file)
        visit_key = (file_key, t_desc, getattr(t_func, "lineno", 0), t_side)
        if visit_key in visited_targets:
            return
        visited_targets.add(visit_key)
        bfs_queue.append((
            Path(t_file),
            t_tree,
            t_func,
            t_side,
            t_desc,
            t_cls,
            t_depth,
            t_via,
            set(t_safe_tmp or ()),
            set(t_hardcoded or ()),
            set(t_float or ()),
        ))

    enqueue_target(
        test_file,
        test_tree,
        test_func,
        "Test Side",
        test_func.name,
        t_cls=enclosing_cls,
        t_depth=0,
        t_via=None,
        t_safe_tmp=fixture_safe_tmp_names,
    )
    for s_node in setup_nodes:
        enqueue_target(
            test_file,
            test_tree,
            s_node,
            "Test Side",
            f"{enclosing_cls.name}.{s_node.name}" if enclosing_cls else s_node.name,
            t_cls=enclosing_cls,
            t_depth=0,
            t_via=None,
            t_safe_tmp=fixture_safe_tmp_names,
        )
    for fix_name, fix_file, fix_tree, fix_node in fixture_defs:
        enqueue_target(
            fix_file,
            fix_tree,
            fix_node,
            "Test Side",
            fix_name,
            t_cls=enclosing_cls if fix_file == test_file else None,
            t_depth=0,
            t_via=None,
            t_safe_tmp=fixture_safe_tmp_names,
        )

    float_returning_callees = []
    set_returning_callees = []

    while bfs_queue:
        (
            cur_file,
            cur_tree,
            cur_func,
            cur_side,
            cur_desc,
            cur_cls,
            cur_depth,
            cur_via,
            cur_safe_tmp,
            cur_hardcoded,
            cur_float,
        ) = bfs_queue.popleft()

        cur_rel = _rel_path(cur_file, project_path)
        cur_imported, cur_globals = _collect_imports_and_globals(
            cur_tree, project_path, current_file=cur_file
        )
        if cur_file.resolve() == test_file.resolve():
            for k, v in imported.items():
                cur_imported.setdefault(k, v)
        cur_imported = _collect_local_imports(
            cur_func, cur_imported, project_dir=project_path, current_file=cur_file
        )

        if cur_side == "Test Side" and _tree_uses_network_mock(cur_func, imported=cur_imported):
            has_net_mock = True

        extra_decs = cls_decorators if (cur_func is test_func) else None
        cur_signals = _scan_function_signals(
            cur_func,
            cur_rel,
            cur_side,
            has_tmp_fixture=has_tmp_fixture,
            has_net_mock=has_net_mock,
            imported=cur_imported,
            initial_safe_tmp_vars=cur_safe_tmp,
            initial_hardcoded_vars=cur_hardcoded,
            initial_float_vars=cur_float,
            extra_decorators=extra_decs,
        )

        if cur_side == "Test Side":
            scanned_test_side_funcs.append((cur_file, cur_rel, cur_func, cur_via, cur_imported))
            for key in indicators:
                indicators[key].extend(_tag_via_helper(cur_signals[key], cur_via))
            shared_hits = _scan_shared_globals_in_func(cur_func, cur_rel, cur_globals, "Test Side")
            indicators["shared_state_interference"].extend(_tag_via_helper(shared_hits, cur_via))
            if cur_cls is not None and getattr(cur_func, "name", "") not in SETUP_METHODS:
                cls_shared_hits = _scan_class_shared_state(
                    cur_func, cur_cls, cur_rel, "Test Side", reset_class_attrs, tree=cur_tree
                )
                indicators["shared_state_interference"].extend(_tag_via_helper(cls_shared_hits, cur_via))
            if cur_depth > 0 and cur_signals["returns_float"]:
                float_returning_callees.append((cur_rel, cur_func, cur_desc))
            if cur_depth > 0 and cur_signals["returns_set"]:
                set_returning_callees.append((cur_rel, cur_func, cur_desc))
        else:
            for key in (
                "unseeded_randomness",
                "race_conditions",
                "shared_state_interference",
                "timezone_sensitivity",
                "resource_leak",
                "external_io",
            ):
                indicators[key].extend(cur_signals[key])

            if (
                not cur_signals["race_conditions"]
                and str(cur_file) not in reset_modules
                and not cur_func.name.startswith(("reset_", "clear_", "init_"))
            ):
                indicators["shared_state_interference"].extend(
                    _scan_shared_globals_in_func(cur_func, cur_rel, cur_globals, "Code Side")
                )
                if cur_cls is not None:
                    indicators["shared_state_interference"].extend(
                        _scan_class_shared_state(
                            cur_func, cur_cls, cur_rel, "Code Side", reset_class_attrs, tree=cur_tree
                        )
                    )

            if cur_signals["returns_set"]:
                set_returning_callees.append((cur_rel, cur_func, cur_desc))
            if cur_signals["returns_float"]:
                float_returning_callees.append((cur_rel, cur_func, cur_desc))

        if cur_depth >= MAX_CALL_DEPTH:
            continue

        eff_safe_tmp, eff_hardcoded, eff_float, eff_local_exprs = _extract_caller_context(
            cur_func,
            cur_imported,
            initial_safe_tmp=cur_safe_tmp,
            initial_hardcoded=cur_hardcoded,
            initial_float=cur_float,
            extra_decorators=extra_decs,
        )

        local_vars = dict(base_local_vars) if cur_side == "Test Side" else {}
        self_attrs = dict(base_self_attrs) if cur_side == "Test Side" else {}
        for g_name, (_, g_call) in cur_globals.items():
            if g_call:
                local_vars.setdefault(g_name, g_call)
        if cur_cls is not None:
            for attr_k, attr_v in _collect_class_self_attrs(cur_cls, tree=cur_tree).items():
                self_attrs.setdefault(attr_k, attr_v)

        for node in ast.walk(cur_func):
            targets, val = _extract_assign_targets_and_value(node)
            if targets and isinstance(val, ast.Call):
                head = _call_head_name(val)
                if head:
                    resolved_cls = local_vars.get(f"__factory__:{head}", head)
                    for target in targets:
                        if isinstance(target, ast.Name):
                            local_vars[target.id] = resolved_cls
                        elif (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                        ):
                            self_attrs[target.attr] = resolved_cls

        pending_callees = {}

        def stage_callee(
            call_node,
            c_file,
            c_tree,
            c_func,
            c_side,
            c_desc,
            c_cls=None,
            c_via=None,
            is_method=False,
        ):
            if c_func is None or c_tree is None:
                return
            try:
                f_key = str(Path(c_file).resolve())
            except OSError:
                f_key = str(c_file)
            key = (f_key, c_desc, getattr(c_func, "lineno", 0), c_side)
            b_safe, b_hard, b_float = _bind_call_arg_sets(
                call_node,
                c_func,
                eff_safe_tmp,
                eff_hardcoded,
                eff_float,
                eff_local_exprs,
                cur_imported,
                is_method=is_method,
            )
            if key not in pending_callees:
                pending_callees[key] = [
                    c_file,
                    c_tree,
                    c_func,
                    c_side,
                    c_desc,
                    c_cls,
                    c_via,
                    set(b_safe),
                    set(b_hard),
                    set(b_float),
                ]
            else:
                pending_callees[key][7].update(b_safe)
                pending_callees[key][8].update(b_hard)
                pending_callees[key][9].update(b_float)

        call_candidates = [sub for sub in ast.walk(cur_func) if isinstance(sub, ast.Call)]
        for dec in list(getattr(cur_func, "decorator_list", [])) + list(extra_decs or []):
            if isinstance(dec, (ast.Name, ast.Attribute)):
                call_candidates.append(
                    ast.Call(
                        func=dec,
                        args=[],
                        keywords=[],
                        lineno=getattr(dec, "lineno", getattr(cur_func, "lineno", 1)),
                    )
                )

        for sub in call_candidates:
            if isinstance(sub.func, ast.Name):
                callee_name = sub.func.id
                local_def = _find_top_def(cur_tree, callee_name)
                if isinstance(local_def, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    next_via = (cur_via or callee_name) if cur_side == "Test Side" else None
                    stage_callee(
                        sub,
                        cur_file,
                        cur_tree,
                        local_def,
                        cur_side,
                        callee_name,
                        c_cls=None,
                        c_via=next_via,
                        is_method=False,
                    )
                elif isinstance(local_def, ast.ClassDef):
                    init_m = _find_method_in_class(local_def, "__init__", tree=cur_tree)
                    if init_m is not None:
                        next_via = (cur_via or f"{callee_name}.__init__") if cur_side == "Test Side" else None
                        stage_callee(
                            sub,
                            cur_file,
                            cur_tree,
                            init_m,
                            cur_side,
                            f"{callee_name}.__init__",
                            c_cls=local_def,
                            c_via=next_via,
                            is_method=True,
                        )
                elif callee_name in cur_imported:
                    entry = cur_imported[callee_name]
                    if isinstance(entry, tuple):
                        mod_file, remote_name, _ = entry
                        if mod_file is not None:
                            mod_tree = _parse_file(mod_file)
                            if mod_tree is not None:
                                defn = _find_top_def(mod_tree, remote_name)
                                next_side = (
                                    "Test Side" if _is_test_side_file(mod_file, test_file) else "Code Side"
                                )
                                next_via = (cur_via or remote_name) if next_side == "Test Side" else None
                                if isinstance(defn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                    stage_callee(
                                        sub,
                                        mod_file,
                                        mod_tree,
                                        defn,
                                        next_side,
                                        remote_name,
                                        c_cls=None,
                                        c_via=next_via,
                                        is_method=False,
                                    )
                                elif isinstance(defn, ast.ClassDef):
                                    init_m = _find_method_in_class(defn, "__init__", tree=mod_tree)
                                    if init_m is not None:
                                        stage_callee(
                                            sub,
                                            mod_file,
                                            mod_tree,
                                            init_m,
                                            next_side,
                                            f"{remote_name}.__init__",
                                            c_cls=defn,
                                            c_via=next_via,
                                            is_method=True,
                                        )

            elif isinstance(sub.func, ast.Attribute):
                method_name = sub.func.attr
                if (
                    cur_cls is not None
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id in ("self", "cls")
                ):
                    helper_m = _find_method_in_class(cur_cls, method_name, tree=cur_tree)
                    if helper_m is not None:
                        m_desc = f"{cur_cls.name}.{method_name}"
                        next_via = (cur_via or m_desc) if cur_side == "Test Side" else None
                        stage_callee(
                            sub,
                            cur_file,
                            cur_tree,
                            helper_m,
                            cur_side,
                            m_desc,
                            c_cls=cur_cls,
                            c_via=next_via,
                            is_method=True,
                        )
                        continue

                class_symbol = None
                if isinstance(sub.func.value, ast.Name):
                    receiver = sub.func.value.id
                    class_symbol = local_vars.get(receiver, receiver)
                elif (
                    isinstance(sub.func.value, ast.Attribute)
                    and isinstance(sub.func.value.value, ast.Name)
                    and sub.func.value.value.id == "self"
                ):
                    class_symbol = self_attrs.get(sub.func.value.attr)
                elif isinstance(sub.func.value, ast.Call):
                    class_symbol = _call_head_name(sub.func.value)

                if not class_symbol:
                    continue

                local_cls = _find_top_def(cur_tree, class_symbol)
                if isinstance(local_cls, ast.ClassDef):
                    m_node = _find_method_in_class(local_cls, method_name, tree=cur_tree)
                    if m_node is not None:
                        m_desc = f"{local_cls.name}.{method_name}"
                        next_via = (cur_via or m_desc) if cur_side == "Test Side" else None
                        stage_callee(
                            sub,
                            cur_file,
                            cur_tree,
                            m_node,
                            cur_side,
                            m_desc,
                            c_cls=local_cls,
                            c_via=next_via,
                            is_method=True,
                        )
                        continue

                mod_file = None
                remote_symbol = None
                if class_symbol in cur_imported and isinstance(cur_imported[class_symbol], tuple):
                    mod_file, remote_symbol, _ = cur_imported[class_symbol]
                elif "." in class_symbol:
                    mod_alias, cls_part = class_symbol.split(".", 1)
                    if mod_alias in cur_imported and isinstance(cur_imported[mod_alias], tuple):
                        mod_file = cur_imported[mod_alias][0]
                        remote_symbol = cls_part

                if mod_file is not None:
                    mod_tree = _parse_file(mod_file)
                    if mod_tree is not None:
                        next_side = (
                            "Test Side" if _is_test_side_file(mod_file, test_file) else "Code Side"
                        )
                        defn = _find_top_def(mod_tree, remote_symbol)
                        if isinstance(defn, ast.ClassDef):
                            m_node = _find_method_in_class(defn, method_name, tree=mod_tree)
                            if m_node is not None:
                                m_desc = f"{remote_symbol}.{method_name}"
                                next_via = (cur_via or m_desc) if next_side == "Test Side" else None
                                stage_callee(
                                    sub,
                                    mod_file,
                                    mod_tree,
                                    m_node,
                                    next_side,
                                    m_desc,
                                    c_cls=defn,
                                    c_via=next_via,
                                    is_method=True,
                                )
                        else:
                            top_def = _find_top_def(mod_tree, method_name)
                            if isinstance(top_def, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                m_desc = f"{remote_symbol}.{method_name}"
                                next_via = (cur_via or method_name) if next_side == "Test Side" else None
                                stage_callee(
                                    sub,
                                    mod_file,
                                    mod_tree,
                                    top_def,
                                    next_side,
                                    m_desc,
                                    c_cls=None,
                                    c_via=next_via,
                                    is_method=False,
                                )
                            elif isinstance(top_def, ast.ClassDef):
                                init_m = _find_method_in_class(top_def, "__init__", tree=mod_tree)
                                if init_m is not None:
                                    m_desc = f"{method_name}.__init__"
                                    next_via = (cur_via or m_desc) if next_side == "Test Side" else None
                                    stage_callee(
                                        sub,
                                        mod_file,
                                        mod_tree,
                                        init_m,
                                        next_side,
                                        m_desc,
                                        c_cls=top_def,
                                        c_via=next_via,
                                        is_method=True,
                                    )

        for (
            c_file,
            c_tree,
            c_func,
            c_side,
            c_desc,
            c_cls,
            c_via,
            c_safe,
            c_hard,
            c_float,
        ) in pending_callees.values():
            enqueue_target(
                c_file,
                c_tree,
                c_func,
                c_side,
                c_desc,
                t_cls=c_cls,
                t_depth=cur_depth + 1,
                t_via=c_via,
                t_safe_tmp=c_safe,
                t_hardcoded=c_hard,
                t_float=c_float,
            )

    _enrich_hash_order_from_set_callees(indicators, set_returning_callees)
    _link_float_returning_callees(indicators, float_returning_callees, scanned_test_side_funcs)

    if suspected_polluters:
        existing_shared = indicators["shared_state_interference"]
        existing_sides = {h["side"] for h in existing_shared}
        seen_shared_keys = {
            (h["side"], h["location"], h["detail"]) for h in existing_shared
        }
        for polluter_id in suspected_polluters:
            if not isinstance(polluter_id, str) or polluter_id == test_id:
                continue
            p_file_part, p_node_parts = _split_test_id(polluter_id)
            if p_file_part != file_part or not p_node_parts:
                continue
            p_info = inspect_test_source(
                polluter_id, project_dir=project_dir, suspected_polluters=None
            )
            p_shared = (p_info.get("indicators") or {}).get("shared_state_interference", [])
            for hit in p_shared:
                if existing_sides and hit["side"] not in existing_sides:
                    continue
                h_key = (hit["side"], hit["location"], hit["detail"])
                if h_key not in seen_shared_keys:
                    seen_shared_keys.add(h_key)
                    existing_shared.append(dict(hit))

    return {
        "available": True,
        "indicators": indicators,
    }
