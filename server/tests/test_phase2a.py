"""Phase 2A smoke tests that do not require external Flask/hardware packages."""
import ast
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, Any

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / 'server'
for directory in (ROOT, SERVER):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


class Phase2ASmokeTests(unittest.TestCase):
    def test_expected_layout(self):
        for path in ('client/templates/index.html', 'client/static/index/index.js',
                     'client/static/index/phase2a.css', 'server/app.py',
                     'database/aquaculture.db', 'database/maintenance.py', '.env'):
            self.assertTrue((ROOT/path).is_file(), path)
        for absent in ('models', '.venv'):
            self.assertFalse((ROOT/absent).exists(), absent)

    def test_three_pages_and_retired_image_test(self):
        page = (ROOT/'client/templates/index.html').read_text()
        for target in ('homePage', 'waterPage', 'fishPage', 'recommendationScanButton',
                       'visualScanButton', 'testRecommendationScanButton', 'loginModal'):
            self.assertIn(f'id="{target}"', page)
        self.assertNotIn('testImageFiles', page)
        self.assertNotIn('testImageUrl', page)
        source = (SERVER/'api_manager.py').read_text()
        for route in ('/water-testing', '/fish-scanning', '/api/models/status'):
            self.assertIn(route, source)
        self.assertNotIn('/api/test/visual/upload', source)
        self.assertNotIn('/api/test/visual/url', source)

    def test_template_and_assets_render(self):
        env = Environment(loader=FileSystemLoader(str(ROOT/'client/templates')))
        env.globals['url_for'] = lambda _endpoint,filename: '/static/' + filename
        text = env.get_template('index.html').render()
        self.assertIn('/static/index/phase2a.css', text)
        self.assertIn('Fish Weight Scanning', text)
        import re
        for filename in re.findall(r"url_for\('static', filename='([^']+)'\)", (ROOT/'client/templates/index.html').read_text()):
            self.assertTrue((ROOT/'client/static'/filename).is_file(),filename)

    def test_database_records_and_indexes_preserved(self):
        database = ROOT/'database/aquaculture.db'
        from database.maintenance import ensure_query_indexes
        with sqlite3.connect(database) as conn:
            counts_before = {t:conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                             for t in ('species','prediction','water_reading')}
        self.assertGreater(counts_before['species'], 0)
        self.assertGreaterEqual(ensure_query_indexes(database),0)
        with sqlite3.connect(database) as conn:
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0],'ok')
            for t,n in counts_before.items():
                self.assertEqual(conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0], n)
            indexes = {r[0] for r in conn.execute('SELECT name FROM sqlite_master WHERE type="index"')}
            self.assertTrue({'idx_ac_species_name','idx_ac_water_created','idx_ac_prediction_created'} <= indexes)

    def test_database_index_utility_idempotent_on_temporary_copy(self):
        from database.maintenance import ensure_query_indexes
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'tiny.db'
            with sqlite3.connect(target) as conn:
                conn.execute('CREATE TABLE species (id INTEGER PRIMARY KEY,name TEXT)')
                conn.execute("INSERT INTO species(name) VALUES('Tilapia')")
            self.assertEqual(ensure_query_indexes(target),1)
            self.assertEqual(ensure_query_indexes(target),0)
            with sqlite3.connect(target) as conn:
                self.assertEqual(conn.execute('SELECT name FROM species').fetchone()[0],'Tilapia')

    def test_model_missing_safety(self):
        source = (SERVER/'api_manager.py').read_text()
        module = ast.parse(source)
        function = next(n for n in module.body if isinstance(n,ast.FunctionDef) and n.name=='_model_readiness')
        code = compile(ast.Module(body=[function],type_ignores=[]),'<model_check>','exec')
        with tempfile.TemporaryDirectory() as folder:
            local={'BASE_DIR':Path(folder),'Dict':Dict,'Any':Any}
            exec(code,local)
            status=local['_model_readiness']()
            self.assertFalse(status['ready'])
            model=Path(folder)/'models/FishInv_ncnn_model'
            model.mkdir(parents=True)
            (model/'model.ncnn.param').write_text('test')
            self.assertFalse(local['_model_readiness']()['ready'])
            (model/'model.ncnn.bin').write_bytes(b'placeholder')
            self.assertTrue(local['_model_readiness']()['ready'])

    def test_physics_warning(self):
        html=(ROOT/'client/templates/index.html').read_text()
        source=(ROOT/'client/static/index/index.js').read_text()
        self.assertIn('MQ-137 measures gas',source)
        self.assertIn('id="testAmmonia"',html)
        self.assertNotIn('disabled title="Gas-risk scoring',html)
        self.assertIn('No demo values are substituted',html)

    def test_original_manual_recommendation_still_runs(self):
        from scanner.fish_recommendation import recommend_from_values
        result = recommend_from_values({'temperature':28.5, 'ph':7.2, 'oxygen':6.8}, top=3)
        self.assertTrue(result.get('ok'))
        self.assertTrue(result.get('result', {}).get('suggestions'))
        self.assertEqual(len(result['result']['suggestions']),3)

    def test_placeholder_config_and_ble_deferred(self):
        config=(ROOT/'.env').read_text()
        self.assertIn('UNISMS_API_SECRET=\n',config)
        self.assertIn('AQUACULTURE_BLE_ENABLED=0',config)
        from network.ble_service import provisioning_status
        self.assertEqual(provisioning_status()['status'],'not_implemented')

if __name__=='__main__': unittest.main()
