"""Standard-library AST heuristics to disambiguate causes and locate fault side.

Static source inspection is used only as a heuristic hint to help separate
runtime-ambiguous candidates (such as unseeded_randomness vs race_conditions)
and indicate whether suspicious patterns appear in the test file (Test Side)
or imported project code (Code Side). Concurrency primitives or random calls
alone do not prove a root cause, and synchronized (joined) single threads or
locks/events alone are not treated as race indicators.
"""

import ast
from collections import deque
from functools import lru_cache
from pathlib import Path
import re

MAX_CALL_DEPTH = 3

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
ASYNC_CONCURRENCY_MODULES = {"asyncio", "concurrent", "concurrent.futures"}
ASYNC_CONCURRENCY_FUNCTIONS = {
    "create_task", "gather", "submit", "ensure_future", "wait", "as_completed",
}
ORDER_SENSITIVE_STR_METHODS = {"join"}
PATH_JOIN_RECEIVERS = {"path", "posixpath", "ntpath", "os", "os.path"}
CACHE_DECORATORS = {"lru_cache", "cache"}
SETUP_METHODS = {
    "setup_method", "teardown_method", "setup_class", "teardown_class", "setUp", "tearDown",
}
TMP_FIXTURE_NAMES = {"tmp_path", "tmp_path_factory", "tmpdir", "tmpdir_factory"}
TEMPFILE_SAFE_MODULES = {"tempfile"}
TEMPFILE_SAFE_CALLS = {
    "NamedTemporaryFile", "TemporaryDirectory", "SpooledTemporaryFile",
    "TemporaryFile", "mkstemp", "mkdtemp", "gettempdir",
    "StringIO", "BytesIO",
}
PATH_MUTATION_METHODS = {
    "write_text", "write_bytes", "mkdir", "unlink", "rmdir", "touch", "rename", "replace",
}
OS_FS_MUTATION_FUNCS = {
    "remove", "unlink", "mkdir", "makedirs", "rmdir", "rename", "replace",
}
SHUTIL_MUTATION_FUNCS = {"rmtree", "copy", "copy2", "copytree", "move"}
PORT_BIND_SERVER_CLASSES = {
    "HTTPServer", "ThreadingHTTPServer", "TCPServer", "UDPServer",
    "ThreadingTCPServer", "ThreadingUDPServer", "WSGIServer",
}
PORT_BIND_FUNCS = {"start_server", "create_server", "make_server"}
NET_MOCK_NAMES = {
    "responses", "respx", "respx_mock",
    "httpretty", "requests_mock", "aioresponses", "vcr",
}
NET_MOCK_TARGET_TOKENS = {
    "requests", "httpx", "urllib", "aiohttp", "socket", "smtplib",
    "urlopen", "urlretrieve", "Session", "Client", "AsyncClient",
    "ClientSession", "create_connection", "SMTP", "SMTP_SSL",
}
REQUESTS_HTTPX_METHODS = {
    "get", "post", "put", "delete", "patch", "head", "options", "request",
    "stream", "Session", "Client", "AsyncClient",
}
URLLIB_REQUEST_FUNCS = {"urlopen", "urlretrieve"}
AIOHTTP_TARGETS = {"ClientSession", "request"}
SOCKET_NET_FUNCS = {"create_connection"}
SMTPLIB_TARGETS = {"SMTP", "SMTP_SSL"}
FLOAT_SAFE_FUNCS = {
    "approx", "isclose", "allclose", "round", "Decimal", "assertAlmostEqual",
}
MATH_FLOAT_FUNCS = {
    "sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
    "exp", "log", "log10", "log2", "pow", "fsum", "hypot", "radians", "degrees",
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
        return Path(path).resolve().relative_to(Path(project_dir).resolve()).as_posix()
    except ValueError:
        return Path(path).as_posix()


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
                target = alias.name if alias.asname else alias.name.split(".")[0]
                mod_file = _resolve_module_file(project_dir, alias.name)
                imported[local] = (mod_file, target, alias.name)
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


def _call_head_name(node, imported=None):
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            fn = node.func.id
            if imported and fn in imported:
                entry = imported[fn]
                return entry[1] if isinstance(entry, tuple) else entry
            return fn
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            raw_obj = node.func.value.id
            if imported and raw_obj in imported:
                entry = imported[raw_obj]
                raw_obj = entry[1] if isinstance(entry, tuple) else entry
            return f"{raw_obj}.{node.func.attr}"
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
        ):
            raw_root = node.func.value.value.id
            if imported and raw_root in imported:
                entry = imported[raw_root]
                raw_root = entry[1] if isinstance(entry, tuple) else entry
            return f"{raw_root}.{node.func.value.attr}.{node.func.attr}"
    return None


