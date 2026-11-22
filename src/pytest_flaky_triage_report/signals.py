"""SECTION 2: Function-level flaky signal scanners and shared-state detectors."""

import ast
import re

from pytest_flaky_triage_report.ast_utils import (
    AIOHTTP_TARGETS,
    ASYNC_CONCURRENCY_FUNCTIONS,
    ASYNC_CONCURRENCY_MODULES,
    ATTR_INIT_METHODS,
    CACHE_DECORATORS,
    EAGER_STDLIB_SUBMODULES,
    FLOAT_SAFE_FUNCS,
    KNOWN_LAZY_SUBMODULES,
    MATH_FLOAT_FUNCS,
    NET_MOCK_NAMES,
    NET_MOCK_TARGET_TOKENS,
    NON_FLOAT_ATTRS,
    NON_FLOAT_CALLS,
    ORDER_SENSITIVE_STR_METHODS,
    OS_FS_MUTATION_FUNCS,
    PATH_JOIN_RECEIVERS,
    PATH_MUTATION_METHODS,
    POOL_EXECUTOR_CLASSES,
    PORT_BIND_FUNCS,
    PORT_BIND_SERVER_CLASSES,
    RANDOM_FUNCTIONS,
    RANDOM_MODULES,
    REQUESTS_HTTPX_METHODS,
    SETUP_METHODS,
    SHUTIL_MUTATION_FUNCS,
    SMTPLIB_TARGETS,
    SOCKET_NET_FUNCS,
    TEARDOWN_METHODS,
    TEMPFILE_SAFE_CALLS,
    TEMPFILE_SAFE_MODULES,
    THREAD_PROCESS_CLASSES,
    THREAD_PROCESS_MODULES,
    TMP_FIXTURE_NAMES,
    URLLIB_REQUEST_FUNCS,
    UUID_FUNCTIONS,
    _call_head_name,
    _collect_local_imports,
    _collect_submodule_imports,
    _dedup_preserve_order,
    _extract_assign_targets_and_value,
    _iter_class_hierarchy,
    _resolve_call_parts,
    _resolve_root_name,
)


def _is_worker_spawn_call(call_node, imported=None):
    """Return primitive name if call_node spawns a Thread, Process, Timer, or Executor, else None."""
    if not isinstance(call_node, ast.Call):
        return None
    mod_or_obj, attr, bare_fn = _resolve_call_parts(call_node.func, imported)
    if mod_or_obj and attr:
        if mod_or_obj in THREAD_PROCESS_MODULES and attr in THREAD_PROCESS_CLASSES:
            return f"{mod_or_obj}.{attr}"
        if attr in POOL_EXECUTOR_CLASSES:
            return f"{mod_or_obj}.{attr}"
    if bare_fn:
        if bare_fn in THREAD_PROCESS_CLASSES or bare_fn in POOL_EXECUTOR_CLASSES:
            return bare_fn
    return None


def _has_fixed_seed_call(func_node, imported=None):
    """Return True if the function calls random.seed(<constant>) with a non-None constant."""
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        mod_or_obj, attr, bare_fn = _resolve_call_parts(node.func, imported)
        is_seed = (mod_or_obj == "random" and attr == "seed") or bare_fn == "seed"
        if is_seed and node.args:
            arg0 = node.args[0]
            if isinstance(arg0, ast.Constant) and arg0.value is not None:
                return True
    return False


def _is_sorted_arg(arg_node):
    """Return True if arg_node is a call to sorted(...)."""
    return (
        isinstance(arg_node, ast.Call)
        and isinstance(arg_node.func, ast.Name)
        and arg_node.func.id == "sorted"
    )


def _call_has_tz_arg(call_node, pos_index=0):
    """Return True if call_node supplies a non-None positional or keyword `tz` argument."""
    if len(call_node.args) > pos_index:
        arg = call_node.args[pos_index]
        if not (isinstance(arg, ast.Constant) and arg.value is None):
            return True
    for kw in call_node.keywords:
        if kw.arg in ("tz", "tzinfo"):
            if not (isinstance(kw.value, ast.Constant) and kw.value.value is None):
                return True
    return False


def _strftime_uses_gmtime(call_node, imported=None):
    """Return True if time.strftime(fmt, t) explicitly passes a gmtime(...) tuple."""
    if len(call_node.args) >= 2:
        sec_arg = call_node.args[1]
        if isinstance(sec_arg, ast.Call):
            _, attr, bare_fn = _resolve_call_parts(sec_arg.func, imported)
            if bare_fn == "gmtime" or attr == "gmtime":
                return True
    return False


def _has_time_freeze_or_tz_override(func_node, imported=None):
    """Return True if func_node uses freeze_time/freezer or monkeypatches TZ/datetime/time."""
    for dec in getattr(func_node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        _, attr, bare_fn = _resolve_call_parts(target, imported)
        if bare_fn == "freeze_time" or attr == "freeze_time":
            return True
        if isinstance(target, ast.Name) and target.id == "freeze_time":
            return True
        if isinstance(target, ast.Attribute) and target.attr == "freeze_time":
            return True
    for arg in getattr(func_node.args, "args", []):
        if arg.arg in ("freezer", "freeze_time"):
            return True
    for node in ast.walk(func_node):
        if isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call):
                    fn = ctx.func
                    if isinstance(fn, ast.Name) and fn.id == "freeze_time":
                        return True
                    if isinstance(fn, ast.Attribute) and fn.attr == "freeze_time":
                        return True
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "monkeypatch":
                if (
                    node.func.attr == "setenv"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "TZ"
                ):
                    return True
                if node.func.attr == "setattr" and node.args:
                    for arg in node.args[:2]:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            if any(tok in arg.value for tok in ("datetime", "date", "time")):
                                return True
                        elif isinstance(arg, ast.Name):
                            resolved = _resolve_root_name(arg.id, imported)
                            if resolved in ("datetime", "date", "time"):
                                return True
    return False


def _has_tmp_fixture_in_nodes(func_nodes):
    """Return True if any function node declares a pytest temporary directory fixture parameter."""
    for fn in func_nodes:
        args_obj = getattr(fn, "args", None)
        if args_obj is None:
            continue
        for arg in getattr(args_obj, "args", []) + getattr(args_obj, "kwonlyargs", []):
            if arg.arg in TMP_FIXTURE_NAMES:
                return True
    return False


def _call_targets_network_mock(call_node, imported=None):
    """Return True if `call_node` is a monkeypatch.setattr / mocker.patch / mock.patch call targeting network APIs."""
    if not isinstance(call_node, ast.Call):
        return False
    func = call_node.func
    is_patch_call = False
    if isinstance(func, ast.Name):
        resolved = _resolve_root_name(func.id, imported)
        if func.id == "patch" or resolved in ("patch", "unittest.mock.patch", "mock.patch"):
            is_patch_call = True
    elif isinstance(func, ast.Attribute):
        attr = func.attr
        if attr in ("setattr", "patch"):
            if isinstance(func.value, ast.Name):
                recv = _resolve_root_name(func.value.id, imported)
                if func.value.id in ("monkeypatch", "mocker", "mock") or recv in (
                    "monkeypatch", "mocker", "mock", "unittest.mock",
                ):
                    is_patch_call = True
        elif attr == "object" and isinstance(func.value, ast.Attribute) and func.value.attr == "patch":
            is_patch_call = True
        elif attr == "object" and isinstance(func.value, ast.Name):
            recv = _resolve_root_name(func.value.id, imported)
            if func.value.id == "patch" or recv in ("patch", "unittest.mock.patch", "mock.patch"):
                is_patch_call = True

    if not is_patch_call:
        return False

    target_exprs = list(call_node.args[:2]) + [
        kw.value for kw in call_node.keywords if kw.arg in ("target", "name", "attribute")
    ]
    for expr in target_exprs:
        for sub in ast.walk(expr):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                parts = re.split(r"[.\s:/]+", sub.value)
                if any(p in NET_MOCK_TARGET_TOKENS for p in parts) or any(
                    tok in sub.value for tok in ("requests", "httpx", "urllib", "aiohttp", "socket", "urlopen")
                ):
                    return True
            elif isinstance(sub, ast.Name):
                resolved = _resolve_root_name(sub.id, imported)
                if sub.id in NET_MOCK_TARGET_TOKENS or resolved in NET_MOCK_TARGET_TOKENS:
                    return True
                if any(p in NET_MOCK_TARGET_TOKENS for p in resolved.split(".")):
                    return True
            elif isinstance(sub, ast.Attribute):
                if sub.attr in NET_MOCK_TARGET_TOKENS:
                    return True
    return False


