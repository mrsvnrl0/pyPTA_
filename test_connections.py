"""Offline provider, credential boundary, live-switch and settings regressions."""
import copy
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from adaptive_crypto.ai_providers import ai_request, check_telegram, ProviderError
from adaptive_crypto.connection_credentials import ConnectionCredentials
from adaptive_crypto.connections import Connections
from adaptive_crypto.core import DataError, Rules
from adaptive_crypto.ledger import StateStore, atomic_json, queue_event
from adaptive_crypto.notifications import ai_comment, telegram_send
from adaptive_crypto.runtime import DashboardRuntime
from adaptive_crypto.web import create_app
from test_deribit_settings import fake_protector

ASSETS = {"BTC": {"symbol": "BTC/USD", "price_decimals": 2}}
KEY_A, KEY_G, TOKEN, CHAT = 'private-openai-fixture', 'private-gemini-fixture', '123:private-bot-fixture', '-1234567'


def response(body, status=200):
    result = Mock(status_code=status)
    result.json.return_value = body
    return result


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {}
        self.store = ConnectionCredentials(self.root, environment=self.env, protector=fake_protector)

    def test_empty_and_partial_environment_do_not_write_or_report_ready(self):
        self.assertIsNone(self.store.read('ai'))
        self.assertIsNone(self.store.read('telegram'))
        self.env['OPENAI_API_KEY'] = KEY_A
        self.assertFalse(self.store.status('ai')['configured'])
        with self.assertRaises(DataError): self.store.read('ai')
        self.env['TELEGRAM_BOT_TOKEN'] = TOKEN
        with self.assertRaises(DataError): self.store.read('telegram')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_provider_keys_and_models_survive_switching_without_resubmitting_secrets(self):
        self.store.save_ai('openai', 'model-a', KEY_A)
        self.store.save_ai('gemini', 'models/model-g', KEY_G)
        self.assertEqual(self.store.read('ai'), {'provider':'gemini','api_key':KEY_G,'model':'model-g'})
        self.store.save_ai('openai', 'model-a2', '')
        self.assertEqual(self.store.read('ai')['api_key'], KEY_A)
        self.assertEqual(self.store.status('ai')['profiles']['gemini']['model'], 'model-g')
        raw = self.store.path('ai').read_bytes()
        self.assertNotIn(KEY_A.encode(), raw)
        self.assertNotIn(KEY_G.encode(), raw)
        self.assertNotIn(KEY_A, json.dumps(self.store.status('ai')))
        self.assertNotIn(KEY_G, json.dumps(self.store.status('ai')))

    def test_missing_key_for_other_provider_never_uses_openai_key(self):
        self.store.save_ai('openai', 'model-a', KEY_A)
        original = self.store.path('ai').read_bytes()
        with self.assertRaises(DataError): self.store.save_ai('gemini','model-g','')
        self.assertEqual(original, self.store.path('ai').read_bytes())

    def test_saved_values_override_environment_and_disable_survives_restart(self):
        self.env.update(OPENAI_API_KEY=KEY_A, OPENAI_MODEL='model-a', TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT)
        self.store.save_ai('openai','model-a','')
        self.store.save_telegram('','')
        self.env['OPENAI_API_KEY'] = 'later-environment-key'
        self.assertEqual(self.store.read('ai')['api_key'], KEY_A)
        self.store.disable('ai')
        self.assertIsNotNone(self.store.read('telegram'))
        self.store.disable('telegram')
        restarted = ConnectionCredentials(self.root,environment=self.env,protector=fake_protector)
        self.assertIsNone(restarted.read('ai'))
        self.assertIsNone(restarted.read('telegram'))
        restarted.save_ai('openai','model-a','')
        restarted.save_telegram('','')
        self.assertEqual(restarted.read('ai')['api_key'], KEY_A)
        self.assertEqual(restarted.read('telegram')['bot_token'], TOKEN)

    def test_telegram_blank_fields_keep_current_values_and_status_hides_destination(self):
        self.store.save_telegram(TOKEN, CHAT)
        self.store.save_telegram('', '-7654321')
        self.assertEqual(self.store.read('telegram'), {'bot_token':TOKEN,'chat_id':'-7654321'})
        self.store.save_telegram('456:new-bot','')
        self.assertEqual(self.store.read('telegram')['chat_id'], '-7654321')
        for value in (TOKEN,CHAT,'-7654321'):
            self.assertNotIn(value, json.dumps(self.store.status('telegram')))

    def test_invalid_or_corrupt_saved_file_never_falls_back_to_environment(self):
        self.env.update(OPENAI_API_KEY=KEY_A,OPENAI_MODEL='model-a')
        self.store.path('ai').write_bytes(b'corrupt')
        with self.assertRaises(DataError): self.store.read('ai')
        self.assertFalse(self.store.status('ai')['configured'])
        with self.assertRaises(DataError): self.store.save_ai('openai','model-a','')
        self.store.save_ai('gemini','model-g',KEY_G)
        self.assertEqual(self.store.read('ai')['api_key'],KEY_G)

    def test_failed_atomic_replace_keeps_old_credentials_and_cleans_temporary_file(self):
        self.store.save_ai('openai','model-a',KEY_A)
        before=self.store.path('ai').read_bytes()
        with patch('adaptive_crypto.connection_credentials.os.replace',side_effect=OSError(KEY_G)):
            with self.assertRaises(DataError) as failure: self.store.save_ai('gemini','model-g',KEY_G)
        self.assertNotIn(KEY_G,str(failure.exception))
        self.assertEqual(self.store.path('ai').read_bytes(),before)
        self.assertEqual(list(self.root.glob('.connection-*.tmp')),[])

    def test_validation_blocks_model_urls_token_paths_and_unknown_provider(self):
        for provider,model,key in [('unknown','model',KEY_A),('gemini','https://other.test',KEY_G),
                                   ('openai','../secret',KEY_A),('openai','valid','bad\nkey')]:
            with self.assertRaises(DataError): self.store.save_ai(provider,model,key)
        for token,chat in [('abc/../../',CHAT),(TOKEN,'https://other.test'),(TOKEN,'')]:
            with self.assertRaises(DataError): self.store.save_telegram(token,chat)

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI verification')
    def test_real_windows_encryption_roundtrip_in_disposable_directory(self):
        store=ConnectionCredentials(self.root,environment={})
        store.save_ai('gemini','model-g',KEY_G)
        store.save_telegram(TOKEN,CHAT)
        self.assertEqual(store.read('ai')['api_key'],KEY_G)
        self.assertEqual(store.read('telegram')['bot_token'],TOKEN)
        self.assertNotIn(KEY_G.encode(),store.path('ai').read_bytes())
        self.assertNotIn(TOKEN.encode(),store.path('telegram').read_bytes())


