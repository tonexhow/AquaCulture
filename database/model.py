"""
AquaCulture Database Models
Purpose:
- Centralized table definitions for AquaCulture SQLite database.
- Uses lightweight dataclasses only; no heavy ORM.
- Imported by database.py and create_database.py.

Python: 3.13+
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, ClassVar


@dataclass(slots=True)
class BaseModel:
    """Base helper for converting dataclass rows into dictionaries."""

    table_name: ClassVar[str] = ""
    columns: ClassVar[tuple[str, ...]] = ()
    insert_columns: ClassVar[tuple[str, ...]] = ()
    blob_columns: ClassVar[tuple[str, ...]] = ()
    has_id: ClassVar[bool] = True

    def to_dict(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    def to_insert_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        allowed = self.insert_columns or tuple(data.keys())
        return {key: data[key] for key in allowed if key in data and data[key] is not None}


@dataclass(slots=True)
class Species(BaseModel):
    table_name: ClassVar[str] = "species"
    columns: ClassVar[tuple[str, ...]] = (
        "id",
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
        "image",
        "image_path",
        "created_at",
        "updated_at",
    )
    insert_columns: ClassVar[tuple[str, ...]] = (
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
        "image",
        "image_path",
        "updated_at",
    )
    blob_columns: ClassVar[tuple[str, ...]] = ("image",)

    name: str
    temperature: str
    ph: str
    oxygen: str
    turbidity: str
    ammonia: str
    environment: str
    regulation: str
    ecology: str
    safety: str
    image: bytes | None = None
    image_path: str | None = None
    updated_at: str | None = None


@dataclass(slots=True)
class Session(BaseModel):
    table_name: ClassVar[str] = "session"
    columns: ClassVar[tuple[str, ...]] = (
        "id",
        "device_token",
        "device_name",
        "ip_address",
        "mac_address",
        "is_registered",
        "user_agent",
        "last_seen_at",
        "created_at",
    )
    insert_columns: ClassVar[tuple[str, ...]] = (
        "device_token",
        "device_name",
        "ip_address",
        "mac_address",
        "is_registered",
        "user_agent",
    )

    device_token: str
    device_name: str | None = None
    ip_address: str | None = None
    mac_address: str | None = None
    is_registered: int = 0
    user_agent: str | None = None


@dataclass(slots=True)
class Log(BaseModel):
    table_name: ClassVar[str] = "log"
    columns: ClassVar[tuple[str, ...]] = ("session_id", "message", "created_at")
    insert_columns: ClassVar[tuple[str, ...]] = ("session_id", "message")
    has_id: ClassVar[bool] = False

    message: str
    session_id: int | None = None


@dataclass(slots=True)
class Prediction(BaseModel):
    table_name: ClassVar[str] = "prediction"
    columns: ClassVar[tuple[str, ...]] = ("id", "session_id", "data", "data_path", "created_at")
    insert_columns: ClassVar[tuple[str, ...]] = ("session_id", "data", "data_path")
    blob_columns: ClassVar[tuple[str, ...]] = ("data",)

    data: bytes | None = None
    data_path: str | None = None
    session_id: int | None = None


@dataclass(slots=True)
class WaterReading(BaseModel):
    table_name: ClassVar[str] = "water_reading"
    columns: ClassVar[tuple[str, ...]] = (
        "id",
        "created_at",
        "creation",
        "temperature",
        "ph",
        "oxygen",
        "turbidity",
        "ammonia",
        "ammonia_gas_ppm",
    )
    insert_columns: ClassVar[tuple[str, ...]] = (
        "creation",
        "temperature",
        "ph",
        "oxygen",
        "turbidity",
        "ammonia",
        "ammonia_gas_ppm",
    )

    creation: str
    temperature: float | None = None
    ph: float | None = None
    oxygen: float | None = None
    turbidity: float | None = None
    ammonia: float | None = None
    ammonia_gas_ppm: float | None = None


@dataclass(slots=True)
class Subscriber(BaseModel):
    table_name: ClassVar[str] = "subscriber"
    columns: ClassVar[tuple[str, ...]] = ("id", "type", "contact", "created_at", "updated_at")
    insert_columns: ClassVar[tuple[str, ...]] = ("type", "contact", "updated_at")

    type: str
    contact: str
    updated_at: str | None = None


MODEL_REGISTRY: dict[str, type[BaseModel]] = {
    Species.table_name: Species,
    Session.table_name: Session,
    Log.table_name: Log,
    Prediction.table_name: Prediction,
    WaterReading.table_name: WaterReading,
    Subscriber.table_name: Subscriber,
}


TABLE_SQL: dict[str, str] = {
    "species": """
CREATE TABLE IF NOT EXISTS species (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    temperature TEXT NOT NULL,
    ph TEXT NOT NULL,
    oxygen TEXT NOT NULL,
    turbidity TEXT NOT NULL,
    ammonia TEXT NOT NULL,
    environment TEXT NOT NULL,
    regulation TEXT NOT NULL,
    ecology TEXT NOT NULL,
    safety TEXT NOT NULL,
    image BLOB,
    image_path TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);
""",
    "session": """
CREATE TABLE IF NOT EXISTS session (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_token TEXT NOT NULL UNIQUE,
    device_name TEXT,
    ip_address TEXT,
    mac_address TEXT,
    is_registered INTEGER NOT NULL DEFAULT 0,
    user_agent TEXT,
    last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
""",
    "log": """
CREATE TABLE IF NOT EXISTS log (
    session_id INTEGER,
    message TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
""",
    "prediction": """
CREATE TABLE IF NOT EXISTS prediction (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    data BLOB,
    data_path TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
""",
    "water_reading": """
CREATE TABLE IF NOT EXISTS water_reading (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    creation TEXT NOT NULL,
    temperature REAL,
    ph REAL,
    oxygen REAL,
    turbidity REAL,
    ammonia REAL,
    ammonia_gas_ppm REAL
);
""",
    "subscriber": """
CREATE TABLE IF NOT EXISTS subscriber (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK(type IN ('SMS', 'EMAIL')),
    contact TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);
""",
}


EXPECTED_TABLES: tuple[str, ...] = (
    "species",
    "session",
    "log",
    "prediction",
    "water_reading",
    "subscriber",
)
