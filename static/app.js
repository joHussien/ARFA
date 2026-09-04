const SEV_COLOR={major:'#dc2626',moderate:'#f87171',minor:'#fb923c',action:'#facc15',no_flooding:'#22c55e',unknown:'#94a3b8'};
    const SEV_LABEL={major:'Major',moderate:'Moderate',minor:'Minor',action:'Action',no_flooding:'No flooding',unknown:'Unclassified'};
    const SEV_TAG  ={major:'tmaj',moderate:'tmo',minor:'tmi',action:'ta',no_flooding:'tn',unknown:'tu'};
    const SEV_ORDER={major:5,moderate:4,minor:3,action:2,no_flooding:1,unknown:0};
    const RISK_FILL  ={none:'rgba(147,197,253,0.3)',no_flooding:'rgba(34,197,94,0.2)',action:'rgba(250,204,21,0.4)',minor:'rgba(251,146,60,0.5)',moderate:'rgba(248,113,113,0.6)',major:'rgba(220,38,38,0.7)'};
    const RISK_BORDER={none:'#93c5fd',no_flooding:'#16a34a',action:'#ca8a04',minor:'#ea580c',moderate:'#dc2626',major:'#991b1b'};

    let map,gauges=[],mkrs={},resolvedCounties=[],gaugeCache={};
    let tractLayer=null,coverageLayer=null,tractRisk={};
    let bgLayer=null,bgMkrs={},loadTimer=null,searchedLids=new Set();
    // Isolation analysis state
    let roadLayer=null,facilityLayer=null,isoActive=false,isoData=null;
    let currentCountyBbox=null;
    let inundationLayer=null;
    const MIN_ZOOM=5; // don't load below this zoom
    
    map=L.map('map',{zoomControl:false}).setView([39.5,-98.35],4);
    L.control.zoom({position:'topright'}).addTo(map);
    
    // Keep structure footprints above tract polygons.
    map.createPane('structuresPane');
    map.getPane('structuresPane').style.zIndex=610;
    map.getPane('structuresPane').style.pointerEvents='auto';

    // Keep water-gauge markers ABOVE structure polygons so they remain clickable.
    map.createPane('gaugesPane');
    map.getPane('gaugesPane').style.zIndex=650;
    map.getPane('gaugesPane').style.pointerEvents='auto';
    map.createPane('inundationPane');
    map.getPane('inundationPane').style.zIndex=470;
    map.getPane('inundationPane').style.pointerEvents='none';

    // Routing overlays sit above structures but below gauge markers.
    map.createPane('routesPane');
    map.getPane('routesPane').style.zIndex=625;
    map.getPane('routesPane').style.pointerEvents='none';
    map.createPane('sheltersPane');
    map.getPane('sheltersPane').style.zIndex=640;
    map.getPane('sheltersPane').style.pointerEvents='auto';

    // ESRI World Topo — similar to Google Maps terrain style
    // L.tileLayer(
    //   'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
    //   {
    //     attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
    //     subdomains: 'abcd',
    //     maxZoom: 20
    //   }
    // ).addTo(map);

        L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
  attribution: 'Tiles © Esri',
  maxZoom: 19
}).addTo(map);
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', {
  attribution: '',
  maxZoom: 19,
  opacity: 0.8
}).addTo(map);
    // Structures are intentionally NOT refreshed on move/zoom.
    // They are loaded only when the user explicitly toggles the Structures layer on.
    // Panning/zooming keeps the loaded snapshot until the user turns it off and on again.

    async function loadBboxGauges(){
      const b=map.getBounds();
      const minLat=b.getSouth(),maxLat=b.getNorth(),minLon=b.getWest(),maxLon=b.getEast();

      const params=new URLSearchParams({
        minLat:b.getSouth().toFixed(4),maxLat:b.getNorth().toFixed(4),
        minLon:b.getWest().toFixed(4), maxLon:b.getEast().toFixed(4),
      });
      setStatus('Loading gauges…',true);
      const data=await apiFetch(`/api/gauges/bbox?${params}`);
      if(!data||data.error){setStatus('Ready',false);return;}
      renderBgGauges(data);
      setStatus(`${data.length} gauges in view`,false);
    }

    function renderBgGauges(list){
      // Remove old bg markers not in new list
      const newIds=new Set(list.map(g=>g.lid));
      for(const [lid,m] of Object.entries(bgMkrs)){
        if(!newIds.has(lid)){m.remove();delete bgMkrs[lid];}
      }
      for(const g of list){
        if(bgMkrs[g.lid]) continue; // already on map
        if(searchedLids.has(g.lid)) continue; // shown as colored gauge already
        const lat=parseFloat(g.latitude),lon=parseFloat(g.longitude);
        if(isNaN(lat)||isNaN(lon))continue;
        const z = map.getZoom();
        const size = z >= 10 ? 10 : z >= 8 ? 9 : 8;
        const icon=L.divIcon({className:'',
          html:`<div style="width:${size}px;height:${size}px;border-radius:50%;background:#6366f1;border:2px solid white;box-shadow:0 0 4px rgba(99,102,241,.6);cursor:pointer"></div>`,
          iconSize:[size,size],iconAnchor:[size/2,size/2]});
        const m=L.marker([lat,lon],{icon,pane:'gaugesPane',zIndexOffset:1000});
        m.on('click',()=>selBg(g));
        m.addTo(map);
        bgMkrs[g.lid]=m;
      }
    }

    function clearBgGauges(){
      Object.values(bgMkrs).forEach(m=>m.remove());bgMkrs={};
    }

    // Click on a background (grey) gauge — load its readings directly
    async function selBg(g){
      // Build a minimal gauge object and pass to sel pipeline
      const lat=parseFloat(g.latitude),lon=parseFloat(g.longitude);
      map.flyTo([lat,lon],12,{duration:.7});
      document.getElementById('gl').style.display='none';
      document.getElementById('av').classList.add('on');
      document.getElementById('atitle').textContent=g.name||g.lid;
      document.getElementById('ameta').textContent=`${g.lid} · ${lat.toFixed(4)}, ${lon.toFixed(4)}`;
      document.getElementById('alog').innerHTML='';
    renderFloodHubDetail({...g,_sev:'unknown',_basinArea:0,_basinName:''});
    }

    function parseSev(g){
      const c=(g?.status?.observed?.floodCategory||g?.floodCategory||'').toLowerCase();
      if(c.includes('major'))return'major';
      if(c.includes('moderate'))return'moderate';
      if(c.includes('minor'))return'minor';
      if(c.includes('action'))return'action';
      if(c.includes('no flood')||c.includes('normal')||c==='none')return'no_flooding';
      return'unknown';
    }
    async function apiFetch(p){try{const r=await fetch(p);if(!r.ok)return null;return r.json();}catch{return null;}}
    async function apiPost(p,body){try{const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json().catch(()=>null);return r.ok?d:null;}catch{return null;}}
    function setStatus(msg,active){
      document.getElementById('stxt').textContent=msg;
      const p=document.getElementById('pulse');
      p.style.background=active?'#f59e0b':'#22c55e';
      p.className=active?'pulse pa':'pulse';
    }
    function haversineKm(la1,lo1,la2,lo2){
      const R=6371,φ1=la1*Math.PI/180,φ2=la2*Math.PI/180;
      const dφ=(la2-la1)*Math.PI/180,dλ=(lo2-lo1)*Math.PI/180;
      const a=Math.sin(dφ/2)**2+Math.cos(φ1)*Math.cos(φ2)*Math.sin(dλ/2)**2;
      return R*2*Math.atan2(Math.sqrt(a),Math.sqrt(1-a));
    }
    function basinRadius(areaKm2){return areaKm2>0?Math.sqrt(areaKm2/Math.PI):20;}

    // ── Search (removed from UI — agent handles location queries directly) ──────

    // ── County bar ──────────────────────────────────────────────────────────────
    function renderCountyBar(counties){
      const bar=document.getElementById('countyBar');
      if(!counties.length){bar.style.display='none';return;}
      bar.style.display='flex';
      let html='<span style="color:var(--mu);font-size:11px;flex-shrink:0">Area:</span>';
      if(counties.length>1)
        html+=`<div class="ctag all-tag" id="ct-all" onclick="loadAll()">All (${counties.length})</div>`;
      html+=counties.map((c,i)=>
        `<div class="ctag ${i===0?'active':''}" id="ct-${c.usgs_county_cd}" onclick="switchCounty('${c.usgs_county_cd}')">${c.name}</div>`
      ).join('');
      bar.innerHTML=html;
    }

    async function switchCounty(cd){
      const county=resolvedCounties.find(c=>c.usgs_county_cd===cd);if(!county)return;
      document.querySelectorAll('.ctag').forEach(t=>t.classList.remove('active'));
      document.getElementById('ct-'+cd)?.classList.add('active');
      clearAll();back();
      await loadCounty(county);
    }

    async function loadAll(preserveAgent=false){
      document.querySelectorAll('.ctag').forEach(t=>t.classList.remove('active'));
      document.getElementById('ct-all')?.classList.add('active');
      clearAll(preserveAgent);back();
      setStatus(`Loading all ${resolvedCounties.length} counties…`,true);
      const [tractResults,gaugeResults]=await Promise.all([
        Promise.all(resolvedCounties.map(c=>fetchTracts(c))),
        Promise.all(resolvedCounties.map(c=>apiFetch(`/api/gauges?county_cd=${c.usgs_county_cd}`)))
      ]);
      drawTracts(tractResults.flat());
      let all=gaugeResults.flat().filter(g=>g&&!g.error);
      // Render raw USGS observations immediately. Hydrologic assessment continues in parallel.
      gauges=all;renderList();renderMarkers();colorTracts();fitMap();

      // loadAll() is used when a location resolves to multiple counties (for
      // example Riverside, CA). Keep a valid loaded-area bbox so follow-up
      // structure/facility requests such as "schools in this area" reuse the
      // current area instead of incorrectly trying to resolve that sentence as
      // a new geographic location.
      const loadedBounds=map.getBounds();
      if(loadedBounds&&loadedBounds.isValid()){
        currentCountyBbox={
          minLat:loadedBounds.getSouth(), maxLat:loadedBounds.getNorth(),
          minLon:loadedBounds.getWest(),  maxLon:loadedBounds.getEast(),
        };
      }

      setStatus(`${all.length} gauges · assessing current conditions…`,true);
      all=await enrichGauges(all);
      gauges=all;renderList();renderMarkers();colorTracts();
      const assessed=gauges.filter(g=>effectiveGaugeStatus(g)!=='unknown').length;
      setStatus(`${gauges.length} gauges · ${assessed} assessed`,false);
      document.getElementById('psub').textContent=`All counties · ${gauges.length} gauges · ${assessed} assessed`;
    }

    // ── Load one county ──────────────────────────────────────────────────────────
    async function loadCounty(county){
      setStatus(`Loading ${county.name}…`,true);
      document.getElementById('psub').textContent=`Loading ${county.name}…`;
      const [tracts,list]=await Promise.all([
        fetchTracts(county),
        apiFetch(`/api/gauges?county_cd=${county.usgs_county_cd}`)
      ]);
      drawTracts(tracts);
      if(!list||list.error||!list.length){
        const idx=resolvedCounties.findIndex(c=>c.usgs_county_cd===county.usgs_county_cd);
        if(idx>=0&&idx+1<resolvedCounties.length){
          const next=resolvedCounties[idx+1];
          setStatus(`No gauges in ${county.name} — trying ${next.name}…`,true);
          document.querySelectorAll('.ctag').forEach(t=>t.classList.remove('active'));
          document.getElementById('ct-'+next.usgs_county_cd)?.classList.add('active');
          await loadCounty(next);return;
        }
        setStatus('No gauges found',false);
        document.getElementById('gl').innerHTML=`<div class="empty"><p>No gauges in ${county.name}</p></div>`;
        return;
      }
      gauges=list;renderList();renderMarkers();colorTracts();fitMap();
      setStatus(`${list.length} gauges · assessing current conditions…`,true);
      const enriched=await enrichGauges(list);
      gauges=enriched;renderList();renderMarkers();colorTracts();
      const assessed=gauges.filter(g=>effectiveGaugeStatus(g)!=='unknown').length;
      setStatus(`${gauges.length} gauges · ${assessed} assessed`,false);
      document.getElementById('psub').textContent=`${county.name} · ${gauges.length} gauges · ${assessed} assessed`;

      // Store bbox for isolation analysis
      const lats=gauges.map(g=>parseFloat(g.latitude)).filter(v=>!isNaN(v));
      const lons=gauges.map(g=>parseFloat(g.longitude)).filter(v=>!isNaN(v));
      if(lats.length){
        currentCountyBbox={
          minLat:Math.min(...lats)-0.05, maxLat:Math.max(...lats)+0.05,
          minLon:Math.min(...lons)-0.05, maxLon:Math.max(...lons)+0.05,
        };
       
      }
    }

    // ── Census tracts ────────────────────────────────────────────────────────────
    async function fetchTracts(county){
      const d=await apiFetch(`/api/tracts?state_fips=${county.state_fips}&county_fips=${county.county_fips}`);
      return d?.features||[];
    }
    function drawTracts(features){
      if(tractLayer){tractLayer.remove();tractLayer=null;}tractRisk={};
      if(!features.length)return;
      tractLayer=L.geoJSON({type:'FeatureCollection',features},{
        style:f=>{
          const risk=tractRisk[f.properties?.GEOID]||'none';
          return{fillColor:RISK_FILL[risk],fillOpacity:1,color:RISK_BORDER[risk],weight:0.8,opacity:0.8};
        },
        onEachFeature:(f,layer)=>{
          const risk=tractRisk[f.properties?.GEOID]||'none';
          layer.bindTooltip(`<b>Tract ${f.properties?.NAME||''}</b><br>Risk: ${risk}`,{sticky:true});
        }
      }).addTo(map);
    }
    function colorTracts(){
      // Tracts are rendered neutral — only the HAND flood hazard polygon
      // (from /api/flood/hazard) colors the affected area. The naive NWM
      // point-in-bbox coloring is removed; HAND is the authoritative source.
      loadHANDHazard();
    }

    async function loadHANDHazard(){
      if(inundationLayer){inundationLayer.remove();inundationLayer=null;}
      if(window._handLayer){map.removeLayer(window._handLayer);window._handLayer=null;}
      if(!gauges.length) return;

      // Derive bbox from gauge locations
      const lats=gauges.map(g=>parseFloat(g.latitude)).filter(v=>!isNaN(v));
      const lons=gauges.map(g=>parseFloat(g.longitude)).filter(v=>!isNaN(v));
      if(!lats.length) return;
      const minLon=(Math.min(...lons)-.02).toFixed(5),maxLon=(Math.max(...lons)+.02).toFixed(5);
      const minLat=(Math.min(...lats)-.02).toFixed(5),maxLat=(Math.max(...lats)+.02).toFixed(5);

      // Also fetch NWM for the inundation layer (keeps the sidebar gauge coloring)
      const nwmParams=new URLSearchParams({minLat,minLon,maxLat,maxLon});
      const nwmData=await apiFetch(`/api/inundation?${nwmParams}`);
      const nwmFeatures=nwmData?.features||[];
      if(nwmFeatures.length){
        inundationLayer=L.geoJSON({type:'FeatureCollection',features:nwmFeatures},{
          pane:'inundationPane',
          style:f=>{
            const sev=f.properties?.severity||'action';
            const cols={major:'rgba(220,38,38,0.55)',moderate:'rgba(248,113,113,0.5)',minor:'rgba(251,146,60,0.45)',action:'rgba(250,204,21,0.4)',no_flooding:'rgba(34,197,94,0.2)',unknown:'rgba(147,197,253,0.3)'};
            const brd={major:'#991b1b',moderate:'#dc2626',minor:'#ea580c',action:'#ca8a04',no_flooding:'#16a34a',unknown:'#93c5fd'};
            return{fillColor:cols[sev]||cols.unknown,fillOpacity:1,color:brd[sev]||brd.unknown,weight:0.8,opacity:0.9};
          },
          onEachFeature:(f,layer)=>{
            const p=f.properties||{};
            layer.bindTooltip(`<b>NWM Flood Inundation</b><br>Severity: ${p.severity||'?'}<br>Flow: ${p.streamflow_cfs!=null?Math.round(p.streamflow_cfs)+' cfs':'—'}`,{sticky:true});
          }
        }).addTo(map);
      }

      // HAND hazard polygon — only drawn when there is an active flood category
      // Clamp bbox to ≤1.5° (flood_hazard/geo.py limit)
      const dLat=parseFloat(maxLat)-parseFloat(minLat);
      const dLon=parseFloat(maxLon)-parseFloat(minLon);
      if(dLat>1.5||dLon>1.5){
        console.log('[HAND] bbox too large for DEM analysis, skipping');
        return;
      }
      try{
        // Pass pre-fetched gauges to avoid double USGS/NOAA round-trip
        const handResp=await fetch(`/api/flood/hazard?minLon=${minLon}&minLat=${minLat}&maxLon=${maxLon}&maxLat=${maxLat}`,{
          method:'GET',headers:{'Content-Type':'application/json'},
        });
        if(!handResp.ok) return;
        const handData=await handResp.json();
        if(handData.error){console.warn('[HAND]',handData.error);return;}
        const handFeatures=(handData.features||[]);
        if(!handFeatures.length) return;
        window._handLayer=L.geoJSON({type:'FeatureCollection',features:handFeatures},{
          pane:'inundationPane',
          style:()=>({fillColor:'rgba(239,68,68,0.35)',fillOpacity:1,color:'#b91c1c',weight:1.5,opacity:0.9,dashArray:'4 3'}),
          onEachFeature:(f,layer)=>{
            const m=handData.metadata||{};
            layer.bindTooltip(
              `<b>HAND Flood Hazard</b><br>`+
              `Category: ${m.flood_category||'?'} · Threshold: ${m.hand_threshold_m??'?'} m<br>`+
              `<span style="font-size:10px;color:#666">${m.method||'HAND screening'}</span>`,
              {sticky:true}
            );
          }
        }).addTo(map);
        console.log(`[HAND] ${handFeatures.length} hazard polygon(s) · category=${handData.metadata?.flood_category} threshold=${handData.metadata?.hand_threshold_m}m`);
      }catch(e){
        console.warn('[HAND] hazard fetch failed:',e);
      }
    }

