(() => {
  const panel = document.getElementById('market-brief');
  if (!panel) return;
  const copy = panel.querySelector('[data-brief-copy]'), status = panel.querySelector('[data-brief-status]');
  const time = panel.querySelector('[data-brief-time]'), refresh = panel.querySelector('[data-brief-refresh]');
  const search = panel.querySelector('[data-brief-search]');
  const shadow = search.attachShadow({mode:'open'});
  let latest = null, rendered = null, fetching = false, sending = false, localError = '';
  function link(value) {
    try { const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password ? url.href : null; } catch (_) { return null; }
  }
  function stamp(value) { return new Date(value).toISOString().replace('T',' ').slice(0,19)+' UTC'; }
  function suggestions(html) {
    // Isolate the provider's search widget styles. Never attach scripts, event
    // handlers, frames or active URLs from returned HTML to the application.
    shadow.replaceChildren();
    if (!html) { search.hidden = true; return; }
    const parsed = new DOMParser().parseFromString(html, 'text/html');
    const allowed = new Set(['DIV','SPAN','P','A','STYLE','SVG','PATH','G','RECT','CIRCLE','STRONG','SMALL','BR']);
    function clean(node) {
      if (node.nodeType === 3) return document.createTextNode(node.textContent);
      if (node.nodeType !== 1 || !allowed.has(node.tagName.toUpperCase())) return null;
      if (node.tagName === 'STYLE' && /@import|url\s*\(|expression\s*\(/i.test(node.textContent)) return null;
      const element = node.namespaceURI === 'http://www.w3.org/2000/svg' ? document.createElementNS(node.namespaceURI,node.localName) : document.createElement(node.tagName);
      for (const attr of node.attributes) {
        if (['class','viewBox','d','fill','width','height','xmlns','cx','cy','r','rx','x','y','transform','aria-label','role'].includes(attr.name)) element.setAttribute(attr.name, attr.value);
        if (attr.name === 'style' && !/url\s*\(|expression\s*\(|@import/i.test(attr.value)) element.setAttribute('style', attr.value);
        if (attr.name === 'href' && node.tagName === 'A' && link(attr.value)) element.setAttribute('href',link(attr.value));
      }
      if (node.tagName === 'A') { element.target='_blank'; element.rel='noopener noreferrer'; }
      for (const child of node.childNodes) { const safe=clean(child); if(safe) element.append(safe); }
      return element;
    }
    for (const node of [...parsed.head.childNodes,...parsed.body.childNodes]) { const safe=clean(node); if(safe) shadow.append(safe); }
    search.hidden = !shadow.childNodes.length;
  }
  function renderBrief(brief) {
    const identity = brief ? JSON.stringify([brief.generated_ms,brief.provider,brief.model]) : '';
    if (identity === rendered) return;
    rendered = identity; copy.replaceChildren();
    if (!brief) { suggestions(''); return; }
    const sources = new Map();
    for (const block of brief.blocks || []) {
      const p = document.createElement('p');
      for (const fragment of block) {
        if (typeof fragment.text === 'string') p.append(document.createTextNode(fragment.text.replace(/cite[^]*/g,'')));
        else if (link(fragment.url)) {
          if (!sources.has(fragment.url)) sources.set(fragment.url,sources.size+1);
          const a = document.createElement('a'); a.href=link(fragment.url); a.target='_blank'; a.rel='noopener noreferrer';
          a.textContent='['+sources.get(fragment.url)+']'; a.title=fragment.title || 'Source'; a.setAttribute('aria-label','Source '+sources.get(fragment.url)+': '+a.title); p.append(a);
        }
      }
      copy.append(p);
    }
    suggestions(brief.search_suggestions);
  }
  function render() {
    if (!latest) return;
    const brief=latest.brief, stale=brief && Date.now()-brief.asof_ms>=latest.refresh_seconds*1000;
    panel.dataset.state=stale?'stale':latest.state;
    status.dataset.problem=localError || latest.error || stale ? '1':'0';
    status.textContent=localError || latest.error || (latest.state==='off'?'Enable an AI connection in Settings to get market summaries.':
      latest.state==='updating'?'Researching current news and public posts…':
      stale?'Previous summary · waiting for the next update.':brief?'News and public posts checked with '+(brief.provider==='gemini'?'Gemini':'OpenAI')+'.':
      'Waiting for fresh market prices and the next scheduled summary.');
    time.textContent=brief?'As of '+stamp(brief.asof_ms)+' · every 15 min':'Updates every 15 minutes';
    refresh.disabled=sending || !latest.editable || !latest.can_refresh || latest.state==='off';
    renderBrief(brief);
  }
  async function poll() {
    if (fetching || document.hidden) return;
    fetching=true;
    try {
      const response=await fetch('/api/market-brief',{cache:'no-store',signal:AbortSignal.timeout(10000)});
      if(!response.ok) throw new Error();
      latest=await response.json(); localError=''; render();
    } catch (_) { localError='Market summary connection unavailable; retrying automatically.'; status.textContent=localError; status.dataset.problem='1'; }
    finally { fetching=false; }
  }
  refresh.addEventListener('click',async()=>{
    if(sending || refresh.disabled) return;
    sending=true; refresh.disabled=true; localError='';
    try {
      const response=await fetch('/api/market-brief/refresh',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':panel.dataset.csrf},body:'{}',signal:AbortSignal.timeout(10000)});
      const body=await response.json();
      if(!response.ok) throw new Error(body.error || 'Summary request failed.');
      await poll();
    } catch(error) { localError=error.message; status.textContent=localError; status.dataset.problem='1'; }
    finally { sending=false; if(latest) refresh.disabled=!latest.editable || !latest.can_refresh; }
  });
  poll(); setInterval(poll,5000); setInterval(render,1000);
  document.addEventListener('visibilitychange',()=>{ if(!document.hidden) poll(); });
})();
