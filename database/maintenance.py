"""Small, non-destructive indexes for frequently accessed AquaCulture records."""
from __future__ import annotations
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent / "aquaculture.db"


def ensure_query_indexes(db_path: str | Path = DEFAULT_DB) -> int:
    """Add indexed lookups without changing rows or removing existing schema."""
    created = 0
    with sqlite3.connect(db_path, timeout=15) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = (
            ("species", "idx_ac_species_name", "name"),
            ("water_reading", "idx_ac_water_created", "created_at"),
            ("prediction", "idx_ac_prediction_created", "created_at"),
        )
        for table, index, column in indexes:
            if table not in tables:
                continue
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
            if column not in columns:
                continue
            existed = conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (index,)).fetchone()
            conn.execute(f'CREATE INDEX IF NOT EXISTS "{index}" ON "{table}"("{column}")')
            if not existed:
                created += 1
    return created
