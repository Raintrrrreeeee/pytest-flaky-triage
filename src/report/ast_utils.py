"""SECTION 1: AST loader, file-fingerprint caching, and module/class resolution utilities."""

import ast
from collections import deque
from functools import lru_cache
from pathlib import Path

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
ATTR_INIT_METHODS = SETUP_METHODS | {"__init__"}
TEARDOWN_METHODS = {"teardown_method", "teardown_class", "tearDown"}
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


def _extract_assign_targets_and_value(node):
    """Return (targets_list, value_node) for ast.Assign or ast.AnnAssign with a value, else ([], None)."""
    if isinstance(node, ast.Assign):
        return list(node.targets), node.value
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return [node.target], node.value
    return [], None


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
        else:
            targets, val = _extract_assign_targets_and_value(node)
            if targets and val is not None:
                call_name = _call_head_name(val)
                for target in targets:
                    if isinstance(target, ast.Name):
                        if isinstance(val, (ast.Constant, ast.Tuple)) and target.id not in mutated_via_global:
                            continue
                        globals_map[target.id] = (node.lineno, call_name)
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


def _iter_class_hierarchy(class_node, tree=None):
    """Yield `class_node` followed by any same-file base ClassDef nodes in MRO-like breadth-first order."""
    if not isinstance(class_node, ast.ClassDef):
        return
    queue = deque([class_node])
    seen = {class_node.name}
    while queue:
        cur = queue.popleft()
        yield cur
        if tree is None:
            continue
        for base in getattr(cur, "bases", []):
            base_name = base.id if isinstance(base, ast.Name) else None
            if base_name and base_name not in seen:
                base_def = _find_top_def(tree, base_name)
                if isinstance(base_def, ast.ClassDef):
                    seen.add(base_name)
                    queue.append(base_def)


def _find_method_in_class(class_node, method_name, tree=None):
    for cls in _iter_class_hierarchy(class_node, tree=tree):
        for node in getattr(cls, "body", []):
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