def _resolve_root_name(name, imported):
    if not imported or name not in imported:
        return name
    entry = imported[name]
    if isinstance(entry, tuple):
        _, remote_name, mod_name = entry
        if mod_name and remote_name and mod_name != remote_name and mod_name in (
            "urllib", "os", "concurrent", "http", "unittest",
        ):
            return f"{mod_name}.{remote_name}"
        return remote_name or mod_name or name
    if isinstance(entry, str):
        return entry
    return name


def _resolve_call_parts(func_expr, imported=None):
    """Normalize a Call's func AST node into (mod_or_obj, attr, bare_fn) using `imported`."""
    if isinstance(func_expr, ast.Name):
        fn = func_expr.id
        if imported and fn in imported:
            entry = imported[fn]
            if isinstance(entry, tuple):
                _, remote_name, mod_name = entry
                if mod_name and remote_name and mod_name != remote_name:
                    return mod_name, remote_name, remote_name
                return None, None, (remote_name or fn)
            if isinstance(entry, str):
                if "." in entry:
                    m, a = entry.rsplit(".", 1)
                    return m, a, a
                return None, None, entry
        return None, None, fn

    if isinstance(func_expr, ast.Attribute) and isinstance(func_expr.value, ast.Name):
        raw_obj = func_expr.value.id
        attr = func_expr.attr
        resolved_obj = _resolve_root_name(raw_obj, imported)
        return resolved_obj, attr, None

    if (
        isinstance(func_expr, ast.Attribute)
        and isinstance(func_expr.value, ast.Attribute)
        and isinstance(func_expr.value.value, ast.Name)
    ):
        raw_root = func_expr.value.value.id
        mid = func_expr.value.attr
        attr = func_expr.attr
        root = _resolve_root_name(raw_root, imported)
        if root in ("np", "numpy") and mid == "random":
            return f"{root}.random", attr, None
        if root == "datetime" and mid in ("datetime", "date"):
            return mid, attr, None
        if root == "urllib" and mid == "request":
            return "urllib.request", attr, None
        if root == "os" and mid == "path":
            return "os.path", attr, None
        return f"{root}.{mid}", attr, None

    return None, None, None


