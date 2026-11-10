"""
table_reader_launcher.py: trusted sandbox entry point that decodes a stored table the API did not mark.

Started by `storedTables.readUntrusted*` through `runTrustedSandbox`, with RLIMIT_DATA set to
TABLE_READER_MEMORY_BYTES and no table access. Reads JSON from stdin: {"input_path": ..., "result_path": ...,
"request": {"op": "table" | "page" | "info" | "schema", "maxDecodedBytes": ..., "offset": ..., "limit": ...}, "channel": ...}.

Before decoding, the whole table's encoded, uncompressed size is checked against `maxDecodedBytes` (0 = no limit).
"table", "page" and "schema" then exit with `EXIT_TOO_LARGE`; "info" answers with the shape and no sample. "table" and "page"
answer with an uncompressed Arrow IPC stream: dictionaries at any depth are decoded to their values here, under this
child's limit, so the parent's result cap bounds what it holds; the schema keeps only a pandas entry that fits the
table (and, for a page, the table's row count). "info" answers with the JSON `storedTables.tableInfo` builds, its
sample cut to `MAX_SAMPLE_CHARS`. "schema" decodes nothing: it answers with the footer's Arrow schema, serialised as
an IPC schema message, with plain types and no metadata (`storedTables.tableSchema`). Any other failure exits 1 with
the error on stderr.
"""

import json
import os
import sys
import traceback

# No linear algebra runs here. OpenBLAS reserves a buffer per thread when NumPy loads, and that reservation counts
# against the reader's memory limit, so it gets one thread. Set before anything imports NumPy.
os.environ["OPENBLAS_NUM_THREADS"] = "1"


def _plainTable(table, extra: dict):
    """The table with dictionaries at every depth decoded and no metadata but a valid pandas entry and `extra`."""
    import pyarrow as pa

    from utils.storedTables import hasDictionary, plainType, trustedPandasMetadata

    metadata: dict[bytes, bytes] = {}
    pandasEntry = trustedPandasMetadata(table.schema)
    if pandasEntry is not None:
        metadata[b"pandas"] = pandasEntry
    metadata.update(extra)
    columns, fields = [], []
    for field, column in zip(table.schema, table.columns):
        if hasDictionary(column.type):
            column = column.cast(plainType(column.type))
        columns.append(column)
        fields.append(pa.field(field.name, column.type, field.nullable))
    return pa.Table.from_arrays(columns, schema=pa.schema(fields, metadata=metadata or None))


def _answer(inputPath: str, resultPath: str, request: dict) -> None:
    import pyarrow as pa

    from utils.storedTables import MAX_SAMPLE_CHARS, PAGE_ROWS_KEY, TableTooLargeError, decodePage, decodeTable, tableInfo, tableSchema

    limit = int(request["maxDecodedBytes"])
    op = request["op"]
    if op == "info":
        info = tableInfo(inputPath, limit)
        if info["sample"] is not None:
            info["sample"] = info["sample"][:MAX_SAMPLE_CHARS]
        with open(resultPath, "w") as target:
            json.dump(info, target)
        return
    if op == "schema":
        with open(resultPath, "wb") as target:
            target.write(tableSchema(inputPath, limit).serialize().to_pybytes())
        return
    if op == "table":
        table, extra = decodeTable(inputPath, limit), {}
    elif op == "page":
        table, rows = decodePage(inputPath, int(request["offset"]), int(request["limit"]), limit)
        extra = {PAGE_ROWS_KEY: str(int(rows)).encode()}
    else:
        raise ValueError(f"Unknown read {op!r}.")
    table = _plainTable(table, extra)
    if limit and table.nbytes > limit:
        raise TableTooLargeError(f"The decoded table is over {limit} bytes.")
    options = pa.ipc.IpcWriteOptions(compression=None)
    with pa.OSFile(resultPath, "wb") as sink, pa.ipc.new_stream(sink, table.schema, options=options) as writer:
        writer.write_table(table)


def main() -> None:
    payload = json.load(sys.stdin)
    from utils.sandboxRunner import enterSandbox

    enterSandbox(payload["channel"])
    from utils.storedTables import EXIT_TOO_LARGE, TableTooLargeError

    try:
        _answer(payload["input_path"], payload["result_path"], payload["request"])
    except TableTooLargeError:
        sys.exit(EXIT_TOO_LARGE)
    except Exception:
        traceback.print_exc(limit=5)
        sys.exit(1)


if __name__ == "__main__":
    main()
