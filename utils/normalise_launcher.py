"""
normalise_launcher.py: trusted sandbox entry point that rewrites a run's Parquet output before it is stored.

Started by `storedTables.normaliseRunOutput` through `runTrustedSandbox`, under the run limits, with no table access.
Reads JSON from stdin: {"input_path": ..., "result_path": ..., "request": {"marker": ..., "maxDecodedBytes": ...},
"channel": ...}. The marker is the value the parent computed for this table; the child never holds the key behind it.

The input is streamed batch by batch and refused once the decoded batches pass `maxDecodedBytes` (0 = no limit),
which exits with `EXIT_TOO_LARGE`. The output is written with the API's own settings and a fresh schema: no
metadata from the input is kept except a pandas entry that fits the table, and the only marker is the new one.
Any other failure exits 1 with the error on stderr.
"""

import json
import os
import sys
import traceback

# No linear algebra runs here. OpenBLAS reserves a buffer per thread when NumPy loads, and that reservation counts
# against the process's memory limit, so it gets one thread. Set before anything imports NumPy.
os.environ["OPENBLAS_NUM_THREADS"] = "1"


def _normalise(inputPath: str, resultPath: str, marker: str, limit: int) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    from utils.storedTables import (
        MARKER_KEY,
        NORMALISE_BATCH_ROWS,
        ROW_GROUP_ROWS,
        WRITE_SETTINGS,
        TableTooLargeError,
        trustedPandasMetadata,
    )

    parquet = pq.ParquetFile(inputPath)
    source = parquet.schema_arrow
    metadata: dict[bytes, bytes] = {}
    pandasEntry = trustedPandasMetadata(source)
    if pandasEntry is not None:
        metadata[b"pandas"] = pandasEntry
    if marker:
        metadata[MARKER_KEY] = marker.encode("ascii")
    # Fields are rebuilt too, so no field carries metadata from the input.
    schema = pa.schema([pa.field(field.name, field.type, field.nullable) for field in source], metadata=metadata or None)
    decoded = 0
    pending: list = []
    pendingRows = 0
    with pq.ParquetWriter(resultPath, schema, **WRITE_SETTINGS) as writer:
        for batch in parquet.iter_batches(batch_size=NORMALISE_BATCH_ROWS):
            decoded += batch.nbytes
            if limit and decoded > limit:
                raise TableTooLargeError(f"The decoded batches passed {limit} bytes.")
            pending.append(pa.RecordBatch.from_arrays(batch.columns, schema=schema))
            pendingRows += batch.num_rows
            # Batches are gathered into whole row groups, as the API's writer lays them out.
            while pendingRows >= ROW_GROUP_ROWS:
                table = pa.Table.from_batches(pending, schema)
                writer.write_table(table.slice(0, ROW_GROUP_ROWS), row_group_size=ROW_GROUP_ROWS)
                rest = table.slice(ROW_GROUP_ROWS)
                pending, pendingRows = rest.to_batches(), rest.num_rows
        if pendingRows:
            writer.write_table(pa.Table.from_batches(pending, schema), row_group_size=ROW_GROUP_ROWS)


def main() -> None:
    payload = json.load(sys.stdin)
    from utils.sandboxRunner import enterSandbox

    enterSandbox(payload["channel"])
    from utils.storedTables import EXIT_TOO_LARGE, TableTooLargeError

    request = payload["request"]
    try:
        _normalise(payload["input_path"], payload["result_path"], str(request["marker"]), int(request["maxDecodedBytes"]))
    except TableTooLargeError:
        sys.exit(EXIT_TOO_LARGE)
    except Exception:
        traceback.print_exc(limit=5)
        sys.exit(1)


if __name__ == "__main__":
    main()
