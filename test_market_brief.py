"""Offline grounding, scheduling, price provenance and route regressions."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from adaptive_crypto.core import H4, Candle, DataError
from adaptive_crypto.market_brief import INTERVAL_MS, MarketBrief, market_context
from adaptive_crypto.market_brief_provider import grounded_brief, safe_url
from adaptive_crypto.ai_providers import ProviderError
import test_connections as fixtures

CONFIG = {'provider':'openai','model':'text-search-model','api_key':'private-fixture-key'}
SOURCE = 'https://example.com/market-update'
RESULT = {'blocks':[[{'text':'Market update.'},{'url':SOURCE,'title':'Original announcement'}]],'search_suggestions':''}


def openai_response(text='Market update.'):
    return {'status':'completed','output':[{'type':'web_search_call','status':'completed'},
        {'type':'message','content':[{'type':'output_text','text':text,
         'annotations':[{'type':'url_citation','url':SOURCE,'title':'Announcement','end_index':len(text)}]}]}]}


class GroundedProviderTests(unittest.TestCase):
    def test_openai_requires_live_search_and_returns_only_provider_citations(self):
        with patch('adaptive_crypto.market_brief_provider.request_json',return_value=openai_response()) as request:
            result=grounded_brief(CONFIG,{'prices':[{'asset':'BTC','price':100}]})
        sent=request.call_args.kwargs
        self.assertEqual(sent['payload']['tool_choice'],'required')
        self.assertFalse(sent['payload']['store'])
        self.assertEqual(sent['payload']['tools'][0]['type'],'web_search')
        self.assertEqual(result['blocks'][0][-1]['url'],SOURCE)
        self.assertNotIn(CONFIG['api_key'],json.dumps(result))
        self.assertNotIn(CONFIG['api_key'],sent['payload']['input'])

    def test_unsourced_incomplete_or_nonsearched_answers_are_rejected(self):
        responses=[openai_response() for _ in range(4)]
        responses[0]['status']='incomplete'
        responses[1]['output'].pop(0)
        responses[2]['output'][1]['content'][0]['annotations']=[]
        responses[3]['output'][1]['content'][0]['annotations'][0]['url']='javascript:alert(1)'
        for response in responses:
            with self.subTest(response=response), patch('adaptive_crypto.market_brief_provider.request_json',return_value=response):
                with self.assertRaises(ProviderError): grounded_brief(CONFIG,{})

    def test_provider_error_never_switches_models_or_providers(self):
        with patch('adaptive_crypto.market_brief_provider.request_json',side_effect=ProviderError('Search unavailable.')) as request:
            with self.assertRaisesRegex(ProviderError,'Search unavailable'): grounded_brief(CONFIG,{})
        self.assertEqual(request.call_count,1)

    def test_unicode_openai_citations_preserve_text(self):
        text='BTC 📈 – market.'
        with patch('adaptive_crypto.market_brief_provider.request_json',return_value=openai_response(text)):
            result=grounded_brief(CONFIG,{})
        self.assertEqual(result['blocks'][0][0],{'text':text})

    def test_gemini_uses_utf8_offsets_and_original_part_indices(self):
        text='BTC 📈 rose. No verified post.'
        first='BTC 📈 rose.'
        response={'candidates':[{'finishReason':'STOP','content':{'parts':[{'thought':True,'text':'private reasoning'}, {'text':text}]},
            'groundingMetadata':{'webSearchQueries':['latest bitcoin'], 'groundingChunks':[{'web':{'uri':SOURCE,'title':'News'}}],
              'groundingSupports':[{'segment':{'partIndex':1,'endIndex':len(first.encode('utf-8'))},'groundingChunkIndices':[0]}],
              'searchEntryPoint':{'renderedContent':'<div>Google Search</div>'}}}]}
        with patch('adaptive_crypto.market_brief_provider.request_json',return_value=response) as request:
            result=grounded_brief({**CONFIG,'provider':'gemini'}, {})
        self.assertEqual(request.call_args.kwargs['payload']['tools'],[{'google_search':{}}])
        self.assertEqual(result['blocks'][0],[{'text':first},{'url':SOURCE,'title':'News'},{'text':' No verified post.'}])
        self.assertEqual(result['search_suggestions'],'<div>Google Search</div>')
        self.assertNotIn('private reasoning',json.dumps(result))

    def test_gemini_without_grounding_is_not_presented_as_news(self):
        with patch('adaptive_crypto.market_brief_provider.request_json',return_value={'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'Invented news'}]}}]}):
            with self.assertRaisesRegex(ProviderError,'citations'): grounded_brief({**CONFIG,'provider':'gemini'}, {})

    def test_source_links_reject_credentials_local_targets_and_active_schemes(self):
        for url in ['javascript:alert(1)','http://example.com','https://localhost/a','https://127.0.0.1/a','https://192.168.1.1/a','https://user:pass@example.com','https://example.com:5000','https://example.com/\nhi','https://example.com\\@localhost']:
            self.assertIsNone(safe_url(url),url)
        self.assertEqual(safe_url('https://x.com/author/status/123'),'https://x.com/author/status/123')


class BriefWorkerTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup); self.root=Path(temp.name)
        self.now=H4*120000+60000
        self.config=copy.deepcopy(CONFIG)
        self.credentials=SimpleNamespace(read=lambda kind:copy.deepcopy(self.config))
        self.runtime=SimpleNamespace(lock=threading.RLock(),stop=threading.Event(),generation='one',
            assets={'BTC':{'symbol':'BTC/USD'}},market={},chart_fallback={},connections=SimpleNamespace(credentials=self.credentials))
        self.fresh()
        self.request=Mock(return_value=copy.deepcopy(RESULT))
        self.brief=MarketBrief(self.runtime,self.root,requester=self.request,clock=lambda:self.now)

    def fresh(self):
        self.runtime.market={'BTC':{'quote':{'last':110,'asof_ms':self.now}}}
        self.runtime.chart_fallback={'BTC':{'candles':[Candle(self.now//H4*H4-i*H4,100,120,95,105,1000,H4) for i in range(6,0,-1)]}}

    def test_context_sends_only_fresh_public_prices_and_explicit_baselines(self):
        self.runtime.market['BTC'].update(secret_notes='private',position={'balance':999})
        context,generation=market_context(self.runtime,self.now)
        self.assertEqual(context['prices'][0]['change_percent'],10)
        self.assertIn('change_since_utc',context['prices'][0])
        self.assertNotIn('private',json.dumps(context)); self.assertNotIn('balance',json.dumps(context))
        self.now+=46000
        self.assertEqual(market_context(self.runtime,self.now)[0]['prices'],[])

    def test_gapped_or_old_candles_do_not_become_returns(self):
        self.runtime.chart_fallback['BTC']['candles'].pop(2)
        self.assertNotIn('change_percent',market_context(self.runtime,self.now)[0]['prices'][0])

    def test_fifteen_minute_single_schedule_and_read_only_status(self):
        self.assertTrue(self.brief.tick()); self.assertFalse(self.brief.tick())
        for _ in range(5): self.brief.status()
        self.assertEqual(self.request.call_count,1)
        self.now+=INTERVAL_MS-1; self.fresh(); self.assertFalse(self.brief.tick())
        self.now+=1; self.fresh(); self.assertTrue(self.brief.tick())
        self.assertEqual(self.request.call_count,2)

    def test_restart_keeps_charge_schedule_but_does_not_store_research_or_keys(self):
        self.brief.tick()
        text=self.brief.path.read_text()
        self.assertNotIn(CONFIG['api_key'],text); self.assertNotIn('Market update',text)
        restarted=MarketBrief(self.runtime,self.root,requester=self.request,clock=lambda:self.now)
        self.assertFalse(restarted.tick()); self.assertIsNone(restarted.status()['brief'])
        self.assertEqual(self.request.call_count,1)

    def test_error_preserves_previous_brief_and_limits_retries(self):
        self.brief.tick(); self.now+=INTERVAL_MS; self.fresh()
        self.request.side_effect=ProviderError('Search quota reached.')
        self.assertFalse(self.brief.tick())
        status=self.brief.status(); self.assertEqual(status['state'],'stale'); self.assertIsNotNone(status['brief'])
        self.assertEqual(status['error'],'Search quota reached.')
        self.assertFalse(self.brief.tick()); self.assertEqual(self.request.call_count,2)

    def test_missing_prices_disabled_ai_and_stopping_never_spend(self):
        self.runtime.market={}; self.assertFalse(self.brief.tick())
        self.fresh(); self.config=None; self.assertFalse(self.brief.tick()); self.assertEqual(self.brief.status()['state'],'off')
        self.config=copy.deepcopy(CONFIG); self.runtime.stop.set(); self.assertFalse(self.brief.tick())
        self.request.assert_not_called()

    def test_inflight_result_discarded_after_provider_disable_or_generation_change(self):
        for change in ['provider','disable','generation']:
            with self.subTest(change=change):
                self.config=copy.deepcopy(CONFIG); self.runtime.generation='one'; self.now+=INTERVAL_MS; self.fresh()
                def request(config,context):
                    if change=='provider': self.config={**CONFIG,'provider':'gemini'}
                    elif change=='disable': self.config=None
                    else: self.runtime.generation='two'
                    return copy.deepcopy(RESULT)
                self.brief.requester=request
                self.assertFalse(self.brief.tick()); self.assertIsNone(self.brief.status()['brief'])

    def test_new_provider_is_requested_without_showing_old_provider_text(self):
        self.brief.tick(); self.config={**CONFIG,'provider':'gemini'}
        self.assertIsNone(self.brief.status()['brief'])
        self.assertTrue(self.brief.tick()); self.assertEqual(self.brief.status()['brief']['provider'],'gemini')

    def test_refresh_is_local_singleflight_and_rate_limited(self):
        self.brief.tick()
        with self.assertRaises(DataError): self.brief.request_refresh()
        self.now+=60000; self.fresh(); self.brief.request_refresh(); self.brief.request_refresh()
        self.assertTrue(self.brief.tick()); self.assertEqual(self.request.call_count,2)

    def test_long_research_does_not_block_status_or_runtime_lock(self):
        started,release=threading.Event(),threading.Event()
        def request(*args): started.set(); release.wait(3); return copy.deepcopy(RESULT)
        self.brief.requester=request
        thread=threading.Thread(target=self.brief.tick); thread.start()
        try:
            self.assertTrue(started.wait(1)); self.assertEqual(self.brief.status()['state'],'updating')
            self.assertTrue(self.runtime.lock.acquire(timeout=.1)); self.runtime.lock.release()
            self.assertFalse(self.brief.tick())
        finally: release.set(); thread.join(2)
        self.assertFalse(thread.is_alive())


class BriefRouteTests(unittest.TestCase):
    setUp = fixtures.ConnectionIntegrationTests.setUp
    post = fixtures.ConnectionIntegrationTests.post
    def test_brief_get_does_not_generate_and_refresh_checks_csrf_and_local_host(self):
        self.post()
        with patch.object(self.runtime.market_brief,'requester') as request:
            response=self.client.get('/api/market-brief')
            self.assertEqual(response.status_code,200); self.assertEqual(response.headers['Cache-Control'],'no-store')
            request.assert_not_called()
        self.assertNotIn(fixtures.KEY_A,json.dumps(response.json))
        self.assertEqual(self.client.post('/api/market-brief/refresh',json={}).status_code,400)
        self.assertEqual(self.client.post('/api/market-brief/refresh',json={},headers={'X-CSRF-Token':self.token},environ_overrides={'REMOTE_ADDR':'192.168.1.50'}).status_code,403)
        self.assertEqual(self.client.post('/api/market-brief/refresh',json={},headers={'X-CSRF-Token':self.token}).status_code,202)
        self.runtime.generation='new'
        self.assertEqual(self.client.post('/api/market-brief/refresh',json={},headers={'X-CSRF-Token':self.token}).status_code,400)

    def test_header_contains_panel_above_navigation(self):
        page=self.client.get('/').data.decode()
        self.assertIn('id="market-brief"',page)
        self.assertLess(page.index('id="market-brief"'),page.index('class="site-nav"') if 'class="site-nav"' in page else page.index('<nav'))


if __name__=='__main__': unittest.main()