def _collect_local_imports(func_node, base_imported=None, project_dir=None, current_file=None):
    """Merge module-level `base_imported` with any `import` / `from ... import` inside `func_node`."""
    merged = dict(base_imported) if base_imported else {}
    for node in ast.walk(func_node):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                target = alias.name if alias.asname else alias.name.split(".")[0]
                mod_file = _resolve_module_file(project_dir, alias.name) if project_dir else None
                merged[local] = (mod_file, target, alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod_name = node.module or ""
            rel_base = None
            if project_dir and getattr(node, "level", 0) and current_file is not None:
                rel_base = Path(current_file).resolve().parent
                for _ in range(max(0, node.level - 1)):
                    rel_base = rel_base.parent
            mod_file = (
                _resolve_module_file(project_dir, mod_name, base_dir=rel_base)
                if project_dir
                else None
            )
            for alias in node.names:
                local = alias.asname or alias.name
                if project_dir:
                    sub_mod = f"{mod_name}.{alias.name}" if mod_name else alias.name
                    sub_file = _resolve_module_file(project_dir, sub_mod, base_dir=rel_base)
                    if sub_file is not None and sub_file.name != "__init__.py":
                        merged[local] = (sub_file, alias.name, sub_mod)
                        continue
                merged[local] = (mod_file, alias.name, mod_name)
    return merged


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


def _annotation_is_float(ann_node):
    """Return True if a parameter/return/variable type annotation denotes `float`."""
    if ann_node is None:
        return False
    for sub in ast.walk(ann_node):
        if isinstance(sub, ast.Name) and sub.id == "float":
            return True
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and "float" in sub.value:
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
            if any(
                isinstance(sub, ast.Constant) and isinstance(sub.value, float)
                for sub in ast.walk(argvalues_node)
            ):
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
                        if idx < len(param_names) and any(
                            isinstance(sub, ast.Constant) and isinstance(sub.value, float)
                            for sub in ast.walk(cell)
                        ):
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
                if d_node is not None and any(
                    isinstance(sub, ast.Constant) and isinstance(sub.value, float)
                    for sub in ast.walk(d_node)
                ):
                    float_vars.add(pos_args[offset + idx].arg)
        for idx, d_node in enumerate(getattr(args_obj, "kw_defaults", [])):
            if idx < len(kw_args) and d_node is not None and any(
                isinstance(sub, ast.Constant) and isinstance(sub.value, float)
                for sub in ast.walk(d_node)
            ):
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


def _expr_involves_float_calc(expr, local_exprs, float_vars=None, imported=None):
    """Return True if expr or its local variable inputs involve float literals, float vars, arithmetic, sum(), or float math."""
    fvars = float_vars or set()
    for sub in _expand_expr_nodes(expr, local_exprs):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, float):
            return True
        if isinstance(sub, ast.BinOp):
            if isinstance(sub.op, ast.Div) and not _is_path_div(sub, local_exprs, imported=imported):
                return True
            if isinstance(sub.op, (ast.Add, ast.Sub, ast.Mult, ast.Pow, ast.Mod, ast.FloorDiv)):
                for op_sub in _expand_expr_nodes(sub, local_exprs):
                    if isinstance(op_sub, ast.Constant) and isinstance(op_sub.value, float):
                        return True
                    if isinstance(op_sub, ast.Name) and op_sub.id in fvars:
                        return True
        if isinstance(sub, ast.Call):
            mod_or_obj, attr, bare_fn = _resolve_call_parts(sub.func, imported)
            if bare_fn in ("sum", "float", "fsum") or (
                bare_fn in (MATH_FLOAT_FUNCS | {"mean", "fmean", "stdev", "var"})
                and mod_or_obj in ("math", "np", "numpy", "statistics")
            ):
                return True
            if (
                mod_or_obj in ("math", "np", "numpy", "statistics")
                and attr in (MATH_FLOAT_FUNCS | {"mean", "fmean", "stdev", "var"})
            ):
                return True
    return False


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

    # Collect local variable assignments for temp-path, hardcoded-path, port-int, and float tracking
    safe_tmp_vars = {
        arg.arg
        for arg in (
            list(getattr(getattr(func_node, "args", None), "posonlyargs", []))
            + list(getattr(getattr(func_node, "args", None), "args", []))
            + list(getattr(getattr(func_node, "args", None), "kwonlyargs", []))
        )
        if arg.arg in TMP_FIXTURE_NAMES
    }
    if initial_safe_tmp_vars:
        safe_tmp_vars.update(initial_safe_tmp_vars)
    hardcoded_path_vars = set(initial_hardcoded_vars or ())
    local_int_consts = {}
    local_exprs = {}
    float_vars = _extract_func_float_vars(
        func_node, initial_float_vars=initial_float_vars, extra_decorators=extra_decorators
    )

    for node in ast.walk(func_node):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    local_exprs[target.id] = node.value
                    if (
                        isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, int)
                        and not isinstance(node.value.value, bool)
                    ):
                        local_int_consts[target.id] = node.value.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None and isinstance(node.target, ast.Name):
            local_exprs[node.target.id] = node.value

    for _ in range(2):
        for node in ast.walk(func_node):
            if isinstance(node, ast.With):
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
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        if _is_safe_temp_expr(
                            node.value, safe_tmp_vars, local_exprs=local_exprs, imported=eff_imported
                        ):
                            safe_tmp_vars.add(target.id)
                            hardcoded_path_vars.discard(target.id)
                        elif _is_hardcoded_path_expr(
                            node.value,
                            safe_tmp_vars,
                            hardcoded_path_vars,
                            local_exprs=local_exprs,
                            imported=eff_imported,
                        ):
                            hardcoded_path_vars.add(target.id)
                        if _expr_involves_float_calc(
                            node.value, local_exprs, float_vars=float_vars, imported=eff_imported
                        ) or (isinstance(node.value, ast.Name) and node.value.id in float_vars):
                            float_vars.add(target.id)

    # Check for @lru_cache / @cache decorators on Code Side functions
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
                if isinstance(child, ast.Call) and _is_worker_spawn_call(child, imported=eff_imported) is not None:
                    spawns_in_loops.add(id(child))

    # Track Thread/Process calls that are assigned to variables so we know if they are joined
    assigned_worker_calls = {}
    total_worker_spawns = 0
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call) and _is_worker_spawn_call(node, imported=eff_imported) is not None:
            total_worker_spawns += 1
        if isinstance(node, ast.Assign):
            prim = _is_worker_spawn_call(node.value, imported=eff_imported)
            if prim is not None:
                target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                assigned_worker_calls[id(node.value)] = target_names

    for node in ast.walk(func_node):
        if isinstance(node, ast.Return) and node.value is not None:
            if isinstance(node.value, (ast.Set, ast.SetComp)):
                signals["returns_set"] = True
            elif isinstance(node.value, ast.Call):
                if isinstance(node.value.func, ast.Name) and node.value.func.id == "set":
                    signals["returns_set"] = True
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
                    signals["returns_float"] = True

        if side == "Test Side" and isinstance(node, ast.Assert):
            if not _expr_has_float_safe_guard(node.test, local_exprs, imported=eff_imported):
                for sub in ast.walk(node.test):
                    if isinstance(sub, ast.Compare) and any(
                        isinstance(op, (ast.Eq, ast.NotEq)) for op in sub.ops
                    ):
                        if _expr_involves_float_calc(
                            sub, local_exprs, float_vars=float_vars, imported=eff_imported
                        ):
                            raw_float_asserts.append(node.lineno)
                            break

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

            # Order-sensitive string join in test (requires an argument, e.g. ",".join(items),
            # excluding os.path.join and ",".join(sorted(...)))
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

            # Test Side unittest self.assertEqual / self.assertNotEqual float precision check
            if (
                side == "Test Side"
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("assertEqual", "assertNotEqual")
                and len(node.args) >= 2
                and not _expr_has_float_safe_guard(node, local_exprs, imported=eff_imported)
            ):
                if any(
                    _expr_involves_float_calc(
                        a, local_exprs, float_vars=float_vars, imported=eff_imported
                    )
                    for a in node.args[:2]
                ):
                    raw_float_asserts.append(node.lineno)

            # Resource leak: call-level check on hardcoded file/database mutations
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

            # Resource leak: hardcoded non-zero network port bindings
            if isinstance(node.func, ast.Attribute) and node.func.attr == "bind" and node.args:
                addr = node.args[0]
                if isinstance(addr, (ast.Tuple, ast.List)) and len(addr.elts) >= 2:
                    port_val = _extract_nonzero_int_const(addr.elts[1], local_int_consts)
                    if port_val is not None:
                        raw_res_port.append((node.lineno, f".bind((..., {port_val}))"))
            else:
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

            # External I/O: unmocked network requests
            if not local_has_net_mock:
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


