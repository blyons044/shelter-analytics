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

Pages are streamed to newline-delimited JSON and bulk loaded by DuckDB rather
than inserted row by row. At roughly 190k records per table the difference is
minutes against seconds, and memory stays flat because no page is held once it
is written.

Usage:
    python -m ingest.extract                 # pull from the live API
    python -m ingest.extract --sample 4000   # generate sample data, no network
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

import duckdb
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "shelter.duckdb"

PAGE_SIZE = 50_000
REQUEST_TIMEOUT = 90
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
        "$order": ":id",
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
    raise RuntimeError(
        f"{resource} offset {offset} failed after {MAX_RETRIES} attempts"
    ) from last_error


def write_ndjson(rows: list[dict], handle) -> int:
    for row in rows:
        handle.write(json.dumps(row, separators=(",", ":")))
        handle.write("\n")
    return len(rows)


def stream_dataset(name: str, handle) -> int:
    """Page through the API, writing each page out before fetching the next."""
    spec = DATASETS[name]
    total = 0
    offset = 0
    while True:
        page = fetch_page(spec["resource"], spec["columns"], offset)
        total += write_ndjson(page, handle)
        print(f"  {name}: {total:,} rows", flush=True)
        if len(page) < PAGE_SIZE:
            return total
        offset += PAGE_SIZE


def load(con: duckdb.DuckDBPyConnection, name: str, path: Path, expected: int) -> None:
    """Bulk load the staged file into raw.<name>.

    Every column lands as VARCHAR on purpose. The API returns everything as
    strings and roughly 2% of age fields carry values like 'NULL' or negative
    weeks. Casting at ingest would either drop those rows or fail the load;
    staging handles them where the rules are visible and testable.

    Socrata omits keys whose value is null rather than emitting a null, so a
    column that happens to be empty across the whole feed will not appear in
    the scanned file at all. Missing columns are substituted with NULL rather
    than allowed to fail the load.
    """
    if expected == 0:
        raise RuntimeError(f"{name}: source returned zero rows, refusing to replace the table")

    columns = DATASETS[name]["columns"]
    scan = f"read_json_auto('{path.as_posix()}', format='newline_delimited', union_by_name=true)"

    discovered = {
        desc[0] for desc in con.execute(f"select * from {scan} limit 0").description
    }

    projection = ", ".join(
        (f'cast("{c}" as varchar) as "{c}"' if c in discovered else f'cast(null as varchar) as "{c}"')
        for c in columns
    )

    missing = [c for c in columns if c not in discovered]
    if missing:
        print(f"  note: {name} has no values for {', '.join(missing)}; loaded as null")

    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    con.execute(f"CREATE OR REPLACE TABLE raw.{name} AS SELECT {projection} FROM {scan}")

    count = con.execute(f"SELECT count(*) FROM raw.{name}").fetchone()[0]
    if count != expected:
        raise RuntimeError(f"{name}: staged {expected:,} rows but loaded {count:,}")
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

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)

        if args.sample:
            from ingest.sample import generate

            print(f"Generating sample data for {args.sample:,} animals")
            generated = generate(args.sample)
            staged = {}
            for name, rows in generated.items():
                path = tmpdir / f"{name}.ndjson"
                with path.open("w") as handle:
                    staged[name] = (path, write_ndjson(rows, handle))
        else:
            print("Fetching from data.austintexas.gov")
            staged = {}
            for name in DATASETS:
                path = tmpdir / f"{name}.ndjson"
                with path.open("w") as handle:
                    staged[name] = (path, stream_dataset(name, handle))

        for name, (path, expected) in staged.items():
            load(con, name, path, expected)

    con.close()
    print(f"Done. Database at {DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
