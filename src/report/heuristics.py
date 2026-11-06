"""Standard-library AST heuristics to disambiguate causes and locate fault side.

Static source inspection is used only as a heuristic hint to help separate
runtime-ambiguous candidates (such as unseeded_randomness vs race_conditions)
and indicate whether suspicious patterns appear in the test file (Test Side)
or imported project code (Code Side). Concurrency primitives or random calls
alone do not prove a root cause, and synchronized (joined) single threads or
locks/events alone are not treated as race indicators.
"""

import ast
from functools import lru_cache
from pathlib import Path
import re

RANDOM_MODULES = {"random", "secrets"}
RANDOM_FUNCTIONS = {
    "choice", "choices", "randint", "random", "randrange",
    "sample", "shuffle", "uniform", "getrandbits", "randbytes",
    "gauss", "normalvariate", "expovariate", "triangular",
    "betavariate", "gammavariate", "lognormvariate", "vonmisesvariate",
    "paretovariate", "weibullvariate",
    "randbelow", "randbits", "token_bytes", "token_hex", "token_urlsafe",
}
UUID_FUNCTIONS = {"uuid1", "uuid4"}
TIME_CLOCK_FUNCTIONS = {"time", "time_ns", "Now", "now", "utcnow"}
THREAD_PROCESS_MODULES = {"threading", "multiprocessing"}
THREAD_PROCESS_CLASSES = {"Thread", "Process", "Timer"}
POOL_EXECUTOR_CLASSES = {"ThreadPoolExecutor", "ProcessPoolExecutor", "Pool"}
ASYNC_CONCURRENCY_MODULES = {"asyncio", "concurrent"}
ASYNC_CONCURRENCY_FUNCTIONS = {
    "create_task", "gather", "submit", "ensure_future", "wait", "as_completed",
}
ORDER_SENSITIVE_STR_METHODS = {"join"}
PATH_JOIN_RECEIVERS = {"path", "posixpath", "ntpath", "os"}
CACHE_DECORATORS = {"lru_cache", "cache"}
SETUP_METHODS = {
    "setup_method", "teardown_method", "setup_class", "teardown_class", "setUp", "tearDown",
}


@lru_cache(maxsize=512)
def _parse_file_cached(resolved_path_str, mtime_ns, file_size):
    try:
        source = Path(resolved_path_str).read_text(encoding="utf-8")
        return ast.parse(source, filename=resolved_path_str)
    except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError):
        return None


def _parse_file(path):
    try:
        p = Path(path).resolve()
        st = p.stat()
    except OSError:
        return None
    return _parse_file_cached(str(p), st.st_mtime_ns, st.st_size)


def _rel_path(path, project_dir):
    try:
        return str(Path(path).resolve().relative_to(Path(project_dir).resolve()))
    except ValueError:
        return str(path)


def _resolve_module_file(project_dir, module_name, base_dir=None):
    if not module_name and not base_dir:
        return None
    parts = [p for p in (module_name or "").split(".") if p]
    project_root = Path(project_dir)
    search_bases = []
    if base_dir is not None:
        search_bases.append(Path(base_dir))
    search_bases.extend([project_root / "src", project_root])

    for base in search_bases:
        if parts:
            rel_py = Path(*parts).with_suffix(".py")
            rel_init = Path(*parts) / "__init__.py"
            for candidate in (base / rel_py, base / rel_init):
                if candidate.is_file():
                    return candidate
        else:
            init_cand = base / "__init__.py"
            if init_cand.is_file():
                return init_cand
    return None


def _split_test_id(test_id):
    if "::" not in test_id:
        return test_id, []
    file_part, node_part = test_id.split("::", 1)
    node_clean = node_part.split("[", 1)[0]
    parts = [p for p in node_clean.split("::") if p]
    return file_part, parts


def _find_target_node(tree, node_parts):
    scope = tree
    for part in node_parts:
        found = None
        for child in getattr(scope, "body", []):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == part:
                found = child
                break
        if found is None:
            return None
        scope = found
    return scope if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