class ProviderTests(unittest.TestCase):
    def config(self, provider='openai'):
        return {'provider':provider,'api_key':KEY_A if provider=='openai' else KEY_G,'model':'text-model'}

    def test_openai_responses_request_uses_selected_model_and_ignores_reasoning(self):
        body={'status':'completed','output':[{'type':'reasoning','content':[{'type':'output_text','text':'private-thought'}]},
             {'type':'message','content':[{'type':'output_text','text':'A measured signal.'}]}]}
        with patch('requests.sessions.Session.request',return_value=response(body)) as request:
            result=ai_comment({'payload':{'price':100}},config=self.config())
        self.assertEqual(result['commentary'],'A measured signal.')
        self.assertEqual(result['ai_provider'],'openai')
        call=request.call_args
        self.assertEqual(call.args,('POST','https://api.openai.com/v1/responses'))
        self.assertEqual(call.kwargs['headers']['Authorization'],'Bearer '+KEY_A)
        self.assertFalse(call.kwargs['json']['store'])
        self.assertEqual(call.kwargs['json']['model'],'text-model')
        self.assertFalse(call.kwargs['allow_redirects'])
        self.assertNotIn(KEY_A,json.dumps(call.kwargs['json']))

    def test_gemini_native_request_filters_thoughts_and_returns_text(self):
        body={'candidates':[{'finishReason':'STOP','content':{'parts':[{'thought':True,'text':'private-thought'}, {'text':'Commentary.'}]}}]}
        with patch('requests.sessions.Session.request',return_value=response(body)) as request:
            result=ai_comment({'payload':{'signal':'BUY'}},config=self.config('gemini'))
        self.assertEqual(result['commentary'],'Commentary.')
        self.assertEqual(result['ai_provider'],'gemini')
        call=request.call_args
        self.assertTrue(call.args[1].endswith('/text-model:generateContent'))
        self.assertEqual(call.kwargs['headers']['x-goog-api-key'],KEY_G)
        self.assertNotIn('Authorization',call.kwargs['headers'])
        self.assertIn('systemInstruction',call.kwargs['json'])
        self.assertNotIn(KEY_G,call.args[1])

    def test_blocked_empty_truncated_and_malformed_ai_responses_fail_without_echoing(self):
        cases=[('openai',{'status':'incomplete','output':[]}),('openai',{'status':'completed','output':[]}),
               ('openai',{'status':'completed','output':KEY_A}),('gemini',{'promptFeedback':{'blockReason':'SAFETY'}}),
               ('gemini',{'candidates':[{'finishReason':'MAX_TOKENS','content':{'parts':[{'text':KEY_G}]}}]})]
        for provider,body in cases:
            with patch('requests.sessions.Session.request',return_value=response(body)):
                result=ai_comment({'payload':{}},config=self.config(provider))
            self.assertEqual(result['status'],'failed')
            self.assertNotIn(KEY_A+KEY_G,json.dumps(result))
            self.assertNotIn(KEY_A,json.dumps(result))
            self.assertNotIn(KEY_G,json.dumps(result))

    def test_provider_errors_redirects_and_transport_exceptions_never_echo_secrets_or_fallback(self):
        for status in (401,403,404,429,500,302):
            with patch('requests.sessions.Session.request',return_value=response({'error':KEY_G},status)) as request:
                result=ai_comment({'payload':{}},config=self.config('gemini'))
            self.assertEqual(request.call_count,1)
            self.assertEqual(result['status'],'failed')
            self.assertNotIn(KEY_G,json.dumps(result))
        with patch('requests.sessions.Session.request',side_effect=RuntimeError(KEY_A)):
            self.assertNotIn(KEY_A,json.dumps(ai_comment({'payload':{}},config=self.config())))

    def test_checks_only_retrieve_model_metadata_and_never_generate(self):
        for provider,body in [('openai',{'id':'text-model'}),('gemini',{'supportedGenerationMethods':['generateContent']})]:
            with patch('requests.sessions.Session.request',return_value=response(body)) as request:
                text=ai_request(self.config(provider),check=True)
            self.assertIn('has not been tested',text)
            self.assertEqual(request.call_args.args[0],'GET')
            self.assertNotIn(':generateContent',request.call_args.args[1])
            self.assertIsNone(request.call_args.kwargs['json'])

    def test_gemini_check_rejects_embedding_only_model(self):
        with patch('requests.sessions.Session.request',return_value=response({'supportedGenerationMethods':['embedContent']})):
            with self.assertRaises(ProviderError): ai_request(self.config('gemini'),check=True)

    def test_telegram_check_never_sends_a_message(self):
        with patch('requests.sessions.Session.request',side_effect=[response({'ok':True,'result':{'is_bot':True}}),response({'ok':True,'result':{'id':123}})]) as request:
            text=check_telegram({'bot_token':TOKEN,'chat_id':CHAT})
        self.assertIn('No message was sent',text)
        self.assertEqual([call.args[1].split('/')[-1] for call in request.call_args_list],['getMe','getChat'])
        self.assertEqual(request.call_args.kwargs['json'],{'chat_id':CHAT})

    def test_telegram_timeout_is_uncertain_rate_limit_retries_and_rejections_are_private(self):
        event={'text':'A synthetic alert','attempts':1}
        config={'bot_token':TOKEN,'chat_id':CHAT}
        with patch('requests.sessions.Session.request',side_effect=RuntimeError(TOKEN)):
            result=telegram_send(event,config)
        self.assertEqual(result['status'],'uncertain')
        self.assertNotIn(TOKEN,json.dumps(result))
        with patch('requests.sessions.Session.request',return_value=response({'error_code':429,'parameters':{'retry_after':'bad'},'description':TOKEN},429)):
            self.assertEqual(telegram_send(event,config)['status'],'queued')
        with patch('requests.sessions.Session.request',return_value=response({'ok':False,'description':TOKEN},403)):
            result=telegram_send(event,config)
        self.assertEqual(result['status'],'failed')
        self.assertNotIn(TOKEN,json.dumps(result))


class ConnectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.rules=Rules()
        self.settings=self.root/'settings.json'
        atomic_json(self.settings,{'assets':[{'name':'BTC',**ASSETS['BTC']}],'strategy':asdict(self.rules)})
        self.store=StateStore(self.root/'state.json',ASSETS,self.rules)
        self.runtime=DashboardRuntime(ASSETS,self.rules,self.store,settings_path=self.settings)
        credentials=ConnectionCredentials(self.root,environment={},protector=fake_protector)
        self.connections=Connections(self.root,credentials=credentials)
        self.runtime.connections=self.store.connections=self.runtime.positions.connections=self.connections
        self.client=create_app(self.runtime).test_client()
        self.client.get('/settings')
        with self.client.session_transaction() as session: self.token=session['csrf_token']
        blocker=patch('requests.sessions.Session.request',side_effect=AssertionError('No live network'))
        blocker.start(); self.addCleanup(blocker.stop)

    def post(self,kind='ai',action='',body=None,**kwargs):
        if body is None: body={'provider':'openai','model':'text-model','api_key':KEY_A} if not action else {}
        return self.client.post('/api/settings/connections/'+kind+('/'+action if action else ''),json=body,
                                headers={'X-CSRF-Token':self.token,**kwargs.pop('headers',{})},**kwargs)

    def seed(self,kind='ai',identity='event'):
        self.store.transaction(lambda doc: queue_event(doc,identity,kind,'Synthetic signal',100,{'price':100}))

    def test_save_status_and_snapshot_share_live_selection_without_exposing_keys(self):
        before=self.store.snapshot()
        result=self.post()
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['state'],'configured')
        self.assertEqual(result.headers['Cache-Control'],'no-store')
        self.assertEqual(self.store.snapshot(),before)
        snapshot=self.runtime.snapshot()
        self.assertTrue(snapshot['ai_configured'])
        self.assertEqual(snapshot['ai']['provider'],'openai')
        for output in (result.json,snapshot,self.client.get('/api/settings/connections').json):
            self.assertNotIn(KEY_A,json.dumps(output))
        self.assertNotIn(KEY_A,self.settings.read_text())

    def test_remote_requests_csrf_generation_and_schema_are_enforced(self):
        self.assertFalse(self.client.get('/api/settings/connections',environ_overrides={'REMOTE_ADDR':'192.0.2.1'}).json['editable'])
        for args in ({'environ_overrides':{'REMOTE_ADDR':'192.0.2.1'}},{'base_url':'http://evil.test'},
                     {'headers':{'X-Forwarded-For':'127.0.0.1'}},{'headers':{'Forwarded':'for=127.0.0.1'}}):
            self.assertEqual(self.post(**args).status_code,403)
            self.assertEqual(self.post(action='check',**args).status_code,403)
            self.assertEqual(self.post(action='disable',**args).status_code,403)
        self.assertEqual(self.client.post('/api/settings/connections/ai',json={}).status_code,400)
        self.assertEqual(self.post(body={'provider':'openai','model':'text-model','api_key':KEY_A,'url':'https://evil.test'}).status_code,400)
        self.runtime.generation='changed'
        self.assertEqual(self.post().status_code,400)
        self.assertFalse(self.connections.credentials.path('ai').exists())

    def test_explicit_check_records_safe_status_and_disable_clears_it(self):
        self.post()
        with patch('requests.sessions.Session.request',return_value=response({'id':'text-model'})):
            result=self.post(action='check')
        self.assertEqual(result.json['state'],'verified')
        self.assertIsNotNone(result.json['last_check_ms'])
        self.assertEqual(self.post(action='disable').json['state'],'off')
        self.assertFalse(self.runtime.snapshot()['ai_configured'])

    def test_dispatch_uses_selected_provider_and_never_replays_terminal_events(self):
        self.connections.credentials.save_ai('gemini','model-g',KEY_G)
        self.seed()
        with patch('adaptive_crypto.notifications.ai_comment',return_value={'status':'done','commentary':'Measured.'}) as sender:
            self.assertTrue(self.connections.dispatch(self.store,'ai',now=101))
            self.assertEqual(sender.call_args.kwargs['config']['api_key'],KEY_G)
            self.assertFalse(self.connections.dispatch(self.store,'ai',now=102))
        self.assertEqual(self.connections.status('ai')['state'],'working')
        self.assertIsNotNone(self.connections.status('ai')['last_success_ms'])

    def test_disabled_or_broken_credentials_leave_queued_events_unclaimed(self):
        self.seed()
        self.connections.credentials.disable('ai')
        self.assertFalse(self.connections.dispatch(self.store,'ai',now=101))
        self.connections.credentials.path('ai').write_bytes(b'corrupt')
        self.assertFalse(self.connections.dispatch(self.store,'ai',now=101))
        self.assertEqual(self.store.snapshot()['outbox'][0]['attempts'],0)

    def test_switch_waits_for_current_delivery_but_status_remains_responsive(self):
        self.connections.credentials.save_ai('openai','model-a',KEY_A)
        self.seed()
        entered,release=threading.Event(),threading.Event()
        def send(event,config):
            self.assertEqual(config['api_key'],KEY_A)
            entered.set()
            self.assertTrue(release.wait(3))
            return {'status':'done','commentary':'Complete.'}
        with patch('adaptive_crypto.notifications.ai_comment',side_effect=send), ThreadPoolExecutor(max_workers=3) as pool:
            delivering=pool.submit(self.connections.dispatch,self.store,'ai',101)
            self.assertTrue(entered.wait(2))
            changed=pool.submit(self.connections.change,'ai',{'provider':'gemini','model':'model-g','api_key':KEY_G})
            try:
                status=pool.submit(self.connections.status,'ai').result(timeout=.5)
                self.assertEqual(status['provider'],'openai')
                self.assertFalse(changed.done())
            finally: release.set()
            self.assertTrue(delivering.result(timeout=2))
            changed.result(timeout=2)
        self.assertEqual(self.connections.credentials.read('ai')['api_key'],KEY_G)
        self.assertEqual(self.connections.status('ai')['state'],'configured')


if __name__ == '__main__': unittest.main()
