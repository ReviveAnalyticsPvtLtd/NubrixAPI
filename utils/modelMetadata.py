"""The project metadata the chart model is shown.

The reporting service and the chart evaluation harness build it with these functions, so an evaluation sees the
tables, hints and keys a served request does: inactive tables and `_` keys are left out, and every table carries
the size hints.
"""

__all__ = ["activeTables", "augmentWithSizeHints", "metadataForModel"]


def activeTables(metadata: dict) -> dict:
    """Only the table entries where `isActive` is not False; a missing key counts as active. `_` keys are not tables."""
    if not isinstance(metadata, dict):
        return {}
    return {k: v for k, v in metadata.items() if not k.startswith("_") and v.get("isActive", True) is not False}


def augmentWithSizeHints(metadata: dict, projectId: str) -> dict:
    """Annotate each table entry in `metadata`, in place, with a deterministic size_class.

    The metadata shape is ``{<tableName>: {"shape": [rows, cols], "columns": [...]}}``.
    The hint steers the LLM away from guessing memory cost and toward lazy
    Polars for large tables. Entries that are not dicts and `_` keys are left alone.
    """
    from utils.initMethods import classify_table_size

    for table_name, entry in (metadata or {}).items():
        if not isinstance(entry, dict) or table_name.startswith("_"):
            continue
        cls = classify_table_size(projectId, table_name)
        entry["size_class"] = cls.get("size_class", "unknown")
        entry["size_hint"] = cls.get("hint", "")
        existing_rows = cls.get("rows_estimate")
        if existing_rows is not None and "shape" in entry and isinstance(entry["shape"], list):
            entry["_size_check"] = (existing_rows == entry["shape"][0])
    return metadata


def metadataForModel(metadata: dict, projectId: str) -> dict:
    """What a chart request passes to the workflow: the size hints added (in place), then only the active tables."""
    return activeTables(augmentWithSizeHints(metadata, projectId))