def _find_enclosing_class(tree, node_parts):
    if len(node_parts) < 2:
        return None
    scope = tree
    for part in node_parts[:-1]:
        found = None
        for child in getattr(scope, "body", []):
            if isinstance(child, ast.ClassDef) and child.name == part:
                found = child
                break
        if found is None:
            return None
        scope = found
    return scope if isinstance(scope, ast.ClassDef) else None


def _collect_imports_and_globals(tree, project_dir, current_file=None):
    """Return (imported_symbols, module_globals) for a parsed module AST.

    imported_symbols maps local_name -> (file_path_or_None, remote_symbol_name, module_name).
    module_globals maps global_var_name -> (lineno, assigned_class_or_call_name).
    """
    imported = {}
    globals_map = {}
    mutated_via_global = {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Global)
        for name in node.names
    }
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                mod_file = _resolve_module_file(project_dir, alias.name)
                imported[local] = (mod_file, alias.name, alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod_name = node.module or ""
            rel_base = None
            if getattr(node, "level", 0) and current_file is not None:
                rel_base = Path(current_file).resolve().parent
                for _ in range(max(0, node.level - 1)):
                    rel_base = rel_base.parent
            mod_file = _resolve_module_file(project_dir, mod_name, base_dir=rel_base)
            for alias in node.names:
                local = alias.asname or alias.name
                sub_mod = f"{mod_name}.{alias.name}" if mod_name else alias.name
                sub_file = _resolve_module_file(project_dir, sub_mod, base_dir=rel_base)
                if sub_file is not None and sub_file.name != "__init__.py":
                    imported[local] = (sub_file, alias.name, sub_mod)
                else:
                    imported[local] = (mod_file, alias.name, mod_name)
        elif isinstance(node, ast.Assign):
            call_name = _call_head_name(node.value)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if isinstance(node.value, (ast.Constant, ast.Tuple)) and target.id not in mutated_via_global:
                        continue
                    globals_map[target.id] = (node.lineno, call_name)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            call_name = _call_head_name(node.value)
            if isinstance(node.target, ast.Name):
                if isinstance(node.value, (ast.Constant, ast.Tuple)) and node.target.id not in mutated_via_global:
                    continue
                globals_map[node.target.id] = (node.lineno, call_name)
    return imported, globals_map


def _call_head_name(node):
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            return f"{node.func.value.id}.{node.func.attr}"
    return None


def _find_top_def(tree, symbol_name):
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == symbol_name:
            return node
    return None


def _find_method_in_class(class_node, method_name):
    for node in getattr(class_node, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method_name:
            return node
    return None


def _dedup_preserve_order(items):
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _is_worker_spawn_call(call_node):
    """Return primitive name if call_node spawns a Thread, Process, Timer, or Executor, else None."""
    if not isinstance(call_node, ast.Call):
        return None
    if isinstance(call_node.func, ast.Attribute) and isinstance(call_node.func.value, ast.Name):
        mod = call_node.func.value.id
        attr = call_node.func.attr
        if mod in THREAD_PROCESS_MODULES and attr in THREAD_PROCESS_CLASSES:
            return f"{mod}.{attr}"
        if attr in POOL_EXECUTOR_CLASSES:
            return f"{mod}.{attr}"
    elif isinstance(call_node.func, ast.Name):
        if call_node.func.id in THREAD_PROCESS_CLASSES or call_node.func.id in POOL_EXECUTOR_CLASSES:
            return call_node.func.id
    return None


def _has_fixed_seed_call(func_node):
    """Return True if the function calls random.seed(<constant>) with a non-None constant."""
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        is_seed = (
            (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
             and node.func.value.id == "random" and node.func.attr == "seed")
            or (isinstance(node.func, ast.Name) and node.func.id == "seed")
        )
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


def _scan_function_signals(func_node, file_rel, side):
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
        "returns_set": False,
    }
    raw_random = []
    raw_race = []
    raw_sleep = []
    raw_join = []

    fixed_seed = _has_fixed_seed_call(func_node)

    # Check for @lru_cache / @cache decorators on Code Side functions
    for dec in getattr(func_node, "decorator_list", []):
        dec_name = None
        if isinstance(dec, ast.Name):
            dec_name = dec.id
        elif isinstance(dec, ast.Attribute):
            dec_name = dec.attr
        elif isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Name):
                dec_name = dec.func.id
            elif isinstance(dec.func, ast.Attribute):
                dec_name = dec.func.attr
        if dec_name in CACHE_DECORATORS:
            signals["shared_state_interference"].append({
                "side": side,
                "location": f"{file_rel}:{func_node.lineno}",
                "detail": f"caches state across calls via `@{dec_name}` on `{func_node.name}`",
            })

    # Collect variables on which `.join()` is called with no arguments (thread/process join)
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

    # Identify worker spawns inside loops (for/while), which spawn multiple concurrent workers
    spawns_in_loops = set()
    for node in ast.walk(func_node):
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and _is_worker_spawn_call(child) is not None:
                    spawns_in_loops.add(id(child))

    # Track Thread/Process calls that are assigned to variables so we know if they are joined
    assigned_worker_calls = {}
    total_worker_spawns = 0
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call) and _is_worker_spawn_call(node) is not None:
            total_worker_spawns += 1
        if isinstance(node, ast.Assign):
            prim = _is_worker_spawn_call(node.value)
            if prim is not None:
                target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                assigned_worker_calls[id(node.value)] = target_names

    for node in ast.walk(func_node):
        if isinstance(node, ast.Return) and isinstance(node.value, (ast.Set, ast.SetComp)):
            signals["returns_set"] = True
        elif isinstance(node, ast.Return) and isinstance(node.value, ast.Call):
            if isinstance(node.value.func, ast.Name) and node.value.func.id == "set":
                signals["returns_set"] = True

        if isinstance(node, ast.Call):
            spawn_prim = _is_worker_spawn_call(node)
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

            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                mod_or_obj = node.func.value.id
                attr = node.func.attr
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
                elif mod_or_obj in ASYNC_CONCURRENCY_MODULES and attr in ASYNC_CONCURRENCY_FUNCTIONS:
                    raw_race.append((node.lineno, f"`{mod_or_obj}.{attr}(...)`"))
                elif mod_or_obj == "time" and attr == "sleep":
                    raw_sleep.append((node.lineno, "`time.sleep`"))
            elif (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)
                and isinstance(node.func.value.value, ast.Name)
                and node.func.value.value.id in ("np", "numpy")
                and node.func.value.attr == "random"
            ):
                raw_random.append((node.lineno, f"{node.func.value.value.id}.random.{node.func.attr}(...)"))
            elif isinstance(node.func, ast.Name):
                fn = node.func.id
                if fn in RANDOM_FUNCTIONS and not fixed_seed:
                    raw_random.append((node.lineno, f"{fn}(...)"))
                elif fn in UUID_FUNCTIONS:
                    raw_random.append((node.lineno, f"{fn}()"))
                elif fn in ASYNC_CONCURRENCY_FUNCTIONS:
                    raw_race.append((node.lineno, f"`{fn}(...)`"))
                elif fn == "sleep":
                    raw_sleep.append((node.lineno, "`sleep`"))

            # Order-sensitive string join in test (requires an argument, e.g. ",".join(items),
            # excluding os.path.join and ",".join(sorted(...)))
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in ORDER_SENSITIVE_STR_METHODS
                and node.args
                and side == "Test Side"
            ):
                is_path_join = (
                    (isinstance(node.func.value, ast.Name) and node.func.value.id in PATH_JOIN_RECEIVERS)
                    or (
                        isinstance(node.func.value, ast.Attribute)
                        and node.func.value.attr in PATH_JOIN_RECEIVERS
                    )
                )
                if not is_path_join and len(node.args) == 1 and not _is_sorted_arg(node.args[0]):
                    raw_join.append(node.lineno)

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


