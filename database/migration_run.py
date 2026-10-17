"""Run from the AquaCulture project root: python database/migration_run.py

Idempotent: new installations, migrated databases and old BLOB databases are supported.
"""
from pathlib import Path
from media_storage import migrate_legacy_media
if __name__ == '__main__':
    print(migrate_legacy_media(Path(__file__).parent / 'aquaculture.db'))
