-- Original schema extracted from supplied aquaculture.db. Does not contain records.

CREATE TABLE log (
    session_id INTEGER,
    message TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE prediction (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    data BLOB,
    data_path TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE session (
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

CREATE TABLE species (
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

CREATE TABLE subscriber (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK(type IN ('SMS', 'EMAIL')),
    contact TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);

CREATE TABLE water_reading (
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

-- Reference-only species have no invented sensor tolerances and do not enter RBS.
CREATE TABLE IF NOT EXISTS species_reference (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    common_name TEXT NOT NULL UNIQUE,
    scientific_name TEXT NOT NULL,
    habitat TEXT NOT NULL,
    source_url TEXT NOT NULL,
    scoring_status TEXT NOT NULL DEFAULT 'Reference only - ranges unverified',
    source_title TEXT NOT NULL DEFAULT '',
    catalog_scope TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    source_checked_at TEXT NOT NULL DEFAULT '',
    aliases_json TEXT NOT NULL DEFAULT '[]',
    sources_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- Out-of-scope legacy records are preserved here with original IDs and data.
CREATE TABLE IF NOT EXISTS species_archive (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
    temperature TEXT NOT NULL, ph TEXT NOT NULL, oxygen TEXT NOT NULL,
    turbidity TEXT NOT NULL, ammonia TEXT NOT NULL, environment TEXT NOT NULL,
    regulation TEXT NOT NULL, ecology TEXT NOT NULL, safety TEXT NOT NULL,
    image BLOB, image_path TEXT, created_at TEXT, updated_at TEXT,
    archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archived_reason TEXT NOT NULL DEFAULT 'Outside active PH aquaculture finfish scope'
);
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_key TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