def _infer_returned_class(func_node):
    """If a fixture returns or yields a Call like `Cart('user')` or a variable assigned one, return its head name."""
    local_assigns = {}
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            head = _call_head_name(node.value)
            if head:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        local_assigns[t.id] = head
    for node in ast.walk(func_node):
        if isinstance(node, (ast.Return, ast.Yield)) and node.value is not None:
            if isinstance(node.value, ast.Call):
                head = _call_head_name(node.value)
                if head:
                    return head
            elif isinstance(node.value, ast.Name) and node.value.id in local_assigns:
                return local_assigns[node.value.id]
    return None


def _find_fixture_defs(param_names, test_tree, test_file, project_path):
    """Find fixture definitions matching `param_names` in `test_tree` or ancestor `conftest.py` files."""
    if not param_names:
        return []
    wanted = set(param_names)
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

    seen_names = set()
    for f_path, f_tree in candidate_files:
        for node in getattr(f_tree, "body", []):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in wanted
                and node.name not in seen_names
                and _is_fixture_func(node)
            ):
                seen_names.add(node.name)
                found.append((node.name, f_path, f_tree, node))
    return found


def _scan_shared_globals_in_func(func_node, file_rel, module_globals, side):
    """Return list of shared_state_interference indicator dicts for module_globals accessed/mutated in func_node."""
    global_decls = {
        name
        for node in ast.walk(func_node)
        if isinstance(node, ast.Global)
        for name in node.names
    }
    local_assigned_names = {
        target.id
        for node in ast.walk(func_node)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id not in global_decls
    }
    shared_hits = []
    for node in ast.walk(func_node):
        if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.value, ast.Name):
            var_name = node.value.id
            if var_name in module_globals and var_name not in local_assigned_names:
                shared_hits.append((node.lineno, var_name))
        elif isinstance(node, ast.Global):
            for var_name in node.names:
                if var_name in module_globals:
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