def _tree_uses_network_mock(tree_or_node, imported=None):
    """Return True if an AST subtree uses a dedicated network mock library or patches network targets."""
    if tree_or_node is None:
        return False
    if isinstance(tree_or_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args_obj = getattr(tree_or_node, "args", None)
        if args_obj is not None:
            for arg in getattr(args_obj, "args", []) + getattr(args_obj, "kwonlyargs", []):
                if arg.arg in NET_MOCK_NAMES:
                    return True
    for sub in ast.walk(tree_or_node):
        if isinstance(sub, ast.Name):
            resolved = _resolve_root_name(sub.id, imported)
            if sub.id in NET_MOCK_NAMES or resolved in NET_MOCK_NAMES:
                return True
        elif isinstance(sub, ast.Attribute):
            if sub.attr in NET_MOCK_NAMES:
                return True
        elif isinstance(sub, ast.Call):
            if _call_targets_network_mock(sub, imported=imported):
                return True
    return False


def _has_network_mock_in_nodes(func_nodes, enclosing_cls=None, imported=None):
    """Return True if test/fixture functions or enclosing class setup/decorators mock network I/O."""
    for fn in func_nodes:
        if _tree_uses_network_mock(fn, imported=imported):
            return True

    if enclosing_cls is not None:
        for dec in getattr(enclosing_cls, "decorator_list", []):
            if _tree_uses_network_mock(dec, imported=imported):
                return True
        for item in getattr(enclosing_cls, "body", []):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name in SETUP_METHODS:
                if _tree_uses_network_mock(item, imported=imported):
                    return True
    return False


def _expand_expr_nodes(expr, local_exprs):
    """Yield AST nodes in expr and recursively in local variable value expressions referenced by expr."""
    stack = [expr]
    seen_vars = set()
    while stack:
        current = stack.pop()
        if current is None:
            continue
        for sub in ast.walk(current):
            yield sub
            if (
                isinstance(sub, ast.Name)
                and local_exprs
                and sub.id in local_exprs
                and sub.id not in seen_vars
            ):
                seen_vars.add(sub.id)
                stack.append(local_exprs[sub.id])


def _is_safe_temp_expr(expr, safe_tmp_vars, local_exprs=None, imported=None):
    """Return True if expr derives from tmp_path, tmpdir, tempfile, StringIO/BytesIO, or :memory:."""
    if expr is None:
        return True
    for sub in _expand_expr_nodes(expr, local_exprs):
        if isinstance(sub, ast.Name):
            resolved = _resolve_root_name(sub.id, imported)
            if (
                sub.id in safe_tmp_vars
                or sub.id in TMP_FIXTURE_NAMES
                or sub.id in TEMPFILE_SAFE_MODULES
                or sub.id in TEMPFILE_SAFE_CALLS
                or resolved in TEMPFILE_SAFE_MODULES
                or resolved in TEMPFILE_SAFE_CALLS
            ):
                return True
        elif isinstance(sub, ast.Attribute):
            if sub.attr in TEMPFILE_SAFE_CALLS or sub.attr == "devnull":
                return True
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            val = sub.value.strip()
            if val in (":memory:", "/dev/null", "nul", "NUL") or "mode=memory" in val:
                return True
    return False


def _is_hardcoded_path_expr(expr, safe_tmp_vars, hardcoded_path_vars, local_exprs=None, imported=None):
    """Return True if expr represents a hardcoded file/directory/DB path not backed by a temp facility."""
    if expr is None or _is_safe_temp_expr(expr, safe_tmp_vars, local_exprs=local_exprs, imported=imported):
        return False
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str) and expr.value.strip():
        return True
    if isinstance(expr, ast.Name):
        if expr.id in hardcoded_path_vars:
            return True
        if local_exprs and expr.id in local_exprs:
            return _is_hardcoded_path_expr(
                local_exprs[expr.id], safe_tmp_vars, hardcoded_path_vars, local_exprs=None, imported=imported
            )
    if isinstance(expr, ast.Call):
        head = _call_head_name(expr, imported=imported)
        if head in ("Path", "pathlib.Path", "PurePath", "os.path.join", "path.join", "str"):
            return any(
                _is_hardcoded_path_expr(
                    a, safe_tmp_vars, hardcoded_path_vars, local_exprs=local_exprs, imported=imported
                )
                for a in expr.args
            )
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, (ast.Div, ast.Add)):
        return (
            _is_hardcoded_path_expr(
                expr.left, safe_tmp_vars, hardcoded_path_vars, local_exprs=local_exprs, imported=imported
            )
            or _is_hardcoded_path_expr(
                expr.right, safe_tmp_vars, hardcoded_path_vars, local_exprs=local_exprs, imported=imported
            )
        )
    return False


