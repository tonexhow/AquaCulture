"""
AquaCulture SQLite Database Helper
Purpose:
- Handles SQLite connection and CRUD operations.
- Uses model.py as centralized table/model definition.
- Supports normal rows, SQLite rowid rows, and BLOB fields.

Python: 3.13+
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    from .model import BaseModel, EXPECTED_TABLES, MODEL_REGISTRY, TABLE_SQL
except ImportError:
    from model import BaseModel, EXPECTED_TABLES, MODEL_REGISTRY, TABLE_SQL


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "aquaculture.db"


class DatabaseError(RuntimeError):
    """Raised when a safe database operation cannot be completed."""


class AquaCultureDB:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def connection(self):
        """Commit/rollback and close SQLite connections deterministically."""
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create_tables(self) -> None:
        with self.connection() as conn:
            for table_name in EXPECTED_TABLES:
                conn.execute(TABLE_SQL[table_name])
            # Idempotent non-destructive migration for existing installations.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(water_reading)")}
            if "ammonia_gas_ppm" not in columns:
                conn.execute("ALTER TABLE water_reading ADD COLUMN ammonia_gas_ppm REAL")
            conn.commit()

    def table_exists(self, table_name: str) -> bool:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            ).fetchone()
            return row is not None

    def validate_table(self, table_name: str) -> None:
        if table_name not in MODEL_REGISTRY:
            raise DatabaseError(f"Table is not allowed or not planned: {table_name}")

    def get_table_names(self) -> list[str]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            return [row["name"] for row in rows]

    def get_table_columns(self, table_name: str, include_rowid: bool = False) -> list[str]:
        self.validate_table(table_name)
        with self.connection() as conn:
            rows = conn.execute(f"PRAGMA table_info({quote_identifier(table_name)})").fetchall()
            columns = [row["name"] for row in rows]
        if include_rowid and table_name == "log":
            return ["rowid", *columns]
        return columns

    def insert(self, table_name: str, data: dict[str, Any] | BaseModel) -> int:
        self.validate_table(table_name)
        clean_data = data.to_insert_dict() if isinstance(data, BaseModel) else dict(data)
        clean_data = self._filter_allowed_columns(table_name, clean_data, allow_id=False)
        if not clean_data:
            raise DatabaseError("No valid data supplied for insert.")

        columns = list(clean_data.keys())
        placeholders = ", ".join("?" for _ in columns)
        column_sql = ", ".join(quote_identifier(col) for col in columns)
        values = [clean_data[col] for col in columns]

        with self.connection() as conn:
            cursor = conn.execute(
                f"INSERT INTO {quote_identifier(table_name)} ({column_sql}) VALUES ({placeholders})",
                values,
            )
            conn.commit()
            return int(cursor.lastrowid)

    def update_by_id(self, table_name: str, row_id: int, data: dict[str, Any]) -> int:
        self.validate_table(table_name)
        if table_name == "log":
            raise DatabaseError("log has no id column. Use update_by_rowid for log.")
        clean_data = self._filter_allowed_columns(table_name, data, allow_id=False)
        if not clean_data:
            return 0
        assignments = ", ".join(f"{quote_identifier(col)} = ?" for col in clean_data)
        values = [*clean_data.values(), row_id]
        with self.connection() as conn:
            cursor = conn.execute(
                f"UPDATE {quote_identifier(table_name)} SET {assignments} WHERE id = ?",
                values,
            )
            conn.commit()
            return int(cursor.rowcount)

    def update_by_rowid(self, table_name: str, rowid: int, data: dict[str, Any]) -> int:
        self.validate_table(table_name)
        clean_data = self._filter_allowed_columns(table_name, data, allow_id=False)
        if not clean_data:
            return 0
        assignments = ", ".join(f"{quote_identifier(col)} = ?" for col in clean_data)
        values = [*clean_data.values(), rowid]
        with self.connection() as conn:
            cursor = conn.execute(
                f"UPDATE {quote_identifier(table_name)} SET {assignments} WHERE rowid = ?",
                values,
            )
            conn.commit()
            return int(cursor.rowcount)

    def delete_by_id(self, table_name: str, row_id: int) -> int:
        self.validate_table(table_name)
        if table_name == "log":
            raise DatabaseError("log has no id column. Use delete_by_rowid for log.")
        with self.connection() as conn:
            cursor = conn.execute(
                f"DELETE FROM {quote_identifier(table_name)} WHERE id = ?",
                (row_id,),
            )
            conn.commit()
            return int(cursor.rowcount)

    def delete_by_rowid(self, table_name: str, rowid: int) -> int:
        self.validate_table(table_name)
        with self.connection() as conn:
            cursor = conn.execute(
                f"DELETE FROM {quote_identifier(table_name)} WHERE rowid = ?",
                (rowid,),
            )
            conn.commit()
            return int(cursor.rowcount)

    def delete_all(self, table_name: str) -> int:
        self.validate_table(table_name)
        with self.connection() as conn:
            cursor = conn.execute(f"DELETE FROM {quote_identifier(table_name)}")
            conn.commit()
            return int(cursor.rowcount)

    def fetch_one(
        self,
        table_name: str,
        where: str = "",
        params: Sequence[Any] = (),
        include_rowid: bool = False,
    ) -> dict[str, Any] | None:
        rows = self.fetch_many(table_name, where=where, params=params, limit=1, include_rowid=include_rowid)
        return rows[0] if rows else None

    def fetch_by_id(self, table_name: str, row_id: int) -> dict[str, Any] | None:
        self.validate_table(table_name)
        if table_name == "log":
            raise DatabaseError("log has no id column. Use fetch_by_rowid for log.")
        return self.fetch_one(table_name, "id = ?", (row_id,))

    def fetch_by_rowid(self, table_name: str, rowid: int) -> dict[str, Any] | None:
        self.validate_table(table_name)
        return self.fetch_one(table_name, "rowid = ?", (rowid,), include_rowid=True)

    def fetch_many(
        self,
        table_name: str,
        where: str = "",
        params: Sequence[Any] = (),
        order_by: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        include_rowid: bool = False,
    ) -> list[dict[str, Any]]:
        self.validate_table(table_name)
        select_sql = "rowid, *" if include_rowid or table_name == "log" else "*"
        sql = f"SELECT {select_sql} FROM {quote_identifier(table_name)}"
        if where:
            sql += f" WHERE {where}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, limit)
        if offset is not None:
            sql += " OFFSET ?"
            params = (*params, offset)

        with self.connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def set_species_image(self, species_id: int, image_bytes: bytes | None) -> int:
        from .media_storage import save_media
        if image_bytes is None:
            return self.update_by_id("species", species_id, {"image_path": None, "image": None})
        path = save_media(image_bytes, kind="species", record_id=species_id)
        return self.update_by_id("species", species_id, {"image_path": path, "image": None})

    def get_species_image(self, species_id: int) -> bytes | None:
        from .media_storage import resolve_media_path
        with self.connection() as conn:
            row = conn.execute("SELECT image, image_path FROM species WHERE id=?", (species_id,)).fetchone()
        if row is None:
            return None
        path = resolve_media_path(row["image_path"], kind="species")
        return path.read_bytes() if path and path.is_file() else row["image"]

    def set_prediction_zip(self, prediction_id: int, zip_bytes: bytes) -> int:
        from .media_storage import save_media
        path = save_media(zip_bytes, kind="prediction", record_id=prediction_id)
        return self.update_by_id("prediction", prediction_id, {"data_path": path, "data": None})

    def get_prediction_zip_path(self, prediction_id: int):
        from .media_storage import resolve_media_path
        with self.connection() as conn:
            row = conn.execute("SELECT data_path FROM prediction WHERE id=?", (prediction_id,)).fetchone()
        path = resolve_media_path(row["data_path"], kind="prediction") if row else None
        return path if path and path.is_file() else None

    def get_prediction_zip(self, prediction_id: int) -> bytes | None:
        path = self.get_prediction_zip_path(prediction_id)
        if path:
            return path.read_bytes()
        with self.connection() as conn:
            row = conn.execute("SELECT data FROM prediction WHERE id=?", (prediction_id,)).fetchone()
        return bytes(row["data"]) if row and row["data"] is not None else None

    def insert_prediction_zip(self, zip_bytes: bytes, session_id: int | None = None) -> int:
        # Reserve the ID first, then write an atomic path-backed export.
        from .media_storage import save_media
        with self.connection() as conn:
            cursor = conn.execute("INSERT INTO prediction (session_id, data, data_path) VALUES (?,NULL,NULL)", (session_id,))
            prediction_id = int(cursor.lastrowid)
            path = save_media(zip_bytes, kind="prediction", record_id=prediction_id)
            conn.execute("UPDATE prediction SET data_path=? WHERE id=?", (path, prediction_id))
        return prediction_id

    def _filter_allowed_columns(self, table_name: str, data: dict[str, Any], allow_id: bool) -> dict[str, Any]:
        columns = set(self.get_table_columns(table_name))
        blocked = {"created_at"}
        if not allow_id:
            blocked.add("id")
        return {key: value for key, value in data.items() if key in columns and key not in blocked}


def quote_identifier(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum():
        raise DatabaseError(f"Unsafe SQL identifier: {identifier}")
    return f'"{identifier}"'


# Convenience singleton for simple imports in future backend files.
db = AquaCultureDB()
