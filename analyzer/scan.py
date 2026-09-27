"""
FAILSAFE static analyzer — factual AST extraction, no inference.

Produces analyzer/inventory.json with three sections:
  - functions:          every function found, with ordered call list and line number
  - shared_mutable_state: module-level dicts/lists, lock protection status, read/write lines
  - idempotency_patterns: whether each function checks a cache BEFORE its first external call
"""

import ast
import json
import os
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).parent.parent / "app"
OUTPUT = Path(__file__).parent / "inventory.json"

# Names that we treat as "external side-effect" calls (charge / payment processors).
EXTERNAL_CALL_NAMES = {
    "charge_card",
    "charge",
    "submit_payment",
    "send_payment",
    "debit",
    "external_charge",
}

# Names that we treat as persistence calls (database writes).
PERSISTENCE_CALL_NAMES = {
    "save_order",
    "commit",
    "add",
    "bulk_save_objects",
    "execute",
    "insert",
    "update",
    "delete",
    "flush",
}

# Names that we treat as cache reads (idempotency lookup).
CACHE_READ_NAMES = {
    "get",
    "cache_get",
    "lookup",
    "fetch",
    "hget",
    "exists",
}

# Names that we treat as cache writes (idempotency store).
CACHE_WRITE_NAMES = {
    "set",
    "cache_set",
    "hset",
    "put",
    "store",
}

# Module prefixes/names associated with caching or idempotency.
CACHE_MODULES = {"cache", "redis", "memcache", "idempotency"}