def inspect_test_source(test_id, project_dir=None):
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

    test_rel = _rel_path(test_file, project_path)
    imported, module_globals = _collect_imports_and_globals(
        test_tree, project_path, current_file=test_file
    )

    indicators = {
        "unseeded_randomness": [],
        "race_conditions": [],
        "hash_order_sensitivity": [],
        "shared_state_interference": [],
    }

    # Scan the test function itself (Test Side)
    test_signals = _scan_function_signals(test_func, test_rel, "Test Side")
    for key in indicators:
        indicators[key].extend(test_signals[key])

    # Track local variable and self.<attr> assignments to classes: e.g., cart = Cart("user")
    local_vars = {}
    self_attrs = {}
    for g_name, (_, g_call) in module_globals.items():
        if g_call:
            local_vars[g_name] = g_call

    # Inspect enclosing test class (setup_method, class-level attributes) if test is a method
    enclosing_cls = _find_enclosing_class(test_tree, node_parts)
    if enclosing_cls is not None:
        cls_attrs = {}
        for item in getattr(enclosing_cls, "body", []):
            if isinstance(item, ast.Assign) and not isinstance(item.value, (ast.Constant, ast.Tuple)):
                call_head = _call_head_name(item.value)
                for t in item.targets:
                    if isinstance(t, ast.Name):
                        cls_attrs[t.id] = (item.lineno, call_head)
                        if call_head:
                            self_attrs[t.id] = call_head
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name in SETUP_METHODS:
                for sub in ast.walk(item):
                    if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Call):
                        head = _call_head_name(sub.value)
                        if head:
                            for t in sub.targets:
                                if (
                                    isinstance(t, ast.Attribute)
                                    and isinstance(t.value, ast.Name)
                                    and t.value.id == "self"
                                ):
                                    self_attrs[t.attr] = head

    # Inspect pytest fixtures referenced by test_func parameters
    param_names = [arg.arg for arg in test_func.args.args if arg.arg != "self"]
    fixture_defs = _find_fixture_defs(param_names, test_tree, test_file, project_path)
    for fix_name, fix_file, fix_tree, fix_node in fixture_defs:
        fix_rel = _rel_path(fix_file, project_path)
        fix_imported, fix_globals = _collect_imports_and_globals(
            fix_tree, project_path, current_file=fix_file
        )
        for k, v in fix_imported.items():
            imported.setdefault(k, v)
        fix_signals = _scan_function_signals(fix_node, fix_rel, "Test Side")
        for key in indicators:
            indicators[key].extend(fix_signals[key])
        indicators["shared_state_interference"].extend(
            _scan_shared_globals_in_func(fix_node, fix_rel, fix_globals, "Test Side")
        )
        ret_cls = _infer_returned_class(fix_node)
        if ret_cls:
            local_vars[fix_name] = ret_cls

    for node in ast.walk(test_func):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            head = _call_head_name(node.value)
            if head:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        local_vars[target.id] = head
                    elif (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        self_attrs[target.attr] = head

    # Track class attributes and modules explicitly reset by the test
    # (e.g. Cart._inventory["mouse"] = 10 or reset_stock("mouse", 100))
    reset_class_attrs = set()
    reset_modules = set()
    for node in ast.walk(test_func):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                    reset_class_attrs.add((t.value.id, t.attr))
                elif (
                    isinstance(t, ast.Subscript)
                    and isinstance(t.value, ast.Attribute)
                    and isinstance(t.value.value, ast.Name)
                ):
                    reset_class_attrs.add((t.value.value.id, t.value.attr))
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in imported:
                fn_name = node.func.id
                mod_file, remote_name, _ = imported[fn_name]
                if mod_file is not None and (
                    fn_name.startswith(("reset_", "clear_", "init_"))
                    or remote_name.startswith(("reset_", "clear_", "init_"))
                ):
                    reset_modules.add(str(mod_file))

    # Detect shared module-level state access/mutation in Test Side (sorted by line number)
    indicators["shared_state_interference"].extend(
        _scan_shared_globals_in_func(test_func, test_rel, module_globals, "Test Side")
    )

    # Follow calls from the test into imported project functions/methods (Code Side, up to 3 hops)
    visited_targets = set()

    def record_code_target(mod_file, func_node, desc, cls_node=None, depth=0):
        visit_key = (str(mod_file), desc)
        if visit_key in visited_targets or depth > 3:
            return
        visited_targets.add(visit_key)
        code_rel = _rel_path(mod_file, project_path)
        mod_tree = _parse_file(mod_file)
        mod_imported, mod_globals = (
            _collect_imports_and_globals(mod_tree, project_path, current_file=mod_file)
            if mod_tree is not None
            else ({}, {})
        )

        code_signals = _scan_function_signals(func_node, code_rel, "Code Side")
        indicators["unseeded_randomness"].extend(code_signals["unseeded_randomness"])
        indicators["race_conditions"].extend(code_signals["race_conditions"])
        indicators["shared_state_interference"].extend(code_signals["shared_state_interference"])

        # Code Side shared module-level state (only when func doesn't already have an unsynchronized
        # thread race and the module's state wasn't explicitly reset by the test)
        if (
            not code_signals["race_conditions"]
            and str(mod_file) not in reset_modules
            and not func_node.name.startswith(("reset_", "clear_", "init_"))
        ):
            indicators["shared_state_interference"].extend(
                _scan_shared_globals_in_func(func_node, code_rel, mod_globals, "Code Side")
            )
            # Also check class-level mutable attributes on cls_node if not reset by the test
            if cls_node is not None:
                cls_mutable_attrs = {}
                for item in getattr(cls_node, "body", []):
                    if isinstance(item, ast.Assign) and not isinstance(item.value, (ast.Constant, ast.Tuple)):
                        for t in item.targets:
                            if isinstance(t, ast.Name):
                                cls_mutable_attrs[t.id] = item.lineno
                if cls_mutable_attrs:
                    for sub in ast.walk(func_node):
                        if (
                            isinstance(sub, (ast.Attribute, ast.Subscript))
                            and isinstance(sub.value, ast.Attribute)
                            and isinstance(sub.value.value, ast.Name)
                            and sub.value.value.id in (cls_node.name, "self", "cls")
                            and sub.value.attr in cls_mutable_attrs
                            and (cls_node.name, sub.value.attr) not in reset_class_attrs
                        ):
                            decl_ln = cls_mutable_attrs[sub.value.attr]
                            indicators["shared_state_interference"].append({
                                "side": "Code Side",
                                "location": f"{code_rel}:{sub.lineno}",
                                "detail": (
                                    f"accesses/mutates shared class-level attribute "
                                    f"`{cls_node.name}.{sub.value.attr}` (declared at `{code_rel}:{decl_ln}`)"
                                ),
                            })
                            break

        if code_signals["returns_set"] and indicators["hash_order_sensitivity"]:
            first_hit = indicators["hash_order_sensitivity"][0]
            first_hit["detail"] = (
                f"unsorted `join(...)` on unordered `set` from "
                f"`{code_rel}:{func_node.lineno}` (`{desc}()`)"
            )

        # Follow up to 3 hops: self.<helper>(), same-module helper functions, and imported functions
        for sub in ast.walk(func_node):
            if not isinstance(sub, ast.Call):
                continue
            if (
                cls_node is not None
                and isinstance(sub.func, ast.Attribute)
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == "self"
            ):
                helper = _find_method_in_class(cls_node, sub.func.attr)
                if helper is not None:
                    record_code_target(
                        mod_file,
                        helper,
                        f"{cls_node.name}.{sub.func.attr}",
                        cls_node=cls_node,
                        depth=depth + 1,
                    )
            elif isinstance(sub.func, ast.Name) and mod_tree is not None:
                callee_name = sub.func.id
                local_def = _find_top_def(mod_tree, callee_name)
                if isinstance(local_def, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    record_code_target(mod_file, local_def, callee_name, cls_node=None, depth=depth + 1)
                elif callee_name in mod_imported:
                    ext_file, ext_sym, _ = mod_imported[callee_name]
                    if ext_file is not None:
                        ext_tree = _parse_file(ext_file)
                        if ext_tree is not None:
                            ext_def = _find_top_def(ext_tree, ext_sym)
                            if isinstance(ext_def, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                record_code_target(ext_file, ext_def, ext_sym, cls_node=None, depth=depth + 1)

    scan_nodes = [test_func] + [fix_node for _, _, _, fix_node in fixture_defs]
    for scope_node in scan_nodes:
        for node in ast.walk(scope_node):
            if not isinstance(node, ast.Call):
                continue

            if isinstance(node.func, ast.Name) and node.func.id in imported:
                mod_file, remote_name, _ = imported[node.func.id]
                if mod_file is not None:
                    mod_tree = _parse_file(mod_file)
                    if mod_tree is not None:
                        defn = _find_top_def(mod_tree, remote_name)
                        if isinstance(defn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            record_code_target(mod_file, defn, remote_name)
                        elif isinstance(defn, ast.ClassDef):
                            init_m = _find_method_in_class(defn, "__init__")
                            if init_m is not None:
                                record_code_target(
                                    mod_file, init_m, f"{remote_name}.__init__", cls_node=defn
                                )

            elif isinstance(node.func, ast.Attribute):
                receiver = None
                method_name = node.func.attr
                if isinstance(node.func.value, ast.Name):
                    receiver = node.func.value.id
                    class_symbol = local_vars.get(receiver, receiver)
                elif (
                    isinstance(node.func.value, ast.Attribute)
                    and isinstance(node.func.value.value, ast.Name)
                    and node.func.value.value.id == "self"
                ):
                    receiver = f"self.{node.func.value.attr}"
                    class_symbol = self_attrs.get(node.func.value.attr)
                elif isinstance(node.func.value, ast.Call):
                    class_symbol = _call_head_name(node.func.value)
                else:
                    class_symbol = None

                if class_symbol and class_symbol in imported:
                    mod_file, remote_symbol, _ = imported[class_symbol]
                    if mod_file is not None:
                        mod_tree = _parse_file(mod_file)
                        if mod_tree is not None:
                            defn = _find_top_def(mod_tree, remote_symbol)
                            if isinstance(defn, ast.ClassDef):
                                m_node = _find_method_in_class(defn, method_name)
                                if m_node is not None:
                                    record_code_target(
                                        mod_file, m_node, f"{remote_symbol}.{method_name}", cls_node=defn
                                    )
                            else:
                                top_fn = _find_top_def(mod_tree, method_name)
                                if isinstance(top_fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                    record_code_target(
                                        mod_file, top_fn, f"{remote_symbol}.{method_name}"
                                    )

    return {
        "available": True,
        "indicators": indicators,
    }


ALL_CAUSE_CATEGORIES = (
    "unseeded_randomness",
    "race_conditions",
    "hash_order_sensitivity",
    "shared_state_interference",
)


def disambiguate_causes(suspected_causes, source_info):
    """Combine runtime suspected_causes with AST heuristic hints.

    When source inspection is unavailable or no matching AST indicator is found,
    fault_side remains 'Undetermined' (never guessed from a typical default).
    When AST indicators exist only for categories other than the runtime
    candidates, they are preserved in `conflicting_hints` without altering
    `resolved_candidates`.
    """
    if not suspected_causes:
        return {
            "resolved_candidates": [],
            "fault_side": "N/A",
            "heuristic_notes": [],
            "compact_hints": [],
            "conflicting_hints": [],
            "basis": "—",
            "disambiguated": False,
        }

    indicators = (source_info or {}).get("indicators", {})
    resolved = []
    sides = []
    notes = []
    compact_hints = []
    conflicting_hints = []
    disambiguated = False
    has_ambiguous = False
    all_runtime_cands = []

    for group in suspected_causes:
        candidates = list(group.get("candidates", []))
        all_runtime_cands.extend(candidates)
        if len(candidates) > 1:
            matched_cands = [c for c in candidates if indicators.get(c)]
            if 0 < len(matched_cands) < len(candidates):
                resolved.extend(matched_cands)
                disambiguated = True
                for cat in matched_cands:
                    for hit in indicators.get(cat, []):
                        sides.append(hit["side"])
                        notes.append(
                            f"Heuristic hint (`{cat}`, {hit['side']}): "
                            f"`{hit['location']}` {hit['detail']}."
                        )
                        compact_hints.append(f"`{hit['location']}` {hit['detail']}")
            else:
                has_ambiguous = True
                resolved.extend(candidates)
                for cat in candidates:
                    for hit in indicators.get(cat, []):
                        sides.append(hit["side"])
                        notes.append(
                            f"Heuristic hint (`{cat}`, {hit['side']}): "
                            f"`{hit['location']}` {hit['detail']}."
                        )
                        compact_hints.append(f"`{hit['location']}` {hit['detail']}")
        else:
            if group.get("status") == "ambiguous":
                has_ambiguous = True
            for cand in candidates:
                resolved.append(cand)
                hits = indicators.get(cand, [])
                for hit in hits:
                    sides.append(hit["side"])
                    notes.append(
                        f"Heuristic hint (`{cand}`, {hit['side']}): "
                        f"`{hit['location']}` {hit['detail']}."
                    )
                    compact_hints.append(f"`{hit['location']}` {hit['detail']}")

    dedup_runtime = _dedup_preserve_order(all_runtime_cands)
    runtime_cands_set = set(dedup_runtime)
    runtime_str = " / ".join(f"`{c}`" for c in dedup_runtime)

    for other_cat in ALL_CAUSE_CATEGORIES:
        if other_cat in runtime_cands_set:
            continue
        for hit in indicators.get(other_cat, []):
            conflict_msg = (
                f"Conflicting hint: `{hit['location']}` {hit['detail']} "
                f"(`{other_cat}`?, {hit['side']}), runtime suggests {runtime_str}"
            )
            conflicting_hints.append(conflict_msg)
            notes.append(f"{conflict_msg}.")

    unique_sides = _dedup_preserve_order(sides)
    fault_side = " / ".join(unique_sides) if unique_sides else "Undetermined"

    if disambiguated:
        basis = "AST Hint *"
    elif has_ambiguous:
        basis = "Ambiguous"
    elif compact_hints:
        basis = "Runtime+AST"
    elif conflicting_hints:
        basis = "Runtime (AST conflict)"
    else:
        basis = "Runtime"

    return {
        "resolved_candidates": _dedup_preserve_order(resolved),
        "fault_side": fault_side,
        "heuristic_notes": notes,
        "compact_hints": compact_hints,
        "conflicting_hints": conflicting_hints,
        "basis": basis,
        "disambiguated": disambiguated,
    }
