"""UI/file-handling regression guards; requires no Flask or hardware."""
import ast
import sqlite3
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

class Phase2BPart1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        api_source = (ROOT / 'server/api_manager.py').read_text(encoding='utf-8')
        module = ast.parse(api_source)
        mime_function = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == 'guess_mime')
        local = {}
        exec(compile(ast.Module(body=[mime_function], type_ignores=[]), '<mime>', 'exec'), local)
        cls.guess_mime = staticmethod(local['guess_mime'])
        cls.source = api_source

    def test_rbs_three_sensors_and_ammonia_still_present(self):
        import sys
        sys.path[:0] = [str(ROOT), str(ROOT / 'server')]
        from scanner.fish_recommendation import WATER_KEYS, MINIMUM_RECOMMENDATION_SENSOR_COUNT
        self.assertEqual(MINIMUM_RECOMMENDATION_SENSOR_COUNT, 3)
        self.assertEqual(set(WATER_KEYS), {'temperature','ph','turbidity','oxygen','ammonia'})

    def test_species_image_mime_matches_existing_database(self):
        expected = {1:'image/jpeg', 12:'image/webp', 159:'image/avif', 160:'image/webp', 161:'image/webp', 165:'image/png'}
        from database.media_storage import resolve_media_path, image_type
        with sqlite3.connect(ROOT/'database/aquaculture.db') as db:
            for ident, mime in expected.items():
                row = db.execute('SELECT image_path FROM species WHERE id=?',(ident,)).fetchone()
                if row is None:
                    row = db.execute('SELECT image_path FROM species_archive WHERE id=?',(ident,)).fetchone()
                self.assertIsNotNone(row,ident)
                path = resolve_media_path(row[0],kind='species')
                self.assertTrue(path and path.is_file(),ident)
                with path.open('rb') as picture:
                    self.assertEqual(image_type(picture.read(32))[1],mime,ident)
        self.assertEqual(self.guess_mime(b'not-an-image'), 'application/octet-stream')

    def test_species_image_endpoint_uses_blob_not_base64(self):
        route = self.source.split('def api_species_image(species_id:',1)[1].split('@app.post("/api/admin/species")',1)[0]
        self.assertIn('SELECT image_path, image FROM species WHERE id=?',route)
        self.assertNotIn('include_image=True',route)
        self.assertIn('fallback-species.svg',route)
        self.assertIn('X-Content-Type-Options',route)

    def test_navigation_and_controls(self):
        html=(ROOT/'client/templates/index.html').read_text(encoding='utf-8')
        js=(ROOT/'client/static/index/index.js').read_text(encoding='utf-8')
        css=(ROOT/'client/static/index/phase2b-part1.css').read_text(encoding='utf-8')
        self.assertIn('bulma/css/bulma.min.css',html)
        for value in ('homePage','waterPage','fishPage','mirrorCameraButton','rotateCameraButton','resetCameraButton','loginModal'):
            self.assertIn(f'id="{value}"',html)
        for value in ('cameraRotation', 'cameraMirrored', 'applyCameraView', 'bindCameraViewControls'):
            self.assertIn(value,js)
        self.assertIn('position: fixed;',css)
        self.assertIn('window.AQUACULTURE_ENABLE_WEBSOCKET',js)

    def test_database_counts_unmodified(self):
        with sqlite3.connect(ROOT/'database/aquaculture.db') as db:
            self.assertEqual(db.execute('pragma integrity_check').fetchone()[0], 'ok')
            active = db.execute('select count(*) from species').fetchone()[0]
            archived = db.execute('select count(*) from species_archive').fetchone()[0]
            self.assertEqual(active + archived, 165)
            self.assertEqual(db.execute('select count(*) from prediction').fetchone()[0], 25)
            self.assertEqual(db.execute('select count(*) from water_reading').fetchone()[0], 1)

if __name__ == '__main__':unittest.main()