function colorTractsFromInundation(features){
  if(!tractLayer)return;
  // Build bbox list for fast centroid-in-polygon check
  const iBoxes=features.map(f=>{
    const coords=f.geometry?.coordinates||[];
    const flat=coords.flat(2);
    const lons=flat.map(p=>p[0]),lats=flat.map(p=>p[1]);
    return{
      minLon:Math.min(...lons),maxLon:Math.max(...lons),
      minLat:Math.min(...lats),maxLat:Math.max(...lats),
      severity:f.properties?.severity||'action'
    };
  });

  tractLayer.eachLayer(layer=>{
    const f=layer.feature;
    const geoid=f.properties?.GEOID||'';
    const tb=layer.getBounds();
    const clat=(tb.getNorth()+tb.getSouth())/2;
    const clon=(tb.getEast()+tb.getWest())/2;

    // Find worst severity among inundation polygons covering this tract centroid
    let worst='none',worstOrd=-1;
    for(const ib of iBoxes){
      if(clat>=ib.minLat&&clat<=ib.maxLat&&clon>=ib.minLon&&clon<=ib.maxLon){
        const ord=SEV_ORDER[ib.severity]||0;
        if(ord>worstOrd){worstOrd=ord;worst=ib.severity;}
      }
    }

    // Blend with nearest gauge severity as a floor
    const gSev=nearestGaugeSev(clat,clon);
    if((SEV_ORDER[gSev]||0)>worstOrd) worst=gSev;

    if(worst==='unknown'||worst==='no_flooding'&&worstOrd<=0) worst='none';
    tractRisk[geoid]=worst;
    layer.setStyle({
      fillColor:RISK_FILL[worst]||RISK_FILL.none,
      fillOpacity:1,
      color:RISK_BORDER[worst]||RISK_BORDER.none,
      weight:worst==='none'?0.6:1.5,
      opacity:0.85,
    });
    layer.setTooltipContent(`<b>Tract ${f.properties?.NAME||''}</b><br>Risk: ${worst}`);
  });
}

function nearestGaugeSev(clat,clon){
  let bestSev='none',bestDist=999;
  for(const g of gauges){
    const glat=parseFloat(g.latitude),glon=parseFloat(g.longitude);
    if(isNaN(glat))continue;
    const d=haversineKm(clat,clon,glat,glon);
    if(d<bestDist){bestDist=d;bestSev=g._sev||'none';}
  }
  return bestDist<=30?bestSev:'none';
}

