const assert=require('node:assert/strict');
const {test,before,after}=require('node:test');
const fs=require('node:fs');
let chromium,browser;
try{({chromium}=require('playwright'));}catch(_){}
before(async()=>{if(chromium)browser=await chromium.launch({headless:true,...(process.env.PYPTA_BROWSER_PATH?{executablePath:process.env.PYPTA_BROWSER_PATH}:{})});});
after(async()=>{if(browser)await browser.close();});
const browserTest=(name,fn)=>test(name,{skip:!chromium&&'Playwright unavailable'},fn);
const file=name=>fs.readFileSync(__dirname+'/adaptive_crypto/'+name,'utf8');
const initial=()=>({state:'ready',editable:true,can_refresh:true,refresh_seconds:900,provider:'openai',model:'model-a',
  brief:{provider:'openai',model:'model-a',generated_ms:Date.now(),asof_ms:Date.now(),search_suggestions:'',
    blocks:[[{text:'BTC is moving with broader crypto. '},{url:'https://example.com/news',title:'Original news'},
             {text:'\nNo recent original public post was verified.'}]]}});
async function harness(t){
  const page=await browser.newPage({viewport:{width:1280,height:900}});t.after(()=>page.close());
  const errors=[],posts=[];let data=initial(),fail=false;
  page.on('pageerror',error=>errors.push(error.message));t.after(()=>assert.deepEqual(errors,[]));
  const template=file('templates/market_brief.html').replace('{{ csrf_token }}','test-token').replace(/{{[^}]*}}/g,'/settings');
  const html=`<html><head><style>${file('static/dashboard.css')}\n${file('static/market_brief.css')}</style></head><body data-generation="generation"><main><header class="market-header"><div class="market-title"><h1>Market Dashboard</h1><p>Live market context</p></div>${template}<small class="market-scan-time">Last scan</small></header><nav>Home · Positions · Settings</nav></main></body></html>`;
  await page.route('http://pypta.test/**',async route=>{
    if(route.request().method()==='POST'){
      posts.push({body:route.request().postDataJSON(),headers:route.request().headers()});data.state='updating';data.can_refresh=false;
      return route.fulfill({status:202,contentType:'application/json',body:JSON.stringify({message:'Requested'})});
    }
    if(route.request().url().endsWith('/api/market-brief'))return route.fulfill({status:fail?503:200,contentType:'application/json',body:JSON.stringify(data)});
    return route.fulfill({contentType:'text/html',body:html});
  });
  await page.goto('http://pypta.test/');
  await page.evaluate(()=>{window.polls=[];window.setInterval=fn=>window.polls.push(fn);});
  await page.addScriptTag({content:file('static/market_brief.js')});
  await page.waitForFunction(()=>document.querySelector('[data-brief-copy]').textContent.includes('BTC'));
  return {page,posts,html,set:value=>{data=value;},fail:value=>{fail=value;},poll:()=>page.evaluate(()=>window.polls[0]())};
}
browserTest('sourced summary renders in header on desktop and mobile without overflow',async t=>{
  const ui=await harness(t);
  const anchor=ui.page.locator('[data-brief-copy] a');
  assert.equal(await anchor.getAttribute('href'),'https://example.com/news');
  assert.match(await anchor.getAttribute('rel'),/noopener/);
  for(const width of [1920,1280,762,390]){
    await ui.page.setViewportSize({width,height:900});
    assert.equal(await ui.page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.equal(await ui.page.evaluate(()=>document.querySelector('#market-brief').getBoundingClientRect().bottom<=document.querySelector('nav').getBoundingClientRect().top),true);
  }
});
browserTest('refresh posts only CSRF and concurrent clicks produce one request',async t=>{
  const ui=await harness(t);
  await ui.page.locator('[data-brief-refresh]').click();
  await ui.page.waitForFunction(()=>document.querySelector('[data-brief-status]').textContent.includes('Researching'));
  assert.equal(ui.posts.length,1);assert.deepEqual(ui.posts[0].body,{});assert.equal(ui.posts[0].headers['x-csrf-token'],'test-token');
  assert.equal(await ui.page.locator('[data-brief-refresh]').isDisabled(),true);
});
browserTest('stale summaries and network failures are not labelled current',async t=>{
  const ui=await harness(t),data=initial();data.brief.asof_ms=Date.now()-901000;ui.set(data);await ui.poll();
  assert.equal(await ui.page.locator('#market-brief').getAttribute('data-state'),'stale');
  assert.match(await ui.page.locator('[data-brief-status]').textContent(),/Previous summary/);
  ui.fail(true);await ui.poll();await ui.page.evaluate(()=>window.polls[1]());
  assert.match(await ui.page.locator('[data-brief-status]').textContent(),/connection unavailable/);
  assert.match(await ui.page.locator('[data-brief-copy]').textContent(),/BTC/);
});
browserTest('disabled or changed provider clears old text and remote view cannot refresh',async t=>{
  const ui=await harness(t);ui.set({state:'waiting',provider:'gemini',model:'model-g',brief:null,editable:false,can_refresh:true,refresh_seconds:900});await ui.poll();
  assert.equal(await ui.page.locator('[data-brief-copy]').textContent(),'');
  assert.equal(await ui.page.locator('[data-brief-refresh]').isDisabled(),true);
  ui.set({state:'off',brief:null,editable:true,refresh_seconds:900});await ui.poll();
  assert.match(await ui.page.locator('[data-brief-status]').textContent(),/Enable an AI connection/);
});
browserTest('provider output and Google search widget cannot execute active content',async t=>{
  const ui=await harness(t),data=initial();data.brief.generated_ms+=100;data.brief.provider='gemini';
  data.brief.blocks=[[{text:'<img src=x onerror="window.pwned=true">'},{url:'javascript:window.pwned=true',title:'bad'},{url:'https://example.com/source',title:'Source'}]];
  data.brief.search_suggestions='<style>.box{color:green}</style><div class="box" onclick="window.pwned=true">Google <a href="https://www.google.com/search?q=crypto">Search</a><script>window.pwned=true</script><iframe srcdoc="bad"></iframe><svg onload="window.pwned=true"><path d="M0 0"/></svg><a href="javascript:window.pwned=true">bad</a></div>';
  ui.set(data);await ui.poll();
  assert.equal(await ui.page.evaluate(()=>window.pwned),undefined);
  assert.equal(await ui.page.locator('[data-brief-copy] img').count(),0);
  assert.equal(await ui.page.locator('[data-brief-copy] a').count(),1);
  assert.equal(await ui.page.locator('[data-brief-search] script, [data-brief-search] iframe').count(),0);
  assert.equal(await ui.page.locator('[data-brief-search] a[href^="https:"]').count(),1);
  assert.equal(await ui.page.locator('[data-brief-search] [onclick], [data-brief-search] [onload]').count(),0);
});
browserTest('normal market refresh preserves the summary DOM and source focus',async t=>{
  const ui=await harness(t);
  await ui.page.addScriptTag({content:file('static/live_refresh.js')});
  await ui.page.locator('[data-brief-copy] a').focus();
  await ui.page.evaluate(()=>{window.originalBrief=document.getElementById('market-brief');});
  await ui.page.evaluate(()=>window.pyptaRefresh(()=>true));
  assert.equal(await ui.page.evaluate(()=>window.originalBrief===document.getElementById('market-brief')),true);
  assert.equal(await ui.page.locator('[data-brief-copy] a').evaluate(a=>a===document.activeElement),true);
  assert.match(await ui.page.locator('[data-brief-copy]').textContent(),/BTC/);
});
