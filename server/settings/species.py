"""
AquaCulture Species Settings

Purpose:
- Make species records easy to list, add, edit, remove, and view.
- Keep the existing database table structure unchanged.
- Store images as verified files; only relative paths are saved in SQLite.

Expected species columns:
name, temperature, ph, oxygen, turbidity, ammonia, environment, regulation, ecology, safety, image
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[2]
DATABASE_DIR = BASE_DIR / "database"
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    from database.database import AquaCultureDB, DatabaseError
    from database.media_storage import MAX_IMAGE_BYTES, image_type, save_media, resolve_media_path
except Exception as exc:  # noqa: BLE001
    AquaCultureDB = None  # type: ignore[assignment]
    DatabaseError = RuntimeError  # type: ignore[assignment]
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

DB_PATH = DATABASE_DIR / "aquaculture.db"
REQUIRED_FIELDS = ("name", "temperature", "ph", "oxygen", "turbidity", "ammonia", "environment", "regulation", "ecology", "safety")
OPTIONAL_FIELDS = ("image", "image_path")
ALL_FIELDS = (*REQUIRED_FIELDS, *OPTIONAL_FIELDS)
RANGE_FIELDS = ("temperature", "ph", "oxygen", "turbidity", "ammonia")
TEXT_FIELDS = ("environment", "regulation", "ecology", "safety")


def get_db(db_path: str | Path = DB_PATH) -> AquaCultureDB:
    if AquaCultureDB is None:
        raise RuntimeError(f"Cannot import database helper: {_IMPORT_ERROR}")
    db = AquaCultureDB(db_path)
    db.create_tables()
    return db


def list_species(*, db_path: str | Path = DB_PATH, include_image: bool = False) -> dict[str, object]:
    try:
        database = get_db(db_path)
        if not include_image:
            # The uploaded DB contains tens of MB of image BLOBs. Never load
            # all images for the Home carousel: image thumbnails are lazy-loaded.
            with database.connection() as conn:
                rows = conn.execute("""
                    SELECT id, name, temperature, ph, oxygen, turbidity, ammonia,
                           environment, regulation, ecology, safety,
                           created_at, updated_at, image_path,
                           (image_path IS NOT NULL OR (image IS NOT NULL AND length(image) > 0)) AS has_image
                    FROM species ORDER BY name COLLATE NOCASE ASC
                """).fetchall()
                items = [{**dict(row), "has_image": bool(row["has_image"])} for row in rows]
            return {"ok": True, "species": items}
        rows = database.fetch_many("species", order_by="name COLLATE NOCASE ASC")
        return {"ok": True, "species": [format_species_row(row, include_image=True) for row in rows]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "species": []}


def list_reference_species(*, db_path: str | Path = DB_PATH) -> dict[str, object]:
    """Public, sourced species identities; these have no recommendation rules."""
    try:
        database = get_db(db_path)
        with database.connection() as conn:
            rows = conn.execute("SELECT * FROM species_reference ORDER BY common_name COLLATE NOCASE").fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for json_field, public_field in (("aliases_json", "aliases"), ("sources_json", "sources")):
                try:
                    decoded = json.loads(item.pop(json_field, "[]"))
                except (ValueError, TypeError):
                    decoded = []
                item[public_field] = decoded if isinstance(decoded, list) else []
            item.update(name=item["common_name"], environment=item["habitat"],
                        reference_only=True, has_image=False)
            items.append(item)
        distinct_count = len({str(item["scientific_name"]).casefold() for item in items
                              if "hybrid" not in str(item["scientific_name"]).lower()})
        return {"ok": True, "species": items, "total": len(items), "distinct_species": distinct_count}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "species": [], "total": 0, "distinct_species": 0}


def get_species(species_id: int, *, db_path: str | Path = DB_PATH, include_image: bool = True) -> dict[str, object]:
    try:
        row = get_db(db_path).fetch_by_id("species", int(species_id))
        if not row:
            return {"ok": False, "message": "Species not found.", "species": None}
        return {"ok": True, "species": format_species_row(row, include_image=include_image)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "species": None}


def add_species(data: dict[str, Any], *, db_path: str | Path = DB_PATH) -> dict[str, object]:
    try:
        clean = clean_species_data(data, partial=False)
        image_bytes = clean.pop("image", None)
        if image_bytes:
            if len(image_bytes) > MAX_IMAGE_BYTES:
                raise ValueError("Image exceeds the 12 MB limit.")
            image_type(image_bytes)
        database = get_db(db_path)
        new_id = database.insert("species", clean)
        if image_bytes:
            try:
                database.set_species_image(new_id, image_bytes)
            except Exception:
                database.delete_by_id("species", new_id)
                raise
        return {"ok": True, "message": "Species added.", "id": new_id}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}


def update_species(species_id: int, data: dict[str, Any], *, db_path: str | Path = DB_PATH) -> dict[str, object]:
    try:
        db = get_db(db_path)
        existing = db.fetch_by_id("species", int(species_id))
        if not existing:
            return {"ok": False, "message": "Species not found."}
        clean = clean_species_data(data, partial=True)
        if not clean:
            return {"ok": False, "message": "No valid species data supplied."}
        image_bytes = clean.pop("image", None)
        if image_bytes:
            if len(image_bytes) > MAX_IMAGE_BYTES:
                raise ValueError("Image exceeds the 12 MB limit.")
            image_type(image_bytes)
            clean["image_path"] = save_media(image_bytes, kind="species", record_id=species_id)
            clean["image"] = None
        updated = db.update_by_id("species", int(species_id), clean)
        return {"ok": True, "message": "Species updated.", "updated": updated}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}


def delete_species(species_id: int, *, db_path: str | Path = DB_PATH) -> dict[str, object]:
    try:
        database = get_db(db_path)
        row = database.fetch_by_id("species", int(species_id))
        deleted = database.delete_by_id("species", int(species_id))
        # Cleanup only files previously attached to this row, inside the managed species folder.
        path = resolve_media_path(row.get("image_path"), kind="species") if row else None
        if deleted and path and path.is_file():
            path.unlink(missing_ok=True)
        return {"ok": deleted > 0, "message": "Species deleted." if deleted else "Species not found.", "deleted": deleted}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "deleted": 0}


def find_duplicate_names(*, db_path: str | Path = DB_PATH) -> dict[str, object]:
    """Find duplicate species names without changing the database."""
    try:
        rows = get_db(db_path).fetch_many("species", order_by="name COLLATE NOCASE ASC")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            name = str(row.get("name") or "").strip()
            key = " ".join(name.lower().split())
            if not key:
                continue
            grouped.setdefault(key, []).append(format_species_row(row, include_image=False))
        duplicates = [
            {"name": items[0].get("name") or key, "count": len(items), "items": items}
            for key, items in grouped.items()
            if len(items) > 1
        ]
        return {"ok": True, "duplicates": duplicates, "duplicate_count": len(duplicates)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "duplicates": [], "duplicate_count": 0}


def set_species_image(species_id: int, image_bytes: bytes | None, *, db_path: str | Path = DB_PATH) -> dict[str, object]:
    try:
        database = get_db(db_path)
        updated = database.set_species_image(int(species_id), image_bytes)
        return {"ok": updated > 0, "message": "Species image updated." if updated else "Species not found.", "updated": updated}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "updated": 0}


def clean_species_data(data: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Species data must be an object.")

    clean: dict[str, Any] = {}
    missing: list[str] = []

    for field in REQUIRED_FIELDS:
        if field not in data:
            if not partial:
                missing.append(field)
            continue
        value = normalize_species_value(field, data.get(field))
        if value == "":
            if not partial:
                missing.append(field)
            continue
        clean[field] = value

    if missing:
        raise ValueError("Missing required species field(s): " + ", ".join(missing))

    if "image" in data:
        clean["image"] = normalize_image_value(data.get("image"))

    clean["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return clean


def normalize_species_value(field: str, value: Any) -> str:
    if value is None:
        return ""
    if field in RANGE_FIELDS:
        return normalize_range_rule(value)
    return str(value).strip()


def normalize_range_rule(value: Any) -> str:
    """Store sensor scoring rules as text, matching the current database design."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def normalize_image_value(value: Any) -> bytes | None:
    if value is None or value == "":
        return None
    if isinstance(value, bytes):
        return value
    if hasattr(value, "read"):
        return value.read()
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("data:image") and "," in text:
            text = text.split(",", 1)[1]
        try:
            return base64.b64decode(text)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("Image must be bytes or base64 text.") from exc
    raise ValueError("Unsupported image value.")


def format_species_row(row: dict[str, Any], *, include_image: bool) -> dict[str, Any]:
    item = dict(row)
    image = item.get("image")
    item["has_image"] = bool(item.get("image_path") or image)
    # No image BLOBs are included in JSON; the image route serves the file.
    item.pop("image", None)
    return item
