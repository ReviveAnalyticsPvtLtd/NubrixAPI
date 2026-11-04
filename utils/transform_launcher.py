"""
transform_launcher.py — subprocess entry point for transformation code execution.

Reads JSON from stdin: {"projectId": ..., "code": ..., "result_path": ...}
Executes the code, writes final_df as parquet to result_path.
Writes error traceback to stderr if anything fails.
"""

import sys
import os
import json
import io
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
        result_path = input_data["result_path"]
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
        pl = None

    try:
        exec(pythonCode, g)
        finalDf = g.get("final_df")
        if finalDf is None:
            sys.stderr.write("Transformation code must create a final_df variable.\n")
            sys.exit(1)

        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except Exception:
            pa = None
            pq = None

        if pl is not None and isinstance(finalDf, pl.LazyFrame):
            finalDf = finalDf.collect()
        if pl is not None and isinstance(finalDf, pl.DataFrame):
            try:
                arrow_table = finalDf.to_arrow()
            except Exception:
                arrow_table = pa.Table.from_pandas(finalDf.to_pandas(), preserve_index=False) if pa else None
        elif isinstance(finalDf, pd.DataFrame):
            arrow_table = pa.Table.from_pandas(finalDf, preserve_index=False) if pa else None
        else:
            sys.stderr.write(f"final_df must be a pandas/polars DataFrame or LazyFrame, got {type(finalDf).__name__}.\n")
            sys.exit(1)

        with open(result_path, "wb") as f:
            buf = io.BytesIO()
            if pq is not None and arrow_table is not None:
                pq.write_table(arrow_table, buf, compression="snappy")
            else:
                df_to_write = arrow_table.to_pandas() if arrow_table is not None else (
                    finalDf.to_pandas() if pl is not None and isinstance(finalDf, pl.DataFrame) else finalDf
                )
                df_to_write.to_parquet(buf, compression="snappy")
            f.write(buf.getvalue())
        # Success
        sys.exit(0)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()