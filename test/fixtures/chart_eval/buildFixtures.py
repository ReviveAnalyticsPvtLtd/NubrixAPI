"""Writes the synthetic evaluation tables and metadata.json next to this file.

Invented names and values, no randomness: running it again gives the same rows.
Usage (from the repo root): python test/fixtures/chart_eval/buildFixtures.py
"""

import datetime as dt
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

PROJECT = "11111111-2222-4333-8444-555555555555"
OUT = Path(__file__).resolve().parent / "data" / PROJECT

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


def column(name, kind, text):
    return {"name": name, "type": kind, "description": text}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ordersData = orders()
    pq.write_table(pa.table({**ordersData, "order_date": pa.array(ordersData["order_date"], pa.date32())}), OUT / "orders.parquet")
    products = list(zip(*PRODUCTS))
    productData = dict(zip(("product_id", "product_name", "category", "unit_price"), map(list, products)))
    pq.write_table(pa.table(productData), OUT / "products.parquet")
    stores = list(zip(*STORES))
    storeData = dict(zip(("store_id", "store_name", "city", "latitude", "longitude", "employees"), map(list, stores)))
    pq.write_table(pa.table(storeData), OUT / "stores.parquet")
    metadata = {
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
    }
    (OUT / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
