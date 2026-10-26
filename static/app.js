'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const token = document.querySelector('meta[name="atlas-token"]').content;
  const HOME = [[26.2296745, -80.2974021], [26.3109974, -80.2016082]];
  const HOUSE = '<svg viewBox="0 0 24 26" aria-hidden="true"><path d="M2 12 12 3l10 9-3 1v11H5V13Z"/><path d="M10 24v-8h4v8M8 12h2m4 0h2"/></svg>';
  let allHosts = [], selected = null, view = null, plan = null;
  let busy = false, groupFilter = null, toastTimer, detailsSequence = 0, viewSequence = 0, viewTimer;
  let moving = false, viewPending = true, accountData = null;
  const footprints = L.layerGroup();
  const markers = new Map();
  const selectionLayer = L.layerGroup();
  const map = L.map('map', {zoomControl:false, worldCopyJump:true, minZoom:3, maxZoom:19});
  const streets = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom:19, keepBuffer:1,
    attribution:'© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors'
  }).addTo(map);
  const satellite = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
    maxZoom:19, keepBuffer:1,
    attribution:'Imagery © Esri, Maxar, Earthstar Geographics and the GIS User Community'
  });
  const clusters = L.markerClusterGroup({
    maxClusterRadius:48, showCoverageOnHover:false, zoomToBoundsOnClick:false,
    spiderfyOnMaxZoom:false, animate:!matchMedia('(prefers-reduced-motion: reduce)').matches,
    iconCreateFunction(cluster) {
      const count=cluster.getChildCount(),large=count>99;
      return L.divIcon({html:`<div class="cluster-bubble${large?' large':''}">${count}</div>`,className:'marker-cluster-atlas',iconSize:L.point(large?48:42,large?48:42)});
    }
  }).addTo(map);
  selectionLayer.addTo(map);
  map.fitBounds(HOME,{padding:[30,35]});
  footprints.addTo(map);
  map.on('movestart',()=>{moving=true;viewPending=true;++viewSequence;clearTimeout(viewTimer);updateActions();});
  map.on('moveend',()=>{moving=false;clearTimeout(viewTimer);viewTimer=setTimeout(()=>loadView(),180);});
  map.on('zoomend',()=>setLabelVisibility());
  $('zoom-in').onclick=()=>map.zoomIn(); $('zoom-out').onclick=()=>map.zoomOut();
  $('layer-streets').onclick=()=>setLayer(false);$('layer-satellite').onclick=()=>setLayer(true);
  function setLayer(useSatellite){
    if(useSatellite){map.removeLayer(streets);satellite.addTo(map);}else{map.removeLayer(satellite);streets.addTo(map);}
    for(const[id,active]of [['layer-streets',!useSatellite],['layer-satellite',useSatellite]]){$(id).classList.toggle('active',active);$(id).setAttribute('aria-pressed',String(active));}
  }
  let tileErrors=0;
  for(const layer of [streets,satellite])layer.on('tileerror',()=>{if(++tileErrors===5)toast('Map imagery is temporarily unavailable. Indexed IP details are still accessible.',true);});
  function toast(message,error=false){clearTimeout(toastTimer);$('toast').textContent=message;$('toast').classList.toggle('error',error);$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,error?10000:5500);}
  function boundsInView(){const b=map.getBounds();return[Math.max(-85,b.getSouth()),Math.max(-180,b.getWest()),Math.min(85,b.getNorth()),Math.min(180,b.getEast())];}
  async function api(path,body){
    const response=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json','X-Atlas-Token':token},body:JSON.stringify(body)}:{});
    let result;try{result=await response.json();}catch{throw new Error('Atlas could not read the server response.');}
    if(!response.ok)throw new Error(result.error||'Atlas could not complete this request.');return result;
  }
  function setBusy(value){busy=value;updateActions();}
  function updateActions(){
    const unavailable=busy||viewPending||moving||!plan||Boolean(plan.error);
    const noCredits=accountData?.query_credits===0;
    $('explore-button').disabled=unavailable||(noCredits&&!plan?.cached);
    $('load-more').disabled=unavailable||noCredits;
    $('refresh-area').disabled=unavailable||noCredits;
    $('explore-label').textContent=busy?'Loading this area…':viewPending||moving?'Checking saved areas…':plan?.cached?'Saved IPs loaded':'Explore this area';
    $('explore-cost').textContent=plan?.error||((viewPending||moving)?'Checking saved data · no query credits':plan?.cached?`Saved ${plan.stale?'earlier':'results'} · 0 query credits`:noCredits?'No query credits remaining':'Up to 100 service records · 1 query credit');
    $('refresh-area').hidden=!plan?.cached;
    $('search-view').hidden=!plan?.cached||plan.view_fraction>=.999;
    $('search-view').disabled=unavailable||noCredits;
    $('load-more').hidden=!plan?.cached||!plan.next_page;
    $('load-more-label').textContent='Load next 100 records';
  }
  function savedDate(value){if(!value)return'Not collected';return new Date(value).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});}
  function freshness(value){
    if(!value)return'No saved sample';const hours=Math.max(0,(Date.now()-new Date(value))/3600000);
    return hours<1?'Collected recently':hours<24?`${Math.floor(hours)}h since collection`:`${Math.floor(hours/24)}d since collection`;
  }
  function paintCoverage(){
    if(!view)return;
    const percent=Math.round(view.sampled_fraction*100);
    $('coverage-state').textContent=view.page_count?'Saved map data':'Ready to discover';
    $('coverage-age').textContent=freshness(view.oldest_at);
    $('coverage-age').classList.toggle('aged',view.stale_pages>0);
    $('coverage-age').title=view.oldest_at?`Oldest collection: ${savedDate(view.oldest_at)}. Newest: ${savedDate(view.newest_at)}.`:'No saved searches overlap this view.';
    $('coverage-meter').style.width=percent+'%';
    $('coverage-summary').textContent=`${percent}% of view within saved search areas`;
    $('coverage-detail').textContent=view.page_count?`${view.area_count} area ${view.area_count===1?'sample':'samples'} · ${view.record_count.toLocaleString()} service records saved`:'Explore to save your first sample here.';
    $('coverage-collected').textContent=view.oldest_at?`Collected ${savedDate(view.oldest_at)}${view.newest_at!==view.oldest_at?' – '+savedDate(view.newest_at):''}`:'';
    $('coverage-pages').textContent=plan?.cached?`${plan.loaded_records.toLocaleString()} of ${plan.total_records.toLocaleString()} service records loaded · ${plan.pages.length}/10 pages`:'';
    $('coverage-note').textContent=plan?.stale?'This area is over 24h old. Refresh it to load more pages. All searches are partial inventories.':view.stale_pages?'Some samples are over 24h old. Saved searches are partial inventories.':'Saved searches are samples, not complete inventories.';
    footprints.clearLayers();
    for(const[s,w,n,e]of view.footprints)L.rectangle([[s,w],[n,e]],{color:'#768f76',weight:1,dashArray:'5 5',fillColor:'#89a785',fillOpacity:.045,interactive:false}).addTo(footprints);
    updateActions();
  }
  async function loadView(){
    const sequence=++viewSequence;viewPending=true;updateActions();
    try{
      const data=await api('/api/view?bounds='+encodeURIComponent(JSON.stringify(boundsInView())));
      if(sequence!==viewSequence)return;
      view=data;plan=data.plan;allHosts=data.hosts;
      if(selected&&!allHosts.some(h=>h.ip===selected)){selected=null;selectionLayer.clearLayers();$('detail-panel').hidden=true;++detailsSequence;}
      if(groupFilter)groupFilter=null;
      rebuildProviders();$('clear-filter').hidden=!$('provider-filter').value;render();paintCoverage();
    }catch(error){if(sequence===viewSequence){plan=null;toast(error.message,true);}}
    finally{if(sequence===viewSequence){viewPending=false;updateActions();}}
  }
  function providerNames(){const names=new Map();for(const h of allHosts)names.set(h.provider,(names.get(h.provider)||0)+1);return [...names].sort((a,b)=>b[1]-a[1]);}
  function visibleHosts(){const provider=$('provider-filter').value;return allHosts.filter(h=>(!provider||h.provider===provider)&&(!groupFilter||groupFilter.has(h.ip)));}
  function resetFilters(){groupFilter=null;$('provider-filter').value='';$('clear-filter').hidden=true;}
  function rebuildProviders(){const chosen=$('provider-filter').value;const options=[new Option('All providers','')];for(const[name,count]of providerNames())options.push(new Option(`${name} · ${count}`,name));$('provider-filter').replaceChildren(...options);if(options.some(x=>x.value===chosen))$('provider-filter').value=chosen;$('provider-count').textContent=`${options.length-1} providers`;}
  $('provider-filter').onchange=()=>{groupFilter=null;$('clear-filter').hidden=!$('provider-filter').value;render();};
  $('clear-filter').onclick=()=>{resetFilters();render();};
  function makeIcon(active=false){return L.divIcon({className:'atlas-marker',html:`<span class="building-pin${active?' selected':''}">${HOUSE}</span>`,iconSize:[34,36],iconAnchor:[17,33],tooltipAnchor:[14,-18]});}
  function render(){
    const hosts=visibleHosts();
    const shown=new Set(hosts.map(h=>h.ip));
    for(const[ip,marker]of markers)if(!shown.has(ip)){clusters.removeLayer(marker);markers.delete(ip);}
    if(!hosts.some(h=>h.ip===selected))selectionLayer.clearLayers();
    for(const h of hosts){
      if(!Number.isFinite(h.latitude)||!Number.isFinite(h.longitude))continue;
      const existing=markers.get(h.ip);
      if(existing&&existing.getLatLng().lat===h.latitude&&existing.getLatLng().lng===h.longitude&&existing.atlasHost.provider===h.provider){existing.atlasHost=h;continue;}
      if(existing)clusters.removeLayer(existing);
      const marker=L.marker([h.latitude,h.longitude],{icon:makeIcon(h.ip===selected),title:`${h.ip} — ${h.provider}`,keyboard:true,riseOnHover:true});
      marker.atlasHost=h;
      const label=document.createElement('div');const ip=document.createElement('strong');ip.textContent=h.ip;const provider=document.createElement('span');provider.textContent=h.provider;label.append(ip,provider);
      marker.bindTooltip(label,{className:'atlas-label',direction:'right',permanent:map.getZoom()>=14,opacity:1});marker.on('click',()=>showDetails(marker.atlasHost));markers.set(h.ip,marker);clusters.addLayer(marker);
    }
    renderList(hosts);
    $('host-count').textContent=allHosts.length.toLocaleString();
    const positions=new Set(allHosts.filter(h=>Number.isFinite(h.latitude)&&Number.isFinite(h.longitude)).map(h=>`${h.latitude},${h.longitude}`));
    $('location-count').textContent=`${positions.size} reported locations`;
    $('inventory-context').textContent=groupFilter?'IPs sharing a reported location':$('provider-filter').value?'Provider filter applied':'Saved discoveries in this view';
    $('map-status-text').textContent=`${hosts.length.toLocaleString()} ${hosts.length===1?'IP':'IPs'} in view · ${(view?.known_hosts||0).toLocaleString()} saved discoveries overall`;
  }
  function renderList(hosts){
    const fragment=document.createDocumentFragment();
    for(const h of hosts){
      const button=document.createElement('button');button.className='ip-row'+(h.ip===selected?' selected':'');button.dataset.ip=h.ip;button.setAttribute('aria-label',`${h.ip}, ${h.provider}. Open IP details.`);
      const glyph=document.createElement('span');glyph.className='row-house';glyph.innerHTML=HOUSE;
      const copy=document.createElement('span');copy.className='row-copy';const ip=document.createElement('strong');ip.textContent=h.ip;const provider=document.createElement('small');provider.textContent=h.provider;copy.append(ip,provider);
      const arrow=document.createElement('span');arrow.className='row-arrow';arrow.textContent='›';button.append(glyph,copy,arrow);button.onclick=()=>showDetails(h);fragment.append(button);
    }
    $('ip-list').replaceChildren(fragment);$('list-message').hidden=hosts.length>0;
    $('list-message').textContent=allHosts.length?'No loaded IPs match this provider.':'No saved IPs in this view yet. Explore this area or move back to a saved area.';
  }
  function focusHost(h){
    selectionLayer.clearLayers();
    if(!Number.isFinite(h.latitude)||!Number.isFinite(h.longitude))return;
    map.setView([h.latitude,h.longitude],Math.max(13,Math.min(map.getZoom(),15)),{animate:false});
    if(map.getContainer().clientWidth>600)map.panBy([120,0],{animate:false});
    const marker=L.marker([h.latitude,h.longitude],{icon:makeIcon(true),title:h.ip+' — '+h.provider,zIndexOffset:1200});
    const label=document.createElement('div');const ip=document.createElement('strong');ip.textContent=h.ip;const provider=document.createElement('span');provider.textContent=h.provider;label.append(ip,provider);marker.bindTooltip(label,{className:'atlas-label',direction:'left',permanent:true});selectionLayer.addLayer(marker);
  }
  function setLabelVisibility(){for(const marker of markers.values()){const tooltip=marker.getTooltip();if(!tooltip)continue;tooltip.options.permanent=map.getZoom()>=14;if(tooltip.options.permanent)marker.openTooltip();else marker.closeTooltip();}}
  clusters.on('clusterclick',event=>{
    const child=event.layer.getAllChildMarkers(),box=event.layer.getBounds();
    const samePoint=Math.abs(box.getNorth()-box.getSouth())<.0001&&Math.abs(box.getEast()-box.getWest())<.0001;
    if(!samePoint){map.fitBounds(box,{padding:[90,80],maxZoom:16});return;}
    groupFilter=new Set(child.map(m=>m.atlasHost.ip));$('provider-filter').value='';$('clear-filter').hidden=false;renderList(visibleHosts());$('inventory-context').textContent='IPs sharing a reported location';
    if(child.length<=15)event.layer.spiderfy();
    toast(`${child.length} IPs share these approximate coordinates. Their records are listed on the left.`);
  });
  async function explore(mode='default'){
    if(busy||viewPending||moving||!plan||plan.error)return;
    const action=plan;
    if(mode==='default'&&action.cached){toast('Saved IPs are already on the map. No query credit used.');return;}
    const exact=mode==='exact';
    const bounds=exact?view.bounds:action.bounds;
    const page=mode==='more'?action.next_page:1;
    if(!page)return;
    const refresh=mode==='refresh';
    setBusy(true);
    try{
      const result=await api('/api/area',{bounds,page,refresh,expected_credits:1,expected_snapshot:refresh?action.retrieved_at:null});
      await loadView();
      toast(result.cached?'Saved records reused · 0 query credits.':`Area saved · 1 query credit used.${refresh?' Later pages were reset for this refreshed area.':''}`);
      if(!result.cached){const data=await api('/api/bootstrap');updateAccount(data.account);}
    }catch(error){toast(error.message,true);await loadView();}
    finally{setBusy(false);}
  }
  $('explore-button').onclick=()=>explore();$('load-more').onclick=()=>explore('more');$('refresh-area').onclick=()=>explore('refresh');$('search-view').onclick=()=>explore('exact');
  function updateAccount(data){accountData=data;updateActions();$('connection-label').textContent=data.connected?'Shodan connected':'Shodan unavailable';document.querySelector('.connection').classList.toggle('offline',!data.connected);$('credits').textContent=typeof data.query_credits==='number'?`${data.query_credits} query credits`:'Account unavailable';document.querySelector('.sidebar-footer>div').lastChild.textContent=data.connected?' Shodan connected':' Shodan unavailable';}
  function formatDate(value){if(!value)return'Not reported';const date=new Date(/(?:Z|[+-]\d{2}:?\d{2})$/.test(value)?value:value+'Z');return Number.isNaN(+date)?'Not reported':date.toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});}
  function paintDetails(h){
    $('detail-ip').textContent=h.ip;$('detail-provider').textContent=h.provider||'Unknown provider';$('detail-location').textContent=[h.city,h.country].filter(Boolean).join(', ')||'Location not reported';$('detail-asn').textContent=h.asn||'Not reported';$('detail-org').textContent=h.organization||'Not reported';$('detail-seen').textContent=formatDate(h.last_seen);$('detail-saved').textContent=savedDate(h.saved_at);
    const ports=(h.ports||[]).map(port=>{const tag=document.createElement('span');tag.textContent=String(port);return tag;});$('detail-ports').replaceChildren(...ports);if(!ports.length)$('detail-ports').textContent='No ports returned in this record.';
    $('detail-products').textContent=(h.products||[]).join(' · ');$('shodan-record').href='https://www.shodan.io/host/'+encodeURIComponent(h.ip);
  }
  async function showDetails(h){
    selected=h.ip;focusHost(h);const sequence=++detailsSequence;for(const[ip,marker]of markers)marker.setIcon(makeIcon(ip===selected));for(const row of $('ip-list').querySelectorAll('.ip-row'))row.classList.toggle('selected',row.dataset.ip===selected);
    $('detail-panel').hidden=false;paintDetails(h);$('comparison').textContent='Checking location context…';
    try{
      const detail=await api('/api/host/'+encodeURIComponent(h.ip));if(sequence!==detailsSequence)return;
      paintDetails({...h,...detail,city:h.city,country:h.country,latitude:h.latitude,longitude:h.longitude,products:h.products,saved_at:h.saved_at});const comparison=detail.comparison;$('comparison').replaceChildren();
      if(comparison?.anycast){const heading=document.createElement('strong');heading.textContent='Distributed network';const body=document.createElement('span');body.textContent='IPinfo identifies this as anycast: the same IP is served from multiple locations. A single point is not a unique physical origin.';$('comparison').append(heading,body);}
      else if(comparison?.city){const heading=document.createElement('strong');heading.textContent='Location cross-check';const body=document.createElement('span');body.textContent=`IPinfo reports ${[comparison.city,comparison.region,comparison.country].filter(Boolean).join(', ')}. ${comparison.city.toLowerCase()===(h.city||'').toLowerCase()?'The city labels agree with the map snapshot; the exact site remains unverified.':'This differs from the map snapshot, so treat the location as uncertain.'}`;$('comparison').append(heading,body);}
      else $('comparison').textContent='An additional location estimate is unavailable. The marker uses Shodan’s reported coordinates.';
    }catch(error){if(sequence===detailsSequence)$('comparison').textContent=error.message;}
  }
  $('close-detail').onclick=()=>{$('detail-panel').hidden=true;++detailsSequence;};
  document.addEventListener('keydown',e=>{if(e.key==='Escape'){$('detail-panel').hidden=true;$('place-results').hidden=true;++detailsSequence;}});
  $('home-view').onclick=()=>{resetFilters();map.fitBounds(HOME,{padding:[30,35]});$('place-name').textContent='Coral Springs';$('place-region').textContent='Florida, United States';loadView();$('detail-panel').hidden=true;};
  $('place-form').onsubmit=async event=>{
    event.preventDefault();const text=$('place-input').value.trim();if(text.length<2)return;const button=$('place-form').querySelector('button');button.disabled=true;$('place-results').hidden=true;
    try{const data=await api('/api/places?q='+encodeURIComponent(text));if(!data.places.length){toast('No matching place found. Try a city and state.');return;}
      const rows=data.places.map(place=>{const b=document.createElement('button');b.type='button';b.textContent=place.name;b.onclick=()=>{const[south,north,west,east]=place.bounds;map.fitBounds([[south,west],[north,east]],{padding:[30,35],maxZoom:15});const parts=place.name.split(',');$('place-name').textContent=parts[0];$('place-region').textContent=parts.slice(1).map(x=>x.trim()).join(', ');$('place-results').hidden=true;$('place-input').value='';loadView();$('detail-panel').hidden=true;};return b;});$('place-results').replaceChildren(...rows);$('place-results').hidden=false;
    }catch(error){toast(error.message,true);}finally{button.disabled=false;}
  };
  async function boot(){
    try{const data=await api('/api/bootstrap');updateAccount(data.account);await loadView();map.invalidateSize();}
    catch(error){toast(error.message,true);$('map-status-text').textContent='Atlas could not connect to its local service.';}
  }
  new ResizeObserver(()=>map.invalidateSize()).observe($('map'));boot();
})();