def _extract_nonzero_int_const(node, local_int_consts=None):
    """Return positive integer port constant (> 0), or None if dynamic (0) or non-constant."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value if node.value > 0 else None
    if isinstance(node, ast.Name) and local_int_consts and node.id in local_int_consts:
        val = local_int_consts[node.id]
        return val if val > 0 else None
    return None


def _is_path_div(binop_node, local_exprs, imported=None):
    """Return True if a `/` BinOp is a pathlib path join rather than numeric division."""
    for side_node in (binop_node.left, binop_node.right):
        for sub in ast.walk(side_node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                return True
            if isinstance(sub, ast.Name):
                resolved = _resolve_root_name(sub.id, imported)
                if (
                    sub.id in TMP_FIXTURE_NAMES
                    or sub.id in ("Path", "PurePath", "pathlib")
                    or resolved in ("Path", "PurePath", "pathlib", "pathlib.Path")
                ):
                    return True
                if local_exprs and sub.id in local_exprs:
                    assigned = local_exprs[sub.id]
                    if isinstance(assigned, ast.Call) and _call_head_name(
                        assigned, imported=imported
                    ) in ("Path", "pathlib.Path", "PurePath"):
                        return True
            if isinstance(sub, ast.Call) and _call_head_name(sub, imported=imported) in (
                "Path", "pathlib.Path", "PurePath"
            ):
                return True
    return False


_CONTAINER_TYPE_NAMES = frozenset({
    "list", "List", "dict", "Dict", "set", "Set", "frozenset", "FrozenSet",
    "tuple", "Tuple", "Sequence", "Iterable", "Iterator", "Collection",
    "Mapping", "MutableMapping", "MutableSequence", "MutableSet", "Callable",
})

_LOGGER_VAR_NAMES = frozenset({
    "logger", "LOGGER", "_logger", "_LOGGER", "log", "LOG", "_log", "_LOG",
})

_LOGGER_INIT_CALLS = frozenset({
    "logging.getLogger",
    "getLogger",
    "structlog.get_logger",
    "get_logger",
    "structlog.getLogger",
    "loguru.logger",
})

_LOGGER_SAFE_METHODS = frozenset({
    "debug",
    "info",
    "warning",
    "warn",
    "error",
    "exception",
    "critical",
    "fatal",
    "log",
    "bind",
    "opt",
    "patch",
    "contextualize",
    "new",
    "isEnabledFor",
    "getEffectiveLevel",
    "hasHandlers",
    "getChild",
})

_FLOAT_STAT_METHODS = MATH_FLOAT_FUNCS | {
    "mean", "avg", "average", "fmean", "std", "stdev", "pstdev", "var", "pvariance", "variance",
}


def _annotation_is_float(ann_node):
    """Return True if a parameter/return/variable type annotation denotes a scalar `float`."""
    if ann_node is None:
        return False
    if isinstance(ann_node, ast.Name):
        return ann_node.id == "float"
    if isinstance(ann_node, ast.Constant) and isinstance(ann_node.value, str):
        return ann_node.value.strip() in ("float", "Optional[float]", "float | None", "None | float")
    if isinstance(ann_node, ast.Subscript):
        head = (
            ann_node.value.id
            if isinstance(ann_node.value, ast.Name)
            else (ann_node.value.attr if isinstance(ann_node.value, ast.Attribute) else None)
        )
        if head in _CONTAINER_TYPE_NAMES:
            return False
        if head in ("Optional", "Union", "Annotated"):
            return _annotation_is_float(ann_node.slice)
        return False
    if isinstance(ann_node, ast.BinOp) and isinstance(ann_node.op, ast.BitOr):
        parts = [ann_node.left, ann_node.right]
        non_none = [
            p
            for p in parts
            if not (isinstance(p, ast.Constant) and p.value is None)
            and not (isinstance(p, ast.Name) and p.id == "None")
        ]
        return bool(non_none) and all(_annotation_is_float(p) for p in non_none)
    if isinstance(ann_node, ast.Tuple):
        non_none = [
            p
            for p in ann_node.elts
            if not (isinstance(p, ast.Constant) and p.value is None)
            and not (isinstance(p, ast.Name) and p.id == "None")
        ]
        return bool(non_none) and all(_annotation_is_float(p) for p in non_none)
    return False


def _is_direct_float_value_node(node):
    """Return True if AST node is a direct float literal, signed float literal, or float arithmetic."""
    if isinstance(node, ast.Constant) and isinstance(node.value, float):
        return True
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.UAdd, ast.USub))
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, float)
    ):
        return True
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
    ):
        return _is_direct_float_value_node(node.left) or _is_direct_float_value_node(node.right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float":
        return True
    return False


def _extract_parametrize_float_vars(decorators):
    """Extract parameter names that receive float literals in `@pytest.mark.parametrize(...)` decorators."""
    float_vars = set()
    for dec in decorators or []:
        if not isinstance(dec, ast.Call) or len(dec.args) < 2:
            continue
        is_param = (
            (isinstance(dec.func, ast.Name) and dec.func.id == "parametrize")
            or (isinstance(dec.func, ast.Attribute) and dec.func.attr == "parametrize")
        )
        if not is_param:
            continue
        argnames_node = dec.args[0]
        param_names = []
        if isinstance(argnames_node, ast.Constant) and isinstance(argnames_node.value, str):
            param_names = [
                p.strip() for p in re.split(r"[, ]+", argnames_node.value.strip()) if p.strip()
            ]
        elif isinstance(argnames_node, (ast.List, ast.Tuple)):
            for elt in argnames_node.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str) and elt.value.strip():
                    param_names.append(elt.value.strip())
        if not param_names:
            continue

        argvalues_node = dec.args[1]
        if len(param_names) == 1:
            if isinstance(argvalues_node, (ast.List, ast.Tuple, ast.Set)):
                if any(
                    _is_direct_float_value_node(elt)
                    or (
                        isinstance(elt, (ast.Tuple, ast.List))
                        and len(elt.elts) == 1
                        and _is_direct_float_value_node(elt.elts[0])
                    )
                    for elt in argvalues_node.elts
                ):
                    float_vars.add(param_names[0])
            elif _is_direct_float_value_node(argvalues_node):
                float_vars.add(param_names[0])
        elif isinstance(argvalues_node, (ast.List, ast.Tuple, ast.Set)):
            for row in argvalues_node.elts:
                row_elts = None
                if isinstance(row, (ast.Tuple, ast.List)):
                    row_elts = row.elts
                elif isinstance(row, ast.Call):
                    row_elts = row.args
                if row_elts is not None:
                    for idx, cell in enumerate(row_elts):
                        if idx < len(param_names) and _is_direct_float_value_node(cell):
                            float_vars.add(param_names[idx])
    return float_vars


def _extract_func_float_vars(func_node, initial_float_vars=None, extra_decorators=None):
    """Extract parameter and local variable names known to hold floating-point values."""
    float_vars = set(initial_float_vars or ())
    decs = list(getattr(func_node, "decorator_list", [])) + list(extra_decorators or [])
    float_vars.update(_extract_parametrize_float_vars(decs))

    args_obj = getattr(func_node, "args", None)
    if args_obj is not None:
        pos_args = list(getattr(args_obj, "posonlyargs", [])) + list(getattr(args_obj, "args", []))
        kw_args = list(getattr(args_obj, "kwonlyargs", []))
        for arg in pos_args + kw_args:
            if _annotation_is_float(getattr(arg, "annotation", None)):
                float_vars.add(arg.arg)
        defaults = list(getattr(args_obj, "defaults", []))
        if defaults and pos_args:
            offset = len(pos_args) - len(defaults)
            for idx, d_node in enumerate(defaults):
                if d_node is not None and _is_direct_float_value_node(d_node):
                    float_vars.add(pos_args[offset + idx].arg)
        for idx, d_node in enumerate(getattr(args_obj, "kw_defaults", [])):
            if idx < len(kw_args) and d_node is not None and _is_direct_float_value_node(d_node):
                float_vars.add(kw_args[idx].arg)

    for node in ast.walk(func_node):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if _annotation_is_float(node.annotation):
                float_vars.add(node.target.id)
    return float_vars


def _expr_has_float_safe_guard(expr, local_exprs, imported=None):
    """Return True if expr or its local variable inputs use approx, isclose, allclose, round, or Decimal."""
    for sub in _expand_expr_nodes(expr, local_exprs):
        if isinstance(sub, ast.Call):
            _, attr, bare_fn = _resolve_call_parts(sub.func, imported)
            if bare_fn in FLOAT_SAFE_FUNCS or attr in FLOAT_SAFE_FUNCS:
                return True
            if isinstance(sub.func, ast.Name) and sub.func.id in FLOAT_SAFE_FUNCS:
                return True
            if isinstance(sub.func, ast.Attribute) and sub.func.attr in FLOAT_SAFE_FUNCS:
                return True
        elif isinstance(sub, ast.Name):
            resolved = _resolve_root_name(sub.id, imported)
            if sub.id in FLOAT_SAFE_FUNCS or resolved in FLOAT_SAFE_FUNCS:
                return True
    return False


def _is_non_float_expr(expr, local_exprs=None, imported=None, include_int_const=True, _seen=None):
    """Return True if `expr` is a collection, metadata attribute, string, type, or integer-only expression."""
    if expr is None:
        return True
    if isinstance(expr, ast.Constant):
        if isinstance(expr.value, float):
            return False
        if isinstance(expr.value, int) and not isinstance(expr.value, bool):
            return include_int_const
        return True
    if isinstance(
        expr,
        (
            ast.List,
            ast.Tuple,
            ast.Set,
            ast.Dict,
            ast.ListComp,
            ast.SetComp,
            ast.DictComp,
            ast.GeneratorExp,
            ast.JoinedStr,
        ),
    ):
        return True
    if isinstance(expr, ast.Attribute):
        if expr.attr in NON_FLOAT_ATTRS:
            return True
    if isinstance(expr, ast.Subscript):
        if isinstance(expr.value, ast.Attribute) and expr.value.attr in NON_FLOAT_ATTRS:
            return True
        if isinstance(expr.value, ast.Name) and local_exprs and expr.value.id in local_exprs:
            seen_next = set(_seen or ())
            if expr.value.id not in seen_next:
                seen_next.add(expr.value.id)
                assigned = local_exprs[expr.value.id]
                if isinstance(assigned, ast.Attribute) and assigned.attr in NON_FLOAT_ATTRS:
                    return True
    if isinstance(expr, ast.Call):
        _, attr, bare_fn = _resolve_call_parts(expr.func, imported)
        fn_name = bare_fn or attr
        if not fn_name:
            if isinstance(expr.func, ast.Name):
                fn_name = expr.func.id
            elif isinstance(expr.func, ast.Attribute):
                fn_name = expr.func.attr
        if fn_name in NON_FLOAT_CALLS:
            return True
    if isinstance(expr, ast.Name) and local_exprs and expr.id in local_exprs:
        seen_next = set(_seen or ())
        if expr.id not in seen_next:
            seen_next.add(expr.id)
            return _is_non_float_expr(
                local_exprs[expr.id],
                local_exprs=local_exprs,
                imported=imported,
                include_int_const=include_int_const,
                _seen=seen_next,
            )
    return False


def _expr_involves_float_calc(expr, local_exprs, float_vars=None, imported=None, _seen=None):
    """Return True if expr or its local variable inputs involve float literals, float vars, division/power, float(), or float math/stats."""
    if expr is None:
        return False
    if _is_non_float_expr(expr, local_exprs=local_exprs, imported=imported, include_int_const=True):
        return False
    fvars = float_vars or set()
    seen = set(_seen or ())

    if isinstance(expr, ast.Constant):
        return isinstance(expr.value, float)

    if isinstance(expr, ast.Name):
        if expr.id in fvars:
            return True
        if local_exprs and expr.id in local_exprs and expr.id not in seen:
            seen.add(expr.id)
            return _expr_involves_float_calc(
                local_exprs[expr.id], local_exprs, float_vars=fvars, imported=imported, _seen=seen
            )
        return False

    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, (ast.UAdd, ast.USub)):
        return _expr_involves_float_calc(
            expr.operand, local_exprs, float_vars=fvars, imported=imported, _seen=seen
        )

    if isinstance(expr, ast.BinOp):
        if isinstance(expr.op, ast.Div) and not _is_path_div(expr, local_exprs, imported=imported):
            return True
        if isinstance(expr.op, ast.Pow):
            return True
        if isinstance(expr.op, (ast.Add, ast.Sub, ast.Mult, ast.Mod, ast.FloorDiv)):
            return _expr_involves_float_calc(
                expr.left, local_exprs, float_vars=fvars, imported=imported, _seen=seen
            ) or _expr_involves_float_calc(
                expr.right, local_exprs, float_vars=fvars, imported=imported, _seen=seen
            )
        return False

    if isinstance(expr, ast.IfExp):
        return _expr_involves_float_calc(
            expr.body, local_exprs, float_vars=fvars, imported=imported, _seen=seen
        ) or _expr_involves_float_calc(
            expr.orelse, local_exprs, float_vars=fvars, imported=imported, _seen=seen
        )

    if isinstance(expr, ast.Call):
        mod_or_obj, attr, bare_fn = _resolve_call_parts(expr.func, imported)
        if bare_fn in ("sum", "float", "fsum"):
            return True
        if bare_fn in _FLOAT_STAT_METHODS and mod_or_obj in (None, "math", "np", "numpy", "statistics"):
            return True
        if mod_or_obj in ("math", "np", "numpy", "statistics") and attr in _FLOAT_STAT_METHODS:
            return True
        if (
            isinstance(expr.func, ast.Attribute)
            and expr.func.attr in ("mean", "avg", "average", "std", "var", "sqrt", "cos", "sin", "exp")
            and mod_or_obj not in _LOGGER_VAR_NAMES
        ):
            return True
        return False

    if isinstance(expr, ast.Compare):
        operands = [expr.left, *expr.comparators]
        for idx, op in enumerate(expr.ops):
            if isinstance(op, (ast.Eq, ast.NotEq)):
                left_op = operands[idx]
                right_op = operands[idx + 1]
                if _is_non_float_expr(
                    left_op, local_exprs=local_exprs, imported=imported, include_int_const=False
                ) or _is_non_float_expr(
                    right_op, local_exprs=local_exprs, imported=imported, include_int_const=False
                ):
                    continue
                if _expr_involves_float_calc(
                    left_op, local_exprs, float_vars=fvars, imported=imported, _seen=seen
                ) or _expr_involves_float_calc(
                    right_op, local_exprs, float_vars=fvars, imported=imported, _seen=seen
                ):
                    return True
        return False

    return False


def _collect_local_var_context(
    func_node,
    eff_imported,
    initial_safe_tmp=None,
    initial_hardcoded=None,
    initial_float=None,
    extra_decorators=None,
):
    """Collect (safe_tmp_vars, hardcoded_path_vars, float_vars, local_exprs, local_int_consts) for `func_node`."""
    safe_tmp_vars = {
        arg.arg
        for arg in (
            list(getattr(getattr(func_node, "args", None), "posonlyargs", []))
            + list(getattr(getattr(func_node, "args", None), "args", []))
            + list(getattr(getattr(func_node, "args", None), "kwonlyargs", []))
        )
        if arg.arg in TMP_FIXTURE_NAMES
    }
    if initial_safe_tmp:
        safe_tmp_vars.update(initial_safe_tmp)
    hardcoded_path_vars = set(initial_hardcoded or ())
    local_int_consts = {}
    local_exprs = {}
    float_vars = _extract_func_float_vars(
        func_node, initial_float_vars=initial_float, extra_decorators=extra_decorators
    )

    for node in ast.walk(func_node):
        targets, val = _extract_assign_targets_and_value(node)
        if targets and val is not None:
            for target in targets:
                if isinstance(target, ast.Name):
                    local_exprs[target.id] = val
                    if (
                        isinstance(val, ast.Constant)
                        and isinstance(val.value, int)
                        and not isinstance(val.value, bool)
                    ):
                        local_int_consts[target.id] = val.value

    for _ in range(2):
        for node in ast.walk(func_node):
            if isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if _is_safe_temp_expr(
                        item.context_expr, safe_tmp_vars, local_exprs=local_exprs, imported=eff_imported
                    ):
                        if isinstance(item.optional_vars, ast.Name):
                            safe_tmp_vars.add(item.optional_vars.id)
                        elif isinstance(item.optional_vars, ast.Tuple):
                            for elt in item.optional_vars.elts:
                                if isinstance(elt, ast.Name):
                                    safe_tmp_vars.add(elt.id)
            else:
                targets, val = _extract_assign_targets_and_value(node)
                if targets and val is not None:
                    for target in targets:
                        if isinstance(target, ast.Name):
                            if _is_safe_temp_expr(
                                val, safe_tmp_vars, local_exprs=local_exprs, imported=eff_imported
                            ):
                                safe_tmp_vars.add(target.id)
                                hardcoded_path_vars.discard(target.id)
                            elif _is_hardcoded_path_expr(
                                val,
                                safe_tmp_vars,
                                hardcoded_path_vars,
                                local_exprs=local_exprs,
                                imported=eff_imported,
                            ):
                                hardcoded_path_vars.add(target.id)
                            if _expr_involves_float_calc(
                                val, local_exprs, float_vars=float_vars, imported=eff_imported
                            ) or (isinstance(val, ast.Name) and val.id in float_vars):
                                float_vars.add(target.id)

    return safe_tmp_vars, hardcoded_path_vars, float_vars, local_exprs, local_int_consts


def _scan_cache_decorators(func_node, file_rel, side, eff_imported):
    """Detect `@lru_cache` / `@cache` decorators on `func_node`."""
    hits = []
    for dec in getattr(func_node, "decorator_list", []):
        dec_name = None
        if isinstance(dec, ast.Name):
            dec_name = _resolve_root_name(dec.id, eff_imported)
        elif isinstance(dec, ast.Attribute):
            dec_name = dec.attr
        elif isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Name):
                dec_name = _resolve_root_name(dec.func.id, eff_imported)
            elif isinstance(dec.func, ast.Attribute):
                dec_name = dec.func.attr
        if dec_name in CACHE_DECORATORS:
            hits.append({
                "side": side,
                "location": f"{file_rel}:{func_node.lineno}",
                "detail": f"caches state across calls via `@{dec_name}` on `{func_node.name}`",
            })
    return hits


def _analyze_worker_synchronization(func_node, eff_imported):
    """Collect joined worker variables, loop-spawned workers, and assigned worker calls."""
    joined_worker_vars = set()
    for node in ast.walk(func_node):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join"
            and not node.args
            and isinstance(node.func.value, ast.Name)
        ):
            joined_worker_vars.add(node.func.value.id)

    spawns_in_loops = set()
    for node in ast.walk(func_node):
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and _is_worker_spawn_call(child, imported=eff_imported) is not None:
                    spawns_in_loops.add(id(child))

    assigned_worker_calls = {}
    total_worker_spawns = 0
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call) and _is_worker_spawn_call(node, imported=eff_imported) is not None:
            total_worker_spawns += 1
        targets, val = _extract_assign_targets_and_value(node)
        if targets and val is not None:
            prim = _is_worker_spawn_call(val, imported=eff_imported)
            if prim is not None:
                target_names = [t.id for t in targets if isinstance(t, ast.Name)]
                assigned_worker_calls[id(val)] = target_names

    return joined_worker_vars, spawns_in_loops, assigned_worker_calls, total_worker_spawns


def _scan_return_node(node, local_exprs, float_vars, has_float_return_ann, eff_imported):
    """Inspect an `ast.Return` node and return `(returns_set, returns_float)` flags."""
    ret_set = False
    ret_float = False
    if node.value is None:
        return ret_set, ret_float
    if isinstance(node.value, (ast.Set, ast.SetComp)):
        ret_set = True
    elif isinstance(node.value, ast.Call):
        if isinstance(node.value.func, ast.Name) and node.value.func.id == "set":
            ret_set = True
    if not _expr_has_float_safe_guard(node.value, local_exprs, imported=eff_imported):
        if _expr_involves_float_calc(
            node.value, local_exprs, float_vars=float_vars, imported=eff_imported
        ) or (
            has_float_return_ann
            and any(
                isinstance(sub, ast.BinOp)
                and isinstance(sub.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow))
                for sub in _expand_expr_nodes(node.value, local_exprs)
            )
        ):
            ret_float = True
    return ret_set, ret_float


def _assert_has_strict_float_eq(assert_node, local_exprs, float_vars, eff_imported):
    """Return True if `assert_node` checks strict `==`/`!=` on floating-point calculations without a guard."""
    if _expr_has_float_safe_guard(assert_node.test, local_exprs, imported=eff_imported):
        return False
    for sub in ast.walk(assert_node.test):
        if isinstance(sub, ast.Compare) and any(
            isinstance(op, (ast.Eq, ast.NotEq)) for op in sub.ops
        ):
            if _expr_involves_float_calc(
                sub, local_exprs, float_vars=float_vars, imported=eff_imported
            ):
                return True
    return False


def _scan_call_random_race_tz(
    node,
    mod_or_obj,
    attr,
    bare_fn,
    fixed_seed,
    has_time_freeze,
    eff_imported,
    raw_random,
    raw_race,
    raw_sleep,
    raw_tz,
):
    """Classify a Call node for unseeded randomness, async concurrency/sleep, and naive timezone usage."""
    if mod_or_obj and attr:
        if mod_or_obj in RANDOM_MODULES and attr in RANDOM_FUNCTIONS:
            if not (mod_or_obj == "random" and fixed_seed):
                raw_random.append((node.lineno, f"{mod_or_obj}.{attr}(...)"))
        elif mod_or_obj == "random" and attr == "Random":
            if not node.args or (isinstance(node.args[0], ast.Constant) and node.args[0].value is None):
                raw_random.append((node.lineno, "random.Random()"))
        elif mod_or_obj == "uuid" and attr in UUID_FUNCTIONS:
            raw_random.append((node.lineno, f"uuid.{attr}()"))
        elif mod_or_obj == "os" and attr == "urandom":
            raw_random.append((node.lineno, "os.urandom(...)"))
        elif mod_or_obj in ("np.random", "numpy.random"):
            raw_random.append((node.lineno, f"{mod_or_obj}.{attr}(...)"))
        elif mod_or_obj in ASYNC_CONCURRENCY_MODULES and attr in ASYNC_CONCURRENCY_FUNCTIONS:
            raw_race.append((node.lineno, f"`{mod_or_obj}.{attr}(...)`"))
        elif mod_or_obj == "time" and attr == "sleep":
            raw_sleep.append((node.lineno, "`time.sleep`"))
        elif not has_time_freeze:
            if mod_or_obj == "datetime" and attr == "now" and not _call_has_tz_arg(node, 0):
                raw_tz.append((node.lineno, "datetime.now()"))
            elif mod_or_obj == "datetime" and attr == "today":
                raw_tz.append((node.lineno, "datetime.today()"))
            elif mod_or_obj == "date" and attr == "today":
                raw_tz.append((node.lineno, "date.today()"))
            elif mod_or_obj == "datetime" and attr == "fromtimestamp" and not _call_has_tz_arg(node, 1):
                raw_tz.append((node.lineno, "datetime.fromtimestamp(...)"))
            elif mod_or_obj == "date" and attr == "fromtimestamp":
                raw_tz.append((node.lineno, "date.fromtimestamp(...)"))
            elif mod_or_obj == "time" and attr in ("localtime", "mktime", "asctime", "ctime"):
                raw_tz.append((node.lineno, f"time.{attr}(...)"))
            elif mod_or_obj == "time" and attr == "strftime" and not _strftime_uses_gmtime(
                node, imported=eff_imported
            ):
                raw_tz.append((node.lineno, "time.strftime(...)"))
    elif bare_fn:
        if bare_fn in RANDOM_FUNCTIONS and not fixed_seed:
            raw_random.append((node.lineno, f"{bare_fn}(...)"))
        elif bare_fn in UUID_FUNCTIONS:
            raw_random.append((node.lineno, f"{bare_fn}()"))
        elif bare_fn in ASYNC_CONCURRENCY_FUNCTIONS:
            raw_race.append((node.lineno, f"`{bare_fn}(...)`"))
        elif bare_fn == "sleep":
            raw_sleep.append((node.lineno, "`sleep`"))
        elif not has_time_freeze:
            if bare_fn in ("localtime", "mktime", "asctime", "ctime"):
                raw_tz.append((node.lineno, f"time.{bare_fn}(...)"))
            elif bare_fn == "strftime" and not _strftime_uses_gmtime(node, imported=eff_imported):
                raw_tz.append((node.lineno, "time.strftime(...)"))

    if (
        not has_time_freeze
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "astimezone"
        and not _call_has_tz_arg(node, 0)
    ):
        raw_tz.append((node.lineno, ".astimezone()"))


def _scan_call_resource_file(
    node,
    mod_or_obj,
    attr,
    bare_fn,
    safe_tmp_vars,
    hardcoded_path_vars,
    local_exprs,
    eff_imported,
    raw_res_file,
):
    """Check a Call node for hardcoded file or SQLite database path mutations."""
    is_builtin_open = (
        bare_fn == "open"
        or (mod_or_obj in ("io", "builtins") and attr == "open")
    )
    if is_builtin_open:
        path_arg = node.args[0] if node.args else None
        mode_arg = node.args[1] if len(node.args) >= 2 else None
        for kw in node.keywords:
            if kw.arg == "file" and path_arg is None:
                path_arg = kw.value
            elif kw.arg == "mode" and mode_arg is None:
                mode_arg = kw.value
        if (
            isinstance(mode_arg, ast.Constant)
            and isinstance(mode_arg.value, str)
            and any(m in mode_arg.value for m in ("w", "a", "x", "+"))
            and _is_hardcoded_path_expr(
                path_arg,
                safe_tmp_vars,
                hardcoded_path_vars,
                local_exprs=local_exprs,
                imported=eff_imported,
            )
        ):
            raw_res_file.append((node.lineno, "open(...)"))
    elif isinstance(node.func, ast.Attribute):
        recv = node.func.value
        if attr in PATH_MUTATION_METHODS and _is_hardcoded_path_expr(
            recv, safe_tmp_vars, hardcoded_path_vars, local_exprs=local_exprs, imported=eff_imported
        ):
            raw_res_file.append((node.lineno, f"Path.{attr}(...)"))
        elif attr == "open" and _is_hardcoded_path_expr(
            recv, safe_tmp_vars, hardcoded_path_vars, local_exprs=local_exprs, imported=eff_imported
        ):
            mode_arg = node.args[0] if node.args else None
            for kw in node.keywords:
                if kw.arg == "mode" and mode_arg is None:
                    mode_arg = kw.value
            if (
                isinstance(mode_arg, ast.Constant)
                and isinstance(mode_arg.value, str)
                and any(m in mode_arg.value for m in ("w", "a", "x", "+"))
            ):
                raw_res_file.append((node.lineno, "Path.open(...)"))
        elif (
            mod_or_obj == "os"
            and attr in OS_FS_MUTATION_FUNCS
            and node.args
            and _is_hardcoded_path_expr(
                node.args[0],
                safe_tmp_vars,
                hardcoded_path_vars,
                local_exprs=local_exprs,
                imported=eff_imported,
            )
        ):
            raw_res_file.append((node.lineno, f"os.{attr}(...)"))
        elif (
            mod_or_obj == "shutil"
            and attr in SHUTIL_MUTATION_FUNCS
            and node.args
            and _is_hardcoded_path_expr(
                node.args[0],
                safe_tmp_vars,
                hardcoded_path_vars,
                local_exprs=local_exprs,
                imported=eff_imported,
            )
        ):
            raw_res_file.append((node.lineno, f"shutil.{attr}(...)"))
        elif mod_or_obj == "sqlite3" and attr == "connect":
            db_arg = node.args[0] if node.args else None
            for kw in node.keywords:
                if kw.arg == "database" and db_arg is None:
                    db_arg = kw.value
            if _is_hardcoded_path_expr(
                db_arg,
                safe_tmp_vars,
                hardcoded_path_vars,
                local_exprs=local_exprs,
                imported=eff_imported,
            ):
                raw_res_file.append((node.lineno, "sqlite3.connect(...)"))
    elif bare_fn:
        if (
            bare_fn in (OS_FS_MUTATION_FUNCS | {"rmtree"})
            and node.args
            and _is_hardcoded_path_expr(
                node.args[0],
                safe_tmp_vars,
                hardcoded_path_vars,
                local_exprs=local_exprs,
                imported=eff_imported,
            )
        ):
            prefix = "shutil" if bare_fn == "rmtree" else "os"
            raw_res_file.append((node.lineno, f"{prefix}.{bare_fn}(...)"))
        elif bare_fn == "connect" and mod_or_obj == "sqlite3":
            db_arg = node.args[0] if node.args else None
            for kw in node.keywords:
                if kw.arg == "database" and db_arg is None:
                    db_arg = kw.value
            if _is_hardcoded_path_expr(
                db_arg,
                safe_tmp_vars,
                hardcoded_path_vars,
                local_exprs=local_exprs,
                imported=eff_imported,
            ):
                raw_res_file.append((node.lineno, "sqlite3.connect(...)"))


def _scan_call_resource_port(node, attr, bare_fn, local_int_consts, raw_res_port):
    """Check a Call node for hardcoded non-zero network port bindings."""
    if isinstance(node.func, ast.Attribute) and node.func.attr == "bind" and node.args:
        addr = node.args[0]
        if isinstance(addr, (ast.Tuple, ast.List)) and len(addr.elts) >= 2:
            port_val = _extract_nonzero_int_const(addr.elts[1], local_int_consts)
            if port_val is not None:
                raw_res_port.append((node.lineno, f".bind((..., {port_val}))"))
        return

    fn_name = bare_fn or attr
    if fn_name in PORT_BIND_SERVER_CLASSES and node.args:
        addr = node.args[0]
        if isinstance(addr, (ast.Tuple, ast.List)) and len(addr.elts) >= 2:
            port_val = _extract_nonzero_int_const(addr.elts[1], local_int_consts)
            if port_val is not None:
                raw_res_port.append((node.lineno, f"{fn_name}((..., {port_val}))"))
    elif fn_name in PORT_BIND_FUNCS:
        port_val = None
        for kw in node.keywords:
            if kw.arg == "port":
                port_val = _extract_nonzero_int_const(kw.value, local_int_consts)
        if port_val is None and node.args:
            if isinstance(node.args[0], (ast.Tuple, ast.List)) and len(node.args[0].elts) >= 2:
                port_val = _extract_nonzero_int_const(node.args[0].elts[1], local_int_consts)
            elif fn_name == "make_server" and len(node.args) >= 2:
                port_val = _extract_nonzero_int_const(node.args[1], local_int_consts)
            elif fn_name == "start_server" and len(node.args) >= 3:
                port_val = _extract_nonzero_int_const(node.args[2], local_int_consts)
        if port_val is not None:
            raw_res_port.append((node.lineno, f"{fn_name}(..., port={port_val})"))


def _scan_call_external_io(node, mod_or_obj, attr, bare_fn, local_has_net_mock, raw_ext_io):
    """Check a Call node for unmocked external network requests."""
    if local_has_net_mock:
        return
    if mod_or_obj and attr:
        if mod_or_obj in ("requests", "httpx") and attr in REQUESTS_HTTPX_METHODS:
            raw_ext_io.append((node.lineno, f"{mod_or_obj}.{attr}(...)"))
        elif mod_or_obj == "aiohttp" and attr in AIOHTTP_TARGETS:
            raw_ext_io.append((node.lineno, f"aiohttp.{attr}(...)"))
        elif mod_or_obj == "socket" and attr in SOCKET_NET_FUNCS:
            raw_ext_io.append((node.lineno, f"socket.{attr}(...)"))
        elif mod_or_obj == "smtplib" and attr in SMTPLIB_TARGETS:
            raw_ext_io.append((node.lineno, f"smtplib.{attr}(...)"))
        elif mod_or_obj in ("request", "urllib.request") and attr in URLLIB_REQUEST_FUNCS:
            raw_ext_io.append((node.lineno, f"urllib.request.{attr}(...)"))
    elif bare_fn:
        if bare_fn in URLLIB_REQUEST_FUNCS:
            raw_ext_io.append((node.lineno, f"urllib.request.{bare_fn}(...)"))
        elif bare_fn == "ClientSession":
            raw_ext_io.append((node.lineno, "aiohttp.ClientSession(...)"))
        elif bare_fn in SOCKET_NET_FUNCS:
            raw_ext_io.append((node.lineno, f"socket.{bare_fn}(...)"))
        elif bare_fn in SMTPLIB_TARGETS:
            raw_ext_io.append((node.lineno, f"smtplib.{bare_fn}(...)"))


def _coalesce_function_signals(
    signals,
    file_rel,
    side,
    raw_random,
    raw_race,
    raw_sleep,
    raw_join,
    raw_tz,
    raw_res_file,
    raw_res_port,
    raw_ext_io,
    raw_float_asserts,
):
    """Format raw per-line hits into coalesced indicator dictionaries on `signals`."""
    if raw_random:
        raw_random.sort(key=lambda item: item[0])
        lines_str = ", ".join(str(ln) for ln in _dedup_preserve_order([ln for ln, _ in raw_random]))
        calls_str = ", ".join(f"`{c}`" for c in _dedup_preserve_order([c for _, c in raw_random]))
        signals["unseeded_randomness"].append({
            "side": side,
            "location": f"{file_rel}:{lines_str}",
            "detail": f"calls {calls_str} without a fixed seed",
        })

    if raw_race:
        combined = sorted(raw_race + raw_sleep, key=lambda item: item[0])
        lines_str = ", ".join(str(ln) for ln in _dedup_preserve_order([ln for ln, _ in combined]))
        prims = _dedup_preserve_order([p for _, p in combined])
        prims_str = ", ".join(prims)
        signals["race_conditions"].append({
            "side": side,
            "location": f"{file_rel}:{lines_str}",
            "detail": f"uses unsynchronized concurrency/timing primitives ({prims_str})",
        })

    if raw_join:
        raw_join.sort()
        lines_str = ", ".join(str(ln) for ln in _dedup_preserve_order(raw_join))
        signals["hash_order_sensitivity"].append({
            "side": "Test Side",
            "location": f"{file_rel}:{lines_str}",
            "detail": "unsorted `join(...)` on iterable",
        })

    if raw_tz:
        raw_tz.sort(key=lambda item: item[0])
        lines_str = ", ".join(str(ln) for ln in _dedup_preserve_order([ln for ln, _ in raw_tz]))
        calls_str = ", ".join(f"`{c}`" for c in _dedup_preserve_order([c for _, c in raw_tz]))
        signals["timezone_sensitivity"].append({
            "side": side,
            "location": f"{file_rel}:{lines_str}",
            "detail": f"calls {calls_str} without explicit timezone (`tz`) or time freeze",
        })

    if raw_res_file:
        raw_res_file.sort(key=lambda item: item[0])
        lines_str = ", ".join(str(ln) for ln in _dedup_preserve_order([ln for ln, _ in raw_res_file]))
        calls_str = ", ".join(f"`{c}`" for c in _dedup_preserve_order([c for _, c in raw_res_file]))
        signals["resource_leak"].append({
            "side": side,
            "location": f"{file_rel}:{lines_str}",
            "detail": f"operates on hardcoded file/database path via {calls_str} without `tmp_path` or `tempfile`",
        })

    if raw_res_port:
        raw_res_port.sort(key=lambda item: item[0])
        lines_str = ", ".join(str(ln) for ln in _dedup_preserve_order([ln for ln, _ in raw_res_port]))
        ports_str = ", ".join(f"`{p}`" for p in _dedup_preserve_order([p for _, p in raw_res_port]))
        signals["resource_leak"].append({
            "side": side,
            "location": f"{file_rel}:{lines_str}",
            "detail": f"binds hardcoded non-zero network port via {ports_str}",
        })

    if raw_ext_io:
        raw_ext_io.sort(key=lambda item: item[0])
        lines_str = ", ".join(str(ln) for ln in _dedup_preserve_order([ln for ln, _ in raw_ext_io]))
        calls_str = ", ".join(f"`{c}`" for c in _dedup_preserve_order([c for _, c in raw_ext_io]))
        signals["external_io"].append({
            "side": side,
            "location": f"{file_rel}:{lines_str}",
            "detail": f"performs unmocked external network I/O via {calls_str}",
        })

    if raw_float_asserts:
        raw_float_asserts.sort()
        lines_str = ", ".join(str(ln) for ln in _dedup_preserve_order(raw_float_asserts))
        signals["float_precision"].append({
            "side": "Test Side",
            "location": f"{file_rel}:{lines_str}",
            "detail": "asserts strict equality (`==`/`!=`) on floating-point values without `pytest.approx` or `math.isclose`",
        })


def _scan_implicit_submodule_imports(
    func_node,
    file_rel,
    top_pkg_aliases=None,
    explicit_submodules=None,
    project_submodules=None,
    imported=None,
):
    """Detect calls to `<alias>.<submodule>.<attr>(...)` in a Test Side function where `<pkg>.<submodule>` was not imported."""
    eff_top_aliases = dict(top_pkg_aliases or {})
    eff_explicit_subs = set(explicit_submodules or ())
    if not eff_top_aliases and imported:
        for local_k, entry in imported.items():
            if isinstance(entry, tuple) and len(entry) == 3:
                _, target, mod_name = entry
                if mod_name and "." not in mod_name and target == mod_name:
                    eff_top_aliases[local_k] = mod_name
                elif mod_name and "." in mod_name:
                    parts = [p for p in mod_name.split(".") if p]
                    if len(parts) >= 2:
                        eff_explicit_subs.add(f"{parts[0]}.{parts[1]}")

    func_top_aliases, func_explicit_subs = _collect_submodule_imports(func_node)
    eff_top_aliases.update(func_top_aliases)
    eff_explicit_subs.update(func_explicit_subs)
    if not eff_top_aliases:
        return []

    local_names = set()
    args_obj = getattr(func_node, "args", None)
    if args_obj is not None:
        for arg in (
            list(getattr(args_obj, "posonlyargs", []))
            + list(getattr(args_obj, "args", []))
            + list(getattr(args_obj, "kwonlyargs", []))
        ):
            local_names.add(arg.arg)
        for special_arg in (getattr(args_obj, "vararg", None), getattr(args_obj, "kwarg", None)):
            if special_arg is not None:
                local_names.add(special_arg.arg)
    for node in ast.walk(func_node):
        targets, val = _extract_assign_targets_and_value(node)
        if targets and val is not None:
            for target in targets:
                if isinstance(target, ast.Name):
                    local_names.add(target.id)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            for sub_t in ast.walk(node.target):
                if isinstance(sub_t, ast.Name) and isinstance(sub_t.ctx, ast.Store):
                    local_names.add(sub_t.id)

    proj_subs = set(project_submodules or ())
    raw_hits = []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        parts = []
        cur = node.func
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name) or len(parts) < 2:
            continue
        alias = cur.id
        if alias in local_names or alias not in eff_top_aliases:
            continue
        parts.reverse()
        submodule = parts[0]
        attr_rest = ".".join(parts[1:])
        pkg = eff_top_aliases[alias]
        full_sub = f"{pkg}.{submodule}"
        if full_sub in eff_explicit_subs:
            continue
        if (pkg, submodule) in EAGER_STDLIB_SUBMODULES:
            continue
        if (pkg, submodule) in KNOWN_LAZY_SUBMODULES or full_sub in proj_subs:
            call_chain = f"{alias}.{submodule}.{attr_rest}"
            raw_hits.append((node.lineno, call_chain, full_sub))

    if not raw_hits:
        return []

    raw_hits.sort(key=lambda item: item[0])
    grouped = {}
    order_keys = []
    for lineno, call_chain, full_sub in raw_hits:
        key = (call_chain, full_sub)
        if key not in grouped:
            grouped[key] = []
            order_keys.append(key)
        grouped[key].append(lineno)

    results = []
    for call_chain, full_sub in order_keys:
        lines_str = ", ".join(str(ln) for ln in _dedup_preserve_order(grouped[(call_chain, full_sub)]))
        results.append({
            "side": "Test Side",
            "location": f"{file_rel}:{lines_str}",
            "detail": (
                f"calls `{call_chain}` without importing `{full_sub}` in this test file "
                f"(implicitly relies on another test file importing it first)"
            ),
        })
    return results


def _scan_function_signals(
    func_node,
    file_rel,
    side,
    has_tmp_fixture=False,
    has_net_mock=False,
    imported=None,
    initial_safe_tmp_vars=None,
    initial_hardcoded_vars=None,
    initial_float_vars=None,
    extra_decorators=None,
    top_pkg_aliases=None,
    explicit_submodules=None,
    project_submodules=None,
):
    """Scan a function/method AST and coalesce static indicators per function.

    Synchronized single threads (where a single spawned Thread/Process variable
    is joined via `.join()` within the same function outside a loop) and standalone
    Lock/Event primitives are not flagged as race indicators.
    """
    signals = {
        "unseeded_randomness": [],
        "race_conditions": [],
        "hash_order_sensitivity": [],
        "shared_state_interference": [],
        "timezone_sensitivity": [],
        "resource_leak": [],
        "external_io": [],
        "float_precision": [],
        "returns_set": False,
        "returns_float": False,
    }
    raw_random = []
    raw_race = []
    raw_sleep = []
    raw_join = []
    raw_tz = []
    raw_res_file = []
    raw_res_port = []
    raw_ext_io = []
    raw_float_asserts = []

    eff_imported = _collect_local_imports(func_node, imported)
    fixed_seed = _has_fixed_seed_call(func_node, imported=eff_imported)
    has_time_freeze = _has_time_freeze_or_tz_override(func_node, imported=eff_imported)
    local_has_net_mock = has_net_mock or _has_network_mock_in_nodes([func_node], imported=eff_imported)

    has_float_return_ann = _annotation_is_float(getattr(func_node, "returns", None))
    if has_float_return_ann:
        signals["returns_float"] = True

    (
        safe_tmp_vars,
        hardcoded_path_vars,
        float_vars,
        local_exprs,
        local_int_consts,
    ) = _collect_local_var_context(
        func_node,
        eff_imported,
        initial_safe_tmp=initial_safe_tmp_vars,
        initial_hardcoded=initial_hardcoded_vars,
        initial_float=initial_float_vars,
        extra_decorators=extra_decorators,
    )

    signals["shared_state_interference"].extend(
        _scan_cache_decorators(func_node, file_rel, side, eff_imported)
    )
    if side == "Test Side":
        signals["shared_state_interference"].extend(
            _scan_implicit_submodule_imports(
                func_node,
                file_rel,
                top_pkg_aliases=top_pkg_aliases,
                explicit_submodules=explicit_submodules,
                project_submodules=project_submodules,
                imported=eff_imported,
            )
        )
    (
        joined_worker_vars,
        spawns_in_loops,
        assigned_worker_calls,
        total_worker_spawns,
    ) = _analyze_worker_synchronization(func_node, eff_imported)

    for node in ast.walk(func_node):
        if isinstance(node, ast.Return) and node.value is not None:
            ret_set, ret_float = _scan_return_node(
                node, local_exprs, float_vars, has_float_return_ann, eff_imported
            )
            if ret_set:
                signals["returns_set"] = True
            if ret_float:
                signals["returns_float"] = True

        if side == "Test Side" and isinstance(node, ast.Assert):
            if _assert_has_strict_float_eq(node, local_exprs, float_vars, eff_imported):
                raw_float_asserts.append(node.lineno)

        if (
            not has_time_freeze
            and isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and _resolve_root_name(node.value.id, eff_imported) == "time"
            and node.attr == "tzname"
        ):
            raw_tz.append((node.lineno, "time.tzname"))

        if isinstance(node, ast.Call):
            spawn_prim = _is_worker_spawn_call(node, imported=eff_imported)
            if spawn_prim is not None:
                is_pool = any(p in spawn_prim for p in POOL_EXECUTOR_CLASSES)
                targets = assigned_worker_calls.get(id(node), [])
                is_joined = (
                    not is_pool
                    and id(node) not in spawns_in_loops
                    and bool(targets)
                    and all(t in joined_worker_vars for t in targets)
                )
                if is_pool or id(node) in spawns_in_loops or total_worker_spawns > 1:
                    label = f"concurrent `{spawn_prim}`" if is_joined else f"unjoined `{spawn_prim}`"
                    raw_race.append((node.lineno, label))
                elif not is_joined:
                    raw_race.append((node.lineno, f"unjoined `{spawn_prim}`"))
                continue

            mod_or_obj, attr, bare_fn = _resolve_call_parts(node.func, eff_imported)

            _scan_call_random_race_tz(
                node,
                mod_or_obj,
                attr,
                bare_fn,
                fixed_seed,
                has_time_freeze,
                eff_imported,
                raw_random,
                raw_race,
                raw_sleep,
                raw_tz,
            )

            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in ORDER_SENSITIVE_STR_METHODS
                and node.args
                and side == "Test Side"
            ):
                is_path_join = (
                    mod_or_obj in PATH_JOIN_RECEIVERS
                    or (
                        isinstance(node.func.value, ast.Name)
                        and _resolve_root_name(node.func.value.id, eff_imported) in PATH_JOIN_RECEIVERS
                    )
                    or (
                        isinstance(node.func.value, ast.Attribute)
                        and node.func.value.attr in PATH_JOIN_RECEIVERS
                    )
                )
                if not is_path_join and len(node.args) == 1 and not _is_sorted_arg(node.args[0]):
                    raw_join.append(node.lineno)

            if (
                side == "Test Side"
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("assertEqual", "assertNotEqual")
                and len(node.args) >= 2
                and not _expr_has_float_safe_guard(node, local_exprs, imported=eff_imported)
            ):
                if not (
                    _is_non_float_expr(
                        node.args[0], local_exprs=local_exprs, imported=eff_imported, include_int_const=False
                    )
                    or _is_non_float_expr(
                        node.args[1], local_exprs=local_exprs, imported=eff_imported, include_int_const=False
                    )
                ) and any(
                    _expr_involves_float_calc(
                        a, local_exprs, float_vars=float_vars, imported=eff_imported
                    )
                    for a in node.args[:2]
                ):
                    raw_float_asserts.append(node.lineno)

            _scan_call_resource_file(
                node,
                mod_or_obj,
                attr,
                bare_fn,
                safe_tmp_vars,
                hardcoded_path_vars,
                local_exprs,
                eff_imported,
                raw_res_file,
            )
            _scan_call_resource_port(node, attr, bare_fn, local_int_consts, raw_res_port)
            _scan_call_external_io(node, mod_or_obj, attr, bare_fn, local_has_net_mock, raw_ext_io)

    _coalesce_function_signals(
        signals,
        file_rel,
        side,
        raw_random,
        raw_race,
        raw_sleep,
        raw_join,
        raw_tz,
        raw_res_file,
        raw_res_port,
        raw_ext_io,
        raw_float_asserts,
    )
    return signals


def _is_fixture_func(func_node):
    for dec in getattr(func_node, "decorator_list", []):
        if isinstance(dec, ast.Name) and dec.id == "fixture":
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == "fixture":
            return True
        if isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Name) and dec.func.id == "fixture":
                return True
            if isinstance(dec.func, ast.Attribute) and dec.func.attr == "fixture":
                return True
    return False


def _is_autouse_fixture(func_node):
    """Return True if `func_node` is decorated with `@pytest.fixture(..., autouse=True)` or `@fixture(..., autouse=True)`."""
    if not _is_fixture_func(func_node):
        return False
    for dec in getattr(func_node, "decorator_list", []):
        if isinstance(dec, ast.Call):
            is_fix = (
                (isinstance(dec.func, ast.Name) and dec.func.id == "fixture")
                or (isinstance(dec.func, ast.Attribute) and dec.func.attr == "fixture")
            )
            if is_fix:
                for kw in dec.keywords:
                    if kw.arg == "autouse" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        return True
    return False


def _fixture_returns_safe_temp(func_node, imported=None):
    """Return True if a fixture function takes a tmp_path/tmpdir fixture or returns a safe temp expression."""
    safe_vars = {
        arg.arg
        for arg in (
            list(getattr(getattr(func_node, "args", None), "posonlyargs", []))
            + list(getattr(getattr(func_node, "args", None), "args", []))
            + list(getattr(getattr(func_node, "args", None), "kwonlyargs", []))
        )
        if arg.arg in TMP_FIXTURE_NAMES
    }
    local_exprs = {}
    for n in ast.walk(func_node):
        targets, val = _extract_assign_targets_and_value(n)
        if targets and val is not None:
            for t in targets:
                if isinstance(t, ast.Name):
                    local_exprs[t.id] = val
    for n in ast.walk(func_node):
        if isinstance(n, (ast.Return, ast.Yield)) and n.value is not None:
            if _is_safe_temp_expr(n.value, safe_vars, local_exprs=local_exprs, imported=imported):
                return True
    return False


def _iter_fixture_teardown_stmts(stmts):
    """Yield AST statements executed in the post-yield teardown phase of a generator fixture."""
    after_yield = False
    for stmt in stmts or []:
        if after_yield:
            yield stmt
            continue
        if isinstance(stmt, ast.Try):
            has_body_yield = any(
                isinstance(sub, (ast.Yield, ast.YieldFrom)) for b in stmt.body for sub in ast.walk(b)
            )
            if has_body_yield:
                yield from _iter_fixture_teardown_stmts(stmt.body)
                yield from stmt.finalbody
                after_yield = True
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            has_body_yield = any(
                isinstance(sub, (ast.Yield, ast.YieldFrom)) for b in stmt.body for sub in ast.walk(b)
            )
            if has_body_yield:
                yield from _iter_fixture_teardown_stmts(stmt.body)
                after_yield = True
        elif any(isinstance(sub, (ast.Yield, ast.YieldFrom)) for sub in ast.walk(stmt)):
            after_yield = True


def _collect_teardown_clear_calls(func_node, is_teardown_func=False):
    """Return set of `id(attr_node)` for `<GLOBAL>.clear()` / `<GLOBAL>.reset()` calls in teardown phases."""
    exempt_attr_ids = set()
    if is_teardown_func:
        teardown_stmts = list(getattr(func_node, "body", []))
    elif _is_fixture_func(func_node):
        teardown_stmts = list(_iter_fixture_teardown_stmts(getattr(func_node, "body", [])))
    else:
        return exempt_attr_ids

    for stmt in teardown_stmts:
        for sub in ast.walk(stmt):
            if (
                isinstance(sub, ast.Call)
                and not sub.args
                and not sub.keywords
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr in ("clear", "reset")
                and isinstance(sub.func.value, ast.Name)
            ):
                exempt_attr_ids.add(id(sub.func))
    return exempt_attr_ids


_READONLY_MODULE_GLOBALS = frozenset({
    "logger",
    "log",
    "LOGGER",
    "LOG",
    "_logger",
    "_LOGGER",
    "_log",
    "_LOG",
    "__all__",
    "__path__",
    "__spec__",
    "__loader__",
    "__package__",
})

_READONLY_GLOBAL_CALLS = frozenset({
    "re.compile",
    "compile",
    "logging.getLogger",
    "getLogger",
    "structlog.get_logger",
    "get_logger",
    "structlog.getLogger",
    "loguru.logger",
    "Path",
    "pathlib.Path",
    "PurePath",
    "PosixPath",
    "WindowsPath",
    "namedtuple",
    "collections.namedtuple",
    "TypeVar",
    "typing.TypeVar",
    "ParamSpec",
    "NewType",
    "Enum",
    "IntEnum",
    "StrEnum",
    "Flag",
    "IntFlag",
    "frozenset",
})


def _scan_shared_globals_in_func(func_node, file_rel, module_globals, side):
    """Return list of shared_state_interference indicator dicts for module_globals accessed/mutated in func_node."""
    global_decls = {
        name
        for node in ast.walk(func_node)
        if isinstance(node, ast.Global)
        for name in node.names
    }
    local_assigned_names = set()
    args_obj = getattr(func_node, "args", None)
    if args_obj is not None:
        for arg in (
            list(getattr(args_obj, "posonlyargs", []))
            + list(getattr(args_obj, "args", []))
            + list(getattr(args_obj, "kwonlyargs", []))
        ):
            if arg.arg not in global_decls:
                local_assigned_names.add(arg.arg)
        for special_arg in (getattr(args_obj, "vararg", None), getattr(args_obj, "kwarg", None)):
            if special_arg is not None and special_arg.arg not in global_decls:
                local_assigned_names.add(special_arg.arg)

    for node in ast.walk(func_node):
        targets, val = _extract_assign_targets_and_value(node)
        if targets and val is not None:
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in global_decls:
                    local_assigned_names.add(target.id)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            for sub_t in ast.walk(node.target):
                if isinstance(sub_t, ast.Name) and isinstance(sub_t.ctx, ast.Store) and sub_t.id not in global_decls:
                    local_assigned_names.add(sub_t.id)

    is_teardown_fn = getattr(func_node, "name", "") in TEARDOWN_METHODS
    exempt_attr_ids = _collect_teardown_clear_calls(func_node, is_teardown_func=is_teardown_fn)
    compound_name_ids = {
        id(node.value)
        for node in ast.walk(func_node)
        if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.value, ast.Name)
    } | {
        id(node.func)
        for node in ast.walk(func_node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    shared_hits = []
    for node in ast.walk(func_node):
        if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.value, ast.Name):
            if isinstance(node, ast.Attribute) and id(node) in exempt_attr_ids:
                continue
            var_name = node.value.id
            if var_name in module_globals and var_name not in local_assigned_names:
                _, call_head = module_globals[var_name]
                is_logger_var = (var_name in _LOGGER_VAR_NAMES) or (
                    (call_head or "") in _LOGGER_INIT_CALLS
                )
                if (
                    is_logger_var
                    and isinstance(node, ast.Attribute)
                    and isinstance(node.ctx, ast.Load)
                    and node.attr in _LOGGER_SAFE_METHODS
                ):
                    continue
                shared_hits.append((node.lineno, var_name))
        elif isinstance(node, ast.Global):
            for var_name in node.names:
                if var_name in module_globals:
                    shared_hits.append((node.lineno, var_name))
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and id(node) not in compound_name_ids
        ):
            var_name = node.id
            if var_name in module_globals and var_name not in local_assigned_names:
                _, call_head = module_globals[var_name]
                if (
                    var_name not in _READONLY_MODULE_GLOBALS
                    and (call_head or "") not in _READONLY_GLOBAL_CALLS
                ):
                    shared_hits.append((node.lineno, var_name))
    shared_hits.sort(key=lambda item: item[0])
    seen_shared = set()
    results = []
    for lineno, var_name in shared_hits:
        if var_name in seen_shared:
            continue
        seen_shared.add(var_name)
        decl_line, _ = module_globals[var_name]
        results.append({
            "side": side,
            "location": f"{file_rel}:{lineno}",
            "detail": (
                f"accesses/mutates shared module-level object `{var_name}` "
                f"(declared at `{file_rel}:{decl_line}`)"
            ),
        })
    return results


def _collect_class_self_attrs(cls_node, tree=None, imported=None):
    """Collect `self.<attr> -> ClassName` mappings across `cls_node` and its same-file base classes.

    Scans class-level attribute assignments, `__init__`, pytest/unittest setup methods, and
    class-level `@pytest.fixture` methods for both `ast.Assign` and `ast.AnnAssign`.
    """
    self_attrs = {}
    for cur_cls in _iter_class_hierarchy(cls_node, tree=tree):
        for item in getattr(cur_cls, "body", []):
            targets, val = _extract_assign_targets_and_value(item)
            if targets and val is not None and not isinstance(val, (ast.Constant, ast.Tuple)):
                call_head = _call_head_name(val, imported=imported)
                if call_head:
                    for t in targets:
                        if isinstance(t, ast.Name):
                            self_attrs.setdefault(t.id, call_head)
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                item.name in ATTR_INIT_METHODS or _is_fixture_func(item)
            ):
                for sub in ast.walk(item):
                    sub_targets, sub_val = _extract_assign_targets_and_value(sub)
                    if sub_targets and isinstance(sub_val, ast.Call):
                        head = _call_head_name(sub_val, imported=imported)
                        if head:
                            for t in sub_targets:
                                if (
                                    isinstance(t, ast.Attribute)
                                    and isinstance(t.value, ast.Name)
                                    and t.value.id == "self"
                                ):
                                    self_attrs.setdefault(t.attr, head)
    return self_attrs


def _scan_class_shared_state(func_node, cls_node, file_rel, side, reset_class_attrs, tree=None):
    """Scan `func_node` for accesses/mutations of class-level shared state on `cls_node` (Test Side or Code Side)."""
    if cls_node is None:
        return []
    cls_mutable_attrs = {}
    cls_all_attrs = {}
    hierarchy_names = {"self", "cls"}
    for c in _iter_class_hierarchy(cls_node, tree=tree):
        hierarchy_names.add(c.name)
        for item in getattr(c, "body", []):
            targets, val = _extract_assign_targets_and_value(item)
            if targets and val is not None:
                for t in targets:
                    if isinstance(t, ast.Name):
                        cls_all_attrs.setdefault(t.id, item.lineno)
                        if not isinstance(val, (ast.Constant, ast.Tuple)):
                            cls_mutable_attrs.setdefault(t.id, item.lineno)

    if not cls_all_attrs:
        return []

    class_only_names = hierarchy_names - {"self"}
    is_init_or_setup = getattr(func_node, "name", "") in ATTR_INIT_METHODS
    hits = []

    for sub in ast.walk(func_node):
        if (
            isinstance(sub, (ast.Attribute, ast.Subscript))
            and isinstance(sub.value, ast.Attribute)
            and isinstance(sub.value.value, ast.Name)
            and sub.value.value.id in hierarchy_names
            and sub.value.attr in cls_mutable_attrs
            and (cls_node.name, sub.value.attr) not in reset_class_attrs
        ):
            hits.append((sub.lineno, sub.value.attr, cls_mutable_attrs[sub.value.attr]))
        elif isinstance(sub, ast.AugAssign):
            target = sub.target
            attr_name = None
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in hierarchy_names
                and target.attr in cls_all_attrs
            ):
                attr_name = target.attr
            elif (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Attribute)
                and isinstance(target.value.value, ast.Name)
                and target.value.value.id in hierarchy_names
                and target.value.attr in cls_all_attrs
            ):
                attr_name = target.value.attr
            if attr_name and (cls_node.name, attr_name) not in reset_class_attrs:
                hits.append((sub.lineno, attr_name, cls_all_attrs[attr_name]))
        elif not is_init_or_setup:
            targets, val = _extract_assign_targets_and_value(sub)
            if targets and val is not None:
                for t in targets:
                    if (
                        isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name)
                        and t.value.id in class_only_names
                        and t.attr in cls_all_attrs
                        and (cls_node.name, t.attr) not in reset_class_attrs
                    ):
                        hits.append((sub.lineno, t.attr, cls_all_attrs[t.attr]))

    hits.sort(key=lambda item: item[0])
    seen_attrs = set()
    results = []
    for lineno, attr_name, decl_ln in hits:
        if attr_name in seen_attrs:
            continue
        seen_attrs.add(attr_name)
        results.append({
            "side": side,
            "location": f"{file_rel}:{lineno}",
            "detail": (
                f"accesses/mutates shared class-level attribute "
                f"`{cls_node.name}.{attr_name}` (declared at `{file_rel}:{decl_ln}`)"
            ),
        })
    return results
