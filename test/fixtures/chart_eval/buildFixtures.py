"""Writes the synthetic evaluation tables and metadata.json next to this file.

Invented names and values, no randomness: running it again gives the same bytes.

The tables are written by the API's own writer (`utils.storedTables.writeStoredTable`), so each carries the marker the
API gives a table it stored, and a marked table's footer is read by HEAD and Range requests. The marker is derived
from a key, so the harness has to serve these files with the same key: `--storage-key` takes FIXTURE_KEY. It is a
made-up value that protects nothing.

Usage (from the repo root): python test/fixtures/chart_eval/buildFixtures.py
"""

import datetime as dt
import json
import os
import sys
from pathlib import Path

import pyarrow as pa

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

PROJECT = "11111111-2222-4333-8444-555555555555"
FIXTURE_KEY = "chart-eval-fixture-key"
OUT = Path(__file__).resolve().parent / "data" / PROJECT
# One row per day, so "the last 12 months" has data whatever today is, until the last day here.
VISITS_FIRST_DAY = dt.date(2020, 1, 1)
VISITS_LAST_DAY = dt.date(2040, 12, 31)

REGIONS = ["North", "South", "East", "West"]
PRODUCTS = [
    (1, "Oak Desk", "Furniture", 120.0),
    (2, "Pine Shelf", "Furniture", 45.5),
    (3, "Desk Lamp", "Lighting", 18.25),
    (4, "Floor Lamp", "Lighting", 60.0),
    (5, "Wool Rug", "Textiles", 80.0),
    (6, "Linen Curtain", "Textiles", 32.75),
]
STORES = [
    (1, "Harbor Store", "Northport", 40.5, -74.0, 12),
    (2, "Hill Store", "Southfield", 34.1, -118.2, 9),
    (3, "Lake Store", "Eastlake", 41.9, -87.6, 15),
]


def orders() -> dict:
    columns = {k: [] for k in ("order_id", "order_date", "region", "product_id", "quantity", "revenue")}
    start = dt.date(2025, 1, 5)
    for i in range(36):
        productId = i % 6 + 1
        quantity = i % 4 + 1
        price = PRODUCTS[productId - 1][3]
        columns["order_id"].append(i + 1)
        columns["order_date"].append(start + dt.timedelta(days=i * 9))
        columns["region"].append(REGIONS[(i * 7 // 3) % 4])
        columns["product_id"].append(productId)
        columns["quantity"].append(quantity)
        columns["revenue"].append(round(quantity * price, 2))
    return columns


def visits() -> dict:
    days = (VISITS_LAST_DAY - VISITS_FIRST_DAY).days + 1
    return {
        "visit_date": pa.array([VISITS_FIRST_DAY + dt.timedelta(days=i) for i in range(days)], pa.date32()),
        "visits": [i * 7 % 11 + 1 for i in range(days)],
    }


def column(name, kind, text):
    return {"name": name, "type": kind, "description": text}


def metadata() -> dict:
    return {
        "orders": {
            "description": "One row per order.",
            "shape": [36, 6],
            "columns": [
                column("order_id", "int64", "Order number."),
                column("order_date", "datetime64[ns]", "Day the order was placed."),
                column("region", "object", "Sales region."),
                column("product_id", "int64", "Product ordered; joins products.product_id."),
                column("quantity", "int64", "Units ordered."),
                column("revenue", "float64", "Order revenue."),
            ],
            "sample_row": {"order_id": 1, "order_date": "2025-01-05", "region": "North", "product_id": 1, "quantity": 1, "revenue": 120.0},
        },
        "products": {
            "description": "One row per product.",
            "shape": [6, 4],
            "columns": [
                column("product_id", "int64", "Product number."),
                column("product_name", "object", "Product name."),
                column("category", "object", "Product category."),
                column("unit_price", "float64", "Price per unit."),
            ],
            "sample_row": {"product_id": 1, "product_name": "Oak Desk", "category": "Furniture", "unit_price": 120.0},
        },
        "stores": {
            "description": "One row per store.",
            "shape": [3, 6],
            "columns": [
                column("store_id", "int64", "Store number."),
                column("store_name", "object", "Store name."),
                column("city", "object", "City."),
                column("latitude", "float64", "Latitude."),
                column("longitude", "float64", "Longitude."),
                column("employees", "int64", "Number of employees."),
            ],
            "sample_row": {"store_id": 1, "store_name": "Harbor Store", "city": "Northport", "latitude": 40.5, "longitude": -74.0, "employees": 12},
        },
        "visits": {
            "description": "Website visits, one row per day from 2020-01-01 to 2040-12-31.",
            "shape": [(VISITS_LAST_DAY - VISITS_FIRST_DAY).days + 1, 2],
            "columns": [
                column("visit_date", "datetime64[ns]", "Day of the visits."),
                column("visits", "int64", "Number of visits that day."),
            ],
            "sample_row": {"visit_date": "2020-01-01", "visits": 1},
        },
        # Not shown to the model: an inactive table with no file, and a key that is not a table.
        "archived_orders": {
            "description": "Orders of a retired shop.",
            "isActive": False,
            "shape": [10, 1],
            "columns": [column("order_id", "int64", "Order number.")],
        },
        "_meta": {"note": "Bookkeeping, not a table."},
    }


def build(out: Path = OUT) -> None:
    """Write the tables and metadata.json into `out`."""
    from utils.storedTables import writeStoredTable

    saved = os.environ.get("SUPABASE_KEY")
    os.environ["SUPABASE_KEY"] = FIXTURE_KEY
    try:
        out.mkdir(parents=True, exist_ok=True)
        ordersData = orders()
        tables = {
            "orders": pa.table({**ordersData, "order_date": pa.array(ordersData["order_date"], pa.date32())}),
            "products": pa.table(dict(zip(("product_id", "product_name", "category", "unit_price"), map(list, zip(*PRODUCTS))))),
            "stores": pa.table(dict(zip(("store_id", "store_name", "city", "latitude", "longitude", "employees"), map(list, zip(*STORES))))),
            "visits": pa.table(visits()),
        }
        for name, table in tables.items():
            (out / f"{name}.parquet").write_bytes(writeStoredTable(table, PROJECT, name))
        (out / "metadata.json").write_text(json.dumps(metadata(), indent=2) + "\n", encoding="utf-8")
    finally:
        if saved is None:
            os.environ.pop("SUPABASE_KEY", None)
        else:
            os.environ["SUPABASE_KEY"] = saved


def main() -> None:
    build()


if __name__ == "__main__":
    main()
