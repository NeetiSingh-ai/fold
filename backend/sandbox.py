"""Validate and run Gemini-generated functions in a restricted namespace.

Generated code may only use a small set of pure standard-library modules, cannot
touch files, network, dunder attributes or dynamic code execution, and every call
runs with a time limit. Anything that fails returns None, which makes the gateway
fall back to Gemini, so a bad compile can cost money but never a wrong answer
silently.
"""
import ast
import builtins
import concurrent.futures as cf
import datetime
import decimal
import json
import math
import re
import string
import unicodedata

ALLOWED_MODULES = {"re": re, "json": json, "math": math, "datetime": datetime, "string": string,
                   "unicodedata": unicodedata, "decimal": decimal}
BANNED_NAMES = {"exec", "eval", "open", "compile", "__import__", "globals", "locals", "getattr", "setattr",
                "delattr", "vars", "input", "breakpoint", "help", "exit", "quit", "memoryview", "type",
                "object", "super", "classmethod", "staticmethod", "property", "dir", "id", "hash"}
_SAFE = ["abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "isinstance", "len",
         "list", "map", "max", "min", "range", "reversed", "round", "set", "sorted", "str", "sum", "tuple",
         "zip", "hasattr", "callable", "bytes", "slice", "format", "hex", "LookupError", "NameError",
         "StopIteration", "ArithmeticError", "OverflowError", "ValueError", "Exception", "KeyError", "IndexError", "TypeError", "AttributeError",
         "ZeroDivisionError", "frozenset", "chr", "ord", "divmod", "pow", "repr", "next", "iter", "print"]
_POOL = cf.ThreadPoolExecutor(max_workers=8)
TIMEOUT_S = 0.5


class UnsafeCode(Exception):
    pass


# stdlib internals that allowed modules import lazily (e.g. datetime.strptime -> _strptime).
# Generated code still can't import these itself: the AST check rejects any import statement for them.
_INTERNAL_OK = {"_strptime", "calendar", "locale", "_locale", "time", "encodings", "_decimal", "_pydecimal",
                "numbers", "sre_constants", "sre_parse", "sre_compile", "_datetime", "_json", "json"}


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".")[0]
    if root not in ALLOWED_MODULES and root not in _INTERNAL_OK:
        raise ImportError(f"module {name} not allowed")
    return __import__(name, globals, locals, fromlist, level)


def validate(code: str):
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise UnsafeCode(f"syntax error: {e}")
    has_run = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for m in mods:
                if m.split(".")[0] not in ALLOWED_MODULES:
                    raise UnsafeCode(f"import of '{m}' is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise UnsafeCode(f"access to '{node.attr}' is not allowed")
        elif isinstance(node, ast.Name) and (node.id in BANNED_NAMES or node.id.startswith("__")):
            raise UnsafeCode(f"use of '{node.id}' is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await)):
            raise UnsafeCode("global/nonlocal/async not allowed")
        elif isinstance(node, ast.FunctionDef) and node.name == "run":
            has_run = True
    if not has_run:
        raise UnsafeCode("code must define run(text)")
    return tree


def load(code: str):
    validate(code)
    safe_builtins = {n: getattr(builtins, n) for n in _SAFE}
    safe_builtins["__import__"] = _safe_import
    ns = {"__builtins__": safe_builtins, **ALLOWED_MODULES}
    exec(compile(code, "<fold-compiled>", "exec"), ns)  # noqa: S102 - validated above
    fn = ns.get("run")
    if not callable(fn):
        raise UnsafeCode("run is not callable")
    return fn


def call(fn, text: str):
    """Run compiled fn with a timeout. Returns (output | None, error | None)."""
    fut = _POOL.submit(fn, text)
    try:
        out = fut.result(timeout=TIMEOUT_S)
    except cf.TimeoutError:
        return None, "timeout"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"
    if out is not None and not isinstance(out, dict):
        return None, "returned non-dict"
    return out, None