function colorTractsBasinFallback(){
  if(!tractLayer||!gauges.length)return;
  tractLayer.eachLayer(layer=>{
    const f=layer.feature;
    const geoid=f.properties?.GEOID||'';
    const bounds=layer.getBounds();
    const clat=(bounds.getNorth()+bounds.getSouth())/2;
    const clon=(bounds.getEast()+bounds.getWest())/2;
    let worst='none',worstOrd=-1;
    for(const g of gauges){
      const glat=parseFloat(g.latitude),glon=parseFloat(g.longitude);
      if(isNaN(glat))continue;
      const dist=haversineKm(clat,clon,glat,glon);
      const rad=basinRadius(g._basinArea||0);
      if(dist<=rad){
        const ord=SEV_ORDER[g._sev]||0;
        if(ord>worstOrd){worstOrd=ord;worst=g._sev||'unknown';}
      }
    }
    if(worst==='unknown')worst='none';
    tractRisk[geoid]=worst;
    layer.setStyle({
      fillColor:RISK_FILL[worst]||RISK_FILL.none,
      fillOpacity:1,
      color:RISK_BORDER[worst]||RISK_BORDER.none,
      weight:worst==='none'?0.6:1.5,
      opacity:0.85,
    });
    layer.setTooltipContent(`<b>Tract ${f.properties?.NAME||''}</b><br>Risk: ${worst}`);
  });
}

    // ── Gauge enrichment ─────────────────────────────────────────────────────────
    function effectiveGaugeStatus(g){
      return (g?._sev||'unknown');
    }

    async function enrichGauges(list){
      const enriched=[...list];

      // Enrich raw USGS observations with NOAA/NWPS metadata and official flood-stage thresholds.
      // If NOAA/NWPS does not provide a current category, we may derive a category only by
      // directly comparing observed stage with official NOAA/NWPS thresholds.
      const noaaPromise=(async()=>{
        for(let i=0;i<enriched.length;i+=8){
          await Promise.all(enriched.slice(i,i+8).map(async(g,bi)=>{
            const idx=i+bi;
            const usgsStage=enriched[idx]._stage;
            const d=await apiFetch(`/api/gauge/${g.lid}`);
            const noaaAvailable=d && !d.error;
            enriched[idx]._d=noaaAvailable?d:null;
            enriched[idx]._sev=noaaAvailable?parseSev(d):'unknown';
            // When NOAA has no official flood category, classify from stage
            if(enriched[idx]._sev==='unknown'){
              const stage=parseFloat(enriched[idx]._stage);
              if(!isNaN(stage)&&stage>0){
                const fl=noaaAvailable?(d?.flood||{}):{};
                // Extract stage thresholds from NOAA categories
                const safeStage=v=>(v&&parseFloat(v)>0)?parseFloat(v):null;
                const cats=fl.categories||{};
                const thAct=safeStage(cats.action?.stage);
                const thMin=safeStage(cats.minor?.stage);
                const thMod=safeStage(cats.moderate?.stage);
                const thMaj=safeStage(cats.major?.stage);
                if(thAct||thMin||thMod||thMaj){
                  // Official NOAA/NWPS thresholds exist — compare stage directly
                  if(thMaj&&stage>=thMaj) enriched[idx]._sev='major';
                  else if(thMod&&stage>=thMod) enriched[idx]._sev='moderate';
                  else if(thMin&&stage>=thMin) enriched[idx]._sev='minor';
                  else if(thAct&&stage>=thAct) enriched[idx]._sev='action';
                  else enriched[idx]._sev='no_flooding';
                } else {
                  // No NOAA thresholds — use 7-day avg * 1.5/3.0/4.0 as fallback
                  const avg=parseFloat(enriched[idx]._stage_avg||0);
                  if(avg>0){
                    if(stage>=avg*4.0) enriched[idx]._sev='major';
                    else if(stage>=avg*3.0) enriched[idx]._sev='moderate';
                    else if(stage>=avg*1.5) enriched[idx]._sev='minor';
                    else enriched[idx]._sev='no_flooding';
                  } else {
                    enriched[idx]._sev='no_flooding';
                  }
                }
              } else {
                enriched[idx]._sev='no_flooding';
              }
            }
            const officialCategory=String(d?.status?.observed?.floodCategory||d?.floodCategory||'').trim();
            enriched[idx]._statusSource = officialCategory ? 'NOAA/NWPS official category' : (enriched[idx]._sev&&enriched[idx]._sev!=='unknown'&&enriched[idx]._sev!=='no_flooding' ? 'NOAA/NWPS threshold-derived' : 'NOAA/NWPS — no flood stage');
            const noaaStage=noaaAvailable?d?.status?.observed?.primary?.value:null;
            enriched[idx]._stage=(noaaStage!=null && noaaStage!=='')?noaaStage:usgsStage;
            enriched[idx]._basinArea=noaaAvailable?(d?.basin?.area||0):0;
            enriched[idx]._basinName=noaaAvailable?(d?.basin?.name||''):'';
          }));
        }
      })();

      await noaaPromise;
      enriched.sort((a,b)=>(SEV_ORDER[effectiveGaugeStatus(b)]||0)-(SEV_ORDER[effectiveGaugeStatus(a)]||0));
      return enriched;
    }

    function renderList(){
      const el=document.getElementById('gl');
      if(!gauges.length){el.innerHTML='<div class="empty"><p>No gauges</p></div>';return;}
      el.innerHTML=gauges.map(g=>{
        const s=effectiveGaugeStatus(g);
        return`<div class="gc" id="c-${g.lid}" onclick="sel('${g.lid}')">
          <div class="gdot" style="background:${SEV_COLOR[s]}"></div>
          <div class="gr">
            <div class="gn">${g.name||g.lid}</div>
            <div class="gm">${g.lid}${g._basinName?' · '+g._basinName:''}</div>
            <div class="gtags">
              <span class="tag ${SEV_TAG[s]}">${SEV_LABEL[s]}</span>
              ${g._stage!=null?`<span class="tag tb">${parseFloat(g._stage).toFixed(2)} ft</span>`:''}
              ${g._basinArea?`<span class="tag" style="background:#f0f9ff;color:#0369a1">${Math.round(g._basinArea)} km²</span>`:''}
            </div>
          </div>
        </div>`;
      }).join('');
    }

    function renderMarkers(){
      Object.values(mkrs).forEach(m=>m.remove());mkrs={};
      searchedLids=new Set(gauges.map(g=>g.lid));
      // Hide bg markers that overlap with searched gauges
      for(const lid of searchedLids){
        if(bgMkrs[lid]){bgMkrs[lid].remove();delete bgMkrs[lid];}
      }
      gauges.forEach(g=>{
        const lat=parseFloat(g.latitude),lon=parseFloat(g.longitude);
        if(isNaN(lat)||isNaN(lon))return;
        const col=SEV_COLOR[effectiveGaugeStatus(g)]||SEV_COLOR.unknown;
        const sevCol=col;
        const icon=L.divIcon({className:'',
          html:`<div class="gm-dot" style="background:${col};border-color:white"></div>`,
          iconSize:[14,14],iconAnchor:[7,7]});
        const m=L.marker([lat,lon],{icon,pane:'gaugesPane',zIndexOffset:2000});
        m.on('click',()=>sel(g.lid));
        m.addTo(map);mkrs[g.lid]=m;
      });
    }

    function fitMap(){
      const lats=gauges.map(g=>parseFloat(g.latitude)).filter(v=>!isNaN(v));
      const lons=gauges.map(g=>parseFloat(g.longitude)).filter(v=>!isNaN(v));
      if(lats.length) map.fitBounds([[Math.min(...lats),Math.min(...lons)],[Math.max(...lats),Math.max(...lons)]],{padding:[40,40],maxZoom:11});
    }

    // ── Select gauge ─────────────────────────────────────────────────────────────
    function sel(lid){
      const g=gauges.find(x=>x.lid===lid);if(!g)return;
      document.querySelectorAll('.gc').forEach(c=>c.classList.remove('sel'));
      document.getElementById('c-'+lid)?.classList.add('sel');
      const lat=parseFloat(g.latitude),lon=parseFloat(g.longitude);
      if(!isNaN(lat)&&!isNaN(lon)) map.flyTo([lat,lon],11,{duration:.7});
      document.getElementById('gl').style.display='none';
      document.getElementById('av').classList.add('on');
      document.getElementById('atitle').textContent=g.name||lid;
      document.getElementById('ameta').textContent=`${lid} · ${lat.toFixed(4)}, ${lon.toFixed(4)}`;
      document.getElementById('alog').innerHTML='';
      renderFloodHubDetail(g);
    }

    // ── Panel toggles (from topbar toolbar) ─────────────────────────────────
    let gaugePanelVisible=true;
    let agentPanelVisible=true;

    function toggleGaugePanel(){
      gaugePanelVisible=!gaugePanelVisible;
      document.getElementById('gaugePanel')?.classList.toggle('collapsed',!gaugePanelVisible);
      document.getElementById('body')?.classList.toggle('gauge-collapsed',!gaugePanelVisible);
      const btn=document.getElementById('tbGaugesBtn');
      if(btn) btn.classList.toggle('panel-off',!gaugePanelVisible);
      if(btn) btn.title=gaugePanelVisible?'Hide gauge panel':'Show gauge panel';
    }

    function toggleAgentPanel(){
      agentPanelVisible=!agentPanelVisible;
      document.getElementById('agentPanel')?.classList.toggle('collapsed',!agentPanelVisible);
      document.getElementById('body')?.classList.toggle('agent-collapsed',!agentPanelVisible);
      const btn=document.getElementById('tbChatBtn');
      if(btn) btn.classList.toggle('panel-off',!agentPanelVisible);
      if(btn) btn.title=agentPanelVisible?'Hide chat':'Show chat';
    }

    // ── Routes visibility (map lines + routing subtab) ───────────────────────
    let routesVisible=true;
    function toggleRoutesVisibility(){
      routesVisible=!routesVisible;
      // Toggle map lines
      if(window._routeLayers){
        window._routeLayers.forEach(l=>{
          if(routesVisible) l.addTo(map); else map.removeLayer(l);
        });
      }
      // Toggle routing subtab
      const routeTools=document.getElementById('agentRouteTools');
      if(routeTools) routeTools.classList.toggle('hidden',!routesVisible);
      // Update toolbar button
      const btn=document.getElementById('tbRoutesBtn');
      if(btn) btn.textContent=routesVisible?'Routes':'Show routes';
      if(btn) btn.classList.toggle('active',!routesVisible);
    }

    // Called after routes are drawn — show toolbar button and routing subtab
    function _showHideRoutesBtn(){
      routesVisible=true;
      const btn=document.getElementById('tbRoutesBtn');
      if(btn){ btn.style.display='inline-flex'; btn.textContent='Routes'; btn.classList.remove('active'); }
      // Always show the routing subtab when routes are rendered
      document.getElementById('agentRouteTools')?.classList.remove('hidden');
    }

    // Gauge coverage circles intentionally removed: basin area is not a circular influence boundary.
    function back(){
      document.getElementById('av').classList.remove('on');
      document.getElementById('gl').style.display='block';
      if(coverageLayer){coverageLayer.remove();coverageLayer=null;}
      
    }

    function clearAll(preserveAgent=false){
      Object.values(mkrs).forEach(m=>m.remove());mkrs={};gauges=[];
      searchedLids=new Set();
      if(tractLayer){tractLayer.remove();tractLayer=null;}tractRisk={};
      if(coverageLayer){coverageLayer.remove();coverageLayer=null;}
      if(inundationLayer){inundationLayer.remove();inundationLayer=null;}
      if(window._queryLayer){map.removeLayer(window._queryLayer);window._queryLayer=null;}
      if(window._handLayer){map.removeLayer(window._handLayer);window._handLayer=null;}
      resetRoutingContext(preserveAgent);
      clearIsolation();
      currentCountyBbox=null;
      document.getElementById('gl').innerHTML='<div class="empty"><p>Loading…</p></div>';
      if(structuresLayer){map.removeLayer(structuresLayer);structuresLayer=null;}
      map.closePopup(structurePopup);
      structuresData=null;
      structuresVisible=false;
      structuresLoading=false;
      if(structuresAbortController){
        structuresAbortController.abort();
        structuresAbortController=null;
      }
      setStructuresProgress({show:false});
      updateStructuresToggleUI();
    }

    // ── AGENTIC LOOP ──────────────────────────────────────────────────────────────
    async function agentRun(gauge){
      const log=document.getElementById('alog');
      const lid=gauge.lid;
      if(gaugeCache[lid]){
        log.innerHTML=gaugeCache[lid];
        setTimeout(()=>{log.scrollTop=0;},50);
        return;
      }

      const s1=addStep(log,'spin','Step 1 · Gauge metadata (NOAA)');
      const meta=await apiFetch(`/api/gauge/${lid}`);
      if(!meta||meta.error){doneStep(s1,'warn','Unavailable',meta?.error||'No data');return;}
      const fl=meta.flood||{};
      const [act,minor,mod,maj]=[fl.action,fl.minor,fl.moderate,fl.major];
      doneStep(s1,'ok','Metadata acquired',()=>{
        const d=document.createElement('div');
        d.innerHTML=`<div class="mg">
          <div class="mc"><div class="mv">${act??'—'}</div><div class="ml">Action (ft)</div></div>
          <div class="mc"><div class="mv">${minor??'—'}</div><div class="ml">Minor (ft)</div></div>
          <div class="mc"><div class="mv">${mod??'—'}</div><div class="ml">Moderate (ft)</div></div>
          <div class="mc"><div class="mv">${maj??'—'}</div><div class="ml">Major (ft)</div></div>
        </div>
        ${gauge._basinArea?`<div style="font-size:11px;color:var(--mu);margin-top:6px">Basin: ${gauge._basinName||'—'} · ${Math.round(gauge._basinArea)} km² · ~${Math.round(basinRadius(gauge._basinArea))} km coverage radius</div>`:''}`;
        return d;
      });

      const s2=addStep(log,'spin','Step 2 · 10-day stage history (NOAA NWPS)');
      const sf=await apiFetch(`/api/gauge/${lid}/stageflow`);
      const obs=sf?.observed?.data||[];
      const stages=obs.map(r=>parseFloat(r.primary)).filter(v=>!isNaN(v)&&v<500);
      const times=obs.map(r=>r.time||'');
      let usgs=null;

      if(!stages.length){
        doneStep(s2,'warn','No observations','NOAA has no recent data.');
      } else {
        const latest=stages[stages.length-1],prev=stages[stages.length-2];
        const mx=Math.max(...stages),mn=Math.min(...stages);
        const trend=prev!=null?(latest-prev>0.05?'↑ rising':latest-prev<-0.05?'↓ falling':'→ steady'):'unknown';
        const tc=trend.includes('↑')?'tu-c':trend.includes('↓')?'td-c':'ts-c';
        doneStep(s2,'ok',`${stages.length} readings · ${mn.toFixed(2)}–${mx.toFixed(2)} ft`,()=>{
          const d=document.createElement('div');
          d.innerHTML=`<div class="mg">
            <div class="mc"><div class="mv">${latest.toFixed(2)}</div><div class="ml">Current (ft)</div><div class="mt ${tc}">${trend}</div></div>
            <div class="mc"><div class="mv">${mx.toFixed(2)}</div><div class="ml">4-day peak (ft)</div></div>
          </div>`;
          if(stages.length>3){
            const cw=document.createElement('div');cw.className='cw';
            const cv=document.createElement('canvas');cv.id='spk1';cv.style.width='100%';cv.style.height='80px';
            cw.appendChild(cv);
            const lbl=document.createElement('div');lbl.className='clbl';
            lbl.innerHTML=`<span>${times[0]?.slice(0,10)||''}</span><span>${times[Math.floor(times.length/2)]?.slice(0,10)||''}</span><span>${times[times.length-1]?.slice(0,10)||''}</span>`;
            cw.appendChild(lbl);d.appendChild(cw);
            setTimeout(()=>spark('spk1',stages,{action:act,minor,moderate:mod,major:maj}),80);
          }
          return d;
        });

        const s3=addStep(log,'spin','Step 3 · Reasoning: stage interpretation');
        doneStep(s3,'think','Stage interpretation',()=>{
          const d=document.createElement('div');
          const rb=document.createElement('div');rb.className='rb';
          rb.innerHTML=`<div class="rl">ARFA REASONING</div>${stageReason(latest,mx,mn,trend,act,minor,mod,maj)}`;
          d.appendChild(rb);
          const gaps=[];
          if(act==null)gaps.push('Action stage undefined — cannot assess flood risk level');
          if(stages.length<8)gaps.push('Sparse data — trend assessment has low confidence');
          if(gaps.length){
            const gd=document.createElement('div');gd.style.marginTop='8px';
            gd.innerHTML=gaps.map(x=>`<div class="eg"><span class="egi">⚠</span>${x}</div>`).join('');
            d.appendChild(gd);
          }
          return d;
        });
      }

      const s4=addStep(log,'spin','Step 4 · USGS cross-verification');
      try{
        const ud=await apiFetch(`/api/usgs/${lid}`);
        const ts=ud?.value?.timeSeries?.[0];
        if(ts){
          const vs=ts.values?.[0]?.value||[];
          const uS=vs.map(v=>parseFloat(v.value)).filter(v=>!isNaN(v)&&v>-99999);
          if(uS.length){
            usgs={latest:uS[uS.length-1],max:Math.max(...uS),stages:uS};
            doneStep(s4,'ok',`${uS.length} USGS readings`,()=>{
              const d=document.createElement('div');
              d.innerHTML=`<div class="mg">
                <div class="mc"><div class="mv">${usgs.latest.toFixed(2)}</div><div class="ml">USGS current (ft)</div></div>
                <div class="mc"><div class="mv">${usgs.max.toFixed(2)}</div><div class="ml">USGS 4-day peak (ft)</div></div>
              </div>`;
              if(uS.length>3){
                const cw=document.createElement('div');cw.className='cw';
                const cv=document.createElement('canvas');cv.id='spk2';cv.style.width='100%';cv.style.height='80px';
                cw.appendChild(cv);d.appendChild(cw);
                setTimeout(()=>spark('spk2',uS,{action:act,minor}),80);
              }
              return d;
            });
          } else doneStep(s4,'warn','No valid USGS readings','');
        } else doneStep(s4,'warn','No USGS match for this site ID','');
      }catch(e){doneStep(s4,'warn','USGS failed',String(e));}

      const s5=addStep(log,'spin','Step 5 · Cross-source reasoning');
      const nL=stages.length?stages[stages.length-1]:null;
      doneStep(s5,'think','Cross-source analysis',()=>{
        const d=document.createElement('div');
        const rb=document.createElement('div');rb.className='rb';
        rb.innerHTML=`<div class="rl">ARFA REASONING</div>${crossReason(nL,usgs?.latest)}`;
        d.appendChild(rb);return d;
      });

      const s6=addStep(log,'spin','Step 6 · Evidence summary');
      const gaps=[];
      if(act==null)gaps.push('Flood stage thresholds not defined for this gauge');
      if(!stages.length)gaps.push('No NOAA stage observations available');
      if(!usgs)gaps.push('USGS cross-verification unavailable');
      doneStep(s6,'ok','Analysis complete',()=>{
        const d=document.createElement('div');
        d.innerHTML=!gaps.length
          ?`<div style="color:var(--ok);font-size:12px">✓ No critical evidence gaps.</div>`
          :`<div style="font-size:11px;color:var(--mu);margin-bottom:6px">${gaps.length} gap(s):</div>`
            +gaps.map(x=>`<div class="eg"><span class="egi">⚠</span>${x}</div>`).join('');
        return d;
      });

      // Cache the rendered HTML
      gaugeCache[lid]=document.getElementById('alog').innerHTML;
    }

    function stageReason(latest,mx,mn,trend,act,minor,mod,maj){
      if(isNaN(latest))return'Current stage unavailable.';
      const lines=[`Current stage <strong>${latest.toFixed(2)} ft</strong>, trend: <strong>${trend}</strong>.`];
      if(act!=null&&latest>=act){
        if(maj!=null&&latest>=maj)lines.push(`Exceeds <strong>major flood threshold (${maj} ft)</strong>. Severe inundation likely.`);
        else if(mod!=null&&latest>=mod)lines.push(`At <strong>moderate flood level (${mod} ft)</strong>. Infrastructure impacts possible.`);
        else if(minor!=null&&latest>=minor)lines.push(`At <strong>minor flood level (${minor} ft)</strong>. Low-lying areas may be affected.`);
        else lines.push(`At <strong>action level (${act} ft)</strong>. Monitoring recommended.`);
      } else if(act!=null){
        const hr=(act-latest).toFixed(2);
        lines.push(`${hr} ft below action stage (${act} ft). No flood conditions currently.`);
        if(trend.includes('↑')&&parseFloat(hr)<2)lines.push(`Rising trend — action stage could be reached if conditions persist.`);
      } else lines.push('No flood thresholds defined — severity cannot be assessed.');
      if(mx-mn>1.5)lines.push(`4-day range of ${(mx-mn).toFixed(2)} ft shows significant vARFAbility.`);
      return lines.join(' ');
    }
    function crossReason(nv,uv){
      if(nv==null&&uv==null)return'Neither source has a reading. Critical evidence gap.';
      if(uv==null)return`NOAA reports <strong>${nv?.toFixed(2)} ft</strong>. No USGS confirmation available.`;
      if(nv==null)return`Only USGS available (${uv?.toFixed(2)} ft). NOAA observation missing.`;
      const diff=Math.abs(nv-uv);
      if(diff<0.3)return`NOAA (<strong>${nv.toFixed(2)} ft</strong>) and USGS (<strong>${uv.toFixed(2)} ft</strong>) agree within ${diff.toFixed(2)} ft. <strong>Strong agreement.</strong>`;
      if(diff<1.0)return`Differ by ${diff.toFixed(2)} ft — possible sensor lag or rating curve difference. Moderate confidence.`;
      return`⚡ <strong>${diff.toFixed(2)} ft discrepancy</strong> — NOAA (${nv.toFixed(2)}) vs USGS (${uv.toFixed(2)}). Analyst review required before acting on either reading.`;
    }

    function spark(id,data,thr={}){
      const cv=document.getElementById(id);if(!cv)return;
      const w=cv.parentElement.offsetWidth||320,h=80;cv.width=w;cv.height=h;
      const ctx=cv.getContext('2d');
      const mn=Math.min(...data)*.97,mx=Math.max(...data)*1.03||mn+1;
      const px=i=>i/(data.length-1||1)*w;
      const py=v=>h-(v-mn)/(mx-mn||1)*h*.82-h*.08;
      ctx.fillStyle='#1e293b';ctx.fillRect(0,0,w,h);
      const tc={action:'#facc15',minor:'#fb923c',moderate:'#f87171',major:'#dc2626'};
      for(const[k,v]of Object.entries(thr)){
        if(v==null||v<mn||v>mx)continue;
        ctx.beginPath();ctx.strokeStyle=tc[k];ctx.lineWidth=1;ctx.setLineDash([3,3]);
        ctx.moveTo(0,py(v));ctx.lineTo(w,py(v));ctx.stroke();ctx.setLineDash([]);
        ctx.fillStyle=tc[k];ctx.font='9px monospace';
        ctx.fillText(k.slice(0,3).toUpperCase()+' '+v,3,py(v)-3);
      }
      ctx.beginPath();ctx.moveTo(px(0),py(data[0]));
      data.forEach((v,i)=>ctx.lineTo(px(i),py(v)));
      ctx.lineTo(px(data.length-1),h);ctx.lineTo(0,h);ctx.closePath();
      const gr=ctx.createLinearGradient(0,0,0,h);
      gr.addColorStop(0,'rgba(96,165,250,.5)');gr.addColorStop(1,'rgba(96,165,250,0)');
      ctx.fillStyle=gr;ctx.fill();
      ctx.beginPath();ctx.moveTo(px(0),py(data[0]));
      data.forEach((v,i)=>ctx.lineTo(px(i),py(v)));
      ctx.strokeStyle='#60a5fa';ctx.lineWidth=1.5;ctx.setLineDash([]);ctx.stroke();
      ctx.beginPath();ctx.arc(px(data.length-1),py(data[data.length-1]),3,0,Math.PI*2);
      ctx.fillStyle='#93c5fd';ctx.fill();
    }

    let _currentGauge=null,_currentScale='linear',_currentThresholds=null,_currentHist=[],_currentHistTimes=[];

    async function renderFloodHubDetail(gauge){
      _currentGauge=gauge;
      _currentScale='linear';
      const log=document.getElementById('alog');
      log.innerHTML='<div class="empty"><p>Loading discharge…</p></div>';
      // To this:
    const [meta,sf,ud]=await Promise.all([
      apiFetch(`/api/gauge/${gauge.lid}`),
      apiFetch(`/api/gauge/${gauge.lid}/stageflow`),
    apiFetch(`/api/usgs/${gauge.lid}?period=P30D`),]);
      // Use USGS gage height (00065 = stage in ft). Fall back to NOAA observed primary (also stage).
      let vals=[], times=[];
      const series=ud?.value?.timeSeries||[];
      const qts=series.find(ts=>String(ts?.variable?.variableCode?.[0]?.value||'')==='00065');
      if(qts){
        const arr=qts.values?.[0]?.value||[];
        vals=arr.map(v=>parseFloat(v.value)).filter(v=>Number.isFinite(v)&&v>-900);
        times=arr.map(v=>v.dateTime||'');
      }
      if(!vals.length){
        // NOAA stageflow observed.data primary is also stage (ft)
        const arr=sf?.observed?.data||[];
        const pairs=arr.map(r=>[parseFloat(r.primary),r.time||'']).filter(x=>Number.isFinite(x[0]) && x[0] > -900);
        vals=pairs.map(x=>x[0]);times=pairs.map(x=>x[1]);
      }
      // const recent=compressDaily(vals,times,10);
      // const hist=recent.values, histTimes=recent.times;
    const initialCutoff=Date.now()-7*86400000;
      const initialPairs=vals.map((v,i)=>[v,times[i]])
        .filter(([,t])=>t&&new Date(t).getTime()>=initialCutoff);
      const initialSample=bucketAverage(
        initialPairs.map(x=>x[0]),
        initialPairs.map(x=>x[1]),
        15
      );
      const hist=initialSample.values, histTimes=initialSample.times;
      const dailyInitial=bucketAverage(hist,histTimes,1440);
      const forecastBasis=dailyInitial.values.slice(-4);
      const fc=linearForecast(forecastBasis,4);
      const current=hist.length?hist[hist.length-1]:null;
    const fl=meta?.flood||{};
    const cats=fl.categories||{};
    const histForFallback=vals.slice(-200); // raw vals before slicing
    const baseAvg=histForFallback.length
      ? histForFallback.reduce((a,b)=>a+b,0)/histForFallback.length : 1;

    // const usingEstimated = !cats.action?.stage && !cats.minor?.stage && !cats.major?.stage;
    const safeStage = v => (v && v > 0) ? v : null;
    const thresholds={
      warning: safeStage(cats.action?.stage) ?? Math.round(baseAvg*1.5*100)/100,
      danger:  safeStage(cats.minor?.stage)  ?? Math.round(baseAvg*3.0*100)/100,
      extreme: safeStage(cats.major?.stage)  ?? Math.round(baseAvg*4.0*100)/100,
    };
    const usingEstimated = !safeStage(cats.action?.stage);

    const sev=severityFromDischarge(current,thresholds);
      gauge._sev=sev;
      // recolor selected marker immediately
      const marker=mkrs[gauge.lid]||bgMkrs[gauge.lid];
      if(marker){const col=SEV_COLOR[sev]||SEV_COLOR.unknown; marker.setIcon(L.divIcon({className:'',html:`<div class="gm-dot" style="background:${col};border-color:white"></div>`,iconSize:[18,18],iconAnchor:[9,9]}));}
      const title=gauge.name||meta?.name||gauge.lid;
      document.getElementById('atitle').textContent=title;
      document.getElementById('ameta').textContent=`${gauge.lid} · ${Number(gauge.latitude).toFixed(4)}, ${Number(gauge.longitude).toFixed(4)}`;
      log.innerHTML=`
        <div class="forecast-wrap">
          <div class="forecast-label">Stage (ft)</div>
    <div class="period-btns">
      <button class="pbtn pactive" onclick="changePeriod('P7D',this)">7D</button>
      <button class="pbtn" onclick="changePeriod('P30D',this)">30D</button>
      <button class="pbtn" onclick="changePeriod('P365D',this)">1Y</button>
      <span class="scale-toggle">
        <button class="pbtn pactive" onclick="changeScale('linear',this)">Linear</button>
    <button class="pbtn" onclick="changeScale('log',this)">Log</button>
      </span>
    </div>
          <canvas id="forecastChart" class="forecast-canvas"></canvas>
        </div>
        <div class="threshold-grid">
          <div class="threshold-item"><span class="tdot" style="background:#f59e0b"></span>Warning<strong>${fmtQ(thresholds.warning)}</strong></div>
          <div class="threshold-item"><span class="tdot" style="background:#ff2d1a"></span>Danger<strong>${fmtQ(thresholds.danger)}</strong></div>
          <div class="threshold-item"><span class="tdot" style="background:#a80707"></span>Extreme<strong>${fmtQ(thresholds.extreme)}</strong></div>
        </div>
        <div class="threshold-note">${usingEstimated
      ? '⚠ No NWS thresholds defined for this gauge. Showing estimated levels: 1.5× / 3× / 4× of recent average stage. For reference only.'
      : 'NWS flood stage thresholds (Action / Minor / Major) from National Weather Service.'
    }</div>
        <div class="gauge-info"><h4>Gauge information</h4><div class="info-grid">
          <div><div class="info-label">River gauge ID</div><div class="info-value">${gauge.lid}</div></div>
          <div><div class="info-label">Source</div><div class="info-value">USGS / NOAA</div></div>
          <div><div class="info-label">Lat/Long</div><div class="info-value">${Number(gauge.latitude).toFixed(6)}, ${Number(gauge.longitude).toFixed(6)}</div></div>
          <div><div class="info-label">Current level</div><div class="info-value"><span class="tdot" style="background:${SEV_COLOR[sev]}"></span>${SEV_LABEL[sev]||sev}${current!=null?' · '+current.toFixed(1)+' ft':''}</div></div>
          <div><div class="info-label">Basin size</div><div class="info-value">${gauge._basinArea?Number(gauge._basinArea).toLocaleString()+' km²':'—'}</div></div>
          <div><div class="info-label">Forecast method</div><div class="info-value">4-day linear trend from the most recent 4 daily values</div></div>
        </div></div>
        <div id="agentAnalysis" class="agent-analysis"><div class="agent-kicker">ARFA agent interpretation</div><h4>Processing retrieved evidence…</h4><p>Combining current discharge, recent trend, forecast, and flood thresholds.</p></div>`;
      _currentThresholds=thresholds;
    _currentHist=hist; _currentHistTimes=histTimes;
    setTimeout(()=>drawForecastChart('forecastChart',hist,histTimes,fc,thresholds),30);
    renderAgentInterpretation(gauge,{hist,histTimes,fc,current,thresholds,sev,thresholdMeta:null,meta});
    }

    async function changePeriod(period,btn){
      document.querySelectorAll('.pbtn:not(.scale-toggle .pbtn)').forEach(b=>b.classList.remove('pactive'));
      btn.classList.add('pactive');
      if(!_currentGauge)return;

      const ud=await apiFetch(`/api/usgs/${_currentGauge.lid}/history?period=${period}`);
      let vals=[],times=[];
      const series=ud?.value?.timeSeries||[];
      const qts=series.find(ts=>String(ts?.variable?.variableCode?.[0]?.value||'')==='00060');

      if(qts){
        const a=qts.values?.[0]?.value||[];
        const pairs=a.map(v=>[parseFloat(v.value),v.dateTime||''])
          .filter(([v,t])=>Number.isFinite(v)&&v>-900&&t&&!Number.isNaN(new Date(t).getTime()));
        vals=pairs.map(x=>x[0]); times=pairs.map(x=>x[1]);
      }

      if(!vals.length && period==='P7D'){
        const sf=await apiFetch(`/api/gauge/${_currentGauge.lid}/stageflow`);
        const a=sf?.observed?.data||[];
        const pairs=a.map(r=>[parseFloat(r.primary),r.validTime||r.time||''])
          .filter(([v,t])=>Number.isFinite(v)&&v>-900&&t&&!Number.isNaN(new Date(t).getTime()));
        vals=pairs.map(x=>x[0]); times=pairs.map(x=>x[1]);
      }

      const days=period==='P7D'?7:period==='P30D'?30:365;
      const cutoff=Date.now()-days*86400000;
      const filtered=vals.map((v,i)=>[v,times[i]]).filter(([,t])=>new Date(t).getTime()>=cutoff);

      const bucketMinutes=period==='P7D'?15:period==='P30D'?60:720;
      const sampled=bucketAverage(filtered.map(x=>x[0]),filtered.map(x=>x[1]),bucketMinutes);

      const hist=sampled.values, histTimes=sampled.times;
      _currentHist=hist; _currentHistTimes=histTimes;

      const daily=bucketAverage(hist,histTimes,1440);
      const fc=linearForecast(daily.values.slice(-4),4);

      drawForecastChart('forecastChart',hist,histTimes,fc,_currentThresholds||{},_currentScale);
    }
    function changeScale(scale,btn){
      _currentScale=scale;
      document.querySelectorAll('.scale-toggle .pbtn').forEach(b=>b.classList.remove('pactive'));
      btn.classList.add('pactive');
      const fc=linearForecast(_currentHist.slice(-4),4);
      drawForecastChart('forecastChart',_currentHist,_currentHistTimes,fc,_currentThresholds||{},scale);
    }

    function bucketAverage(vals,times,bucketMinutes){
      if(!vals.length||!times.length)return{values:[],times:[]};
      const bucketMs=bucketMinutes*60*1000, buckets=new Map();

      vals.forEach((v,i)=>{
        const ms=new Date(times[i]).getTime();
        if(!Number.isFinite(v)||Number.isNaN(ms))return;
        const key=Math.floor(ms/bucketMs)*bucketMs;
        const b=buckets.get(key)||{sum:0,count:0};
        b.sum+=v; b.count++;
        buckets.set(key,b);
      });

      const keys=[...buckets.keys()].sort((a,b)=>a-b);
      return{
        values:keys.map(k=>buckets.get(k).sum/buckets.get(k).count),
        times:keys.map(k=>new Date(k).toISOString())
      };
    }

      function compressDaily(vals,times,n=4){
      if(!vals.length)return{values:[],times:[]};
      const by={}; vals.forEach((v,i)=>{const d=(times[i]||'').slice(0,10)||String(i);by[d]=[v,times[i]||d];});
      const a=Object.values(by).slice(-n);return{values:a.map(x=>x[0]),times:a.map(x=>x[1])};
    }
    function linearForecast(y,days=4){
      if(!y.length)return[]; if(y.length===1)return Array(days).fill(y[0]);
      const n=y.length, sx=(n-1)*n/2, sy=y.reduce((a,b)=>a+b,0), sxx=(n-1)*n*(2*n-1)/6, sxy=y.reduce((a,v,i)=>a+i*v,0);
      const den=n*sxx-sx*sx; const m=den?(n*sxy-sx*sy)/den:0, b=(sy-m*sx)/n;
      return Array.from({length:days},(_,j)=>Math.max(0,b+m*(n+j)));
    }
    function dischargeThresholds(current,hist,api){
      const w=Number(api?.warning), d=Number(api?.danger), e=Number(api?.extreme);
      if(Number.isFinite(w)&&Number.isFinite(d)&&Number.isFinite(e)&&w<d&&d<e)
        return {warning:w,danger:d,extreme:e};
      // Fallback only when long-term USGS history is unavailable. Keep it visibly conservative/demo-only.
      const base=Math.max(...hist, current||0, 1);
      return {warning:base*1.5,danger:base*2.0,extreme:base*2.6};
    }
    function severityFromDischarge(v,t){if(v==null)return'unknown';if(v>=t.extreme)return'major';if(v>=t.danger)return'moderate';if(v>=t.warning)return'minor';return'no_flooding';}
    function fmtQ(v){return Number.isFinite(v)?Math.round(v).toLocaleString():'—';}
    function _drawChart(cv,dpr,w,h,all,hist,times,fc,thr,scale='linear'){
      cv.width=w*dpr;cv.height=h*dpr;
      const c=cv.getContext('2d');c.scale(dpr,dpr);

      const maxV=Math.max(...all,thr.extreme||0,1)*1.08,minV=0;
      const L=46,R=18,T=16,B=34,pw=w-L-R,ph=h-T-B;

      const validTimes=times.map(t=>new Date(t).getTime()).filter(Number.isFinite);
      const histStart=validTimes.length?validTimes[0]:Date.now()-7*86400000;
      const histEnd=validTimes.length?validTimes[validTimes.length-1]:Date.now();
      const forecastEnd=histEnd+Math.max(1,fc.length)*86400000;
      const xTime=ms=>L+((ms-histStart)/Math.max(1,forecastEnd-histStart))*pw;

      const yLin=v=>T+(maxV-v)/(maxV-minV)*ph;
      const logMin=0.1,logMax=Math.max(maxV,1);
      const yLog=v=>T+(Math.log(logMax)-Math.log(Math.max(v,logMin)))/(Math.log(logMax)-Math.log(logMin))*ph;
      const y=scale==='log'?yLog:yLin;

      c.font='11px -apple-system,sans-serif';c.fillStyle='#6b7280';c.strokeStyle='#e5e7eb';c.lineWidth=1;
      for(let j=0;j<=4;j++){
        const v=maxV*j/4,yy=y(v);
        c.beginPath();c.moveTo(L,yy);c.lineTo(w-R,yy);c.stroke();c.fillText(Math.round(v),4,yy+4);
      }

      const lines=[['warning','#f59e0b'],['danger','#ff2d1a'],['extreme','#a80707']];
      lines.forEach(([k,col])=>{
        if(thr[k]==null)return;
        c.strokeStyle=col;c.lineWidth=2;c.setLineDash([]);
        c.beginPath();c.moveTo(L,y(thr[k]));c.lineTo(w-R,y(thr[k]));c.stroke();
      });

      if(hist.length&&validTimes.length){
        c.beginPath();
        hist.forEach((v,i)=>{
          const ms=new Date(times[i]).getTime(); if(Number.isNaN(ms))return;
          const xx=xTime(ms); i?c.lineTo(xx,y(v)):c.moveTo(xx,y(v));
        });
        c.lineTo(xTime(histEnd),h-B);c.lineTo(xTime(histStart),h-B);c.closePath();
        c.fillStyle='rgba(37,99,235,.18)';c.fill();

        c.beginPath();
        hist.forEach((v,i)=>{
          const ms=new Date(times[i]).getTime(); if(Number.isNaN(ms))return;
          const xx=xTime(ms); i?c.lineTo(xx,y(v)):c.moveTo(xx,y(v));
        });
        c.strokeStyle='#1769e0';c.lineWidth=3;c.setLineDash([]);c.stroke();
      }

      const nx=xTime(histEnd);
      c.strokeStyle='#5f6368';c.lineWidth=2;c.setLineDash([5,4]);
      c.beginPath();c.moveTo(nx,T);c.lineTo(nx,h-B);c.stroke();c.setLineDash([]);
      c.fillStyle='#202124';c.font='600 12px sans-serif';
      c.fillText('Now',Math.min(nx+7,w-R-28),T+13);

      if(fc.length&&hist.length){
        c.beginPath();c.moveTo(nx,y(hist[hist.length-1]));
        fc.forEach((v,j)=>c.lineTo(xTime(histEnd+(j+1)*86400000),y(v)));
        c.strokeStyle='#1769e0';c.lineWidth=3;c.setLineDash([3,6]);c.stroke();c.setLineDash([]);
      }

      const spanDays=(histEnd-histStart)/86400000;
      const tickMs=spanDays<=8?86400000:spanDays<=35?5*86400000:30*86400000;
      const fmtDate=ms=>{const d=new Date(ms);return `${d.getMonth()+1}/${d.getDate()}`;};

      c.fillStyle='#6b7280';c.font='10px sans-serif';
      c.fillText(fmtDate(histStart),L-10,h-10);

      let firstTick=Math.ceil(histStart/tickMs)*tickMs;
      for(let ms=firstTick;ms<histEnd;ms+=tickMs){
        const xx=xTime(ms);
        if(xx>L+25&&xx<nx-25)c.fillText(fmtDate(ms),xx-10,h-10);
      }

      c.fillText(fmtDate(histEnd),Math.max(L,nx-10),h-10);
      if(fc.length)c.fillText(fmtDate(forecastEnd),Math.min(w-R-22,xTime(forecastEnd)-10),h-10);

      const x=i=>{
        if(i<hist.length&&times[i])return xTime(new Date(times[i]).getTime());
        return xTime(histEnd+(i-hist.length+1)*86400000);
      };

      return {x,y,L,R,T,B,pw,ph,maxV,minV,nx,all};
    }
    function drawForecastChart(id,hist,times,fc,thr,scale='linear'){
      const cv=document.getElementById(id);if(!cv)return;
      const dpr=window.devicePixelRatio||1,w=cv.clientWidth||400,h=cv.clientHeight||250;
      const state=_drawChart(cv,dpr,w,h,hist.concat(fc),hist,times,fc,thr,scale);
      const {x,y,L,R,T,B,pw,all}=state;

      cv.onmousemove=function(e){
        const rect=cv.getBoundingClientRect();
        const mx=(e.clientX-rect.left)*(cv.width/rect.width)/dpr;
        const idx=Math.min(Math.max(0,Math.round((mx-L)/pw*(all.length-1))),all.length-1);
        const val=all[idx];
        const isHist=idx<hist.length;
        const dateStr=isHist&&times[idx]?new Date(times[idx]).toLocaleString():`Forecast +${idx-hist.length+1}d`;
        _drawChart(cv,dpr,w,h,all,hist,times,fc,thr,scale);
        const c2=cv.getContext('2d');c2.save();
        c2.strokeStyle='rgba(0,0,0,0.25)';c2.lineWidth=1;c2.setLineDash([3,3]);
        c2.beginPath();c2.moveTo(x(idx),T);c2.lineTo(x(idx),h-B);c2.stroke();c2.setLineDash([]);
        c2.fillStyle='rgba(15,23,42,0.88)';
        const tw=190,th2=38,tx2=Math.min(x(idx)+10,w-tw-R),ty2=T+4;
        c2.beginPath();c2.rect(tx2,ty2,tw,th2);c2.fill();
        c2.fillStyle='#cbd5e1';c2.font='10px sans-serif';c2.fillText(dateStr,tx2+8,ty2+14);
        c2.fillStyle='#93c5fd';c2.font='bold 13px sans-serif';c2.fillText(`${val.toFixed(2)} ft`,tx2+8,ty2+30);
        c2.restore();
      };
      cv.onmouseleave=()=>_drawChart(cv,dpr,w,h,all,hist,times,fc,thr,scale);
    }
    function renderAgentInterpretation(gauge,d){
      const el=document.getElementById('agentAnalysis');if(!el)return;
      const {hist,fc,current,thresholds,sev,thresholdMeta}=d;
      if(current==null){el.innerHTML='<div class="agent-kicker">ARFA agent interpretation</div><h4>Insufficient evidence</h4><p>No valid discharge observations were retrieved for this gauge, so ARFA cannot characterize current or forecast flood conditions.</p>';return;}
      const hmin=Math.min(...hist),hmax=Math.max(...hist);
      const first4=hist.slice(-4); 
      const fmax=fc.length?Math.max(...fc):current;
      const last2=hist.slice(-8);
    const prev2=hist.slice(-40,-8);
    const recentAvg=last2.reduce((a,b)=>a+b,0)/Math.max(last2.length,1);
    const priorAvg=prev2.length?prev2.reduce((a,b)=>a+b,0)/prev2.length:recentAvg;
    const spike=recentAvg>priorAvg*1.5&&priorAvg>0;
    const delta=hist.length>1?current-hist[hist.length-2]:0;
    const trend=spike
      ?`rapidly rising (avg ${priorAvg.toFixed(1)} → ${recentAvg.toFixed(1)} ft)`
      :Math.abs(delta)<Math.max(0.05,current*.02)?'roughly stable'
      :delta>0?'rising':'falling';
    const spikeNote=spike?` Readings show a sudden spike from a prior baseline of ${priorAvg.toFixed(1)} ft — conditions changed rapidly in the last few hours.`:'';
      
      
      
      let currentText='below the warning threshold';
      if(current>=thresholds.extreme)currentText='above the extreme threshold';
      else if(current>=thresholds.danger)currentText='above the danger threshold';
      else if(current>=thresholds.warning)currentText='above the warning threshold';
      let forecastText='The simple four-day trend forecast remains below the warning threshold.';
      if(fmax>=thresholds.extreme)forecastText='The simple four-day trend forecast reaches the extreme threshold.';
      else if(fmax>=thresholds.danger)forecastText='The simple four-day trend forecast reaches the danger threshold.';
      else if(fmax>=thresholds.warning)forecastText='The simple four-day trend forecast reaches the warning threshold.';
      const yrs=Number(thresholdMeta?.years)||0;
      const confidence=yrs>=20?'higher':yrs>=8?'moderate':'limited';
      el.innerHTML=`<div class="agent-kicker">ARFA agent interpretation</div><h4>${SEV_LABEL[sev]||'Current'} conditions</h4>
        <p>Current discharge is <strong>${current.toFixed(1)} ft</strong>, ${currentText}. Over the displayed 10-day history, discharge ranged from <strong>${hmin.toFixed(1)}</strong> to <strong>${hmax.toFixed(1)} ft</strong>; the recent trend is <strong>${trend}</strong>.${spikeNote} ${forecastText}</p>
        <div class="agent-evidence">Evidence used: USGS/NOAA gauge observations · 10-day discharge history · 4-day linear forecast · gauge-specific estimated Q2/Q5/Q20 thresholds${yrs?` from ${yrs} annual maxima`:''}. Threshold confidence: ${confidence}. This interpretation is descriptive, not an evacuation recommendation.</div>`;
    }

    // ── ISOLATION ANALYSIS ────────────────────────────────────────────────────────

    function toggleIsolation(){
      if(!isoActive){
        runIsolationAnalysis();
      } else {
        clearIsolation();
      }
    }

    // ── Road status: TomTom Traffic tile layer ────────────────────────────────
    // Displayed as a tile overlay so it works at any zoom without fetching GeoJSON.
    // Green = no reported TomTom incident; orange = delays; red = closures.
    // This does NOT mean roads are safe/passable — it only reflects TomTom incident data.
    let _roadStatusTile=null;
    const TOMTOM_KEY=window.ARFA_TOMTOM_KEY||'';  // set via <script>window.ARFA_TOMTOM_KEY='...'</script> in index.html if available

    async function runIsolationAnalysis(){
      if(isoActive){ clearIsolation(); return; }
      const isoB=document.getElementById('isoBtn');
      if(isoB) isoB.classList.add('active');
      const tbRoadB=document.getElementById('tbRoadBtn');
      if(tbRoadB) tbRoadB.classList.add('active');
      isoActive=true;

      if(TOMTOM_KEY){
        // TomTom Traffic Flow tiles — instant, no GeoJSON fetch
        _roadStatusTile=L.tileLayer(
          `https://api.tomtom.com/traffic/map/4/tile/flow/relative0/{z}/{x}/{y}.png?key=${TOMTOM_KEY}&tileSize=256`,
          {attribution:'© TomTom',opacity:0.7,maxZoom:19,pane:'overlayPane'}
        ).addTo(map);
        setStatus('Road status: TomTom traffic overlay active',false);
        btn.textContent='🛣 Hide road status';
      } else {
        // Fallback: TomTom public flow tiles (no key, lower resolution)
        _roadStatusTile=L.tileLayer(
          'https://api.tomtom.com/traffic/map/4/tile/flow/relative0/{z}/{x}/{y}.png?tileSize=256',
          {attribution:'© TomTom',opacity:0.65,maxZoom:18,pane:'overlayPane'}
        ).addTo(map);
        setStatus('Road status: TomTom traffic tile (no key — limited)',false);
        btn.textContent='🛣 Hide road status';
      }
    }

    function clearIsolation(){
      if(_roadStatusTile){map.removeLayer(_roadStatusTile);_roadStatusTile=null;}
      if(roadLayer){roadLayer.remove();roadLayer=null;}
      if(facilityLayer){facilityLayer.remove();facilityLayer=null;}
      isoActive=false;isoData=null;
      const isoB=document.getElementById('isoBtn');
      if(isoB){isoB.textContent='🛣 Get road status';isoB.classList.remove('active');}
      const tbRoadB=document.getElementById('tbRoadBtn');
      if(tbRoadB) tbRoadB.classList.remove('active');
      document.getElementById('isoPanel')?.remove();
    }
    let structuresLayer=null, structuresVisible=false, structuresLoading=false;
    let structuresAbortController=null;
    let structuresData=null;

    // ── Interactive evacuation assistant + routing ───────────────────────────
    let shelterLayer=null;
    let shelterRenderer=null;
    let shelterCandidates=[];
    let selectedShelter=null;
    let originLatLng=null;
    let originMarker=null;
    let destinationMarker=null;
    let originSelectionActive=false;
    let routeLayers=[];
    let routeData=[];
    let activeRouteIndex=0;
    const ROUTE_COLORS=['#2563eb','#f97316','#7c3aed'];
    // Flood-aware routing
    const FLOOD_AWARE_COLORS=['#16a34a','#15803d'];
    let floodAwareRouteLayers=[];
    let floodAwareRouteData=[];
    let floodCrossingLayer=null;   // markers where active route enters/exits flood zone
    let floodSegmentLayer=null;    // red overlay of flooded road segments on active route

    // The structures dataset is intentionally treated as a facility inventory,
    // not as a pre-labelled evacuation-shelter source. The responder decides which
    // facility classes are relevant before ARFA flags them as *probable* shelters.
    const FACILITY_TYPES={
      hospitals:{label:'Hospitals / healthcare',keywords:['hospital','medical','health','clinic','healthcare','urgent care'],occupancies:['Healthcare','Medical']},
      schools:{label:'Schools / education',keywords:['school','university','college','education','academy','campus'],occupancies:['Education']},
      community:{label:'Community / civic centers',keywords:['community center','community centre','civic','public building','library','community'],occupancies:[]},
      government:{label:'Government / public facilities',keywords:['government','municipal','town hall','city hall','county office','courthouse','public safety'],occupancies:['Government']},
      religious:{label:'Religious facilities',keywords:['church','religious','worship','mosque','synagogue','temple'],occupancies:[]},
      recreation:{label:'Recreation / assembly',keywords:['recreation','assembly','arena','gym','entertainment','auditorium','event center','event centre'],occupancies:['Assembly']}
    };
    let selectedFacilityTypes=new Set();
    let agentState='idle';
    let agentAreaLabel='';

    function featureMatchesFacilityType(feature,typeKey){
      const cfg=FACILITY_TYPES[typeKey];
      if(!cfg) return false;
      const p=feature?.properties||{};
      const occ=String(p['Occupancy Class']||'').trim().toLowerCase();
      const prim=String(p['Primary Use']||'').trim().toLowerCase();
      return cfg.occupancies.some(v=>occ===String(v).toLowerCase()) || cfg.keywords.some(k=>prim.includes(k));
    }

    function isShelterCandidate(feature){
      if(!selectedFacilityTypes.size) return false;
      return [...selectedFacilityTypes].some(typeKey=>featureMatchesFacilityType(feature,typeKey));
    }

    function featureCenter(feature){
      const coords=feature?.geometry?.coordinates;
      if(!coords) return null;
      let minLat=Infinity,maxLat=-Infinity,minLon=Infinity,maxLon=-Infinity,count=0;
      function walk(node){
        if(!Array.isArray(node)) return;
        if(node.length>=2 && typeof node[0]==='number' && typeof node[1]==='number'){
          const lon=node[0],lat=node[1];
          if(Number.isFinite(lat)&&Number.isFinite(lon)){
            minLat=Math.min(minLat,lat);maxLat=Math.max(maxLat,lat);
            minLon=Math.min(minLon,lon);maxLon=Math.max(maxLon,lon);count++;
          }
          return;
        }
        node.forEach(walk);
      }
      walk(coords);
      return count ? L.latLng((minLat+maxLat)/2,(minLon+maxLon)/2) : null;
    }

    function shelterName(feature){
      const p=feature?.properties||{};
      return p['Primary Use'] || p['Occupancy Class'] || 'Probable shelter';
    }

    function ensureShelterLayer(){
      if(shelterLayer) return shelterLayer;
      shelterRenderer=L.canvas({padding:0.5,pane:'sheltersPane'});
      shelterLayer=L.geoJSON(null,{
        pane:'sheltersPane',
        pointToLayer:(feature,latlng)=>L.circleMarker(latlng,{
          renderer:shelterRenderer,pane:'sheltersPane',radius:5,weight:1.5,
          color:'#ffffff',fillColor:'#15803d',fillOpacity:0.92
        }),
        onEachFeature:(feature,layer)=>{
          layer.bindTooltip(`Probable shelter · ${escapeHtml(shelterName(feature))}`,{direction:'top',sticky:true,opacity:0.95});
          layer.on('click',e=>{
            if(e.originalEvent) L.DomEvent.stopPropagation(e.originalEvent);
            const center=featureCenter(feature)||e.latlng;
            showStructurePopup(feature,center);
          });
        }
      }).addTo(map);
      return shelterLayer;
    }

    function rebuildShelterLayer(){
      if(shelterLayer){shelterLayer.remove();shelterLayer=null;}
      shelterRenderer=null;
      shelterCandidates=[];
      if(!structuresData?.features?.length || !selectedFacilityTypes.size){
        updateRoutingUI();
        return 0;
      }
      const points=[];
      for(const feature of structuresData.features){
        if(!isShelterCandidate(feature)) continue;
        const center=featureCenter(feature);
        if(!center) continue;
        shelterCandidates.push({feature,center});
        points.push({type:'Feature',properties:feature.properties||{},geometry:{type:'Point',coordinates:[center.lng,center.lat]}});
      }
      if(points.length) ensureShelterLayer().addData({type:'FeatureCollection',features:points});
      document.getElementById('agentRouteTools')?.classList.toggle('hidden',!points.length);
      updateRoutingUI();
      return points.length;
    }

    // Kept for compatibility with the streaming structure loader. Shelters are no
    // longer flagged automatically while structures arrive; the responder first
    // chooses the facility types that should count as probable shelters.
    function addShelterFeatures(_features){ return; }

    function showPanelView(view){
      // Agent and River Gauges are now permanently visible in separate side panels.
      document.getElementById('gaugesView')?.classList.add('active');
      document.getElementById('agentView')?.classList.add('active');
      if(view==='agent') setTimeout(()=>document.getElementById('agentInput')?.focus(),0);
    }

    function addAgentMessage(role, html, key=null){
      const conv=document.getElementById('agentConversation');
      if(!conv) return;
      if(key){const old=conv.querySelector(`[data-agent-key="${key}"]`);if(old) old.remove();}
      const item=document.createElement('div');
      item.className=`agent-message ${role==='responder'?'responder':'arfa'}`;
      if(key) item.dataset.agentKey=key;
      item.innerHTML=role==='responder'
        ? `<div class="agent-bubble"><div class="agent-name">Emergency Responder</div>${html}</div><div class="agent-avatar responder-avatar">ER</div>`
        : `<div class="agent-avatar">A</div><div class="agent-bubble"><div class="agent-name">ARFA</div>${html}</div>`;
      conv.appendChild(item);conv.scrollTop=conv.scrollHeight;
    }

    function setAgentSuggestions(items=[]){
      const el=document.getElementById('agentSuggestions');if(!el)return;
      el.innerHTML=items.map(x=>`<button class="agent-suggestion" type="button" data-agent-text="${escapeHtml(x.value||x.label)}">${escapeHtml(x.label)}</button>`).join('');
      el.querySelectorAll('.agent-suggestion').forEach(btn=>btn.addEventListener('click',()=>{
        const input=document.getElementById('agentInput');if(input)input.value=btn.dataset.agentText||btn.textContent;agentSubmit();
      }));
    }

    function resetAgentConversation(){
      const conv=document.getElementById('agentConversation');if(!conv)return;
      conv.innerHTML=`<div class="agent-message arfa"><div class="agent-avatar">A</div><div class="agent-bubble"><div class="agent-name">ARFA</div><div id="routingHint">Ask me about flood conditions in a location, for example: <b>What is the current flooding status of Oak Ridge, TN?</b></div></div></div>`;
      setAgentSuggestions([]);
    }

    function resetAgentSession(){
      clearRouting(true);
      selectedFacilityTypes.clear();
      agentState='idle';agentAreaLabel='';agentHistory=[];
      if(shelterLayer){shelterLayer.remove();shelterLayer=null;}shelterRenderer=null;shelterCandidates=[];
      document.getElementById('agentRouteTools')?.classList.add('hidden');
      
      resetAgentConversation();updateRoutingUI();
    }

    function showRoutingPanel(){updateRoutingUI();}

    function gaugeSummaryHtml(){
      if(!gauges.length) return '<p>No active river gauges were found for the resolved county.</p>';
      const rows=gauges.slice(0,12).map(g=>{
        const sev=effectiveGaugeStatus(g);
        const stage=g._stage!=null?`${Number(g._stage).toFixed(2)} ft`:'stage unavailable';
        const basis=g._statusSource||((g._sev||'unknown')!=='unknown'?'NOAA/NWPS':'Unclassified by NOAA/NWPS');
        return `<div class="agent-gauge-row"><span class="agent-gauge-dot" style="background:${SEV_COLOR[sev]||'#94a3b8'}"></span><div class="agent-gauge-copy"><strong>${escapeHtml(g.name||g.lid)}</strong><span>${escapeHtml(SEV_LABEL[sev]||'Unknown')} · ${escapeHtml(stage)} · ${escapeHtml(basis)} · ${escapeHtml(g.lid)}</span></div></div>`;
      }).join('');
      const extra=gauges.length>12?`<div class="agent-facility-note">${gauges.length-12} additional gauges are available in the River gauges tab.</div>`:'';
      return `<div class="agent-gauge-summary">${rows}</div>${extra}`;
    }

    function agentFloodSummary(){
      const counts={major:0,moderate:0,minor:0,action:0,no_flooding:0,unknown:0};
      gauges.forEach(g=>{const s=effectiveGaugeStatus(g);counts[s]=(counts[s]||0)+1;});
      const assessed=counts.major+counts.moderate+counts.minor+counts.action+counts.no_flooding;
      const concerning=counts.major+counts.moderate+counts.minor+counts.action;
      let headline='No gauges were retrieved for this area.';
      if(gauges.length){
        headline=`${gauges.length} gauges retrieved, ${assessed} classified from NOAA/NWPS evidence${counts.unknown?`, ${counts.unknown} unclassified`:''}.`;
          if(concerning) headline+=` ${concerning} gauge${concerning===1?' is':'s are'} currently elevated.`;
          else if(assessed>0) headline+=' No classified gauge is currently above action stage.';
      }
        return `<b>${escapeHtml(agentAreaLabel||'The resolved area')}</b>: ${headline}${gaugeSummaryHtml()}<div class="agent-facility-note">Open <b>River gauges</b> for detailed history and thresholds.</div>`;
    }

    let agentHistory=[];

    function rememberAgent(role,text){
      agentHistory.push({role,content:String(text||'').replace(/<[^>]*>/g,' ').replace(/\s+/g,' ').trim().slice(0,1200)});
      if(agentHistory.length>8)agentHistory=agentHistory.slice(-8);
    }

    function compactGaugeObservation(){
      const statusCounts={major:0,moderate:0,minor:0,action:0,no_flooding:0,unknown:0};
      gauges.forEach(g=>{const s=effectiveGaugeStatus(g);statusCounts[s]=(statusCounts[s]||0)+1;});
      return {
        event:'gauges_assessed',area:agentAreaLabel,county_count:resolvedCounties.length,gauge_count:gauges.length,
        status_counts:statusCounts,
        interpretation_rule:'Use only NOAA/NWPS source-grounded flood classifications. If status_source says threshold-derived, state that the observed stage was compared with official NOAA/NWPS thresholds; do not call it an official current category. If unclassified, report the raw stage/discharge and say that no NOAA/NWPS classification was available. Do not invent another gauge severity scheme.',
        gauges:gauges.slice(0,30).map(g=>({
          id:g.lid,name:g.name,flood_classification:(g._sev||'unknown')!=='unknown'?g._sev:null,
          status_source:g._statusSource||'Unclassified by NOAA/NWPS',
          stage_ft:g._stage??null,discharge_cfs:g._discharge_cfs??null,discharge_m3s:g._discharge_m3s??null,
          latitude:g.latitude,longitude:g.longitude
        })),
        note:gauges.length>30?`${gauges.length-30} additional gauges omitted from LLM context but remain available in the UI.`:null
      };
    }

    async function reasonOverObservation(message,observation,key='agent_reasoning',includeHistory=true){
      addAgentMessage('arfa','<span class="agent-thinking">Reasoning over the retrieved evidence…</span>',key);
      const data=await apiPost('/api/agent/reason',{message,observation,history:includeHistory?agentHistory:[]});
      if(data?.response){
        addAgentMessage('arfa',escapeHtml(data.response).replace(/\n/g,'<br>'),key);
        rememberAgent('assistant',data.response);
        return data.response;
      }
      addAgentMessage('arfa','The evidence is already available on the map and analytical tabs, but the reasoning model did not return an interpretation.',key);
      return null;
    }

    function agentContext(){
      return {state:agentState,area:agentAreaLabel,gauge_count:gauges.length,structures_loaded:!!structuresData?.features?.length,
        structure_count:structuresData?.features?.length||0,facility_types:[...selectedFacilityTypes],candidate_shelters:shelterCandidates.length,
        has_origin:!!originLatLng,has_destination:!!selectedShelter,route_count:routeData.length};
    }

    async function agentAnalyzeLocation(text){
      const send=document.getElementById('agentSendBtn');if(send)send.disabled=true;
      addAgentMessage('arfa','I am resolving the location and retrieving county, tract, and river-gauge evidence…','agent_progress');
      setStatus('ARFA resolving location…',true);
      try{
        clearAll(true);back();
        const data=await apiFetch(`/api/resolve?q=${encodeURIComponent(text)}`);
        if(!data||data.error||!(data.counties||[]).length) throw new Error(data?.error||'I could not identify a U.S. location in that request.');
        resolvedCounties=data.counties||[];renderCountyBar(resolvedCounties);
        const detected=(data.detected||[])[0]||{};
        agentAreaLabel=[detected.name,detected.state].filter(Boolean).join(', ') || resolvedCounties[0]?.name || 'resolved area';
        if(resolvedCounties.length===1) await loadCounty(resolvedCounties[0]); else await loadAll(true);
        // Visualization is complete at this point. Reasoning happens independently afterward.
        addAgentMessage('arfa',agentFloodSummary(),'agent_progress');
        rememberAgent('user',text);
        agentState='awaiting_shelter_offer';
        
        reasonOverObservation(text,compactGaugeObservation(),'agent_reasoning').then(()=>{
          addAgentMessage('arfa','Would you like me to identify <b>probable shelter facilities</b> in this area, or would you like to investigate something else?','shelter_offer');
          setAgentSuggestions([{label:'Yes — identify facilities',value:'Yes, identify probable shelters'},{label:'No — gauges only',value:'No, gauges only'}]);
        });
      }catch(err){
        addAgentMessage('arfa',escapeHtml(err.message||'Unable to analyze that location.'),'agent_progress');
        setStatus('Ready',false);
      }finally{if(send)send.disabled=false;}
    }

    async function agentPrepareFacilities(){
      agentState='loading_structures';setAgentSuggestions([]);
      addAgentMessage('arfa','I will retrieve the USA Structures inside the current area of interest first. I will not flag anything as a shelter until you choose the facility types.','facility_progress');
      if(!structuresData?.features?.length) await getStructures({preserveAgent:true});
      const n=structuresData?.features?.length||0;
      addAgentMessage('arfa',`I retrieved <b>${n.toLocaleString()} structures</b>. Which facility types should I flag as probable shelters? Choose one or describe your own preference, such as <i>only hospitals</i>.`,'facility_progress');
      agentState='awaiting_facility_types';
      setAgentSuggestions([
        {label:'Hospitals / healthcare',value:'Only hospitals and healthcare facilities'},
        {label:'Schools / education',value:'Only schools and education facilities'},
        {label:'Community / civic',value:'Community and civic centers'},
        {label:'Government / public',value:'Government and public facilities'},
        {label:'Religious facilities',value:'Religious facilities'},
        {label:'Recreation / assembly',value:'Recreation and assembly facilities'}
      ]);
    }

    function parseFacilityTypes(text){
      const t=text.toLowerCase();const out=new Set();
      const mapTerms={
        hospitals:['hospital','health','medical','clinic'],schools:['school','education','university','college'],community:['community','civic','library'],government:['government','public','municipal','city hall','town hall','courthouse'],religious:['religious','church','mosque','synagogue','temple','worship'],recreation:['recreation','assembly','entertainment','arena','gym','event center']
      };
      for(const [k,terms] of Object.entries(mapTerms)) if(terms.some(term=>t.includes(term))) out.add(k);
      if(t.includes('all')) Object.keys(FACILITY_TYPES).forEach(k=>out.add(k));
      return out;
    }

    function applyFacilitySelection(types){
      selectedFacilityTypes=new Set(types);
      const count=rebuildShelterLayer();
      const labels=[...selectedFacilityTypes].map(k=>FACILITY_TYPES[k]?.label).filter(Boolean);
      addAgentMessage('arfa',count
        ? `I flagged <b>${count.toLocaleString()} probable shelter location${count===1?'':'s'}</b> matching <b>${escapeHtml(labels.join(', '))}</b>. They are shown as green points on the map. Select one, then set the responder origin to generate route alternatives.`
        : `I did not find structures matching <b>${escapeHtml(labels.join(', '))}</b> in the retrieved area. Try a different facility type or zoom to a larger area.`,'facility_selection');
      agentState=count?'routing_ready':'awaiting_facility_types';
      setAgentSuggestions(count?[{label:'Set route origin',value:'Set my route origin'},{label:'Use different facilities',value:'Change facility types'}]:[
        {label:'Hospitals / healthcare',value:'Only hospitals and healthcare facilities'},{label:'Schools / education',value:'Only schools and education facilities'},{label:'Government / public',value:'Government and public facilities'}
      ]);
      showRoutingPanel();
      const obs={event:'facilities_filtered',area:agentAreaLabel,requested_types:[...selectedFacilityTypes],candidate_count:count,structure_count:structuresData?.features?.length||0};
      reasonOverObservation('CURRENT STAGE: facility filtering. Report only the newly selected facility types and candidate count, then tell the responder to select a destination and set an origin. Do not repeat gauge analysis and do not infer facility safety, exposure, structural integrity, or evacuation priority.',obs,'facility_reasoning',false);
    }

    async function agentSubmit(){
      const input=document.getElementById('agentInput');
      const text=input?.value.trim();
      if(!text)return;
      input.value='';showPanelView('agent');addAgentMessage('responder',escapeHtml(text));setAgentSuggestions([]);
      const low=text.toLowerCase();

      // Semantic interpretation lives in the backend. The frontend only renders
      // and invokes deterministic operations selected by the constrained agents.
      const semantic=await apiPost('/api/agent/structure-query',{message:text,context:agentContext()});

      // If this is a structure query but no county is loaded yet, resolve location first
      if(semantic?.is_structure_query && !currentCountyBbox){
        await agentAnalyzeLocation(text);
        // After resolving, if we have filters or facility types, run the structure query too
        if(currentCountyBbox){
          const types=new Set((semantic.facility_types||[]).filter(k=>FACILITY_TYPES[k]));
          const filters=semantic.filters||{};
          if(Object.keys(filters).length){
            await queryStructures(filters,text,semantic.hazard_relation||'any');
          } else if(types.size){
            if(!structuresData?.features?.length) await getStructures({preserveAgent:true});
            applyFacilitySelection(types);
          }
        }
        return;
      }

      if(semantic?.is_structure_query && currentCountyBbox){
        const types=new Set((semantic.facility_types||[]).filter(k=>FACILITY_TYPES[k]));
        const filters=semantic.filters||{};

        // Facility/shelter requests use the already proven guided candidate UX.
        if(types.size && (agentState==='awaiting_facility_types' || /facilit|shelter|only|candidate|destination/i.test(text))){
          if(!structuresData?.features?.length) await getStructures({preserveAgent:true});
          applyFacilitySelection(types);
          return;
        }

        // General semantic building queries with explicit attribute filters
        if(Object.keys(filters).length){
          await queryStructures(filters,text,semantic.hazard_relation||'any');
          return;
        }

        // Has facility types but no explicit filters — build filters from types and load
        if(types.size){
          if(!structuresData?.features?.length) await getStructures({preserveAgent:true});
          applyFacilitySelection(types);
          return;
        }
      }

      // Preserve the guided response flow, but let the constrained controller
      // choose only the NEXT action rather than construct an arbitrary tool plan.
      const packet=await apiPost('/api/agent/dispatch',{message:text,context:agentContext()});
      const decision=packet?.decision||null;
      if(!decision){await agentAnalyzeLocation(text);return;}
      const action=decision.action;
      const actionLabels={analyze_location:'Resolve location → retrieve flood evidence',offer_facilities:'Retrieve USA Structures → ask for facility scope',load_facilities:'Retrieve USA Structures',filter_facilities:'Interpret facility criteria → filter structures',query_structures:'Interpret attributes → query USA Structures',set_origin:'Collect responder origin',generate_routes:'Generate route alternatives → assess HAND + live roads',compare_routes:'Compare retrieved route evidence',answer_from_context:'Answer from current evidence',repair_missing_structures:'Download missing USA Structures data → rebuild index'};
      addAgentMessage('arfa',`<div class="agent-plan-line"><b>Plan</b> · ${escapeHtml(actionLabels[action]||action)}</div>`,'agent_plan');

      if(action==='analyze_location'){await agentAnalyzeLocation(text);return;}
      if(action==='offer_facilities'||action==='load_facilities'){
        await agentPrepareFacilities();return;
      }
      if(action==='repair_missing_structures'){
        const states=(decision.reply||'').match(/\b[A-Z]{2}\b/g)||[];
        if(states.length) await _executeRepair(states);
        return;
      }
      if(action==='filter_facilities'){
        const types=new Set(((packet.structure?.facility_types)||(decision.facility_types)||[]).filter(k=>FACILITY_TYPES[k]));
        if(types.size){
          if(!structuresData?.features?.length) await getStructures({preserveAgent:true});
          applyFacilitySelection(types);return;
        }
        await agentPrepareFacilities();return;
      }
      if(action==='query_structures'){
        const sq=packet.structure||semantic;
        const sqFilters=sq?.filters||{};
        const sqTypes=new Set((sq?.facility_types||[]).filter(k=>FACILITY_TYPES[k]));
        if(Object.keys(sqFilters).length){
          await queryStructures(sqFilters,text,sq.hazard_relation||'any');return;
        }
        if(sqTypes.size){
          if(!structuresData?.features?.length) await getStructures({preserveAgent:true});
          applyFacilitySelection(sqTypes);return;
        }
        await agentPrepareFacilities();return;
      }
      if(action==='set_origin'){beginOriginSelection();return;}
      if(action==='generate_routes'){
        if(originLatLng&&selectedShelter){findRoutes();return;}
        if(!selectedShelter){addAgentMessage('arfa','Select a destination facility on the map first.','route_missing_destination');return;}
        beginOriginSelection();return;
      }
      if(action==='compare_routes'&&routeData.length){
        const obs={event:'route_comparison_request',area:agentAreaLabel,routes:routeData.map((r,i)=>({
          route:r.label||`Route ${i+1}`,duration_min:+((r.duration_s||0)/60).toFixed(1),distance_km:+((r.distance_m||0)/1000).toFixed(2),
          flood_status:r.flood_exposure?.flood_status||null,flood_rank:r.flood_exposure?.rank||null,
          road_incidents:r._roadConditions?.incident_count||0,has_closures:!!r._roadConditions?.has_closures
        }))};
        await reasonOverObservation('Compare the existing candidate routes using only the supplied HAND flood screening, travel time, and live-road evidence.',obs,'route_reasoning_user',false);
        return;
      }
      if(action==='answer_from_context'&&decision.reply){
        addAgentMessage('arfa',escapeHtml(decision.reply));rememberAgent('assistant',decision.reply);return;
      }
      await agentAnalyzeLocation(text);
    }

    function beginOriginSelection(){
      showPanelView('agent');
      originSelectionActive=!originSelectionActive;
      const btn=document.getElementById('setOriginBtn');
      btn?.classList.toggle('selecting',originSelectionActive);
      if(btn) btn.textContent=originSelectionActive?'Click the map…':'📍 Set origin';
      if(originSelectionActive){
        addAgentMessage('arfa',"Click the map at the responder's current location. I will use that point as the route origin.",'origin_prompt');
      }
    }

    function handleRoutingMapClick(e){
      if(!originSelectionActive) return;
      setOrigin(e.latlng);
      originSelectionActive=false;
      const btn=document.getElementById('setOriginBtn');
      btn?.classList.remove('selecting');
      if(btn) btn.textContent='📍 Change origin';
      map.closePopup();
    }
    map.on('click',handleRoutingMapClick);

    function setOrigin(latlng){
      originLatLng=L.latLng(latlng.lat,latlng.lng);
      if(originMarker) originMarker.remove();
      const icon=L.divIcon({className:'',html:'<div style="width:25px;height:25px;border-radius:50%;background:#be123c;border:3px solid white;box-shadow:0 2px 7px rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;color:white;font-size:12px;font-weight:800">A</div>',iconSize:[25,25],iconAnchor:[12.5,12.5]});
      originMarker=L.marker(originLatLng,{icon,pane:'sheltersPane',zIndexOffset:1800,draggable:true}).addTo(map);
      originMarker.bindTooltip('Route origin',{direction:'top'});
      originMarker.on('dragend',()=>{
        originLatLng=originMarker.getLatLng();
        clearRouteLines();
        updateRoutingUI();
      });
      clearRouteLines();
      updateRoutingUI();
      showPanelView('agent');
      addAgentMessage('responder',`Use <b>${originLatLng.lat.toFixed(5)}, ${originLatLng.lng.toFixed(5)}</b> as my origin.`,'origin_choice');
      addAgentMessage('arfa',selectedShelter?'Origin updated. The destination is already selected; I can regenerate candidate routes.':'Origin set. Select one of the green candidate-shelter markers on the map.','origin_reply');
    }

    function selectShelter(feature){
      const center=featureCenter(feature);
      if(!center) return;
      selectedShelter={feature,center};
      if(destinationMarker) destinationMarker.remove();
      const icon=L.divIcon({className:'',html:'<div style="width:27px;height:27px;border-radius:50%;background:#166534;border:3px solid white;box-shadow:0 2px 7px rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;color:white;font-size:12px;font-weight:800">B</div>',iconSize:[27,27],iconAnchor:[13.5,13.5]});
      destinationMarker=L.marker(center,{icon,pane:'sheltersPane',zIndexOffset:1900}).addTo(map);
      destinationMarker.bindTooltip(`Destination · ${escapeHtml(shelterName(feature))}`,{direction:'top'});
      clearRouteLines();
      map.closePopup();
      updateRoutingUI();
      showPanelView('agent');
      addAgentMessage('responder',`Route me to <b>${escapeHtml(shelterName(feature))}</b>.`,'destination_choice');
      addAgentMessage('arfa',originLatLng?'Destination selected. I can now generate multiple road-network candidates. These are not flood-ranked yet.':"Destination selected. Set the responder's origin on the map, then I can generate routes.",'destination_reply');
    }

    function updateRoutingUI(){
      const panel=document.getElementById('routingPanel');
      if(!panel) return;
      const originText=document.getElementById('originText');
      const destText=document.getElementById('destinationText');
      const btn=document.getElementById('findRoutesBtn');
      const hint=document.getElementById('routingHint');
      if(originText) originText.textContent=originLatLng?`${originLatLng.lat.toFixed(5)}, ${originLatLng.lng.toFixed(5)}`:'Not selected';
      if(destText) destText.textContent=selectedShelter?shelterName(selectedShelter.feature):'Select a shelter marker';
      if(btn) btn.disabled=!(originLatLng&&selectedShelter);
      const faBtn=document.getElementById('findFloodAwareBtn');
      if(faBtn) faBtn.disabled=!(originLatLng&&selectedShelter);
      if(hint && !originSelectionActive && agentState==='idle'){
        hint.innerHTML='Ask me about flood conditions in a location, for example: <b>What is the current flooding status of Oak Ridge, TN?</b>';
      }
    }

    function clearFloodOverlays(){
      if(floodCrossingLayer){floodCrossingLayer.remove();floodCrossingLayer=null;}
      if(floodSegmentLayer){floodSegmentLayer.remove();floodSegmentLayer=null;}
    }

    function clearRouteLines(){
      routeLayers.forEach(l=>l.remove());
      routeLayers=[];routeData=[];activeRouteIndex=0;
      floodAwareRouteLayers.forEach(l=>l.remove());
      floodAwareRouteLayers=[];floodAwareRouteData=[];
      clearFloodOverlays();
      const el=document.getElementById('routeResults');
      if(el) el.innerHTML='';
    }

    function clearRouting(preserveConversation=false){
      clearRouteLines();
      if(originMarker){originMarker.remove();originMarker=null;}
      if(destinationMarker){destinationMarker.remove();destinationMarker=null;}
      originLatLng=null;selectedShelter=null;originSelectionActive=false;
      const b=document.getElementById('setOriginBtn');
      b?.classList.remove('selecting');if(b)b.textContent='📍 Set origin';
      if(!preserveConversation) resetAgentConversation();
      updateRoutingUI();
    }

    function resetRoutingContext(preserveConversation=false){
      clearRouting(preserveConversation);
      if(shelterLayer){shelterLayer.remove();shelterLayer=null;}
      shelterRenderer=null;
      shelterCandidates=[];
      selectedFacilityTypes.clear();
      document.getElementById('agentRouteTools')?.classList.add('hidden');
      if(!preserveConversation){
        
        resetAgentConversation();
      }
    }

    function formatDistance(m){const mi=m/1609.344;return mi>=0.1?`${mi.toFixed(1)} mi`:`${Math.round(m*3.281)} ft`;}

    function formatDuration(s){const min=Math.round(s/60);return min>=60?`${Math.floor(min/60)} h ${min%60} min`:`${min} min`;}

    async function findRoutes(){
      if(!originLatLng||!selectedShelter) return;
      const btn=document.getElementById('findRoutesBtn');
      btn.disabled=true;btn.textContent='Generating…';
      showPanelView('agent');
      addAgentMessage('responder','Generate road-network alternatives to the selected shelter.','route_request');
      addAgentMessage('arfa','Generating candidate routes from the road network…','route_progress');
      clearRouteLines();
      const q=new URLSearchParams({
        originLat:originLatLng.lat.toFixed(7),originLon:originLatLng.lng.toFixed(7),
        destLat:selectedShelter.center.lat.toFixed(7),destLon:selectedShelter.center.lng.toFixed(7)
      });
      setStatus('Generating road-network alternatives…',true);
      try{
        const r=await fetch(`/api/routes?${q}`);
        const data=await r.json().catch(()=>({error:'Invalid routing response'}));
        if(!r.ok||data.error) throw new Error(data.error||'Routing failed');
        drawRouteResults(data.routes||[]);
        setStatus(`${routeData.length} route${routeData.length===1?'':'s'} generated · assessing flood exposure…`,false);
        const msg=routeData.length>1?`I found <b>${routeData.length} distinct route candidates</b>. They are already shown on the map. I am now checking each one against the current NOAA NWM inundation extent.`:`I found one viable road-network route. It is already shown on the map, and I am now checking its current flood exposure.`;
        addAgentMessage('arfa',msg,'route_progress');
        assessRouteFloodExposure();
      }catch(err){
        setStatus(err.message||'Routing unavailable',false);
        const el=document.getElementById('routeResults');
        if(el) el.innerHTML=`<div style="font-size:11px;color:#b91c1c;margin-top:7px">${escapeHtml(err.message||'Routing unavailable')}</div>`;
      }finally{
        btn.disabled=!(originLatLng&&selectedShelter);btn.textContent='Generate routes';
        const faBtn=document.getElementById('findFloodAwareBtn');
        if(faBtn){faBtn.disabled=!(originLatLng&&selectedShelter);faBtn.textContent='🌊 Flood-aware routing';}
      }
    }

    // ── Flood-aware routing ───────────────────────────────────────────────────
    // Calls /api/routes/flood-aware which returns both standard scored routes
    // and avoidance alternatives that steer around the HAND flood hazard zone.
    async function findFloodAwareRoutes(){
      if(!originLatLng||!selectedShelter) return;
      const btn=document.getElementById('findFloodAwareBtn');
      const routeBtn=document.getElementById('findRoutesBtn');
      btn.disabled=true;btn.textContent='Computing…';
      routeBtn.disabled=true;
      showPanelView('agent');
      addAgentMessage('responder','Generate flood-aware route alternatives that avoid the current flood hazard zone.','fa_route_request');
      addAgentMessage('arfa','Running flood-aware routing — computing HAND hazard and finding avoidance paths…','fa_route_progress');
      clearRouteLines();
      setStatus('Computing flood-aware routes…',true);
      const q=new URLSearchParams({
        originLat:originLatLng.lat.toFixed(7),originLon:originLatLng.lng.toFixed(7),
        destLat:selectedShelter.center.lat.toFixed(7),destLon:selectedShelter.center.lng.toFixed(7)
      });
      try{
        const r=await fetch(`/api/routes/flood-aware?${q}`);
        const data=await r.json().catch(()=>({error:'Invalid response'}));
        if(!r.ok||data.error) throw new Error(data.error||'Flood-aware routing failed');

        const stdRoutes=data.standard_routes||[];
        const faRoutes=data.flood_aware_routes||[];
        const allRoutes=[...stdRoutes,...faRoutes];

        if(!allRoutes.length) throw new Error('No routes returned');

        // Draw standard routes in normal colors
        routeData=stdRoutes.map(r=>({...r}));
        routeLayers=stdRoutes.map((r,i)=>{
          const geo={type:'Feature',geometry:r.geometry,properties:{}};
          return L.geoJSON(geo,{pane:'routesPane',style:{color:ROUTE_COLORS[i]||'#64748b',weight:i===0?7:5,opacity:i===0?.92:.78,lineCap:'round',lineJoin:'round'}}).addTo(map);
        });

        // Draw flood-aware routes in green
        floodAwareRouteData=faRoutes.map(r=>({...r}));
        floodAwareRouteLayers=faRoutes.map((r,i)=>{
          const geo={type:'Feature',geometry:r.geometry,properties:{}};
          return L.geoJSON(geo,{pane:'routesPane',style:{color:FLOOD_AWARE_COLORS[i]||'#15803d',weight:6,opacity:.88,lineCap:'round',lineJoin:'round',dashArray:'10 4'}}).addTo(map);
        });

        activeRouteIndex=0;
        window._routeLayers=[...routeLayers,...floodAwareRouteLayers];
        _showHideRoutesBtn();

        // Render combined route cards
        _renderFloodAwareRouteCards(stdRoutes,faRoutes);
        highlightRoute(0,false);

        const group=L.featureGroup([...routeLayers,...floodAwareRouteLayers]);
        if(group.getBounds().isValid()) map.fitBounds(group.getBounds(),{padding:[55,55],maxZoom:15});

        const meta=data.hazard_metadata;
        const methodNote=meta?`HAND threshold: ${meta.hand_threshold_m}m (${meta.flood_category}, ${meta.confidence} confidence)`:'';
        const avoidCount=faRoutes.length;
        addAgentMessage('arfa',
          `Flood-aware routing complete. ${stdRoutes.length} standard route${stdRoutes.length===1?'':'s'} (blue/orange) and `+
          `<b>${avoidCount} avoidance route${avoidCount===1?'':'s'}</b> (green dashed) shown. ${methodNote}<br>`+
          `<small style="color:var(--mu)">Green routes attempt to bypass the flood zone — compare flood status and travel time before choosing.</small>`,
          'fa_route_done');

        // Reasoning over combined evidence
        const obs={
          event:'flood_aware_routing',area:agentAreaLabel,
          hazard_method:meta?.method||'HAND screening',
          flood_category:meta?.flood_category||'unknown',
          standard_routes:stdRoutes.map((r,i)=>({
            route:r.label||`Route ${i+1}`,generation:r.generation,
            distance_km:+((r.distance_m||0)/1000).toFixed(2),
            duration_min:+((r.duration_s||0)/60).toFixed(1),
            flooded_length_m:r.flood_exposure?.flooded_length_m??null,
            flood_status:r.flood_exposure?.flood_status??null,
          })),
          flood_aware_routes:faRoutes.map((r,i)=>({
            route:r.label||`Flood-Aware ${i+1}`,generation:r.generation,
            distance_km:+((r.distance_m||0)/1000).toFixed(2),
            duration_min:+((r.duration_s||0)/60).toFixed(1),
            flooded_length_m:r.flood_exposure?.flooded_length_m??null,
            flood_status:r.flood_exposure?.flood_status??null,
          })),
        };
        reasonOverObservation(
          'CURRENT STAGE: flood-aware route comparison. Standard routes are fastest OSRM alternatives scored for HAND flood exposure. '+
          'Flood-aware routes attempted to avoid the flood zone — compare flood_status and duration_min. '+
          'Never claim a route is passable; terrain screening only.',
          obs,'fa_route_reasoning',false);
        setStatus(`${allRoutes.length} routes · ${faRoutes.length} flood-avoidance alternative${faRoutes.length===1?'':'s'}`,false);
      }catch(err){
        addAgentMessage('arfa',`Flood-aware routing failed: ${escapeHtml(err.message||'unknown error')}.`,'fa_route_error');
        setStatus('Flood-aware routing unavailable',false);
      }finally{
        btn.disabled=!(originLatLng&&selectedShelter);btn.textContent='🌊 Flood-aware routing';
        routeBtn.disabled=!(originLatLng&&selectedShelter);
      }
    }

    function _renderFloodAwareRouteCards(stdRoutes,faRoutes){
      const allRoutes=[...stdRoutes,...faRoutes];
      // Merge into routeData for unified card indexing
      routeData=[...stdRoutes];
      floodAwareRouteData=[...faRoutes];
      const el=document.getElementById('routeResults');
      if(!el) return;
      let html='';
      allRoutes.forEach((r,i)=>{
        const isFA=i>=stdRoutes.length;
        const color=isFA?FLOOD_AWARE_COLORS[i-stdRoutes.length]:(ROUTE_COLORS[i]||'#64748b');
        const label=r.label||(isFA?`Flood-Aware ${i-stdRoutes.length+1}`:`Route ${i+1}`);
        const exp=r.flood_exposure;
        const statusIcon=exp?{safe:'✓ ',caution:'⚠ ',avoid:'✗ '}[exp.flood_status||'']||'':'';
        const expText=exp
          ?`${statusIcon}${formatDistance(exp.flooded_length_m||0)} flooded · ${((exp.flooded_fraction||0)*100).toFixed(1)}% · ${exp.flood_status||''}`
          :'Flood exposure: pending';
        const faTag=isFA?`<span class="route-role-badge" style="background:#dcfce7;color:#15803d">Avoids flood zone</span>`:'';
        html+=`<button class="route-card ${i===activeRouteIndex?'active':''}" onclick="highlightRoute(${i})">
          <span class="route-index">${i+1}</span>
          <span class="route-copy">
            <span class="route-name"><span class="route-swatch" style="background:${color}${isFA?';border:2px dashed rgba(0,0,0,.25)':''}"></span>${escapeHtml(label)}</span>
            <span class="route-metrics">${formatDistance(r.distance_m)} · ${formatDuration(r.duration_s)}</span>
            <span class="route-exposure">${escapeHtml(expText)}</span>
            ${faTag?`<span class="route-role-badges">${faTag}</span>`:''}
          </span>
          <span class="route-chevron">›</span>
        </button>`;
      });
      html+=`<div class="route-export-row"><button type="button" id="exportRouteBtn" class="agent-action-btn">Open selected route in Google Maps</button><div class="route-export-note">Green dashed routes avoid the flood zone. All recommendations require field verification.</div></div>`;
      el.innerHTML=html;
      document.getElementById('exportRouteBtn')?.addEventListener('click',exportSelectedRouteToGoogleMaps);
    }

    function routeExposureText(r){
      if(r._exposureLoading) return 'HAND flood screening: analyzing…';
      if(r._exposureError) return 'HAND flood screening unavailable';
      const e=r.flood_exposure;
      if(!e) return 'Flood exposure: pending';
      const status=e.flood_status||'';
      const icon=status==='safe'?'✓ ':status==='caution'?'⚠ ':status==='avoid'?'✗ ':'';
      return `${icon}${formatDistance(e.flooded_length_m||0)} flooded · ${((e.flooded_fraction||0)*100).toFixed(1)}% · ${status}`;
    }

    function routeRoleBadges(r,i){
      const badges=[];
      if(i===0)badges.push('Fastest');
      if(r._lowestExposure)badges.push('Lowest flood exposure');
      if(r._balanced)badges.push('Balanced');
      return badges.length?`<span class="route-role-badges">${badges.map(b=>`<span class="route-role-badge">${escapeHtml(b)}</span>`).join('')}</span>`:'';
    }

    function renderRouteCards(){
      const el=document.getElementById('routeResults');
      if(!el)return;
      el.innerHTML=routeData.map((r,i)=>{
        const cond=r._roadConditions;
        const condHtml=cond
          ?(cond.has_closures
            ?`<span class="route-exposure" style="color:#dc2626">⚠ ${cond.incident_count} incident${cond.incident_count===1?'':'s'} · road closure detected</span>`
            :(cond.incident_count>0?`<span class="route-exposure">ℹ ${cond.incident_count} incident${cond.incident_count===1?'':'s'} on route</span>`:``))
          :``;
        return `<button class="route-card ${i===activeRouteIndex?'active':''}" onclick="highlightRoute(${i})"><span class="route-index">${i+1}</span><span class="route-copy"><span class="route-name"><span class="route-swatch" style="background:${ROUTE_COLORS[i]||'#64748b'}"></span>${escapeHtml(r.label||`Route ${i+1}`)}</span><span class="route-metrics">${formatDistance(r.distance_m)} · ${formatDuration(r.duration_s)}</span><span class="route-exposure">${escapeHtml(routeExposureText(r))}</span>${condHtml}${routeRoleBadges(r,i)}</span><span class="route-chevron">›</span></button>`;
      }).join('')+
        `<div class="route-export-row"><button type="button" id="exportRouteBtn" class="agent-action-btn">Open selected route in Google Maps</button><div class="route-export-note">Google Maps may recalculate the route. ARFA exports the origin, destination, and sampled route waypoints.</div></div>`;
      document.getElementById('exportRouteBtn')?.addEventListener('click',exportSelectedRouteToGoogleMaps);
    }

    function drawRouteResults(routes){
      routeData=routes.map(r=>({...r,_exposureLoading:true}));
      activeRouteIndex=0;
      const el=document.getElementById('routeResults');
      routeLayers=routes.map((r,i)=>{
        const geo={type:'Feature',geometry:r.geometry,properties:{}};
        return L.geoJSON(geo,{pane:'routesPane',style:{color:ROUTE_COLORS[i]||'#64748b',weight:i===0?7:5,opacity:i===0?.92:.78,lineCap:'round',lineJoin:'round'}}).addTo(map);
      });
      window._routeLayers=routeLayers; // expose for hide/show toggle
      _showHideRoutesBtn();
      renderRouteCards();
      highlightRoute(0,false);
      const group=L.featureGroup(routeLayers);
      if(group.getBounds().isValid()) map.fitBounds(group.getBounds(),{padding:[55,55],maxZoom:15});
    }

    function rankRoutesByExposure(){
      routeData.forEach(r=>{r._lowestExposure=false;r._balanced=false;});
      const valid=routeData.map((r,i)=>({r,i,e:r.flood_exposure})).filter(x=>x.e);
      if(!valid.length)return;
      const lowest=valid.slice().sort((a,b)=>(a.e.exposed_percent||0)-(b.e.exposed_percent||0)||a.r.duration_s-b.r.duration_s)[0];
      lowest.r._lowestExposure=true;
      const times=valid.map(x=>x.r.duration_s||0), exps=valid.map(x=>x.e.exposed_percent||0);
      const tmin=Math.min(...times),tmax=Math.max(...times),emin=Math.min(...exps),emax=Math.max(...exps);
      valid.forEach(x=>{
        const tn=tmax>tmin?((x.r.duration_s-tmin)/(tmax-tmin)):0;
        const en=emax>emin?((x.e.exposed_percent-emin)/(emax-emin)):0;
        x.score=.45*tn+.55*en;
      });
      valid.sort((a,b)=>a.score-b.score)[0].r._balanced=true;
    }

    async function assessRouteFloodExposure(){
      if(!routeData.length) return;
      try{
        // Build a GeoJSON FeatureCollection of candidate routes for the HAND scoring endpoint.
        // /api/flood/score-routes automatically derives the bbox from route geometry and runs
        // the full DEM → PyFlwDir → HAND → hazard pipeline (cached after first run).
        const routesFC={
          type:'FeatureCollection',
          features:routeData.map(r=>({
            type:'Feature',
            geometry:r.geometry,
            properties:{id:r.id,distance_km:+(r.distance_m/1000).toFixed(3),travel_time_min:+(r.duration_s/60).toFixed(2)}
          }))
        };
        const rr=await fetch('/api/flood/score-routes',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({
            routes:routesFC,
            // Pass already-fetched gauge data so HAND service skips duplicate USGS/NOAA calls
            gauges:gauges.map(g=>({lid:g.lid,name:g.name,latitude:g.latitude,longitude:g.longitude,_stage:g._stage,_sev:g._sev,_d:g._d||null})),
          })});
        const data=await rr.json().catch(()=>({error:'Invalid response'}));
        if(!rr.ok||data.error) throw new Error(data.error||'HAND flood screening failed');

        // Map scored features back to routeData by route id
        const byId=new Map((data.features||[]).map(f=>[f.properties?.id, f.properties]));
        const ranking=new Map((data.ranking||[]).map(r=>[r.route_id, r]));
        routeData.forEach(r=>{
          r._exposureLoading=false;
          const props=byId.get(r.id);
          const rank=ranking.get(r.id);
          r.flood_exposure=props?{
            flooded_length_m:    props.flooded_length_m,
            flooded_fraction:    props.flooded_fraction,
            flood_status:        props.flood_status,
            flood_risk_score:    props.flood_risk_score,
            rank:                rank?.rank,
            flooded_segments:    props.flooded_segments||null,
            flood_crossing_points: props.flood_crossing_points||null,
          }:null;
        });

        // _lowestExposure = rank 1 per HAND scoring; _balanced = safe/caution with shortest time
        routeData.forEach(r=>{r._lowestExposure=r.flood_exposure?.rank===1; r._balanced=false;});
        const safeRoutes=routeData.filter(r=>['safe','caution'].includes(r.flood_exposure?.flood_status));
        if(safeRoutes.length) safeRoutes.sort((a,b)=>a.duration_s-b.duration_s)[0]._balanced=true;

        renderRouteCards();
        highlightRoute(activeRouteIndex,false);

        // Live road conditions check (TomTom/OSM) — runs in parallel, updates cards when done
        checkRoadConditions(routeData);

        const meta=data.hazard_metadata;
        const methodNote=meta
          ? `HAND threshold: ${meta.hand_threshold_m}m (${meta.flood_category}, ${meta.confidence} confidence · ${meta.method})`
          : '';
        addAgentMessage('arfa',
          `HAND flood screening complete. ${methodNote}`
          +(meta?.important_limitation?`<br><small style="color:var(--mu)">${escapeHtml(meta.important_limitation)}</small>`:''),
          'route_exposure');

        const obs={
          event:'route_hand_flood_screening',area:agentAreaLabel,
          hazard_method: meta?.method||'HAND screening',
          flood_category: meta?.flood_category||'unknown',
          hand_threshold_m: meta?.hand_threshold_m,
          confidence: meta?.confidence,
          routes:routeData.map((r,i)=>({
            route:r.label||`Route ${i+1}`,
            distance_km:+((r.distance_m||0)/1000).toFixed(2),
            duration_min:+((r.duration_s||0)/60).toFixed(1),
            flooded_length_m:r.flood_exposure?.flooded_length_m??null,
            flood_status:r.flood_exposure?.flood_status??null,
            rank:r.flood_exposure?.rank??null,
            fastest:i===0,
            lowest_flood_exposure:!!r._lowestExposure,
            balanced:!!r._balanced,
          })),
        };
        reasonOverObservation(
          'CURRENT STAGE: route comparison. Routes are ranked by HAND-based flood screening (rank 1 = lowest flood exposure). '
          +'Recommend routes by rank. Explain the trade-off between flood status (safe/caution/avoid) and travel time. '
          +'Never claim a route is passable or road conditions are known — this is a terrain-screening layer only.',
          obs,'route_reasoning',false);
        setStatus(`${routeData.length} routes · HAND flood screening complete`,false);
      }catch(err){
        routeData.forEach(r=>{r._exposureLoading=false; r._exposureError=true;});
        renderRouteCards();
        highlightRoute(activeRouteIndex,false);
        addAgentMessage('arfa',`I generated the routes, but HAND flood screening could not complete: ${escapeHtml(err.message||'service unavailable')}.`,'route_exposure');
        setStatus(`${routeData.length} routes generated · HAND screening unavailable`,false);
      }
    }

    function exportSelectedRouteToGoogleMaps(){
      const r=routeData[activeRouteIndex];
      if(!r||!originLatLng||!selectedShelter)return;
      const coords=r.geometry?.coordinates||[];
      const params=new URLSearchParams();
      params.set('api','1');
      params.set('origin',`${originLatLng.lat.toFixed(6)},${originLatLng.lng.toFixed(6)}`);
      params.set('destination',`${selectedShelter.center.lat.toFixed(6)},${selectedShelter.center.lng.toFixed(6)}`);
      params.set('travelmode','driving');
      if(coords.length>2){
        const maxWaypoints=6;
        const sampled=[];
        for(let k=1;k<=maxWaypoints;k++){
          const idx=Math.round(k*(coords.length-1)/(maxWaypoints+1));
          const c=coords[idx];
          if(c)sampled.push(`${Number(c[1]).toFixed(6)},${Number(c[0]).toFixed(6)}`);
        }
        if(sampled.length)params.set('waypoints',sampled.join('|'));
      }
      window.open(`https://www.google.com/maps/dir/?${params.toString()}`,'_blank','noopener,noreferrer');
    }

    function _drawFloodOverlaysForRoute(r){
      clearFloodOverlays();
      if(!r||!r.flood_exposure) return;
      const exp=r.flood_exposure;

      // --- Flooded segments: red thick line overlay ---
      if(exp.flooded_segments && !exp.flooded_segments.coordinates?.length===false){
        try{
          floodSegmentLayer=L.geoJSON({type:'Feature',geometry:exp.flooded_segments,properties:{}},{
            pane:'routesPane',
            style:{color:'#dc2626',weight:7,opacity:0.82,lineCap:'round',lineJoin:'round',dashArray:'6 4'},
          }).addTo(map);
        }catch(e){}
      }

      // --- Flood crossing point markers ---
      const fc=exp.flood_crossing_points;
      if(fc && fc.features && fc.features.length){
        const markers=[];
        fc.features.forEach(f=>{
          const [lon,lat]=f.geometry.coordinates;
          const icon=L.divIcon({
            className:'',
            html:`<div style="width:14px;height:14px;border-radius:50%;background:#dc2626;border:2px solid #fff;box-shadow:0 0 4px rgba(0,0,0,.5)" title="Flood zone boundary"></div>`,
            iconSize:[14,14],iconAnchor:[7,7],
          });
          markers.push(L.marker([lat,lon],{icon,pane:'sheltersPane',zIndexOffset:2500})
            .bindTooltip('⚠ Flood zone boundary crossing',{direction:'top',offset:[0,-8]}));
        });
        floodCrossingLayer=L.layerGroup(markers).addTo(map);
      }
    }

    function highlightRoute(index,bring=true){
      activeRouteIndex=index;
      // Combine standard + flood-aware for styling
      const allLayers=[...routeLayers,...floodAwareRouteLayers];
      const allData=[...routeData,...floodAwareRouteData];
      routeLayers.forEach((layer,i)=>{
        layer.setStyle({weight:i===index?8:4,opacity:i===index?.96:.55});
        if(i===index && bring) layer.bringToFront?.();
      });
      floodAwareRouteLayers.forEach((layer,i)=>{
        const fi=routeLayers.length+i;
        layer.setStyle({weight:fi===index?8:4,opacity:fi===index?.96:.55});
        if(fi===index && bring) layer.bringToFront?.();
      });
      document.querySelectorAll('.route-card').forEach((el,i)=>el.classList.toggle('active',i===index));
      // Show flood overlays for the selected route
      const selectedRoute=allData[index];
      _drawFloodOverlaysForRoute(selectedRoute||null);
    }

    // USA Structures OCC_CLS categories are based largely on HAZUS.
    // Use muted, earth-toned colors so structures do not compete visually with flood-risk layers.
    const OCC_COLORS={
      'Residential':'#9a6b3f',
      'Commercial':'#b07a45',
      'Government':'#7d6748',
      'Education':'#a1845c',
      'Assembly':'#8d7052',
      'Industrial':'#6f6254',
      'Utility and Misc':'#8a7b68',
      'Agriculture':'#8b7653',
      'Agricultural':'#8b7653',
      'Unknown':'#9b8b78'
    };

    function occColor(occ){
      const key=(occ||'').trim();
      return OCC_COLORS[key]||OCC_COLORS.Unknown;
    }

    function escapeHtml(value){
      return String(value ?? '')
        .replaceAll('&','&amp;')
        .replaceAll('<','&lt;')
        .replaceAll('>','&gt;')
        .replaceAll('"','&quot;')
        .replaceAll("'",'&#039;');
    }

    function fmtStructureValue(v){
      if(v==null || v==='') return '';
      if(typeof v==='number'){
        return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined,{maximumFractionDigits:2});
      }
      return escapeHtml(v);
    }

    // One shared popup for ALL structures.
    // This avoids creating thousands of Leaflet popup objects up front.
    const structurePopup=L.popup({
      maxHeight:340,
      maxWidth:380,
      autoPan:true,
      closeButton:true,
      autoClose:true,
      closeOnClick:true
    });

    // One shared Canvas renderer for ALL structure polygons.
    // Canvas is substantially lighter than SVG when rendering thousands of buildings.
    const structuresRenderer=L.canvas({
      pane:'structuresPane',
      padding:0.5,
      tolerance:6
    });

    function structurePopupHtml(feature){
      const p=feature?.properties||{};
      const occ=p['Occupancy Class']||'Unknown';
      const prim=p['Primary Use']||'';

      const address=[p['Address'],p['City'],p['State'],p['ZIP Code']]
        .filter(v=>v!=null&&v!=='')
        .map(escapeHtml)
        .join(', ');

      const rows=Object.entries(p)
        .map(([k,v])=>{
          const missing=(v==null || v==='');
          return `<tr>
            <td style="color:#6b7280;padding:3px 10px 3px 0;font-size:11px;white-space:nowrap;vertical-align:top">${escapeHtml(k)}</td>
            <td style="font-size:11px;font-weight:${missing?'400':'500'};color:${missing?'#9ca3af':'#111827'};word-break:break-word">${missing?'N/A':fmtStructureValue(v)}</td>
          </tr>`;
        })
        .join('');

      const col=occColor(occ);
      const title=prim||occ||'Structure';

      return `
        <div style="min-width:260px;max-width:360px">
          <div style="display:flex;align-items:center;gap:7px;margin-bottom:5px">
            <span style="width:10px;height:10px;border-radius:2px;background:${col};border:1px solid #5b4632;display:inline-block"></span>
            <div style="font-weight:700;font-size:13px;color:#2f241b">${escapeHtml(title)}</div>
          </div>
          ${address?`<div style="font-size:11px;color:#6b7280;margin-bottom:8px">${address}</div>`:''}
          ${isShelterCandidate(feature)?'<span class="shelter-popup-tag">Probable shelter</span>':''}
          <table style="border-collapse:collapse;width:100%;margin-top:5px">${rows}</table>
          <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
            <button class="select-shelter-btn" type="button" style="flex:1">Use as destination</button>
            <button class="select-origin-btn" type="button" style="flex:1;background:#e0f2fe;border-color:#0ea5e9;color:#0369a1">Use as origin</button>
          </div>
        </div>`;
    }

    function showStructurePopup(feature,latlng){
      structurePopup
        .setLatLng(latlng)
        .setContent(structurePopupHtml(feature))
        .openOn(map);

      // Bind after the popup is in the DOM.
      setTimeout(()=>{
        const destBtn=document.querySelector('.select-shelter-btn');
        if(destBtn) destBtn.onclick=()=>{ structurePopup.close(); selectShelter(feature); };
        const origBtn=document.querySelector('.select-origin-btn');
        if(origBtn) origBtn.onclick=()=>{
          structurePopup.close();
          const center=featureCenter(feature);
          if(center) setOrigin(center);
        };
      },0);
    }

    function buildStructureStyle(){
      return{
        renderer:structuresRenderer,
        pane:'structuresPane',

        style:f=>{
          const occ=f.properties?.['Occupancy Class']||'Unknown';
          const col=occColor(occ);

          return{
            pane:'structuresPane',
            renderer:structuresRenderer,
            interactive:true,
            bubblingMouseEvents:false,
            color:'#5b4632',
            weight:0.7,
            opacity:0.9,
            fillColor:col,
            fillOpacity:0.52
          };
        },

        onEachFeature:(f,layer)=>{
          // Do NOT pre-build/bind one popup per feature.
          // Generate the popup HTML only when the user actually clicks a building.
          layer.on('click',e=>{
            if(e.originalEvent){
              L.DomEvent.stopPropagation(e.originalEvent);
            }
            showStructurePopup(f,e.latlng);
          });
        }
      };
    }
    function updateStructuresToggleUI(){
      const checkbox=document.getElementById('structuresToggle');
      const label=document.getElementById('structuresToggleLabel');
      const text=document.getElementById('tbStructuresToggleText');
      const getBtn=document.getElementById('tbStructuresBtn');

      const count=structuresData?.features?.length||0;
      const hasData=count>0;

      if(checkbox){ checkbox.disabled=!hasData||structuresLoading; checkbox.checked=hasData&&structuresVisible; }
      if(label){ label.classList.toggle('disabled',!hasData||structuresLoading); }
      if(getBtn){ getBtn.disabled=structuresLoading; getBtn.textContent=structuresLoading?'Loading…':'Structures'; }
      if(text){
        if(!hasData) text.textContent='Show';
        else text.textContent=structuresVisible?`Hide (${count.toLocaleString()})`:`Show (${count.toLocaleString()})`;
      }
    }

    function setStructuresProgress({show=true,text='',count=null,pct=null}={}){
      const wrap=document.getElementById('structuresProgress');
      const label=document.getElementById('structuresProgressText');
      const countEl=document.getElementById('structuresProgressCount');
      const bar=document.getElementById('structuresProgressBar');

      wrap.style.display=show?'block':'none';
      if(text!==null) label.textContent=text;
      if(count!==null) countEl.textContent=Number(count).toLocaleString();

      if(pct!==null){
        const safe=Math.max(0,Math.min(100,Number(pct)||0));
        bar.style.width=`${safe}%`;
      }
    }

    function nextFrame(){
      return new Promise(resolve=>requestAnimationFrame(()=>resolve()));
    }

    // ── Structures auto-repair: agent-driven ──────────────────────────────────
    // When the stream returns missing_gdbs, the result is passed to the
    // reasoning agent as a tool observation. The agent diagnoses the problem
    // and explains it. The controller agent then decides to repair_missing_structures,
    // which executes the download + reindex via /api/structures/repair.
    let _repairPollTimer=null;

    async function _executeRepair(states){
      addAgentMessage('arfa',`Initiating download for ${states.join(', ')}. Monitoring progress…`,'repair_started');
      try{
        const r=await fetch('/api/structures/repair',{
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({states}),
        });
        const data=await r.json();
        if(!r.ok||data.error) throw new Error(data.error||'Repair request failed');
        if(data.started===false){
          addAgentMessage('arfa',`A repair job is already running for: ${data.status?.states?.join(', ')||'unknown states'}.`,'repair_already');
          _pollRepairStatus(states);
          return;
        }
        _pollRepairStatus(states);
      }catch(err){
        addAgentMessage('arfa',`Download could not start: ${escapeHtml(err.message)}.`,'repair_error');
      }
    }

    function _pollRepairStatus(states){
      let lastLogLen=0;
      if(_repairPollTimer) clearInterval(_repairPollTimer);
      _repairPollTimer=setInterval(async()=>{
        try{
          const r=await fetch('/api/structures/repair/status');
          const s=await r.json();
          const newLines=(s.log||[]).slice(lastLogLen);
          lastLogLen=(s.log||[]).length;
          if(newLines.length) setStatus(newLines[newLines.length-1],true);
          if(s.done){
            clearInterval(_repairPollTimer);_repairPollTimer=null;
            if(s.ok){
              const obs={event:'structures_repair_complete',states_repaired:s.states||states};
              reasonOverObservation(
                'The missing USA Structures GDB files have been downloaded and indexed. Report this success briefly, then tell the responder you will now reload the structure data.',
                obs,'repair_done',false
              ).then(()=>setTimeout(()=>getStructures({preserveAgent:true}),600));
            }else{
              const obs={event:'structures_repair_failed',error:s.error||'unknown',states:s.states||states};
              reasonOverObservation(
                'The automatic download of missing USA Structures data failed. Explain what went wrong and advise the responder to check that ARFA_STRUCTURES_GDB_DIR is set correctly and that network access to disasters.geoplatform.gov is available.',
                obs,'repair_failed',false
              );
              setStatus('Structures download failed',false);
            }
          }
        }catch(e){ /* network glitch — keep polling */ }
      },3000);
    }

    async function getStructures(options={}){
      if(structuresLoading) return;

      const btn=document.getElementById('tbStructuresBtn');

      const checkbox=document.getElementById('structuresToggle');

      // Cancel an older stream if one somehow still exists.
      if(structuresAbortController){
        structuresAbortController.abort();
      }
      structuresAbortController=new AbortController();

      structuresLoading=true;
      btn.disabled=true;
      checkbox.disabled=true;
      btn.textContent='⏳ Getting structures…';

      const b=map.getBounds();
      const params=new URLSearchParams({
        minLat:b.getSouth().toFixed(6),
        minLon:b.getWest().toFixed(6),
        maxLat:b.getNorth().toFixed(6),
        maxLon:b.getEast().toFixed(6)
      });

      if(window._bboxDebug) window._bboxDebug.remove();
      window._bboxDebug=L.rectangle(
        [[b.getSouth(),b.getWest()],[b.getNorth(),b.getEast()]],
        {color:'#ef4444',weight:2,fillOpacity:0.05,dashArray:'6 4'}
      ).addTo(map);

      // Replace any previously retrieved structures immediately.
      if(structuresLayer){
        map.removeLayer(structuresLayer);
        structuresLayer=null;
      }
      map.closePopup(structurePopup);
      resetRoutingContext(Boolean(options.preserveAgent));

      structuresData={type:'FeatureCollection',features:[]};
      structuresVisible=true;

      // Create one persistent Canvas-backed GeoJSON layer and incrementally addData().
      structuresLayer=L.geoJSON(null,{
        ...buildStructureStyle(),
        renderer:structuresRenderer,
        pane:'structuresPane',
        interactive:true
      }).addTo(map);

      setStructuresProgress({
        show:true,
        text:'Finding relevant state files…',
        count:0,
        pct:2
      });
      setStatus('Retrieving structures…',true);

      let statesTotal=0;
      let statesDone=0;
      let finalMeta=null;

      try{
        const response=await fetch(`/api/structures/stream?${params}`,{
          signal:structuresAbortController.signal,
          cache:'no-store'
        });

        if(!response.ok){
          throw new Error(`Structures request failed (${response.status})`);
        }
        if(!response.body){
          throw new Error('Streaming response is not supported by this browser.');
        }

        const reader=response.body.getReader();
        const decoder=new TextDecoder();
        let buffer='';

        while(true){
          const {value,done}=await reader.read();
          if(done) break;

          buffer+=decoder.decode(value,{stream:true});
          const lines=buffer.split('\n');
          buffer=lines.pop()||'';

          for(const line of lines){
            if(!line.trim()) continue;

            const msg=JSON.parse(line);

            if(msg.kind==='meta'){
              statesTotal=Number(msg.states_total||0);
              const stateText=statesTotal
                ? `${statesTotal} state file${statesTotal===1?'':'s'} identified`
                : 'No state files intersect viewport';

              setStructuresProgress({
                show:true,
                text:stateText,
                count:0,
                pct:statesTotal?5:100
              });
              continue;
            }

            if(msg.kind==='batch'){
              const features=msg.features||[];
              if(!features.length) continue;

              // Keep the retrieved GeoJSON so Show/Hide can rebuild the layer later.
              structuresData.features.push(...features);

              // Plot this batch immediately instead of waiting for the full query.
              structuresLayer.addData({
                type:'FeatureCollection',
                features
              });
              addShelterFeatures(features);

              structuresLayer.bringToFront?.();

              const loaded=structuresData.features.length;
              const stateProgress=statesTotal
                ? (statesDone/statesTotal)*80
                : 0;

              // Between state-completion events, use loaded count to make the bar
              // visibly advance without pretending we already know the final total.
              const softProgress=Math.min(94,Math.max(8,stateProgress+10));

              setStructuresProgress({
                show:true,
                text:`Plotting ${msg.state||''} structures…`,
                count:loaded,
                pct:softProgress
              });

              setStatus(`${loaded.toLocaleString()} structures plotted…`,true);
              updateStructuresToggleUI();

              // Yield one animation frame so Leaflet paints this batch and the
              // progress bar stays responsive before the next batch is processed.
              await nextFrame();
              continue;
            }

            if(msg.kind==='state_done'){
              statesDone=Number(msg.states_done||statesDone);
              statesTotal=Number(msg.states_total||statesTotal);

              const pct=statesTotal
                ? 5+(statesDone/statesTotal)*90
                : 95;

              setStructuresProgress({
                show:true,
                text:`State files ${statesDone}/${statesTotal} complete`,
                count:structuresData.features.length,
                pct
              });
              continue;
            }

            if(msg.kind==='error'){
              throw new Error(msg.message||'Structure streaming error');
            }

            if(msg.kind==='done'){
              finalMeta=msg;
              break;
            }
          }
        }

        const returned=Number(finalMeta?.returned||structuresData.features.length);
        const total=Number(finalMeta?.total_in_area||returned);
        const capped=Boolean(finalMeta?.capped);
        const capLimit=Number(finalMeta?.cap_limit||returned);

        if(!returned){
          if(structuresLayer){
            map.removeLayer(structuresLayer);
            structuresLayer=null;
          }
          structuresVisible=false;
          setStructuresProgress({show:true,text:'No structures found in this view',count:0,pct:100});
          setStatus('No structures found in the current map view',false);

          // ── Agent-driven recovery for missing GDB files ────────────────
          const missingGdbs=(finalMeta?.missing_gdbs||[]);
          if(missingGdbs.length){
            showPanelView('agent');
            const obs={
              event:'structures_stream_result',
              states_requested:(finalMeta?.states_queried||[]),
              returned:0,
              missing_gdbs:missingGdbs,
              note:'GDB files for these states were not found on disk.'
            };
            // Missing GDBs are a deterministic data-availability failure, not a
            // semantic planning decision. Explain the problem through the reasoning
            // agent, but always start the existing repair workflow immediately.
            reasonOverObservation(
              'The structure data retrieval returned zero results due to missing database files. Diagnose the problem and explain that automatic download and indexing is starting now.',
              obs,'structures_missing',false
            ).catch(()=>{});
            await _executeRepair(missingGdbs);
          }
        }else{
          structuresVisible=true;

          const capNote=capped
            ? ` · showing ${returned.toLocaleString()} of ${total.toLocaleString()} (cap ${capLimit.toLocaleString()})`
            : '';

          setStructuresProgress({
            show:true,
            text:`Complete${capped?' · capped':''}`,
            count:returned,
            pct:100
          });

          setStatus(
            `${returned.toLocaleString()} structures loaded${capNote} · ${shelterCandidates.length.toLocaleString()} candidate shelters`,
            false
          );
          showRoutingPanel();

          // Leave the completed indicator visible briefly, then remove it.
          setTimeout(()=>{
            if(!structuresLoading){
              setStructuresProgress({show:false});
            }
          },1800);
        }

      }catch(err){
        if(err?.name==='AbortError'){
          setStatus('Structure retrieval cancelled',false);
        }else{
          console.error(err);
          setStatus(err?.message||'Structures unavailable',false);
        }

        if(!structuresData.features.length && structuresLayer){
          map.removeLayer(structuresLayer);
          structuresLayer=null;
          structuresVisible=false;
        }

        setStructuresProgress({
          show:true,
          text:err?.name==='AbortError'?'Cancelled':'Retrieval failed',
          count:structuresData.features.length,
          pct:100
        });

      }finally{
        structuresLoading=false;
        btn.disabled=false;
        btn.textContent='🏛 Get structures';
        structuresAbortController=null;
        updateStructuresToggleUI();
      }
    }

    function toggleStructuresVisibility(){
      const checkbox=document.getElementById('structuresToggle');

      if(!structuresData?.features?.length){
        checkbox.checked=false;
        updateStructuresToggleUI();
        return;
      }

      if(checkbox.checked){
        if(!structuresLayer){
          // Re-use the already-retrieved GeoJSON. No database/API call here.
          structuresLayer=L.geoJSON(structuresData,{
            ...buildStructureStyle(),
            renderer:structuresRenderer,
            pane:'structuresPane',
            interactive:true
          }).addTo(map);
          structuresLayer.bringToFront?.();
        }else if(!map.hasLayer(structuresLayer)){
          structuresLayer.addTo(map);
          structuresLayer.bringToFront?.();
        }
        structuresVisible=true;
        setStatus(`${structuresData.features.length.toLocaleString()} retrieved structures shown`,false);
      }else{
        if(structuresLayer && map.hasLayer(structuresLayer)){
          map.removeLayer(structuresLayer);
        }
        map.closePopup(structurePopup);
        structuresVisible=false;
        setStatus('Retrieved structures hidden',false);
      }

      updateStructuresToggleUI();
    }

    function renderRoads(roads){
      if(roadLayer){roadLayer.remove();roadLayer=null;}

      // Get flooded tract polygons for intersection test
      const floodedPolygons=[];
      if(tractLayer){
        tractLayer.eachLayer(layer=>{
          const risk=tractRisk[layer.feature?.properties?.GEOID];
          if(risk&&risk!=='none'&&risk!=='no_flooding'){
            floodedPolygons.push(layer.getBounds());
          }
        });
      }

      roadLayer=L.geoJSON({type:'FeatureCollection',features:roads},{
        style: f=>{
          const coords=f.geometry.coordinates;
          const flooded=coords.some(([lon,lat])=>
            floodedPolygons.some(b=>b.contains(L.latLng(lat,lon)))
          );
          return{
            color: flooded ? '#dc2626' : '#64748b',
            weight: flooded ? 3 : 1.5,
            opacity: flooded ? 0.9 : 0.5,
            dashArray: flooded ? null : '4 3',
          };
        },
        onEachFeature:(f,layer)=>{
          const name=f.properties.name||f.properties.highway||'Road';
          layer.bindTooltip(name,{sticky:true});
        }
      }).addTo(map);
    }

    function renderFacilities(facilities){
      if(facilityLayer){facilityLayer.remove();facilityLayer=null;}

      const FCOLORS={hospital:'#dc2626',clinic:'#ef4444',fire_station:'#f97316',
                     police:'#3b82f6',shelter:'#8b5cf6'};

      facilityLayer=L.layerGroup();
      facilities.forEach(f=>{
        const [lon,lat]=f.geometry.coordinates;
        const amenity=f.properties.amenity||'facility';
        const col=FCOLORS[amenity]||'#6b7280';
        const name=f.properties.name||amenity.replace('_',' ');

        // Check if facility is itself in a flooded tract
        const inFlood=tractLayer&&[...Object.entries(tractRisk)].some(([geoid,risk])=>{
          if(risk==='none'||risk==='no_flooding')return false;
          // rough check: find tract layer with this geoid
          let inTract=false;
          tractLayer.eachLayer(tl=>{
            if(tl.feature?.properties?.GEOID===geoid){
              if(tl.getBounds().contains(L.latLng(lat,lon))) inTract=true;
            }
          });
          return inTract;
        });

        const marker=L.circleMarker([lat,lon],{
          radius:inFlood?10:7,
          fillColor:col,
          color:inFlood?'#dc2626':'white',
          weight:inFlood?3:2,
          fillOpacity:0.9,
        });
        marker.bindTooltip(
          `<b>${name}</b><br>${amenity.replace('_',' ')}`
            +(inFlood?'<br><b style="color:#dc2626">⚠ In flooded area</b>':''),
          {sticky:true}
        );
        facilityLayer.addLayer(marker);
      });
      facilityLayer.addTo(map);
    }

    function analyzeIsolation(roads, facilities){
      // For each flooded tract, check if all road segments within it are flooded
      // and whether any critical facility is reachable via non-flooded roads.
      // This is a simplified heuristic (no full graph traversal).

      if(!tractLayer) return {isolated_tracts:0,at_risk_tracts:0,facilities_affected:0,details:[]};

      const floodedBboxes=[];
      const tractDetails=[];

      tractLayer.eachLayer(layer=>{
        const risk=tractRisk[layer.feature?.properties?.GEOID];
        const bounds=layer.getBounds();
        if(risk&&risk!=='none'&&risk!=='no_flooding'){
          floodedBboxes.push(bounds);
        }
      });

      // Tag each road segment: flooded or not
      const floodedRoads=roads.filter(r=>
        r.geometry.coordinates.some(([lon,lat])=>
          floodedBboxes.some(b=>b.contains(L.latLng(lat,lon)))
        )
      );
      const safeRoads=roads.filter(r=>!floodedRoads.includes(r));

      // For each flooded tract, estimate isolation
      let isolated=0, atRisk=0;
      tractLayer.eachLayer(layer=>{
        const risk=tractRisk[layer.feature?.properties?.GEOID];
        if(!risk||risk==='none') return;
        const bounds=layer.getBounds();
        const clat=(bounds.getNorth()+bounds.getSouth())/2;
        const clon=(bounds.getEast()+bounds.getWest())/2;

        // Count safe road connections touching this tract boundary
        const safeConnections=safeRoads.filter(r=>
          r.geometry.coordinates.some(([lon,lat])=>bounds.contains(L.latLng(lat,lon)))
        ).length;

        // Nearest critical facility
        const criticalFacilities=facilities.filter(f=>
          ['hospital','clinic','fire_station'].includes(f.properties.amenity)
        );
        let nearestFacDist=Infinity;
        criticalFacilities.forEach(f=>{
          const [flon,flat]=f.geometry.coordinates;
          const d=haversineKm(clat,clon,flat,flon);
          if(d<nearestFacDist) nearestFacDist=d;
        });

        if(safeConnections===0&&risk!=='no_flooding'){
          isolated++;
          tractDetails.push({geoid:layer.feature?.properties?.GEOID,
            name:layer.feature?.properties?.NAME,risk,
            safeConnections,nearestFacDist,isolation:'high'});
        } else if(safeConnections<=2){
          atRisk++;
          tractDetails.push({geoid:layer.feature?.properties?.GEOID,
            name:layer.feature?.properties?.NAME,risk,
            safeConnections,nearestFacDist,isolation:'medium'});
        }
      });

      const facilitiesAffected=facilities.filter(f=>{
        const [flon,flat]=f.geometry.coordinates;
        return floodedBboxes.some(b=>b.contains(L.latLng(flat,flon)));
      }).length;

      return{
        isolated_tracts:isolated,
        at_risk_tracts:atRisk,
        facilities_affected:facilitiesAffected,
        flooded_roads:floodedRoads.length,
        safe_roads:safeRoads.length,
        details:tractDetails,
      };
    }

    function renderIsolationResults(d){
      // Remove existing panel
      document.getElementById('isoPanel')?.remove();

      const panel=document.createElement('div');
      panel.id='isoPanel';
      panel.className='iso-section';
      panel.innerHTML=`
        <h4>🛣 Road Isolation Analysis</h4>
        <div class="iso-row"><span class="iso-label">Flooded road segments</span><span class="iso-val iso-high">${d.flooded_roads}</span></div>
        <div class="iso-row"><span class="iso-label">Safe road segments</span><span class="iso-val iso-low">${d.safe_roads}</span></div>
        <div class="iso-row"><span class="iso-label">Critical facilities in flood zone</span><span class="iso-val iso-high">${d.facilities_affected}</span></div>
        <div class="iso-row"><span class="iso-label">Tracts fully isolated</span><span class="iso-val iso-high">${d.isolated_tracts}</span></div>
        <div class="iso-row"><span class="iso-label">Tracts with limited access (≤2 roads)</span><span class="iso-val iso-med">${d.at_risk_tracts}</span></div>
        ${d.details.slice(0,5).map(t=>`
        <div class="iso-row" style="flex-direction:column;align-items:flex-start;gap:2px">
          <span class="iso-label" style="font-weight:500">Tract ${t.name||t.geoid}</span>
          <span style="font-size:10px;color:var(--mu)">Risk: ${t.risk} · Safe roads: ${t.safeConnections} · Nearest facility: ${t.nearestFacDist<999?t.nearestFacDist.toFixed(1)+'km':'N/A'}</span>
        </div>`).join('')}
        <div style="font-size:10px;color:var(--mu);margin-top:6px">Road intersection based on OSM · Heuristic isolation estimate · Not an evacuation advisory</div>
      `;

      // Insert at top of gauge list panel
      const pb=document.getElementById('pb');
      pb.insertBefore(panel, pb.firstChild);
    }

    // ── Live road conditions (Feature 3) ─────────────────────────────────────────
    async function checkRoadConditions(routes){
      if(!routes||!routes.length) return;
      try{
        const payload={
          routes:routes.map(r=>({id:r.id,geometry:r.geometry})),
        };
        const resp=await apiPost('/api/roads/conditions',payload);
        if(!resp||resp.error) return;
        let anyClosures=false;
        (resp.routes||[]).forEach(rc=>{
          const rd=routeData.find(r=>r.id===rc.route_id);
          if(!rd) return;
          rd._roadConditions=rc;
          if(rc.has_closures) anyClosures=true;
        });
        renderRouteCards();
        if(anyClosures){
          addAgentMessage('arfa','⚠ <b>Live road incidents detected</b> on one or more candidate routes. Check route cards for details. Verify conditions before travel.','road_conditions');
        }
      }catch(e){
        console.warn('[road_conditions]',e);
      }
    }

    // ── Structures filtered query (Feature 2) ────────────────────────────────────
    // Called from the agent conversation when the responder asks for specific
    // building types, heights, or name patterns within the loaded area.
    async function queryStructures(filters={},queryLabel='',hazardRelation='any'){
      if(!currentCountyBbox) return;
      const bbox=[currentCountyBbox.minLon,currentCountyBbox.minLat,currentCountyBbox.maxLon,currentCountyBbox.maxLat];
      const label=queryLabel||JSON.stringify(filters);
      if(hazardRelation!=='any') addAgentMessage('arfa',`I interpreted the request as <b>${escapeHtml(hazardRelation)}</b> the modeled flood hazard. I will first retrieve the semantic structure matches; hazard intersection remains a separate deterministic screening step.`,`struct_hazard_note`);
      addAgentMessage('arfa',`Searching structures: <i>${escapeHtml(label)}</i>…`,'struct_query');
      const data=await apiPost('/api/structures/query',{bbox,filters,hazard_relation:hazardRelation,gauges:gauges,limit:3000});
      if(!data||data.error){
        addAgentMessage('arfa',`Structure query failed: ${escapeHtml(data?.error||'unknown error')}`,'struct_query');
        return;
      }
      const n=data.returned||0;
      if(!n){
        addAgentMessage('arfa',`No structures matched "<b>${escapeHtml(label)}</b>" in the loaded area. The USA Structures dataset uses PRIM_OCC field — try a broader term like "school" or "hospital".`,'struct_query');
        return;
      }
      if(window._queryLayer){map.removeLayer(window._queryLayer);window._queryLayer=null;}
      window._queryLayer=L.geoJSON({type:'FeatureCollection',features:data.features||[]},{
        pane:'structuresPane',
        style:f=>({color:'#7c3aed',weight:2,fillColor:'#a855f7',fillOpacity:0.75}),
        onEachFeature:(f,layer)=>{
          const p=f.properties||{};
          layer.bindTooltip(`<b>${escapeHtml(p['Primary Use']||p['Occupancy Class']||'Structure')}</b><br>`+
            `${escapeHtml([p['Address'],p['City']].filter(Boolean).join(', '))}`+
            (p['Building Height (m)']?`<br>Height: ${p['Building Height (m)']} m`:''),{sticky:true});
        }
      }).addTo(map);
      addAgentMessage('arfa',
        `Found <b>${n.toLocaleString()}</b> result${n===1?'':'s'} for "<b>${escapeHtml(label)}</b>"${data.capped?` (capped at ${n.toLocaleString()} of ${data.total_matched.toLocaleString()})`:''}.`+
        ` Highlighted in purple on the map.`+
        (n>0?` Click any building for details.`:''),'struct_query');
    }

    function addStep(log,icon,label){
      const el=document.createElement('div');el.className='step';
      const ic={spin:'↻',ok:'✓',think:'◈',warn:'⚠'};
      el.innerHTML=`<div class="sh"><div class="si ${icon}">${ic[icon]||'·'}</div><span>${label}</span></div>`;
      log.appendChild(el);log.scrollTop=log.scrollHeight;return el;
    }
    function doneStep(el,type,label,bodyFn){
      const ic={ok:'✓',warn:'⚠',think:'◈'};
      el.querySelector('.sh').innerHTML=`<div class="si ${type}">${ic[type]||'✓'}</div><span>${label}</span>`;
      if(bodyFn){
        let b=el.querySelector('.sb');
        if(!b){b=document.createElement('div');b.className='sb';el.appendChild(b);}
        if(typeof bodyFn==='string')b.innerHTML=`<span style="color:var(--mu)">${bodyFn}</span>`;
        else{const n=bodyFn();if(n)b.appendChild(n);}
      }
      const log=document.getElementById('alog');if(log)log.scrollTop=log.scrollHeight;
    }
