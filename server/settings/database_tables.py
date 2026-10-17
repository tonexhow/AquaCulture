"""
AquaCulture Database Table Settings

Purpose:
- Provide safe helper functions for database table management.
- Keep the existing SQLite database and model definitions unchanged.
- Allow api_manager.py to list, read, insert, update, delete, clear, and inspect tables.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from database.database import AquaCultureDB as DatabaseConnection

BASE_DIR = Path(__file__).resolve().parents[2]
DATABASE_DIR = BASE_DIR / "database"
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    from database.database import AquaCultureDB, DatabaseError, quote_identifier
    from database.model import MODEL_REGISTRY
except Exception as exc:  # noqa: BLE001
    AquaCultureDB = None  # type: ignore[assignment]
    DatabaseError = RuntimeError  # type: ignore[assignment]
    MODEL_REGISTRY = {}  # type: ignore[assignment]
    def quote_identifier(identifier: str) -> str:
        return "\"" + identifier.replace("\"", "") + "\""
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

DB_PATH = DATABASE_DIR / "aquaculture.db"
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
READ_ONLY_TABLES = {"species_archive", "species_reference", "schema_migrations"}


def get_db(db_path: str | Path = DB_PATH) -> DatabaseConnection:
    if AquaCultureDB is None:
        raise RuntimeError(f"Cannot import database helper: {_IMPORT_ERROR}")
    db = AquaCultureDB(db_path)
    db.create_tables()
    return db


def list_tables(*, db_path: str | Path = DB_PATH) -> dict[str, object]:
    try:
        db = get_db(db_path)
        return {"ok": True, "tables": db.get_table_names(), "allowed_tables": sorted((*MODEL_REGISTRY.keys(), *READ_ONLY_TABLES)), "read_only_tables": sorted(READ_ONLY_TABLES)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "tables": [], "allowed_tables": sorted(MODEL_REGISTRY.keys())}


def describe_table(table_name: str, *, db_path: str | Path = DB_PATH) -> dict[str, object]:
    try:
        db = get_db(db_path)
        if table_name in READ_ONLY_TABLES:
            with db.connection() as conn:
                columns = [r[1] for r in conn.execute(f"PRAGMA table_info({quote_identifier(table_name)})")]
        else:
            columns = db.get_table_columns(table_name, include_rowid=True)
        model = MODEL_REGISTRY.get(table_name)
        blob_columns = list(getattr(model, "blob_columns", ()) if model else ())
        has_id = bool(getattr(model, "has_id", True) if model else True)
        return {"ok": True, "table": table_name, "columns": columns, "blob_columns": blob_columns, "has_id": has_id}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "table": table_name, "columns": []}


def read_table(
    table_name: str,
    *,
    db_path: str | Path = DB_PATH,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    order_by: str | None = None,
    include_blob_preview: bool = False,
) -> dict[str, object]:
    try:
        db = get_db(db_path)
        clean_limit = max(1, min(int(limit), MAX_LIMIT))
        clean_offset = max(0, int(offset))
        if table_name in READ_ONLY_TABLES:
            with db.connection() as conn:
                rows = [dict(r) for r in conn.execute(
                    f"SELECT * FROM {quote_identifier(table_name)} ORDER BY id LIMIT ? OFFSET ?"
                    if table_name != "schema_migrations" else
                    "SELECT * FROM schema_migrations ORDER BY migration_key LIMIT ? OFFSET ?",
                    (clean_limit, clean_offset),
                )]
        else:
            rows = db.fetch_many(
                table_name,
                order_by=order_by or default_order_by(table_name),
                limit=clean_limit,
                offset=clean_offset,
                include_rowid=(table_name == "log"),
            )
        with db.connection() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM {quote_identifier(table_name)}").fetchone()[0])
        return {
            "ok": True,
            "table": table_name,
            "rows": [format_row(row, include_blob_preview=include_blob_preview) for row in rows],
            "limit": clean_limit,
            "offset": clean_offset,
            "total": total,
            "has_more": clean_offset + clean_limit < total,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "table": table_name, "rows": []}


def get_row(table_name: str, row_id: int, *, db_path: str | Path = DB_PATH, include_blob_preview: bool = True) -> dict[str, object]:
    try:
        db = get_db(db_path)
        if table_name in READ_ONLY_TABLES:
            with db.connection() as conn:
                row_data = conn.execute(f"SELECT * FROM {quote_identifier(table_name)} WHERE id=?",
                     (int(row_id),)).fetchone() if table_name != "schema_migrations" else None
                row = dict(row_data) if row_data else None
        elif table_name == "log":
            row = db.fetch_by_rowid(table_name, int(row_id))
        else:
            row = db.fetch_by_id(table_name, int(row_id))
        if not row:
            return {"ok": False, "message": "Row not found.", "row": None}
        return {"ok": True, "table": table_name, "row": format_row(row, include_blob_preview=include_blob_preview)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "table": table_name, "row": None}


def insert_row(table_name: str, data: dict[str, Any], *, db_path: str | Path = DB_PATH) -> dict[str, object]:
    try:
        if table_name in READ_ONLY_TABLES:
            return {"ok": False, "message": "This audit table is read-only."}
        if table_name in {"species", "prediction"} and any(k in data for k in ("image", "data", "image_path", "data_path")):
            return {"ok": False, "message": "Use the dedicated species image or prediction export API to manage media files."}
        new_id = get_db(db_path).insert(table_name, normalize_data(data))
        return {"ok": True, "message": "Row inserted.", "id": new_id}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}


def update_row(table_name: str, row_id: int, data: dict[str, Any], *, db_path: str | Path = DB_PATH) -> dict[str, object]:
    try:
        db = get_db(db_path)
        if table_name in READ_ONLY_TABLES:
            return {"ok": False, "message": "This audit table is read-only."}
        if table_name in {"species", "prediction"} and any(k in data for k in ("image", "data", "image_path", "data_path")):
            return {"ok": False, "message": "Media paths can only be changed through the upload API."}
        if table_name == "log":
            updated = db.update_by_rowid(table_name, int(row_id), normalize_data(data))
        else:
            updated = db.update_by_id(table_name, int(row_id), normalize_data(data))
        return {"ok": updated > 0, "message": "Row updated." if updated else "Row not found or unchanged.", "updated": updated}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "updated": 0}


def delete_row(table_name: str, row_id: int, *, db_path: str | Path = DB_PATH) -> dict[str, object]:
    try:
        db = get_db(db_path)
        if table_name in READ_ONLY_TABLES:
            return {"ok": False, "message": "This catalog/audit table is read-only.", "deleted": 0}
        old = db.fetch_by_id("prediction", int(row_id)) if table_name == "prediction" else None
        if table_name == "log":
            deleted = db.delete_by_rowid(table_name, int(row_id))
        else:
            deleted = db.delete_by_id(table_name, int(row_id))
        if deleted and old:
            from database.media_storage import resolve_media_path
            path = resolve_media_path(old.get("data_path"), kind="prediction")
            if path and path.is_file():
                path.unlink(missing_ok=True)
        return {"ok": deleted > 0, "message": "Row deleted." if deleted else "Row not found.", "deleted": deleted}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "deleted": 0}


def clear_table(table_name: str, *, db_path: str | Path = DB_PATH) -> dict[str, object]:
    try:
        deleted = get_db(db_path).delete_all(table_name)
        return {"ok": True, "message": "Table cleared.", "deleted": deleted}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "deleted": 0}


def clear_everything_except_species(*, db_path: str | Path = DB_PATH) -> dict[str, object]:
    """Clear runtime/user-generated tables while preserving the species table and its rules/images."""
    preserved = {"species"}
    deleted: dict[str, int] = {}
    errors: dict[str, str] = {}
    try:
        db = get_db(db_path)
        from database.media_storage import resolve_media_path
        with db.connection() as conn:
            prediction_paths = [resolve_media_path(r[0], kind="prediction")
                                for r in conn.execute("SELECT data_path FROM prediction WHERE data_path IS NOT NULL")]
        tables = [name for name in db.get_table_names() if name in MODEL_REGISTRY and name not in preserved]
        with db.connect() as conn:
            for table_name in tables:
                try:
                    cursor = conn.execute(f"DELETE FROM {quote_identifier(table_name)}")
                    deleted[table_name] = int(cursor.rowcount)
                    conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table_name,))
                except Exception as exc:  # noqa: BLE001
                    errors[table_name] = str(exc)
            conn.commit()
        if not errors.get("prediction"):
            for path in prediction_paths:
                if path and path.is_file():
                    path.unlink(missing_ok=True)
        return {
            "ok": not bool(errors),
            "message": "Database cleared except species." if not errors else "Database partially cleared. Some tables failed safely.",
            "preserved": sorted(preserved),
            "cleared_tables": deleted,
            "errors": errors,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "preserved": sorted(preserved), "cleared_tables": deleted, "errors": errors}


def create_database_tables(*, db_path: str | Path = DB_PATH) -> dict[str, object]:
    try:
        db = get_db(db_path)
        db.create_tables()
        return {"ok": True, "message": "Database tables are ready.", "tables": db.get_table_names()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "tables": []}


def default_order_by(table_name: str) -> str | None:
    if table_name == "log":
        return "rowid DESC"
    if table_name in {"prediction", "water_reading", "session", "subscriber"}:
        return "id DESC"
    if table_name == "species":
        return "name COLLATE NOCASE ASC"
    return None


def normalize_data(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Database row data must be an object.")
    clean: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str) and value.startswith("data:") and "," in value:
            clean[key] = base64.b64decode(value.split(",", 1)[1])
        else:
            clean[key] = value
    return clean


def format_row(row: dict[str, Any], *, include_blob_preview: bool) -> dict[str, Any]:
    formatted: dict[str, Any] = {}
    for key, value in dict(row).items():
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            item: dict[str, Any] = {"type": "BLOB", "bytes": len(raw)}
            if include_blob_preview:
                item["base64"] = base64.b64encode(raw).decode("ascii")
            formatted[key] = item
        else:
            formatted[key] = value
    return formatted