def _infer_returned_class(func_node, imported=None):
    """If a function/fixture returns or yields a Call like `Cart('user')` or a variable assigned one, return its head name."""
    local_assigns = {}
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            head = _call_head_name(node.value, imported=imported)
            if head:
                for t in node.targets:
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
    local_exprs = {
        t.id: n.value
        for n in ast.walk(func_node)
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    for n in ast.walk(func_node):
        if isinstance(n, (ast.Return, ast.Yield)) and n.value is not None:
            if _is_safe_temp_expr(n.value, safe_vars, local_exprs=local_exprs, imported=imported):
                return True
    return False


def _find_fixture_defs(param_names, test_tree, test_file, project_path, enclosing_cls=None):
    """Find fixture definitions matching `param_names` or `autouse=True` (plus upstream fixture deps up to 3 hops)
    in `test_tree`, `enclosing_cls`, or ancestor `conftest.py` files."""
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
        for node in getattr(enclosing_cls, "body", []):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_fixture_func(node):
                all_fixture_nodes.append((node.name, test_file, test_tree, node))
    for f_path, f_tree in candidate_files:
        for node in getattr(f_tree, "body", []):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_fixture_func(node):
                all_fixture_nodes.append((node.name, f_path, f_tree, node))

    seen_names = set()
    frontier = set()
    for fix_name, f_path, f_tree, node in all_fixture_nodes:
        if fix_name not in seen_names and (fix_name in wanted or _is_autouse_fixture(node)):
            seen_names.add(fix_name)
            found.append((fix_name, f_path, f_tree, node))
            for arg in (
                list(getattr(node.args, "posonlyargs", []))
                + list(getattr(node.args, "args", []))
                + list(getattr(node.args, "kwonlyargs", []))
            ):
                if arg.arg != "self" and arg.arg not in seen_names:
                    frontier.add(arg.arg)

    for _ in range(MAX_CALL_DEPTH):
        if not frontier:
            break
        next_frontier = set()
        for fix_name, f_path, f_tree, node in all_fixture_nodes:
            if fix_name in frontier and fix_name not in seen_names:
                seen_names.add(fix_name)
                found.append((fix_name, f_path, f_tree, node))
                for arg in (
                    list(getattr(node.args, "posonlyargs", []))
                    + list(getattr(node.args, "args", []))
                    + list(getattr(node.args, "kwonlyargs", []))
                ):
                    if arg.arg != "self" and arg.arg not in seen_names:
                        next_frontier.add(arg.arg)
        frontier = next_frontier

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


def _extract_caller_context(func_node, imported, initial_safe_tmp=None, initial_hardcoded=None, initial_float=None, extra_decorators=None):
    """Extract a function's effective safe_tmp_vars, hardcoded_path_vars, float_vars, and local_exprs."""
    eff_imported = _collect_local_imports(func_node, imported)
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
    hardcoded_vars = set(initial_hardcoded or ())
    float_vars = _extract_func_float_vars(
        func_node, initial_float_vars=initial_float, extra_decorators=extra_decorators
    )
    local_exprs = {}
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    local_exprs[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None and isinstance(node.target, ast.Name):
            local_exprs[node.target.id] = node.value

    for _ in range(2):
        for node in ast.walk(func_node):
            if isinstance(node, ast.With):
                for item in node.items:
                    if _is_safe_temp_expr(
                        item.context_expr, safe_tmp_vars, local_exprs=local_exprs, imported=eff_imported
                    ):
                        if isinstance(item.optional_vars, ast.Name):
                            safe_tmp_vars.add(item.optional_vars.id)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        if _is_safe_temp_expr(
                            node.value, safe_tmp_vars, local_exprs=local_exprs, imported=eff_imported
                        ):
                            safe_tmp_vars.add(target.id)
                            hardcoded_vars.discard(target.id)
                        elif _is_hardcoded_path_expr(
                            node.value, safe_tmp_vars, hardcoded_vars, local_exprs=local_exprs, imported=eff_imported
                        ):
                            hardcoded_vars.add(target.id)
                        if _expr_involves_float_calc(
                            node.value, local_exprs, float_vars=float_vars, imported=eff_imported
                        ) or (isinstance(node.value, ast.Name) and node.value.id in float_vars):
                            float_vars.add(target.id)
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

    test_rel = _rel_path(test_file, project_path)
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

    # Track local variable and self.<attr> assignments to classes: e.g., cart = Cart("user")
    base_local_vars = {}
    base_self_attrs = {}
    for g_name, (_, g_call) in module_globals.items():
        if g_call:
            base_local_vars[g_name] = g_call

    setup_nodes = []
    if enclosing_cls is not None:
        for item in getattr(enclosing_cls, "body", []):
            if isinstance(item, ast.Assign) and not isinstance(item.value, (ast.Constant, ast.Tuple)):
                call_head = _call_head_name(item.value)
                for t in item.targets:
                    if isinstance(t, ast.Name) and call_head:
                        base_self_attrs[t.id] = call_head
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name in SETUP_METHODS:
                setup_nodes.append(item)
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
                                    base_self_attrs[t.attr] = head

    # Pre-scan fixtures for imports, returned classes, safe temp paths, and network mocks
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

    # Also check same-file helper functions in test_tree for network mocks or returned classes
    for node in getattr(test_tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ret_cls = _infer_returned_class(node)
            if ret_cls:
                # Record factory function -> returned class
                base_local_vars[f"__factory__:{node.name}"] = ret_cls

    # Track class attributes and modules explicitly reset by test/setup/fixtures
    reset_class_attrs = set()
    reset_modules = set()
    for root_node in all_test_func_nodes + setup_nodes:
        for node in ast.walk(root_node):
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
                    entry = imported[fn_name]
                    if isinstance(entry, tuple):
                        mod_file, remote_name, _ = entry
                        if mod_file is not None and (
                            fn_name.startswith(("reset_", "clear_", "init_"))
                            or (remote_name and remote_name.startswith(("reset_", "clear_", "init_")))
                        ):
                            reset_modules.add(str(mod_file))

    # Build BFS queue of (file_path, tree, func_node, side, desc, cls_node, depth, via_helper, safe_tmp, hardcoded, float_vars)
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
        visit_key = (file_key, t_desc, t_side)
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

    # Seed BFS queue with depth-0 Test Side entry points: test_func, setup_methods, and fixtures
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
                    cls_mutable_attrs = {}
                    for item in getattr(cur_cls, "body", []):
                        if isinstance(item, ast.Assign) and not isinstance(
                            item.value, (ast.Constant, ast.Tuple)
                        ):
                            for t in item.targets:
                                if isinstance(t, ast.Name):
                                    cls_mutable_attrs[t.id] = item.lineno
                    if cls_mutable_attrs:
                        for sub in ast.walk(cur_func):
                            if (
                                isinstance(sub, (ast.Attribute, ast.Subscript))
                                and isinstance(sub.value, ast.Attribute)
                                and isinstance(sub.value.value, ast.Name)
                                and sub.value.value.id in (cur_cls.name, "self", "cls")
                                and sub.value.attr in cls_mutable_attrs
                                and (cur_cls.name, sub.value.attr) not in reset_class_attrs
                            ):
                                decl_ln = cls_mutable_attrs[sub.value.attr]
                                indicators["shared_state_interference"].append({
                                    "side": "Code Side",
                                    "location": f"{cur_rel}:{sub.lineno}",
                                    "detail": (
                                        f"accesses/mutates shared class-level attribute "
                                        f"`{cur_cls.name}.{sub.value.attr}` (declared at `{cur_rel}:{decl_ln}`)"
                                    ),
                                })
                                break

            if cur_signals["returns_set"]:
                set_returning_callees.append((cur_rel, cur_func, cur_desc))
            if cur_signals["returns_float"]:
                float_returning_callees.append((cur_rel, cur_func, cur_desc))

        if cur_depth >= MAX_CALL_DEPTH:
            continue

        # Discover callees from cur_func for the next BFS hop
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
            for item in getattr(cur_cls, "body", []):
                if isinstance(item, ast.Assign) and not isinstance(item.value, (ast.Constant, ast.Tuple)):
                    call_head = _call_head_name(item.value)
                    for t in item.targets:
                        if isinstance(t, ast.Name) and call_head:
                            self_attrs.setdefault(t.id, call_head)
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
                                        self_attrs.setdefault(t.attr, head)

        for node in ast.walk(cur_func):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                head = _call_head_name(node.value)
                if head:
                    resolved_cls = local_vars.get(f"__factory__:{head}", head)
                    for target in node.targets:
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
            key = (f_key, c_desc, c_side)
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

        for sub in ast.walk(cur_func):
            if not isinstance(sub, ast.Call):
                continue

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
                    init_m = _find_method_in_class(local_def, "__init__")
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
                                    init_m = _find_method_in_class(defn, "__init__")
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
                    helper_m = _find_method_in_class(cur_cls, method_name)
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

                # 1. Check same-file class definition
                local_cls = _find_top_def(cur_tree, class_symbol)
                if isinstance(local_cls, ast.ClassDef):
                    m_node = _find_method_in_class(local_cls, method_name)
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

                # 2. Check imported class or module
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
                            m_node = _find_method_in_class(defn, method_name)
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
                                init_m = _find_method_in_class(top_def, "__init__")
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

    # Post-pass 1: enrich hash_order_sensitivity when a callee returns an unordered set
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

    # Post-pass 2: link float-returning callees to strict equality assertions on Test Side
    if float_returning_callees and not indicators["float_precision"]:
        for code_rel, func_node, desc in float_returning_callees:
            leaf_sym = desc.split(".")[-1]
            for ts_file, ts_rel, ts_func, ts_via, ts_imported in scanned_test_side_funcs:
                ts_local_exprs = {
                    t.id: n.value
                    for n in ast.walk(ts_func)
                    if isinstance(n, ast.Assign)
                    for t in n.targets
                    if isinstance(t, ast.Name)
                }
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

    return {
        "available": True,
        "indicators": indicators,
    }


ALL_CAUSE_CATEGORIES = (
    "unseeded_randomness",
    "race_conditions",
    "hash_order_sensitivity",
    "shared_state_interference",
    "timezone_sensitivity",
    "resource_leak",
    "external_io",
    "float_precision",
)

_AST_CATEGORY_REASONS = {
    "resource_leak": "Outcomes vary across runs or test orders due to unisolated file or port resources.",
    "external_io": "Outcomes vary under repeated runs due to unmocked external network I/O.",
    "float_precision": "Outcomes vary across hash seeds or repeated runs due to strict floating-point equality assertions.",
}


def _with_polluter_suffix(new_base_reason, original_reason):
    if " Suspected polluting predecessor(s): " in (original_reason or ""):
        _, p_part = original_reason.split(" Suspected polluting predecessor(s): ", 1)
        return f"{new_base_reason} Suspected polluting predecessor(s): {p_part}"
    return new_base_reason


def _is_weak_uuid_only(rand_hits):
    """Return True if all unseeded_randomness hits are solely uuid1/uuid4 calls (e.g., request trace IDs)."""
    if not rand_hits:
        return False
    for hit in rand_hits:
        detail = hit.get("detail", "")
        head_part = detail.split(" without a fixed seed")[0]
        calls = re.findall(r"`([^`]+)`", head_part)
        if not calls or not all(c in ("uuid.uuid4()", "uuid.uuid1()", "uuid4()", "uuid1()") for c in calls):
            return False
    return True


def _is_weak_sleep_only(race_hits):
    """Return True if all race_conditions hits are solely standalone sleep calls without worker threads/tasks."""
    if not race_hits:
        return False
    for hit in race_hits:
        detail = hit.get("detail", "")
        head_part = detail.split(" (via ")[0]
        calls = re.findall(r"`([^`]+)`", head_part)
        if not calls or not all(c in ("time.sleep", "sleep") for c in calls):
            return False
    return True


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
            "runtime_candidates": [],
            "fault_side": "N/A",
            "heuristic_notes": [],
            "compact_hints": [],
            "conflicting_hints": [],
            "basis": "—",
            "resolution": "none",
            "disambiguated": False,
            "refined": False,
        }

    indicators = (source_info or {}).get("indicators", {})
    resolved = []
    sides = []
    notes = []
    compact_hints = []
    conflicting_hints = []
    disambiguated = False
    refined = False
    has_ambiguous = False
    all_runtime_cands = []
    has_strong_ext_io = bool(indicators.get("external_io"))

    for group in suspected_causes:
        candidates = list(group.get("runtime_candidates") or group.get("candidates", []))
        orig_status = group.get("runtime_status") or group.get(
            "status", "ambiguous" if len(candidates) > 1 else "suspected"
        )
        orig_reason = group.get("runtime_reason") or group.get("reason", "")
        all_runtime_cands.extend(candidates)

        if len(candidates) > 1:
            matched_cands = []
            for c in candidates:
                c_hits = indicators.get(c, [])
                if not c_hits:
                    continue
                if has_strong_ext_io and c == "unseeded_randomness" and _is_weak_uuid_only(c_hits):
                    continue
                if has_strong_ext_io and c == "race_conditions" and _is_weak_sleep_only(c_hits):
                    continue
                matched_cands.append(c)

            if 0 < len(matched_cands) < len(candidates):
                resolved.extend(matched_cands)
                disambiguated = True
                group["runtime_candidates"] = list(candidates)
                group["runtime_status"] = orig_status
                group.setdefault("runtime_reason", orig_reason)
                group["candidates"] = list(matched_cands)
                group["status"] = "ambiguous" if len(matched_cands) > 1 else "suspected"
                group["resolution"] = "disambiguated"
                group["disambiguated"] = True
                group["refined"] = False
                group["reason"] = orig_reason
                for cat in matched_cands:
                    for hit in indicators.get(cat, []):
                        sides.append(hit["side"])
                        notes.append(
                            f"Heuristic hint (`{cat}`, {hit['side']}): "
                            f"`{hit['location']}` {hit['detail']}."
                        )
                        compact_hints.append(f"`{hit['location']}` {hit['detail']}")
            elif len(matched_cands) == 0:
                ext_matched = [
                    c for c in ("external_io", "resource_leak", "float_precision")
                    if indicators.get(c)
                ]
                if ext_matched:
                    chosen = [ext_matched[0]]
                    resolved.extend(chosen)
                    disambiguated = True
                    group["runtime_candidates"] = list(candidates)
                    group["runtime_status"] = orig_status
                    group.setdefault("runtime_reason", orig_reason)
                    group["candidates"] = list(chosen)
                    group["status"] = "suspected"
                    group["resolution"] = "disambiguated"
                    group["disambiguated"] = True
                    group["refined"] = False
                    group["reason"] = _with_polluter_suffix(
                        _AST_CATEGORY_REASONS[chosen[0]], orig_reason
                    )
                    for cat in chosen:
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
                    group["candidates"] = list(candidates)
                    group["status"] = orig_status
                    group["reason"] = orig_reason
                    group.pop("resolution", None)
                    group.pop("disambiguated", None)
                    group.pop("refined", None)
            else:
                has_ambiguous = True
                resolved.extend(candidates)
                group["candidates"] = list(candidates)
                group["status"] = orig_status
                group["reason"] = orig_reason
                group.pop("resolution", None)
                group.pop("disambiguated", None)
                group.pop("refined", None)
                for cat in candidates:
                    for hit in indicators.get(cat, []):
                        sides.append(hit["side"])
                        notes.append(
                            f"Heuristic hint (`{cat}`, {hit['side']}): "
                            f"`{hit['location']}` {hit['detail']}."
                        )
                        compact_hints.append(f"`{hit['location']}` {hit['detail']}")
        else:
            if orig_status == "ambiguous":
                has_ambiguous = True
            for cand in candidates:
                hits = indicators.get(cand, [])
                if (
                    cand == "shared_state_interference"
                    and not hits
                    and indicators.get("resource_leak")
                    and (
                        not indicators.get("unseeded_randomness")
                        or _is_weak_uuid_only(indicators.get("unseeded_randomness", []))
                    )
                ):
                    resolved.append("resource_leak")
                    refined = True
                    group["runtime_candidates"] = list(candidates)
                    group["runtime_status"] = orig_status
                    group.setdefault("runtime_reason", orig_reason)
                    group["candidates"] = ["resource_leak"]
                    group["status"] = "suspected"
                    group["resolution"] = "refined"
                    group["refined"] = True
                    group["disambiguated"] = False
                    group["reason"] = _with_polluter_suffix(
                        _AST_CATEGORY_REASONS["resource_leak"], orig_reason
                    )
                    for hit in indicators.get("resource_leak", []):
                        sides.append(hit["side"])
                        notes.append(
                            f"Heuristic hint (`resource_leak`, {hit['side']}): "
                            f"`{hit['location']}` {hit['detail']}."
                        )
                        compact_hints.append(f"`{hit['location']}` {hit['detail']}")
                elif (
                    cand == "hash_order_sensitivity"
                    and not hits
                    and indicators.get("float_precision")
                ):
                    resolved.append("float_precision")
                    refined = True
                    group["runtime_candidates"] = list(candidates)
                    group["runtime_status"] = orig_status
                    group.setdefault("runtime_reason", orig_reason)
                    group["candidates"] = ["float_precision"]
                    group["status"] = "suspected"
                    group["resolution"] = "refined"
                    group["refined"] = True
                    group["disambiguated"] = False
                    group["reason"] = _with_polluter_suffix(
                        _AST_CATEGORY_REASONS["float_precision"], orig_reason
                    )
                    for hit in indicators.get("float_precision", []):
                        sides.append(hit["side"])
                        notes.append(
                            f"Heuristic hint (`float_precision`, {hit['side']}): "
                            f"`{hit['location']}` {hit['detail']}."
                        )
                        compact_hints.append(f"`{hit['location']}` {hit['detail']}")
                else:
                    resolved.append(cand)
                    group["candidates"] = list(candidates)
                    group["status"] = orig_status
                    group["reason"] = orig_reason
                    group.pop("resolution", None)
                    group.pop("disambiguated", None)
                    group.pop("refined", None)
                    for hit in hits:
                        sides.append(hit["side"])
                        notes.append(
                            f"Heuristic hint (`{cand}`, {hit['side']}): "
                            f"`{hit['location']}` {hit['detail']}."
                        )
                        compact_hints.append(f"`{hit['location']}` {hit['detail']}")

    dedup_runtime = _dedup_preserve_order(all_runtime_cands)
    runtime_cands_set = set(dedup_runtime) | set(resolved)
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
        overall_resolution = "disambiguated"
    elif refined:
        basis = "AST Refined *"
        overall_resolution = "refined"
    elif has_ambiguous:
        basis = "Ambiguous"
        overall_resolution = "ambiguous"
    elif compact_hints:
        basis = "Runtime+AST"
        overall_resolution = "unchanged"
    elif conflicting_hints:
        basis = "Runtime (AST conflict)"
        overall_resolution = "unchanged"
    else:
        basis = "Runtime"
        overall_resolution = "unchanged"

    return {
        "resolved_candidates": _dedup_preserve_order(resolved),
        "runtime_candidates": dedup_runtime,
        "fault_side": fault_side,
        "heuristic_notes": notes,
        "compact_hints": compact_hints,
        "conflicting_hints": conflicting_hints,
        "basis": basis,
        "resolution": overall_resolution,
        "disambiguated": disambiguated,
        "refined": refined,
    }
