"""Exercise the actual HTTP listener and worker/store lifecycle without market calls."""
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import requests
from werkzeug.serving import make_server
from adaptive_crypto import cli
from adaptive_crypto.ledger import atomic_json


class RestartLifecycleTests(unittest.TestCase):
    def test_repeated_restart_reloads_settings_and_preserves_holdings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root/'settings.json'
            atomic_json(path, {'assets': [{'name': 'BTC', 'symbol': 'BTC/USD', 'enabled': True, 'price_decimals': 2}],
                               'strategy': {'strategy_model': 'smc_video'}, 'refresh_seconds': 15})
            with socket.socket() as reserve:
                reserve.bind(('127.0.0.1', 0))
                port = reserve.getsockname()[1]
            servers, errors = [], []
            def server_factory(*args, **kwargs):
                server = make_server(*args, **kwargs)
                servers.append(server)
                return server
            def run():
                try:
                    cli.main()
                except BaseException as exc:
                    errors.append(exc)
            argv = ['dashboard', '--settings', str(path), '--state', str(root/'state.json'),
                    '--state-backend', 'sqlite', '--port', str(port), '--host', '127.0.0.1']
            with patch('sys.argv', argv), patch('werkzeug.serving.make_server', side_effect=server_factory), \
                 patch.object(cli.DashboardRuntime, 'run', lambda runtime: runtime.stop.wait()), \
                 patch.object(cli, 'worker', lambda store, kind, stop: stop.wait()):
                thread = threading.Thread(target=run)
                thread.start()
                base = f'http://127.0.0.1:{port}'
                session = requests.Session()
                def wait_ready(previous=None):
                    deadline = time.monotonic()+12
                    while time.monotonic() < deadline:
                        try:
                            data = session.get(base+'/api/dashboard/status', timeout=1).json()
                            if data['generation'] != previous and not data['restarting']:
                                return data
                        except requests.RequestException:
                            pass
                        time.sleep(.1)
                    self.fail(f'Dashboard did not return: {errors}')
                def token():
                    import re
                    html = session.get(base+'/settings', timeout=3).text
                    return re.search('name="csrf_token" value="([^"]+)"', html)[1]
                try:
                    state = wait_ready()
                    headers = {'X-CSRF-Token': token()}
                    opened = session.post(base+'/api/positions', headers=headers,
                        json={'asset':'BTC','side':'long','entry':100,'quantity':1,'request_id':'b'*32}, timeout=3)
                    self.assertEqual(opened.status_code, 201, opened.text)
                    position_id = opened.json()['position']['id']
                    for model, refresh in [('smc_video', 20), ('neural_network', 25)]:
                        doc = json.loads(path.read_text())
                        doc['strategy']['strategy_model'] = model
                        doc['refresh_seconds'] = refresh
                        atomic_json(path, doc)
                        response = session.post(base+'/api/dashboard/restart', headers={'X-CSRF-Token': token()}, json={}, timeout=4)
                        self.assertEqual(response.status_code, 202, response.text)
                        state = wait_ready(state['generation'])
                        self.assertEqual(state['strategy_model'], 'neural_network')
                        snapshot = session.get(base+'/api/state', timeout=3).json()
                        self.assertEqual(snapshot['holdings']['positions'][0]['id'], position_id)
                        page = session.get(base+'/settings', timeout=3).text
                        self.assertIn(f'value="{refresh}"', page)
                    self.assertEqual(len(servers), 3)
                finally:
                    if servers:
                        servers[-1].shutdown()
                    thread.join(8)
                    session.close()
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
