"""Editor and lifecycle regression checks use disposable state, never live data."""
import copy
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from zipfile import ZipFile
from unittest.mock import Mock, patch

from adaptive_crypto.core import Rules, load_settings
from adaptive_crypto.ledger import atomic_json
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.settings_editor import read_editor
from adaptive_crypto.state_paths import open_stores
from adaptive_crypto.web import create_app


class SettingsEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'settings.json'
        self.document = {'assets': [dict(name='BTC', symbol='BTC/USD', enabled=True, price_decimals=2),
                                    dict(name='SOL', symbol='SOL/USD', enabled=False, price_decimals=3)],
                         'strategy': asdict(Rules(strategy_model='neural_network')), 'refresh_seconds': 15}
        atomic_json(self.path, self.document)
        assets, rules, refresh = load_settings(self.path)
        self.owner = open_stores(self.root/'state.json', assets, rules, 'json')
        store, positions = self.owner.__enter__()
        self.addCleanup(self.owner.__exit__, None, None, None)
        self.runtime = DashboardRuntime(assets, rules, store, refresh, position_store=positions, settings_path=self.path)
        self.runtime.state_base = self.root/'state.json'
        self.restart = Mock()
        self.app = create_app(self.runtime, restart_callback=self.restart)
        self.client = self.app.test_client()
        self.client.get('/settings')
        with self.client.session_transaction() as session:
            self.token = session['csrf_token']

    def post(self, path, body=None):
        return self.client.post(path, json=body or {}, headers={'X-CSRF-Token': self.token})

    def test_all_rules_and_disabled_pairs_are_editable(self):
        html = self.client.get('/settings').get_data(as_text=True)
        for field in ('smc_entry_method','smc_setup_minutes','smc_entry_minutes','nn_stop_loss','risk_per_trade'):
            self.assertIn(f'data-rule="{field}"', html)
        for field in ('strategy_model','momentum_rsi_period','liquidity_window'):
            self.assertNotIn(f'data-rule="{field}"', html)
        self.assertIn('id="saved-strategy"',html)
        self.assertIn('value="SOL/USD"', html)
        home = self.client.get('/').get_data(as_text=True)
        self.assertIn('href="/settings"', home)
        self.assertNotIn('id="model-settings"', home)
        self.assertNotIn('id="settings-database"', home)

    def test_save_validates_backs_up_and_does_not_apply(self):
        doc, revision = read_editor(self.runtime)
        doc['refresh_seconds'] = 25
        doc['assets'][1]['enabled'] = True
        doc['strategy']['fee_rate'] = .002
        before = self.path.read_bytes()
        response = self.post('/api/settings/save', {'settings': doc, 'revision': revision})
        self.assertEqual(response.status_code, 200, response.json)
        with ZipFile(response.json['backup']) as backup:
            self.assertEqual(backup.read('settings/settings.json'), before)
            self.assertEqual(json.loads(backup.read('metadata/documents.json'))['documents']['positions'],
                             self.runtime.positions.snapshot())
        self.assertEqual(json.loads(self.path.read_text()), doc)
        self.assertEqual(self.runtime.refresh, 15)
        applied = self.post('/api/settings/apply')
        self.assertEqual(applied.status_code, 200, applied.json)
        self.assertEqual(self.runtime.refresh, 25)
        self.assertIn('SOL', self.runtime.assets)

    def test_bad_fields_and_stale_revision_leave_settings_unchanged(self):
        doc, revision = read_editor(self.runtime)
        before = self.path.read_bytes()
        candidates = []
        for key, value in [('refresh_seconds', 1), ('refresh_seconds', True)]:
            candidate = copy.deepcopy(doc)
            candidate[key] = value
            candidates.append(candidate)
        candidate = copy.deepcopy(doc)
        candidate['assets'][1]['symbol'] = 'SOL/EUR'
        candidates.append(candidate)
        candidate = copy.deepcopy(doc)
        candidate['strategy']['risk_per_trade'] = .9
        candidates.append(candidate)
        for candidate in candidates:
            response = self.post('/api/settings/save', {'settings': candidate, 'revision': revision})
            self.assertEqual(response.status_code, 400, response.json)
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.post('/api/settings/save', {'settings': doc, 'revision': 'old'}).status_code, 400)
        self.assertEqual(self.path.read_bytes(), before)

    def test_save_and_restart_require_csrf(self):
        doc, revision = read_editor(self.runtime)
        self.assertEqual(self.client.post('/api/settings/save', json={'settings': doc, 'revision': revision}).status_code, 400)
        self.assertEqual(self.client.post('/api/dashboard/restart', json={}).status_code, 400)
        self.restart.assert_not_called()

    def test_restart_checks_saved_configuration_and_retains_records(self):
        self.runtime.positions.open_position(self.runtime.assets, 'BTC', 'long', 100, 1, 'a'*32, 100)
        before = self.runtime.positions.snapshot()
        status = self.client.get('/api/dashboard/status').json
        response = self.post('/api/dashboard/restart')
        self.assertEqual(response.status_code, 202, response.json)
        self.restart.assert_called_once()
        self.assertEqual(response.json['generation'], status['generation'])
        self.assertTrue(self.runtime.stop.is_set())
        self.assertEqual(self.runtime.positions.snapshot(), before)
        self.assertEqual(self.post('/api/dashboard/restart').status_code, 503)

    def test_invalid_saved_settings_prevent_restart(self):
        self.path.write_text('{"bad": true}')
        response = self.post('/api/dashboard/restart')
        self.assertEqual(response.status_code, 400)
        self.restart.assert_not_called()
        self.assertFalse(self.runtime.stop.is_set())


if __name__ == '__main__':
    unittest.main()