# Lock types whose presence indicates protection.
LOCK_TYPES = {"Lock", "RLock", "Semaphore", "threading.Lock", "threading.RLock"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _qualified_call_name(node: ast.Call) -> str:
    """Return the best human-readable name for a call node."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name):
            return f"{func.value.id}.{func.attr}"
        if isinstance(func.value, ast.Attribute):
            # e.g.  a.b.c()
            return f"{func.value.value.id}.{func.value.attr}.{func.attr}" if isinstance(func.value.value, ast.Name) else func.attr
        return func.attr
    return "<unknown>"


def _is_cache_call(name: str) -> bool:
    parts = name.split(".")
    module = parts[0] if len(parts) > 1 else ""
    func = parts[-1]
    return module in CACHE_MODULES or func in (CACHE_READ_NAMES | CACHE_WRITE_NAMES)


def _is_cache_read(name: str) -> bool:
    parts = name.split(".")
    module = parts[0] if len(parts) > 1 else ""
    func = parts[-1]
    return (module in CACHE_MODULES and func in CACHE_READ_NAMES) or func in CACHE_READ_NAMES


def _is_cache_write(name: str) -> bool:
    parts = name.split(".")
    module = parts[0] if len(parts) > 1 else ""
    func = parts[-1]
    return (module in CACHE_MODULES and func in CACHE_WRITE_NAMES) or func in CACHE_WRITE_NAMES


def _is_external_call(name: str) -> bool:
    return name.split(".")[-1] in EXTERNAL_CALL_NAMES or name in EXTERNAL_CALL_NAMES


# ---------------------------------------------------------------------------
# Pass 1 — collect all calls in order from a function body (flat walk)
# ---------------------------------------------------------------------------

def _calls_in_order(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict[str, Any]]:
    """Walk the function body and return every Call node in source order."""
    calls = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            name = _qualified_call_name(node)
            calls.append({"name": name, "line": node.lineno})
    # ast.walk does not guarantee source order; sort by line then col
    calls.sort(key=lambda c: (c["line"], c.get("col", 0)))
    return calls


# ---------------------------------------------------------------------------
# Pass 2 — module-level mutable variables and their read/write lines
# ---------------------------------------------------------------------------

def _collect_module_level_mutables(tree: ast.Module, filepath: str) -> list[dict[str, Any]]:
    """Find module-level dict/list assignments and trace read/write lines."""
    results = []
    mutable_names: set[str] = set()

    # Phase A: identify module-level assignments that are dicts or lists
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            # e.g.  _balances: dict[str, float] = {}
            name = node.target.id if isinstance(node.target, ast.Name) else None
            if name and isinstance(node.value, (ast.Dict, ast.List, ast.Set)):
                mutable_names.add(name)
        elif isinstance(node, ast.Assign):
            # e.g.  _store = {}
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(node.value, (ast.Dict, ast.List, ast.Set)):
                    mutable_names.add(target.id)

    if not mutable_names:
        return results

    # Phase B: for each mutable, find all read and write lines across the whole tree
    for var_name in sorted(mutable_names):
        read_lines: list[int] = []
        write_lines: list[int] = []
        has_lock = False

        for node in ast.walk(tree):
            # Detect lock usage anywhere in the file
            if isinstance(node, ast.Call):
                cname = _qualified_call_name(node)
                if any(lt in cname for lt in LOCK_TYPES):
                    has_lock = True
            # with lock: blocks
            if isinstance(node, ast.With):
                for item in node.items:
                    if isinstance(item.context_expr, ast.Call):
                        cname = _qualified_call_name(item.context_expr)
                        if any(lt in cname for lt in LOCK_TYPES):
                            has_lock = True
                    elif isinstance(item.context_expr, ast.Name):
                        if any(lt in item.context_expr.id for lt in {"lock", "Lock", "mutex"}):
                            has_lock = True

            # Read: var_name.get(...) or var_name[...] used in a load context
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    if func.value.id == var_name and func.attr in ("get", "items", "keys", "values", "copy"):
                        read_lines.append(node.lineno)

            if isinstance(node, ast.Subscript):
                if isinstance(node.value, ast.Name) and node.value.id == var_name:
                    if isinstance(node.ctx, ast.Load):
                        read_lines.append(node.lineno)
                    elif isinstance(node.ctx, ast.Store):
                        write_lines.append(node.lineno)

        read_write_lines = sorted(set(read_lines + write_lines))
        results.append({
            "name": var_name,
            "file": filepath,
            "has_lock_protection": has_lock,
            "read_lines": sorted(set(read_lines)),
            "write_lines": sorted(set(write_lines)),
            "read_write_lines": read_write_lines,
        })

    return results


# ---------------------------------------------------------------------------
# Pass 3 — idempotency pattern detection per function
# ---------------------------------------------------------------------------

def _idempotency_pattern(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    file: str,
) -> dict[str, Any] | None:
    """
    Classify whether the function checks a cache BEFORE its first external call.

    Rules:
      - If the function makes no external call: skip (return None).
      - If it makes a cache read AND an external call, compare their line positions.
      - cache_checked_before_external_call = first cache-read line < first external-call line
    """
    ordered_calls = _calls_in_order(func_node)

    first_external_line: int | None = None
    first_cache_read_line: int | None = None
    first_cache_write_line: int | None = None
    has_external = False
    has_cache_read = False

    for c in ordered_calls:
        name = c["name"]
        line = c["line"]

        if _is_external_call(name):
            has_external = True
            if first_external_line is None:
                first_external_line = line

        if _is_cache_read(name):
            has_cache_read = True
            if first_cache_read_line is None:
                first_cache_read_line = line

        if _is_cache_write(name):
            if first_cache_write_line is None:
                first_cache_write_line = line

    if not has_external:
        return None  # no external call → not relevant

    if not has_cache_read:
        # External call exists but cache is never read first
        checked_before = False
    else:
        checked_before = first_cache_read_line < first_external_line  # type: ignore[operator]

    return {
        "function": func_node.name,
        "file": file,
        "cache_checked_before_external_call": checked_before,
        "first_cache_read_line": first_cache_read_line,
        "first_external_call_line": first_external_line,
        "first_cache_write_line": first_cache_write_line,
    }


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def scan_file(py_path: Path) -> dict[str, Any]:
    source = py_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(py_path))

    rel_path = str(py_path.relative_to(py_path.parent.parent))

    functions: list[dict[str, Any]] = []
    idempotency_patterns: list[dict[str, Any]] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ordered = _calls_in_order(node)
            functions.append({
                "name": node.name,
                "file": rel_path,
                "line": node.lineno,
                "calls_in_order": [c["name"] for c in ordered],
                "calls_with_lines": ordered,
            })

            idp = _idempotency_pattern(node, rel_path)
            if idp:
                idempotency_patterns.append(idp)

    shared_mutable = _collect_module_level_mutables(tree, rel_path)

    return {
        "functions": functions,
        "shared_mutable_state": shared_mutable,
        "idempotency_patterns": idempotency_patterns,
    }


def main() -> None:
    all_functions: list[dict] = []
    all_shared: list[dict] = []
    all_idp: list[dict] = []

    py_files = sorted(APP_DIR.glob("*.py"))
    for py_file in py_files:
        if py_file.name.startswith("__"):
            continue
        result = scan_file(py_file)
        all_functions.extend(result["functions"])
        all_shared.extend(result["shared_mutable_state"])
        all_idp.extend(result["idempotency_patterns"])

    # Sort functions by file then line for deterministic output
    all_functions.sort(key=lambda f: (f["file"], f["line"]))

    inventory = {
        "functions": all_functions,
        "shared_mutable_state": all_shared,
        "idempotency_patterns": all_idp,
    }

    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    print(f"Wrote {OUTPUT}")
    print(f"  {len(all_functions)} functions")
    print(f"  {len(all_shared)} shared mutable variables")
    print(f"  {len(all_idp)} idempotency patterns")


if __name__ == "__main__":
    main()
