"""Versioned curation and additive, sourced reference-species migrations.

No RBS equations, sensor thresholds or stored legacy ranges are changed.
Legacy records are archived with their original IDs and rule text.
Only reviewed pond/coastal FINFISH remain in the active recommendation table.
New reference-only entries never get made-up sensor ranges. The expanded
catalog is independent of the original one-time legacy-scope migration.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

# This is a conservative *scope* shortlist, NOT a claim that the legacy
# tolerances are scientifically verified. These are cultured/locally relevant
# freshwater/brackish/coastal finfish. IDs are from the original user database.
ACTIVE_LEGACY_IDS = (
    1, 3, 5, 6, 7, 8, 9, 11, 12, 13, 15, 17, 18,
    42, 43, 44, 45, 46, 47, 51,
)
CATALOG_SOURCE = 'https://www.seafdec.org.ph/our-organization/stations-facilities/tigbauan-main-station/'
BFAR_SOURCE = 'https://www.foi.gov.ph/requests/major-aquatic-species-in-the-philippines/'
REFERENCE_ONLY = [
    ('Milkfish / Bangus', 'Chanos chanos', 'Brackish / Marine', CATALOG_SOURCE),
    ('Nile Tilapia', 'Oreochromis niloticus', 'Freshwater', BFAR_SOURCE),
    ('Red Tilapia', 'Oreochromis hybrid', 'Freshwater / Brackish', BFAR_SOURCE),
    ('Native Catfish / Hito', 'Clarias macrocephalus', 'Freshwater', CATALOG_SOURCE),
    ('African Catfish', 'Clarias gariepinus', 'Freshwater', 'https://www.seafdec.org.ph/free-downloads/'),
    ('Asian Seabass / Apahap', 'Lates calcarifer', 'Brackish / Marine', CATALOG_SOURCE),
    ('Orange-spotted Grouper', 'Epinephelus coioides', 'Brackish / Marine', CATALOG_SOURCE),
    ('Brown-marbled Grouper', 'Epinephelus fuscoguttatus', 'Marine', CATALOG_SOURCE),
    ('Mangrove Red Snapper', 'Lutjanus argentimaculatus', 'Brackish / Marine', CATALOG_SOURCE),
    ('Asian Pompano', 'Trachinotus blochii', 'Marine', CATALOG_SOURCE),
    ('Golden Rabbitfish', 'Siganus guttatus', 'Brackish / Marine', CATALOG_SOURCE),
    ('Silver Therapon', 'Leiopotherapon plumbeus', 'Freshwater', CATALOG_SOURCE),
    ('Striped Catfish', 'Pangasianodon hypophthalmus', 'Freshwater', BFAR_SOURCE),
    ('Common Snakehead / Dalag', 'Channa striata', 'Freshwater', BFAR_SOURCE),
]
REFERENCE_CATALOG_PATH = Path(__file__).with_name('species_catalog.json')
REFERENCE_METADATA_COLUMNS = {
    'source_title': "TEXT NOT NULL DEFAULT ''",
    'catalog_scope': "TEXT NOT NULL DEFAULT ''",
    'notes': "TEXT NOT NULL DEFAULT ''",
    'source_checked_at': "TEXT NOT NULL DEFAULT ''",
    'aliases_json': "TEXT NOT NULL DEFAULT '[]'",
    'sources_json': "TEXT NOT NULL DEFAULT '[]'",
}


def load_reference_catalog() -> tuple[dict[str, object], list[dict[str, object]]]:
    """Read bundled identities and provenance; never infer sensor tolerances."""
    catalog = json.loads(REFERENCE_CATALOG_PATH.read_text(encoding='utf-8'))
    sources = catalog['sources']
    entries: list[dict[str, object]] = []
    scientific_names: set[str] = set()
    for item in catalog['species']:
        scientific = item['scientific_name'].strip()
        if scientific.casefold() in scientific_names:
            raise ValueError(f'Duplicate reference scientific name: {scientific}')
        scientific_names.add(scientific.casefold())
        references = [sources[source_id] for source_id in item['source_ids']]
        entries.append({
            'common_name': item['common_name'],
            'scientific_name': scientific,
            'habitat': item['habitat'],
            'source_url': references[0]['url'],
            'scoring_status': 'Reference only - ranges unverified',
            'source_title': references[0]['title'],
            'catalog_scope': item['catalog_scope'],
            'notes': item.get('notes', ''),
            'source_checked_at': catalog['checked_at'],
            'aliases_json': json.dumps(item.get('aliases', []), ensure_ascii=False),
            'sources_json': json.dumps(references, ensure_ascii=False),
        })
    # Hybrid groups/strains do not increase the biological species count.
    distinct_species = [entry for entry in entries if 'hybrid' not in str(entry['scientific_name']).lower()]
    if len(distinct_species) < 50:
        raise ValueError('The bundled reference catalog must contain at least 50 distinct species.')
    return catalog, entries


def expand_reference_catalog(conn: sqlite3.Connection) -> int:
    """Apply only additive reference rows/metadata; preserve user-edited values."""
    catalog, entries = load_reference_catalog()
    migration_key = f"reference_species_catalog_{catalog['version']}"
    existing_columns = {row[1] for row in conn.execute('PRAGMA table_info(species_reference)')}
    for column, definition in REFERENCE_METADATA_COLUMNS.items():
        if column not in existing_columns:
            conn.execute(f'ALTER TABLE species_reference ADD COLUMN {column} {definition}')
    if conn.execute('SELECT 1 FROM schema_migrations WHERE migration_key=?', (migration_key,)).fetchone():
        return 0
    legacy_sources = {(row[0], row[1]): row[3] for row in REFERENCE_ONLY}
    added = 0
    for entry in entries:
        # Scientific identity prevents duplicate records under another common name.
        existing = conn.execute('''SELECT id, common_name, scientific_name, source_url
            FROM species_reference WHERE lower(scientific_name)=lower(?) OR common_name=?
            ORDER BY id LIMIT 1''', (entry['scientific_name'], entry['common_name'])).fetchone()
        if existing:
            species_id, common, scientific, old_url = existing
            if str(scientific).casefold() != str(entry['scientific_name']).casefold():
                # An administrator gave this name a different identity; do not change it.
                continue
            # Populate new empty provenance fields only, preserving later manual changes.
            for column in REFERENCE_METADATA_COLUMNS:
                conn.execute(f'''UPDATE species_reference SET {column}=?
                    WHERE id=? AND ({column}='' OR {column}='[]')''', (entry[column], species_id))
            # Replace the old bundled generic homepage links, not custom source URLs.
            if old_url == legacy_sources.get((common, scientific)):
                conn.execute('UPDATE species_reference SET source_url=? WHERE id=?', (entry['source_url'], species_id))
            continue
        columns = ', '.join(entry)
        placeholders = ', '.join('?' for _ in entry)
        conn.execute(f'INSERT INTO species_reference ({columns}) VALUES ({placeholders})', tuple(entry.values()))
        added += 1
    conn.execute('INSERT INTO schema_migrations (migration_key) VALUES (?)', (migration_key,))
    return added


def curate_species(db_path: Path, *, archive_legacy: bool = False) -> dict[str, object]:
    """Add reference metadata; legacy active-row archiving requires explicit opt-in.

    A production database can contain user records with the bundled catalog's
    old IDs. Normal startup must never infer their identity from IDs alone.
    """
    db_path = Path(db_path)
    output: dict[str, object] = {'archived': 0, 'active': 0, 'reference_only': 0,
                                'reference_added': 0, 'applied': False}
    if not db_path.is_file():
        return output
    with closing(sqlite3.connect(db_path, timeout=45)) as conn, conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS species_archive (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            temperature TEXT NOT NULL, ph TEXT NOT NULL, oxygen TEXT NOT NULL,
            turbidity TEXT NOT NULL, ammonia TEXT NOT NULL, environment TEXT NOT NULL,
            regulation TEXT NOT NULL, ecology TEXT NOT NULL, safety TEXT NOT NULL,
            image BLOB, image_path TEXT, created_at TEXT, updated_at TEXT,
            archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            archived_reason TEXT NOT NULL DEFAULT 'Outside active PH aquaculture finfish scope'
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS species_reference (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            common_name TEXT NOT NULL UNIQUE,
            scientific_name TEXT NOT NULL,
            habitat TEXT NOT NULL,
            source_url TEXT NOT NULL,
            scoring_status TEXT NOT NULL DEFAULT 'Reference only - ranges unverified',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_key TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''')
        migration_key = 'legacy_species_scope_2026_10_16'
        scope_applied = bool(conn.execute('SELECT 1 FROM schema_migrations WHERE migration_key=?', (migration_key,)).fetchone())
        has_legacy_species = conn.execute('SELECT count(*) FROM species').fetchone()[0] > 0
        # Legacy scoping is a separate opt-in maintenance operation, never a boot side effect.
        placeholders = ','.join('?' for _ in ACTIVE_LEGACY_IDS)
        candidates = conn.execute(
            f'SELECT id FROM species WHERE id <= 165 AND id NOT IN ({placeholders})', ACTIVE_LEGACY_IDS
        ).fetchall() if archive_legacy and not scope_applied and has_legacy_species else []
        cols = ('id','name','temperature','ph','oxygen','turbidity','ammonia',
                'environment','regulation','ecology','safety','image','image_path','created_at','updated_at')
        column_sql = ', '.join(cols)
        try:
            conn.execute('BEGIN IMMEDIATE')
            for (species_id,) in candidates:
                # An archive ID conflict aborts atomically; never delete an active
                # row whose distinct content failed to enter the archive.
                conn.execute(f'''INSERT INTO species_archive ({column_sql})
                    SELECT {column_sql} FROM species WHERE id=?''', (species_id,))
                conn.execute('DELETE FROM species WHERE id=?', (species_id,))
            if not scope_applied and has_legacy_species:
                for common, scientific, habitat, source in REFERENCE_ONLY:
                    conn.execute('''INSERT OR IGNORE INTO species_reference
                        (common_name,scientific_name,habitat,source_url) VALUES (?,?,?,?)''',
                        (common,scientific,habitat,source))
                if archive_legacy:
                    conn.execute('INSERT INTO schema_migrations (migration_key) VALUES (?)', (migration_key,))
            reference_added = expand_reference_catalog(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        conn.execute('CREATE INDEX IF NOT EXISTS idx_species_reference_habitat ON species_reference(habitat)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_species_archive_name ON species_archive(name COLLATE NOCASE)')
        conn.execute('ANALYZE')
        output.update(archived=len(candidates), active=conn.execute('SELECT count(*) FROM species').fetchone()[0],
                      reference_only=conn.execute('SELECT count(*) FROM species_reference').fetchone()[0],
                      reference_added=reference_added,
                      applied=bool((archive_legacy and not scope_applied and has_legacy_species) or reference_added))
    return output
