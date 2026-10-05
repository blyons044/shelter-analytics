"""
Extract Austin Animal Center intake and outcome records into the raw schema.

Source: City of Austin open data portal (Socrata).
  intakes   https://data.austintexas.gov/resource/wter-evkm.json
  outcomes  https://data.austintexas.gov/resource/9t4d-g238.json

The portal pages at 50k records per request. Both datasets are full snapshots
rather than append-only feeds, so each run replaces the raw tables outright.
Incremental loading would be the wrong shape here: records are revised in place
when staff correct an outcome after the fact, and a merge on animal_id alone
would silently keep stale rows for animals with repeat stays.

Usage:
    python -m ingest.extract                 # pull from the live API
    python -m ingest.extract --sample 4000   # generate sample data, no network
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import duckdb
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "shelter.duckdb"

PAGE_SIZE = 50_000
REQUEST_TIMEOUT = 60
MAX_RETRIES = 4

DATASETS = {
    "intakes": {
        "resource": "wter-evkm",
        "columns": [
            "animal_id", "name", "datetime", "found_location", "intake_type",
            "intake_condition", "animal_type", "sex_upon_intake",
            "age_upon_intake", "breed", "color",
        ],
    },
    "outcomes": {
        "resource": "9t4d-g238",
        "columns": [
            "animal_id", "name", "datetime", "date_of_birth", "outcome_type",
            "outcome_subtype", "animal_type", "sex_upon_outcome",
            "age_upon_outcome", "breed", "color",
        ],
    },
}


def fetch_page(resource: str, columns: list[str], offset: int) -> list[dict]:
    """Fetch one page, retrying on transient failures with linear backoff."""
    url = f"https://data.austintexas.gov/resource/{resource}.json"
    params = {
        "$select": ",".join(columns),
        "$limit": PAGE_SIZE,
        "$offset": offset,
        "$order": "animal_id,datetime",
    }
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 3)
    raise RuntimeError(f"{resource} offset {offset} failed after {MAX_RETRIES} attempts") from last_error


def fetch_dataset(name: str) -> list[dict]:
    spec = DATASETS[name]
    rows: list[dict] = []
    offset = 0
    while True:
        page = fetch_page(spec["resource"], spec["columns"], offset)
        rows.extend(page)
        print(f"  {name}: {len(rows):,} rows", flush=True)
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def load(con: duckdb.DuckDBPyConnection, name: str, rows: list[dict]) -> None:
    """Replace the raw table. Every column lands as VARCHAR on purpose.

    The API returns everything as strings and roughly 2% of age fields carry
    values like 'NULL' or negative weeks. Casting at ingest would either drop
    those rows or fail the load; staging handles them where the rules are
    visible and testable.
    """
    if not rows:
        raise RuntimeError(f"{name}: source returned zero rows, refusing to replace the table")

    columns = DATASETS[name]["columns"]
    normalized = [tuple(str(row.get(c)) if row.get(c) is not None else None for c in columns) for row in rows]

    con.execute(f"CREATE SCHEMA IF NOT EXISTS raw")
    col_ddl = ", ".join(f'"{c}" VARCHAR' for c in columns)
    con.execute(f"CREATE OR REPLACE TABLE raw.{name} ({col_ddl})")
    con.executemany(
        f"INSERT INTO raw.{name} VALUES ({', '.join('?' for _ in columns)})",
        normalized,
    )
    count = con.execute(f"SELECT count(*) FROM raw.{name}").fetchone()[0]
    print(f"  loaded raw.{name}: {count:,} rows")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample",
        type=int,
        metavar="N",
        help="skip the API and generate N synthetic animals instead (for offline development)",
    )
    args = parser.parse_args()

    con = duckdb.connect(str(DB_PATH))

    if args.sample:
        from ingest.sample import generate

        print(f"Generating sample data for {args.sample:,} animals")
        data = generate(args.sample)
    else:
        print("Fetching from data.austintexas.gov")
        data = {name: fetch_dataset(name) for name in DATASETS}

    for name, rows in data.items():
        load(con, name, rows)

    con.close()
    print(f"Done. Database at {DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
