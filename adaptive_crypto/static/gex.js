// GEX/SMC POI map and the target selector's point-in-time confluence evidence.
document.querySelectorAll('.gex').forEach(panel => {
  const withSMC = panel.dataset.context !== 'nn';
  const find = selector => panel.querySelector(selector);
  const money = value => Number.isFinite(value) ? '$' + value.toLocaleString('en-US', {
    minimumFractionDigits: Number(panel.dataset.digits), maximumFractionDigits: Number(panel.dataset.digits)
  }) : '—';
  const distance = value => Math.abs(value).toLocaleString('en-US', {maximumFractionDigits: 2}) + '%';
  let last = null, renderedState = '';
  function add(svg, tag, attrs, text) {
    const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, value));
    if (text !== undefined) el.textContent = text;
    svg.append(el); return el;
  }
  function draw(data) {
    const svg = find('.gex-map'); svg.replaceChildren();
    const levels = [
      {price: data.call_wall?.strike, label: 'Call wall', color: '#fa8585'},
      {price: data.gamma_flip, label: 'Gamma flip', color: '#efc36b'},
      {price: data.put_wall?.strike, label: 'Put wall', color: '#63d7a1'},
      {price: data.price ?? data.spot, label: data.price === null ? 'Deribit index' : 'Kraken spot', color: '#e2e8f0'}
    ].filter(item => Number.isFinite(item.price));
    const matched = data.target_confluence?.matches || [];
    const pois = [...(data.pois || [])].sort((a,b) => {
      const aligned = p => matched.some(m => m.poi.kind === p.kind && m.poi.pivot_ms === p.pivot_ms);
      return Number(aligned(b))-Number(aligned(a)) || Math.abs(a.price-data.price)-Math.abs(b.price-data.price);
    }).slice(0, 8);
    const values = levels.map(item => item.price).concat((data.zones || []).flatMap(z => [z.low, z.high]), pois.map(p => p.price));
    if (!values.length) return;
    let low = Math.min(...values), high = Math.max(...values);
    const pad = Math.max((high-low)*.12, high*.003); low -= pad; high += pad;
    const y = price => 286-(price-low)/(high-low)*256;
    add(svg, 'title', {}, withSMC ? 'Price-scaled Naive GEX levels and SMC order blocks' : 'Price-scaled Naive GEX levels and net exposure by strike');
    for (let i = 0; i <= 4; i++) {
      const price = low+(high-low)*i/4;
      add(svg, 'line', {x1:104, x2:402, y1:y(price), y2:y(price), stroke:'#303947'});
      add(svg, 'text', {x:96, y:y(price)+5, fill:'#99a5b5', 'text-anchor':'end', 'font-size':16}, money(price));
    }
    (data.zones || []).forEach(zone => {
      const color = zone.side === 'long' ? '#63d7a1' : '#fa8585';
      const rect = add(svg, 'rect', {x:104, y:y(zone.high), width:298, height:Math.max(2,y(zone.low)-y(zone.high)),
        fill:color, 'fill-opacity':zone.confluence ? .25 : .12, stroke:color,
        'stroke-dasharray':zone.reference_only ? '5 4' : 'none'});
      add(rect, 'title', {}, zone.label + ': ' + money(zone.low) + '–' + money(zone.high));
    });
    pois.sort((a,b) => b.price-a.price);
    const poiLabels = pois.map(p => y(p.price));
    for (let i = 1; i < poiLabels.length; i++) poiLabels[i] = Math.max(poiLabels[i], poiLabels[i-1]+23);
    if (poiLabels.length) poiLabels[poiLabels.length-1] = Math.min(poiLabels[poiLabels.length-1], 290);
    for (let i = poiLabels.length-2; i >= 0; i--) poiLabels[i] = Math.min(poiLabels[i], poiLabels[i+1]-23);
    pois.forEach((poi, i) => {
      const match = matched.find(m => m.poi.kind === poi.kind && m.poi.pivot_ms === poi.pivot_ms);
      const color = match?.eligible ? '#efc36b' : '#b5c7e3';
      const dot = add(svg, 'circle', {cx:116, cy:y(poi.price), r:match?.eligible ? 5 : 3, fill:color});
      add(dot, 'title', {}, poi.label+' '+money(poi.price)+(match ? ' · '+match.reason : ''));
      add(svg, 'line', {x1:124, x2:230, y1:y(poi.price), y2:y(poi.price), stroke:color, 'stroke-dasharray':'2 3'});
      add(svg, 'path', {d:'M116 '+y(poi.price)+' L128 '+poiLabels[i], fill:'none', stroke:color});
      const label = add(svg, 'text', {x:132, y:poiLabels[i]+4, fill:color, 'font-size':18,
        stroke:'#121820', 'stroke-width':3, 'paint-order':'stroke'}, poi.kind.toUpperCase().replace('_',' ')+' '+money(poi.price));
      add(label, 'title', {}, poi.label);
    });
    const visible = (data.strikes || []).filter(r => r.strike >= low && r.strike <= high);
    const scale = Math.max(1, ...visible.map(r => Math.abs(r.net_gex)));
    add(svg, 'line', {x1:254, x2:254, y1:30, y2:286, stroke:'#536171', 'stroke-dasharray':'3 5'});
    visible.forEach(r => {
      const width = Math.abs(r.net_gex)/scale*145;
      const rect = add(svg, 'rect', {x:r.net_gex > 0 ? 254 : 254-width, y:y(r.strike)-2, width, height:4,
        fill:r.net_gex > 0 ? '#63d7a1' : '#fa8585', opacity:.65});
      add(rect, 'title', {}, money(r.strike) + ': ' + r.net_gex.toLocaleString('en-US') + ' ' + data.units);
    });
    levels.sort((a,b) => b.price-a.price);
    const labels = levels.map(level => y(level.price));
    // Spread coincident levels while keeping every label inside the SVG.
    for (let i = 1; i < labels.length; i++) labels[i] = Math.max(labels[i], labels[i-1]+44);
    labels[labels.length-1] = Math.min(labels[labels.length-1], 286);
    for (let i = labels.length-2; i >= 0; i--) labels[i] = Math.min(labels[i], labels[i+1]-44);
    levels.forEach((level, i) => {
      const actualY = y(level.price), labelY = labels[i];
      add(svg, 'line', {x1:104, x2:402, y1:actualY, y2:actualY, stroke:level.color,
        'stroke-width':1.5, 'stroke-dasharray':level.label === 'Gamma flip' ? '6 5' : 'none'});
      add(svg, 'path', {d:'M402 '+actualY+' L426 '+labelY+' L435 '+labelY, fill:'none', stroke:level.color});
      add(svg, 'text', {x:442, y:labelY-4, fill:level.color, 'font-size':20}, level.label);
      add(svg, 'text', {x:442, y:labelY+17, fill:level.color, 'font-size':18}, money(level.price));
    });
    add(svg, 'text', {x:104, y:327, fill:'#99a5b5', 'font-size':16}, '← Negative GEX   |   Positive GEX →');
  }
  function drawCurve(data) {
    const svg = find('.gex-curve'); svg.replaceChildren();
    const points = data.curve || [];
    if (points.length < 2) return;
    const min = points[0].price, max = points[points.length-1].price;
    const scale = Math.max(1, ...points.map(p => Math.abs(p.gex)));
    const x = value => 28+(value-min)/(max-min)*564;
    const y = value => 100-value/scale*70;
    add(svg, 'title', {}, 'Modeled net gamma across hypothetical index prices; zero crossings indicate estimated flips');
    add(svg, 'line', {x1:28, x2:592, y1:100, y2:100, stroke:'#99a5b5', 'stroke-dasharray':'4 5'});
    add(svg, 'text', {x:28, y:20, fill:'#63d7a1', 'font-size':17}, 'Positive gamma');
    add(svg, 'text', {x:28, y:190, fill:'#fa8585', 'font-size':17}, 'Negative gamma');
    add(svg, 'polyline', {points:points.map(p => x(p.price)+','+y(p.gex)).join(' '),
      fill:'none', stroke:'#b5c7e3', 'stroke-width':2});
    (data.flip_candidates || []).forEach(price => {
      const circle = add(svg, 'circle', {cx:x(price), cy:100, r:5, fill:'#efc36b'});
      add(circle, 'title', {}, 'Estimated flip '+money(price));
    });
    add(svg, 'line', {x1:x(data.spot), x2:x(data.spot), y1:28, y2:172, stroke:'#e2e8f0', 'stroke-dasharray':'3 4'});
    for (const [price, anchor] of [[min,'start'], [data.spot,'middle'], [max,'end']]) {
      add(svg, 'text', {x:x(price), y:218, fill:'#99a5b5', 'text-anchor':anchor, 'font-size':17}, money(price));
    }
  }
  function render(data) {
    const ready = data.status === 'ready';
    find('.gex-status').textContent = ready ? 'Options current' : data.status.toUpperCase();
    panel.classList.toggle('gex-stale', !ready);
    find('.gex-content').hidden = !data.strikes;
    find('.gex-error').textContent = data.error || '';
    find('.gex-meta').textContent = data.market ? data.market+' · '+data.option_count+' OI-bearing options · '+new Date(data.asof_ms).toLocaleString()+(data.quote_currency === 'USDC' ? ' · USDC ≈ USD' : '') : 'Deribit options · positioning proxy';
    if (!data.strikes) return;
    for (const level of data.levels || []) {
      find('[data-level="'+level.key+'"]').textContent = level.key === 'flip' && level.price === null ? 'No flip in ±30%' : money(level.price);
      find('[data-distance="'+level.key+'"]').textContent = level.price === null ? 'No modeled level' :
        !ready ? 'Historical level' : level.distance_percent === null ? 'Waiting for Kraken price' :
        level.relation === 'at' ? 'At Kraken price' : distance(level.distance_percent)+' '+level.relation+' Kraken price';
    }
    const net = new Intl.NumberFormat('en-US', {notation:'compact', maximumFractionDigits:2}).format(data.net_gex);
    find('.gex-regime').textContent = (ready ? 'Modeled ' : 'Last recorded ')+data.regime+' gamma · '+net+' '+data.quote_currency+' delta / 1% move at index '+money(data.spot)+'. '+
      (ready ? (data.regime === 'positive' ? 'Potential volatility damping.' : data.regime === 'negative' ? 'Potential volatility amplification.' : 'Balanced modeled exposure.') : 'Stale levels are historical context.')+
      (data.flip_candidates.length > 1 ? ' '+data.flip_candidates.length+' flips detected; nearest shown.' : '');
    find('.gex-basis').textContent = Number.isFinite(data.basis_percent) ?
      'Deribit index is '+distance(data.basis_percent)+' '+(data.basis_percent >= 0 ? 'above' : 'below')+' Kraken spot. Levels retain their original venue prices.' : '';
    const notes = withSMC ? [...(data.zone_errors || [])] : [];
    if (withSMC && !data.smc_fresh) notes.push('Waiting for fresh SMC data; order-block confluence is unavailable.');
    else if (withSMC) {
      if (!data.zones.length && !notes.length) notes.push('No current HTF order block to map.');
      data.zones.forEach(z => notes.push(z.label+' '+money(z.low)+'–'+money(z.high)+' · '+
        (z.relation === 'inside' ? 'price inside block' : distance(z.distance_percent)+' '+z.relation+' price')+' · '+
        (z.reference_only ? 'reference only; missed, retired or awaiting history' : z.confluence ? 'wall confluence' : ready ? 'no matching wall inside block' : 'confluence unavailable')+'.'));
    }
    if (ready && data.price !== null) {
      if (data.call_wall) notes.push(data.call_wall.strike > data.price ? (data.regime === 'positive' ? 'Call wall above price: potential ceiling in positive gamma.' : 'Call wall above price: context only; breakout amplification is possible.') : 'Call wall at/below price: no overhead ceiling.');
      if (data.put_wall) notes.push(data.put_wall.strike < data.price ? (data.regime === 'positive' ? 'Put wall below price: potential floor in positive gamma.' : 'Put wall below price: context only; breakdown amplification is possible.') : 'Put wall at/above price: no underlying floor.');
    }
    find('.gex-confluence').replaceChildren(...notes.map(note => {
      const li = document.createElement('li'); li.textContent = note; return li;
    }));
    if (withSMC) {
      const confluence = data.target_confluence || {};
      const matches = confluence.ready ? confluence.matches || [] : [];
      find('.gex-target-policy').textContent = confluence.enabled
        ? 'Target selection enabled · alignment within '+confluence.alignment_bps+' bps. '+(confluence.ready
          ? 'Nearest eligible aligned liquidity target is preferred; the sweep buffer is then applied.' : confluence.reason || 'Waiting for fresh GEX/SMC analysis; SMC fallback applies.')
        : 'GEX target selection is disabled in settings.';
      find('.gex-poi-rows').replaceChildren(...[
        ['call', 'Call wall', 'Buy-side liquidity / premium order block'],
        ['put', 'Put wall', 'Sell-side liquidity / discount order block'],
        ['flip', 'Gamma flip', 'Market structure shift']
      ].map(([key, label, counterpart]) => {
        const row = document.createElement('tr');
        const level = (data.levels || []).find(l => l.key === key);
        // Compact display only: use the newest matched POI by formation time,
        // independent of price ordering. Keep the explanation tied to that POI.
        const latest = matches.filter(m => m.key === key).reduce((chosen, match) =>
          !chosen || match.poi.pivot_ms > chosen.poi.pivot_ms ? match : chosen, null);
        const values = [label+' · '+money(level?.price), latest ? latest.poi.label+': '+money(latest.poi.price) : counterpart,
          latest ? (latest.eligible ? 'High confluence · ' : 'Context · ')+latest.reason
            : confluence.ready ? 'No confirmed alignment for target selection.' : 'Awaiting current confluence evidence.'];
        values.forEach(value => { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); });
        return row;
      }));
    }
    draw(data); drawCurve(data);
  }
  function renderCurrent(force = false) {
    if (!last) return;
    const now = Date.now();
    const optionsFresh = last.status === 'ready' && now < last.options_valid_until_ms && now >= last.asof_ms-2000;
    const priceFresh = Number.isFinite(last.price) && now < last.price_valid_until_ms;
    const smcFresh = withSMC && last.smc_fresh && priceFresh && now < last.smc_valid_until_ms;
    const targetFresh = optionsFresh && smcFresh && last.target_confluence?.ready && now < last.target_confluence.valid_until_ms;
    const state = optionsFresh+':'+priceFresh+':'+smcFresh+':'+targetFresh;
    if (!force && renderedState === state) return;
    renderedState = state;
    const data = {...last, zones:smcFresh ? last.zones.map(z => ({...z, confluence:optionsFresh && z.confluence})) : [],
      smc_fresh:smcFresh, price:priceFresh ? last.price : null, pois:smcFresh ? last.pois : [],
      target_confluence:{...last.target_confluence, ready:!!targetFresh,
        matches:targetFresh ? last.target_confluence.matches : []},
      basis_percent:optionsFresh && priceFresh ? last.basis_percent : null,
      levels:(last.levels || []).map(level => ({...level, distance_percent:optionsFresh && priceFresh ? level.distance_percent : null}))};
    if (!optionsFresh && data.status === 'ready') Object.assign(data, {status:'stale', stale:true,
      error:'Options snapshot expired; waiting for a successful refresh.'});
    render(data);
  }
  async function refresh() {
    try {
      const response = await fetch('/api/gex/' + encodeURIComponent(panel.dataset.asset), {
        cache:'no-store', signal:AbortSignal.timeout(45000)
      });
      const data = await response.json();
      if (!data.status) throw new Error(data.error || 'Options feed unavailable');
      last = data; renderCurrent(true);
    } catch (_) {
      if (last) {
        last = {...last, status:'stale', stale:true, price:null, zones:[], smc_fresh:false,
          error:'Options refresh failed; retained levels are historical.'}; renderCurrent(true);
      } else {
        find('.gex-status').textContent = 'UNAVAILABLE';
        find('.gex-error').textContent = 'Options feed unavailable. Retrying automatically.';
      }
    } finally { window.setTimeout(refresh, 30000); }
  }
  // Form editing can pause page reloads. Age evidence even while a fetch is pending.
  window.setInterval(renderCurrent, 1000);
  document.addEventListener('visibilitychange', () => renderCurrent());
  refresh();
});
