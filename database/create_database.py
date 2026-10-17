"""
Create AquaCulture Database
Purpose:
- Creates aquaculture.db.
- Creates only the planned tables.
- Reads species_rules.xlsx and seeds species table.
- Leaves species.image as NULL when importing from Excel.
- Ensures configuration.json exists.

Usage:
    python database/create_database.py
    python database/create_database.py --yes
    python database/create_database.py --excel database/species_rules.xlsx
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

try:
    from openpyxl import load_workbook
except ImportError:  # handled at runtime with a clear message
    load_workbook = None

if __package__ in (None, ""):
    # Replace the script directory: its database.py would shadow the package.
    sys.path[0] = str(Path(__file__).resolve().parents[1])

from database.model import EXPECTED_TABLES, TABLE_SQL


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "aquaculture.db"
CONFIG_PATH = BASE_DIR / "configuration.json"
DEFAULT_EXCEL_PATH = BASE_DIR / "species_rules.xlsx"


DEFAULT_CONFIGURATION: dict[str, Any] = {
    "password": "",
    "theme": {
        "accent": "",
        "text": "",
        "background": "",
        "good": "",
        "warning": "",
        "danger": "",
        "info": "",
    },
    "notification": {
        "Temperature": {
            "min": {"value": 0.0, "msg": ""},
            "max": {"value": 0.0, "msg": ""},
            "status": 1,
        },
        "PH": {
            "min": {"value": 0.0, "msg": ""},
            "max": {"value": 0.0, "msg": ""},
            "status": 1,
        },
        "Oxygen": {
            "min": {"value": 0.0, "msg": ""},
            "max": {"value": 0.0, "msg": ""},
            "status": 1,
        },
        "Ammonia": {
            "min": {"value": 0.0, "msg": ""},
            "max": {"value": 0.0, "msg": ""},
            "status": 0,
        },
        "Summary": 3600,
        "Gap": 60,
        "Email": 1,
        "SMS": 0,
    },
}


SPECIES_COLUMNS = (
    "name",
    "temperature",
    "ph",
    "oxygen",
    "turbidity",
    "ammonia",
    "environment",
    "regulation",
    "ecology",
    "safety",
)


HEADER_ALIASES = {
    "name": "name",
    "species": "name",
    "species name": "name",
    "temperature": "temperature",
    "temp": "temperature",
    "ph": "ph",
    "pH": "ph",
    "oxygen": "oxygen",
    "dissolved oxygen": "oxygen",
    "dissolved_oxygen": "oxygen",
    "turbidity": "turbidity",
    "ammonia": "ammonia",
    "environment": "environment",
    "regulation": "regulation",
    "ecology": "ecology",
    "safety": "safety",
}


CREATE_LOG_MESSAGE = "System database created."


def main() -> int:
    parser = argparse.ArgumentParser(description="Create AquaCulture SQLite database.")
    parser.add_argument("--yes", action="store_true", help="Overwrite aquaculture.db if it already exists.")
    parser.add_argument("--excel", type=str, default=str(DEFAULT_EXCEL_PATH), help="Path to species_rules.xlsx.")
    args = parser.parse_args()

    excel_path = Path(args.excel)

    if DB_PATH.exists() and not args.yes:
        print("WARNING: database/aquaculture.db already exists.")
        print("To overwrite it, run:")
        print("  python database/create_database.py --yes")
        print("Database was not changed.")
        return 1

    if DB_PATH.exists() and args.yes:
        DB_PATH.unlink()
        print("Existing aquaculture.db removed because --yes was used.")

    ensure_configuration(CONFIG_PATH)
    create_database(DB_PATH)

    if excel_path.exists():
        inserted = seed_species_from_excel(DB_PATH, excel_path)
        print(f"Species seed completed. Inserted rows: {inserted}")
    else:
        print(f"species_rules.xlsx not found at: {excel_path}")
        print("Database was created without species seed data.")

    print(f"Database ready: {DB_PATH}")
    print(f"Configuration ready: {CONFIG_PATH}")
    return 0


def create_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        for table_name in EXPECTED_TABLES:
            conn.execute(TABLE_SQL[table_name])
        conn.execute("INSERT INTO log (session_id, message) VALUES (?, ?)", (None, CREATE_LOG_MESSAGE))
        conn.commit()


def ensure_configuration(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(json.dumps(DEFAULT_CONFIGURATION, indent=2), encoding="utf-8")
        return

    try:
        current = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup_path = config_path.with_suffix(".invalid.json")
        config_path.replace(backup_path)
        config_path.write_text(json.dumps(DEFAULT_CONFIGURATION, indent=2), encoding="utf-8")
        print(f"Invalid configuration.json was backed up to: {backup_path.name}")
        return

    merged = deep_merge(DEFAULT_CONFIGURATION, current)

    # Compatibility rule: older files may use inform; normalize it to info.
    theme = merged.setdefault("theme", {})
    if "info" not in theme and "inform" in theme:
        theme["info"] = theme.get("inform", "")
    theme.pop("inform", None)

    config_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")


def deep_merge(default: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    result = dict(default)
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def seed_species_from_excel(db_path: Path, excel_path: Path) -> int:
    if load_workbook is None:
        print("ERROR: openpyxl is required to read species_rules.xlsx.")
        print("Install it with: python -m pip install openpyxl")
        return 0

    workbook = load_workbook(excel_path, data_only=True)
    sheet = workbook.active

    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        print("species_rules.xlsx is empty.")
        return 0

    header = [normalize_header(value) for value in rows[0]]
    column_index = build_column_index(header)
    missing = [column for column in SPECIES_COLUMNS if column not in column_index]
    if missing:
        print("ERROR: species_rules.xlsx is missing required columns:")
        for column in missing:
            print(f"- {column}")
        print("No species data was imported.")
        return 0

    records: list[dict[str, str]] = []
    for row_number, row in enumerate(rows[1:], start=2):
        if row is None or all(cell is None or str(cell).strip() == "" for cell in row):
            continue

        record: dict[str, str] = {}
        invalid = False
        for column in SPECIES_COLUMNS:
            raw_value = get_cell(row, column_index[column])
            text_value = stringify_cell(raw_value)
            if column == "name" and not text_value:
                print(f"Skipped row {row_number}: name is empty.")
                invalid = True
                break
            if column != "name" and not text_value:
                print(f"Skipped row {row_number}: {column} is empty.")
                invalid = True
                break
            record[column] = text_value

        if not invalid:
            records.append(record)

    if not records:
        return 0

    placeholders = ", ".join("?" for _ in SPECIES_COLUMNS)
    columns_sql = ", ".join(SPECIES_COLUMNS)
    values = [tuple(record[column] for column in SPECIES_COLUMNS) for record in records]

    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            f"INSERT INTO species ({columns_sql}) VALUES ({placeholders})",
            values,
        )
        conn.commit()

    return len(records)


def normalize_header(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    text = text.replace("_", " ").replace("-", " ")
    text = " ".join(text.split())
    lower = text.lower()
    return HEADER_ALIASES.get(lower, lower)


def build_column_index(headers: list[str]) -> dict[str, int]:
    index: dict[str, int] = {}
    for position, header in enumerate(headers):
        if header in SPECIES_COLUMNS and header not in index:
            index[header] = position
    return index


def get_cell(row: tuple[Any, ...], index: int) -> Any:
    if index >= len(row):
        return None
    return row[index]


def stringify_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


if __name__ == "__main__":
    raise SystemExit(main())
