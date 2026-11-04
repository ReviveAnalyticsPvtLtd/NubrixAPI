"""
inspect_launcher.py — subprocess entry point for transformation inspection.

Reads JSON from stdin: {"projectId": ..., "code": ...}
Executes the code and writes stdout to stdout, stderr to stderr.
"""

import sys
import os
import json
import traceback


def _scopedToProject(reader, allowedProjectId):
    """Refuse other projects. The first argument keeps its name so generated code can pass it by keyword."""
    def scopedReader(projectId, tableName, *args, **kwargs):
        if projectId != allowedProjectId:
            raise PermissionError(f"Access denied: You can only query data for project '{allowedProjectId}' (requested '{projectId}').")
        return reader(projectId, tableName, *args, **kwargs)
    return scopedReader


def main():
    try:
        input_data = json.load(sys.stdin)
        projectId = input_data["projectId"]
        pythonCode = input_data["code"]
    except Exception as e:
        sys.stderr.write(f"Failed to read/parse input JSON from stdin: {e}\n")
        sys.exit(1)

    from utils.initMethods import fetch_data, fetch_data_pl, scan_data, serializer
    import pandas as pd
    import numpy as np
    import datetime
    import math
    import re

    _ALLOWED_MODULES = frozenset({
        "pandas", "numpy", "datetime", "math", "re", "time",
        "sklearn", "scipy", "statsmodels", "polars", "polars.select",
    })
    _DANGEROUS_BUILTINS = frozenset({
        "open", "compile", "eval", "exec", "globals", "locals", "memoryview",
        "input", "breakpoint", "exit", "quit", "help", "license", "copyright",
        "credit", "__import__",
    })

    def _restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
        rootName = name.split(".")[0]
        if rootName not in _ALLOWED_MODULES:
            raise ImportError(f"Import '{name}' is not allowed in transformation code.")
        return __import__(name, globals, locals, fromlist, level)

    import builtins
    safe_builtins = {k: v for k, v in builtins.__dict__.items() if k not in _DANGEROUS_BUILTINS}
    safe_builtins["__import__"] = _restricted_import

    g = {
        "__builtins__": safe_builtins,
        "datetime": datetime,
        "fetch_data": _scopedToProject(fetch_data, projectId),
        "fetch_data_pl": _scopedToProject(fetch_data_pl, projectId),
        "scan_data": _scopedToProject(scan_data, projectId),
        "math": math,
        "np": np,
        "pd": pd,
        "projectId": projectId,
        "serializer": serializer,
    }
    try:
        import polars as pl
        g["pl"] = pl
    except Exception:
        pass

    try:
        exec(pythonCode, g)
        sys.exit(0)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()