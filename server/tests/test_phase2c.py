"""Phase 2C data/UI/media regressions; no Flask, models or sensors required."""
from __future__ import annotations
import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'server'))

from database.media_storage import resolve_media_path, image_type, migrate_legacy_media
from database.catalog_migration import curate_species
from database.database import AquaCultureDB
from server.settings.database_tables import read_table


class Phase2CTests(unittest.TestCase):
    def test_all_legacy_species_preserved_with_archive(self):
        with sqlite3.connect(ROOT / 'database/aquaculture.db') as c:
            original_ids = [r[0] for r in c.execute('SELECT id FROM species UNION SELECT id FROM species_archive')]
            self.assertEqual(len(original_ids), 165)
            self.assertEqual(len(set(original_ids)), 165)
            self.assertEqual(c.execute('SELECT count(*) FROM species').fetchone()[0], 20)
            self.assertGreaterEqual(c.execute('SELECT count(*) FROM species_reference').fetchone()[0], 14)
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_external_storage_has_matching_paths_and_no_blobs(self):
        with sqlite3.connect(ROOT / 'database/aquaculture.db') as c:
            rows = c.execute('SELECT image_path,image FROM species UNION ALL SELECT image_path,image FROM species_archive').fetchall()
            self.assertEqual(sum(bool(x[0]) for x in rows), 14)
            for path, blob in rows:
                self.assertIsNone(blob)
                if path:
                    found = resolve_media_path(path, kind='species')
                    self.assertTrue(found and found.is_file())
                    with found.open('rb') as handle:
                        self.assertTrue(image_type(handle.read(32)))
            prediction_rows = c.execute('SELECT data_path,data FROM prediction').fetchall()
            self.assertEqual(len(prediction_rows), 25)
            for path, blob in prediction_rows:
                self.assertIsNone(blob)
                found = resolve_media_path(path, kind='prediction')
                self.assertTrue(found and found.is_file())

    def test_path_traversal_blocked(self):
        self.assertIsNone(resolve_media_path('../.env', kind='species'))
        self.assertIsNone(resolve_media_path('media/predictions/file.zip', kind='species'))
        self.assertIsNone(resolve_media_path('media/species/sample.jpg', kind='prediction'))

    def test_migrations_idempotent_on_database_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / 'test.db'
            source = sqlite3.connect(ROOT / 'database/aquaculture.db')
            dest = sqlite3.connect(copy)
            source.backup(dest)
            source.close(); dest.close()
            self.assertFalse(migrate_legacy_media(copy)['changed'])
            self.assertFalse(curate_species(copy)['applied'])
            with sqlite3.connect(copy) as c:
                self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(c.execute('SELECT count(*) FROM species_archive').fetchone()[0], 145)

    def test_catalog_is_read_only_and_paginated(self):
        for name in ('species_archive', 'species_reference'):
            result = read_table(name, limit=7, offset=3)
            self.assertTrue(result['ok'], result)
            self.assertEqual(len(result['rows']), 7)
            self.assertGreater(result['total'], 7)
        from server.settings.database_tables import update_row
        self.assertFalse(update_row('species_archive', 1, {'name':'Changed'})['ok'])

    def test_empty_database_initialization_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = AquaCultureDB(Path(tmp) / 'empty.db')
            db.create_tables()
            with db.connection() as c:
                self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertIn('image_path', [r[1] for r in c.execute('pragma table_info(species)')])
                self.assertIn('data_path', [r[1] for r in c.execute('pragma table_info(prediction)')])
                self.assertFalse([r for r in c.execute('pragma table_info(prediction)') if r[1]=='data' and r[3]])

    def test_templates_offline_assets_and_user_controls(self):
        index = (ROOT / 'client/templates/index.html').read_text()
        settings = (ROOT / 'client/templates/settings.html').read_text()
        self.assertIn('vendor/socket.io.min.js', index)
        self.assertNotIn('cdn.socket.io', index)
        self.assertIn('id="cameraFrame"', index)
        self.assertIn('id="speciesTable"', settings)
        self.assertIn('id="speciesReferenceTable"', settings)
        self.assertIn('id="notificationSensors"', settings)
        self.assertIn('id="exportTableButton"', settings)
        self.assertNotIn('user-scalable=no', settings)
        self.assertTrue((ROOT / 'client/static/vendor/socket.io.min.js').is_file())
        self.assertTrue((ROOT / 'driver/nano.ino').is_file())
        self.assertTrue((ROOT / 'driver/wiring.txt').is_file())
        self.assertFalse((ROOT / 'models').exists())

if __name__ == '__main__': unittest.main()
