// ---------------------------------------------------------------------------
// Who Owns Atlanta? — map page
// ---------------------------------------------------------------------------

// Tile URL: relative path in dev (served by local nginx), CloudFront in prod.
// Set PROD_TILES_URL once the CloudFront distribution is live.
const PROD_TILES_URL = null; // e.g. "https://tiles.who-owns-atlanta.org/tiles/{z}/{x}/{y}.pbf"
const DEV_TILES_URL  = `${window.location.origin}/tiles/{z}/{x}/{y}.pbf`;

const DEV_HOSTNAMES = ["who-owns-atlanta.local", "who-owns-atlanta.lan", "localhost"];
const PARCEL_TILES_URL = (DEV_HOSTNAMES.includes(window.location.hostname))
  ? DEV_TILES_URL
  : PROD_TILES_URL;

// ---------------------------------------------------------------------------
// Map init
// ---------------------------------------------------------------------------

// Detect ?cluster=ID before map init so we can suppress the parcel flash.
const pendingClusterId = parseInt(new URLSearchParams(window.location.search).get('cluster')) || null;

const map = new maplibregl.Map({
  container: 'map',
  style: 'https://tiles.openfreemap.org/styles/liberty',
  center: [-84.388, 33.749],  // Fallback center
  zoom: 10,                   // Initial zoom while loading
});

const ATLANTA_BOUNDS = [[-84.551, 33.637], [-84.289, 33.887]];

map.addControl(new maplibregl.NavigationControl(), 'top-left');

let selectedMarker  = null;
let activeClusterId = null;   // cluster currently in "focus" mode
let clusterMarkers  = [];     // teardrop pins placed for each cluster parcel (z13+ only)
let clusterParcels  = [];     // parcel list for the active cluster (for zoom-toggling markers)
let activeAreaFilter = null;  // { label, geometry } when an area filter is active
const hoverPopup    = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 12 });

// Add parcel tile layer once map is ready (only if URL is configured)
map.on('load', () => {
  if (!pendingClusterId) {
    map.fitBounds(ATLANTA_BOUNDS, { padding: 40, duration: 0 });
  }

  if (!PARCEL_TILES_URL) return;

  map.addSource('parcels', {
    type: 'vector',
    tiles: [PARCEL_TILES_URL],
    minzoom: 10,
    maxzoom: 14,
  });

  // Zoom 10-12: color by ownership type
  map.addLayer({
    id: 'parcels-overview',
    type: 'fill',
    source: 'parcels',
    'source-layer': 'parcels',
    maxzoom: 13,
    paint: {
      'fill-color': OVERVIEW_COLOR,
      'fill-outline-color': 'rgba(0,0,0,0.1)',
    },
  });

  // Zoom 13+: color by ownership type + cluster membership (see clusterColor())
  map.addLayer({
    id: 'parcels-detail',
    type: 'fill',
    source: 'parcels',
    'source-layer': 'parcels',
    minzoom: 13,
    paint: {
      'fill-color': clusterColor(),
      'fill-opacity': detailOpacity(),
      'fill-outline-color': 'rgba(0,0,0,0.15)',
    },
  });

  // Atlanta city limits boundary
  map.addSource('city-limits', {
    type: 'geojson',
    data: '/geojson/atlanta_city_limits.json',
  });

  map.addLayer({
    id: 'city-limits-casing',
    type: 'line',
    source: 'city-limits',
    paint: {
      'line-color': '#000000',
      'line-width': 4,
      'line-opacity': 0.5,
    },
  });

  map.addLayer({
    id: 'city-limits',
    type: 'line',
    source: 'city-limits',
    paint: {
      'line-color': '#ffffff',
      'line-width': 2,
      'line-dasharray': [4, 3],
      'line-opacity': 0.9,
    },
  });

  // Area filter overlay — world rectangle with a hole cut out for the selected area.
  // Empty by default; filled by setAreaFilter() via makeOutsideMask().
  map.addSource('area-overlay', {
    type: 'geojson',
    data: { type: 'FeatureCollection', features: [] },
  });

  map.addLayer({
    id: 'area-overlay',
    type: 'fill',
    source: 'area-overlay',
    paint: {
      'fill-color': '#000',
      'fill-opacity': 0.65,
    },
  });

  // Selected parcel highlight layer (outline) — used on individual parcel clicks.
  map.addLayer({
    id: 'parcels-selected',
    type: 'line',
    source: 'parcels',
    'source-layer': 'parcels',
    paint: {
      'line-color': '#2563eb',
      'line-width': 3,
    },
    filter: ['==', 'parcel_id', ''],
  });

  // Click handler
  map.on('click', ['parcels-overview', 'parcels-detail'], (e) => {
    const feat = e.features[0].properties;
    loadParcel(feat.county, feat.parcel_id);
  });

  map.on('mouseenter', 'parcels-overview', () => { map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseenter', 'parcels-detail',   () => { map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseleave', 'parcels-overview', () => { map.getCanvas().style.cursor = ''; hoverPopup.remove(); });
  map.on('mouseleave', 'parcels-detail',   () => { map.getCanvas().style.cursor = ''; hoverPopup.remove(); });

  // Hover tooltip — z13+ only (detail layer has owner/address tile properties)
  map.on('mousemove', 'parcels-detail', (e) => {
    const p = e.features[0].properties;
    if (!p.parcel_id) return;
    hoverPopup
      .setLngLat(e.lngLat)
      .setHTML(`<div class="hover-tip"><div class="hover-address">${escHtml(p.site_address || p.parcel_id)}</div><div class="hover-owner">${escHtml(p.owner_name || '')}</div></div>`)
      .addTo(map);
  });

  updateLegend();
  map.on('zoomend', updateLegend);

});

// ---------------------------------------------------------------------------
// Color scheme for zoom 13+ detail layer
// ---------------------------------------------------------------------------
// Requires cluster_size in tile data (build_tiles.sh includes it from
// ownership_clusters.parcel_count).
//
//   gray  — single owner (cluster_size ≤ 1 or no cluster)
//   red   — corporate owner cluster
//   amber — institutional owner cluster
//   blue  — individual landlord with multiple properties

function clusterColor() {
  return [
    'case',
    // cluster_size < 2 (single owner, or no cluster): gray
    ['<', ['coalesce', ['get', 'cluster_size'], 0], 2], '#94a3b8',
    ['get', 'is_corporate'],                            '#dc2626', // red   — corporate
    ['get', 'is_institutional'],                        '#d97706', // amber — institutional
                                                        '#3b82f6', // blue  — individual w/ portfolio
  ];
}

// Opacity for parcels-detail in normal mode: darker = larger portfolio.
function detailOpacity() {
  return [
    'step', ['coalesce', ['get', 'cluster_size'], 0],
    0.40,        // default: 0–1 parcels (single owner, also gray)
    2,   0.55,   //  2–9 parcels
    10,  0.70,   // 10–49 parcels
    50,  0.90,   // 50+ parcels
  ];
}

// Default fill-color expression for the overview layer (mirrored here so
// exitClusterMode() can restore it without re-reading paint state).
const OVERVIEW_COLOR = [
  'case',
  ['get', 'is_corporate'],     'rgba(220, 38, 38, 0.6)',
  ['get', 'is_institutional'], 'rgba(217, 119, 6, 0.6)',
  'rgba(148, 163, 184, 0.4)',
];

// ---------------------------------------------------------------------------
// Map legend
// ---------------------------------------------------------------------------

function swatch(color, label, shape) {
  if (shape === 'pin') {
    // Mimic a teardrop pin using a circle + point-down triangle
    return `<div class="legend-item">` +
      `<svg width="11" height="15" viewBox="0 0 11 15" style="flex-shrink:0">` +
      `<ellipse cx="5.5" cy="5.5" rx="5" ry="5" fill="${color}"/>` +
      `<polygon points="5.5,15 2,8 9,8" fill="${color}"/>` +
      `</svg>${label}</div>`;
  }
  if (shape === 'boundary') {
    // Dashed white line with dark casing, matching the city limits layer
    return `<div class="legend-item">` +
      `<svg width="20" height="11" viewBox="0 0 20 11" style="flex-shrink:0">` +
      `<line x1="0" y1="5.5" x2="20" y2="5.5" stroke="#000" stroke-width="4" stroke-opacity="0.5"/>` +
      `<line x1="0" y1="5.5" x2="20" y2="5.5" stroke="#fff" stroke-width="2" stroke-dasharray="4 3"/>` +
      `</svg>${label}</div>`;
  }
  return `<div class="legend-item"><span class="legend-swatch" style="background:${color}"></span>${label}</div>`;
}

function updateLegend() {
  const legend = document.getElementById('map-legend');
  legend.hidden = false;
  if (map.getZoom() >= 13) {
    legend.innerHTML =
      swatch('#dc2626', 'Corporate') +
      swatch('#d97706', 'Institutional') +
      swatch('#3b82f6', 'Individual portfolio') +
      swatch('#94a3b8', 'Single owner');
  } else {
    legend.innerHTML =
      swatch('rgba(220,38,38,0.8)',  'Corporate') +
      swatch('rgba(217,119,6,0.8)',  'Institutional') +
      swatch('rgba(148,163,184,0.6)', 'Other');
  }
  legend.innerHTML += swatch(null, 'City limits', 'boundary');
  if (activeClusterId) {
    legend.innerHTML += swatch('#16a34a', 'In cluster', 'pin');
  }
}

// ---------------------------------------------------------------------------
// Highlight helpers
// ---------------------------------------------------------------------------

// Outline a single parcel (on click).
function highlightParcel(parcelId) {
  if (!PARCEL_TILES_URL) return;
  if (map.getLayer('parcels-selected')) {
    map.setFilter('parcels-selected', ['==', 'parcel_id', parcelId || '']);
  }
}

// Cluster mode: dim every parcel NOT in clusterId so the owner's properties
// stand out.  Pass parcels array (from /api/owner/:id) to place dot markers.
// Pass null/0 to restore normal coloring.
function highlightCluster(clusterId, parcels) {
  if (!PARCEL_TILES_URL) return;
  if (clusterId) {
    enterClusterMode(clusterId, parcels);
  } else {
    exitClusterMode();
  }
}

function enterClusterMode(clusterId, parcels) {
  activeClusterId = clusterId;
  clusterParcels  = parcels || [];

  // Keep normal parcel coloring in cluster mode — pins provide the visual indicator.
  if (map.getLayer('parcels-detail'))   map.setPaintProperty('parcels-detail',   'fill-opacity', detailOpacity());
  if (map.getLayer('parcels-overview')) map.setPaintProperty('parcels-overview', 'fill-opacity', 1.0);

  updateLegend();

  // Remove any previous cluster markers.
  for (const m of clusterMarkers) m.remove();
  clusterMarkers = [];

  // Place a teardrop pin on every cluster parcel.
  placeClusterMarkers(clusterParcels);
}

function placeClusterMarkers(parcels) {
  for (const p of parcels) {
    if (!p.lon || !p.lat) continue;
    const marker = new maplibregl.Marker({ color: '#16a34a', scale: 0.75 })
      .setLngLat([p.lon, p.lat])
      .addTo(map);
    marker.getElement().style.cursor = 'pointer';
    marker.getElement().addEventListener('click', (e) => {
      e.stopPropagation();
      loadParcel(p.county, p.parcel_id);
    });
    clusterMarkers.push(marker);
  }
}

function exitClusterMode() {
  activeClusterId = null;
  clusterParcels  = [];
  for (const m of clusterMarkers) m.remove();
  clusterMarkers = [];
  if (selectedMarker) { selectedMarker.remove(); selectedMarker = null; }
  if (map.getLayer('parcels-detail'))   map.setPaintProperty('parcels-detail',   'fill-opacity', detailOpacity());
  if (map.getLayer('parcels-overview')) map.setPaintProperty('parcels-overview', 'fill-opacity', 1.0);
  updateLegend();
}

// ---------------------------------------------------------------------------
// ?cluster=ID deep link — highlight a cluster on page load
// ---------------------------------------------------------------------------

map.on('load', () => {
  if (!pendingClusterId) return;

  const clusterLoading = document.getElementById('cluster-loading');
  clusterLoading.hidden = false;

  fetch(`/api/owner/${pendingClusterId}`)
    .then(r => r.ok ? r.json() : null)
    .then(async data => {
      clusterLoading.hidden = true;
      if (!data || !data.parcels.length) return;

      const withCoords = data.parcels.filter(p => p.lon && p.lat);

      // Fit map to the cluster's bounding box so the full pin spread is visible.
      if (withCoords.length) {
        const bounds = withCoords.reduce(
          (b, p) => b.extend([p.lon, p.lat]),
          new maplibregl.LngLatBounds([withCoords[0].lon, withCoords[0].lat], [withCoords[0].lon, withCoords[0].lat])
        );
        map.fitBounds(bounds, { padding: 80, maxZoom: 15, duration: 0 });
      }

      const first = data.parcels[0];
      await loadParcel(first.county, first.parcel_id);
      highlightCluster(pendingClusterId, data.parcels);
    })
    .catch(() => { clusterLoading.hidden = true; });
});

// ---------------------------------------------------------------------------
// Address search
// ---------------------------------------------------------------------------

const searchInput   = document.getElementById('search-input');
const searchResults = document.getElementById('search-results');

let searchTimeout = null;
let currentResults = [];
let selectedIndex  = -1;

searchInput.addEventListener('input', () => {
  clearTimeout(searchTimeout);
  const q = searchInput.value.trim();
  if (q.length < 3) { hideResults(); return; }
  searchTimeout = setTimeout(() => fetchSearch(q), 300);
});

searchInput.addEventListener('keydown', (e) => {
  if (searchResults.hidden) return;
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    setSelectedIndex(Math.min(selectedIndex + 1, currentResults.length - 1));
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    setSelectedIndex(Math.max(selectedIndex - 1, 0));
  } else if (e.key === 'Enter' && selectedIndex >= 0) {
    e.preventDefault();
    selectResult(currentResults[selectedIndex]);
  } else if (e.key === 'Escape') {
    hideResults();
  }
});

// Close dropdown when clicking outside
document.addEventListener('click', (e) => {
  if (!e.target.closest('.search-wrapper')) hideResults();
});

async function fetchSearch(q) {
  try {
    const res  = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
    const data = await res.json();
    currentResults = data.results || [];
    renderResults(currentResults);
  } catch {
    hideResults();
  }
}

function renderResults(results) {
  searchResults.innerHTML = '';
  selectedIndex = -1;
  if (!results.length) { hideResults(); return; }

  results.forEach((r, i) => {
    const li = document.createElement('li');
    li.setAttribute('role', 'option');
    li.setAttribute('aria-selected', 'false');
    li.innerHTML = `
      <span class="result-address">${r.fulladdr}</span>
      <span class="result-county">${r.county}</span>
    `;
    li.addEventListener('mousedown', (e) => {
      e.preventDefault(); // don't blur input
      selectResult(r);
    });
    searchResults.appendChild(li);
  });

  searchResults.hidden = false;
}

function setSelectedIndex(i) {
  const items = searchResults.querySelectorAll('li');
  items.forEach((el, idx) => {
    el.setAttribute('aria-selected', idx === i ? 'true' : 'false');
  });
  selectedIndex = i;
}

function hideResults() {
  searchResults.hidden = true;
  searchResults.innerHTML = '';
  currentResults = [];
  selectedIndex = -1;
}

function selectResult(result) {
  searchInput.value = result.fulladdr;
  hideResults();
  map.flyTo({ center: [result.lon, result.lat], zoom: 16, duration: 800 });
  placeMarker(result.lon, result.lat);
  loadParcel(result.county, result.parcel_id);
}

// ---------------------------------------------------------------------------
// Marker
// ---------------------------------------------------------------------------

function placeMarker(lon, lat) {
  if (selectedMarker) selectedMarker.remove();
  selectedMarker = new maplibregl.Marker({ color: '#2563eb' })
    .setLngLat([lon, lat])
    .addTo(map);
}

// ---------------------------------------------------------------------------
// Parcel detail
// ---------------------------------------------------------------------------

const detailPanel      = document.getElementById('detail-panel');
const parcelAddress    = document.getElementById('parcel-address');
const parcelBadges     = document.getElementById('parcel-badges');
const parcelOwnerLine  = document.getElementById('parcel-owner-line');
const parcelMeta       = document.getElementById('parcel-meta');
const permitMeta       = document.getElementById('permit-meta');
const parcelPermits    = document.getElementById('parcel-permits');
const parcelLinks      = document.getElementById('parcel-links');
const ownerProfileLink = document.getElementById('owner-profile-link');
const panelClose       = document.getElementById('panel-close');

// ---------------------------------------------------------------------------
// Georgia property class codes
// NOTE: State of Georgia stratification code used to group like properties for
// analysis. Same codes appear in both Fulton (classcode) and DeKalb (classdscrp).
// Sources:
//   https://www.dekalbcountyga.gov/property-appraisal/appraisal-definitions
//   http://share.myfultoncountyga.us/datashare/fultoncounty/Documents/PropertyClasses.pdf
//   docs/FultonCountyPropertyClasses.pdf (local copy)
// ---------------------------------------------------------------------------
const GA_PROPERTY_CLASS = {
  A1:'Agriculture Improved',          A3:'Agriculture Vacant Lot',
  A4:'Agriculture Small Tract ≤9.99 Acres', A5:'Agriculture Property ≥10.00 Acres',
  A6:'Agriculture Institution',       A9:'Agriculture Outbuilding',
  B1:'Brownfield Improved',           B3:'Brownfield Vacant Lot',
  B4:'Brownfield Small Tract',        B5:'Brownfield Large Tract',
  C1:'Commercial Improved',           C3:'Commercial Vacant Lot',
  C4:'Commercial Small Tract ≤4.99 Acres',  C5:'Commercial Large Tract ≥5.00 Acres',
  C9:'Commercial Outbuilding',
  E0:'Non-Profit Homes for the Aged', E1:'Public Property',
  E2:'Religious Property',            E3:'Charitable Property',
  E4:'Religious Property',            E5:'Non-Profit Hospital',
  E6:'Educational Institution',       E9:'Exempt Outbuilding',
  H1:'Historical Property',           H3:'Historical Vacant Lot',
  H5:'Historical Large Tract',
  I1:'Industrial Improved',           I3:'Industrial Vacant Lot',
  I4:'Industrial Small Tract ≤9.99 Acres',  I5:'Industrial Large Tract ≥10.00 Acres',
  I9:'Industrial Outbuilding',
  J3:'Forest Land Conservation Vacant Lot', J4:'Forest Land Conservation Small Tract',
  J5:'Forest Land Conservation Large Tract',
  P1:'Preferential Assessment',       P3:'Preferential Vacant Lot',
  P4:'Preferential Small Tract',      P5:'Preferential Large Tract',
  Q4:'Qualified Timberland Small Tract',    Q5:'Qualified Timberland Large Tract',
  R1:'Residential Improved',          R3:'Residential Vacant Lot',
  R4:'Residential Small Tract ≤1.99 Acres', R5:'Residential Large Tract ≥2.00 Acres',
  R9:'Residential Outbuilding',
  T1:'Residential Transition Improved',    T3:'Residential Transition Vacant Lot',
  T4:'Residential Transition Small Tract ≤1.99 Acres',
  U1:'Improved Public Utility',       U2:'Utility Operating Property',
  U3:'Utility Vacant Lot',            U4:'Utility Small Tract',
  U5:'Utility Large Tract',           U9:'Public Utility Outbuilding',
  V1:'Conservation Assessment',       V3:'Conservation Vacant Lot',
  V4:'Conservation Small Tract',      V5:'Conservation Large Tract',
};

panelClose.addEventListener('click', closePanel);

async function loadParcel(county, parcelId) {
  try {
    const res  = await fetch(`/api/parcel/${county}/${encodeURIComponent(parcelId)}`);
    if (!res.ok) return;
    const data = await res.json();
    renderParcelPanel(data);
    // Stay in cluster mode when the clicked parcel is part of the active cluster;
    // exit only when navigating to a different owner.
    if (!activeClusterId || data.cluster_id !== activeClusterId) {
      highlightCluster(null);
    }
    highlightParcel(parcelId);
    showPanel();
  } catch {
    // silently ignore — map click on empty tile
  }
}

function renderParcelPanel(p) {
  // Address
  parcelAddress.textContent = p.site_address || p.parcel_id;

  // Badges
  parcelBadges.innerHTML = '';
  if (p.is_corporate)     parcelBadges.innerHTML += '<span class="badge-corporate">CORPORATE</span>';
  if (p.is_institutional) parcelBadges.innerHTML += '<span class="badge-institutional">INSTITUTIONAL</span>';

  // Owner line
  const ownerName = (p.owner_name || '').trim();
  if (p.cluster_id) {
    parcelOwnerLine.innerHTML = `<a href="/owner/${p.cluster_id}/">${escHtml(ownerName)}</a>`;
  } else {
    parcelOwnerLine.textContent = ownerName;
  }

  // Metadata
  const meta = [];

  // County + parcel ID
  meta.push(['County', p.county === 'fulton' ? 'Fulton County' : 'DeKalb County']);
  meta.push(['Parcel ID', p.parcel_id]);

  // Property class — same GA state code in both Fulton (classcode) and DeKalb (classdscrp)
  if (p.property_class) {
    meta.push(['Property class', GA_PROPERTY_CLASS[p.property_class] || p.property_class]);
  }

  // Co-owner (DeKalb ownernme2)
  if (p.owner_name2) meta.push(['Co-owner', p.owner_name2]);

  // Geographic context
  if (p.neighborhood)     meta.push(['Neighborhood', p.neighborhood]);
  if (p.npu)              meta.push(['NPU', p.npu]);
  if (p.council_district) meta.push(['Council', `District ${p.council_district}`]);

  // Physical details
  if (p.land_acres != null) meta.push(['Land', `${Number(p.land_acres).toFixed(2)} acres`]);
  if (p.living_units)       meta.push(['Units', p.living_units]);
  if (p.land_use)           meta.push(['Land use', p.land_use]);

  // Homestead exemption (Fulton only) — excode non-empty = homestead exempt
  if (p.county === 'fulton') {
    meta.push(['Exemption', p.exemption_code ? 'Homestead exempt' : 'Not homestead exempt']);
  }

  // Appraised value (DeKalb only)
  if (p.appraised_value != null) {
    meta.push(['Assessed value', '$' + Number(p.appraised_value).toLocaleString() + ' (DeKalb)']);
  }

  // Zoning / historic / overlay (skip if blank — API returns null when blank)
  if (p.zoning)            meta.push(['Zoning', p.zoning]);
  if (p.historic_district) meta.push(['Historic district', p.historic_district]);
  if (p.overlay_district)  meta.push(['Overlay district', p.overlay_district]);

  parcelMeta.innerHTML = meta.map(([k, v]) =>
    `<dt>${escHtml(k)}</dt><dd>${escHtml(String(v))}</dd>`
  ).join('');

  // Owner mailing address — rendered below the dl, above permits
  const mailAddr = [p.owner_mail_addr1, p.owner_mail_addr2].filter(Boolean);
  const mailBlock = parcelMeta.nextElementSibling?.id === 'owner-mail-addr'
    ? parcelMeta.nextElementSibling
    : (() => { const d = document.createElement('div'); d.id = 'owner-mail-addr'; parcelMeta.after(d); return d; })();
  if (mailAddr.length) {
    mailBlock.innerHTML = `<p class="meta-section-label">Owner mailing address</p>`
      + `<p class="owner-mail">${mailAddr.map(escHtml).join('<br>')}</p>`;
    mailBlock.hidden = false;
  } else {
    mailBlock.hidden = true;
  }

  // Permits
  permitMeta.innerHTML = '';
  if (p.permit_count > 0) {
    const openLabel = p.open_permits > 0 ? `, ${p.open_permits} open` : '';
    parcelPermits.querySelector('summary').textContent = `Building complaints (${p.permit_count}${openLabel})`;
    const rows = [
      ['Total', p.permit_count],
      ['Open', p.open_permits],
    ];
    if (p.last_permit_date) {
      rows.push(['Last activity', fmtDate(p.last_permit_date)]);
    }
    permitMeta.innerHTML = rows.map(([k, v]) =>
      `<dt>${escHtml(k)}</dt><dd>${escHtml(String(v))}</dd>`
    ).join('');
    parcelPermits.hidden = false;
    // Auto-expand if there are open complaints
    parcelPermits.open = p.open_permits > 0;
  } else {
    parcelPermits.querySelector('summary').textContent = 'Building complaints';
    parcelPermits.hidden = true;
    parcelPermits.open = false;
  }

  // External links
  renderParcelLinks(p);

  // Owner profile link
  if (p.cluster_id) {
    ownerProfileLink.href = `/owner/${p.cluster_id}/`;
    ownerProfileLink.hidden = false;
  } else {
    ownerProfileLink.hidden = true;
  }
}

function renderParcelLinks(p) {
  const items = [];

  // qPublic — county-specific AppID
  const qpAppId = p.county === 'fulton' ? '936' : '994';
  const qpUrl = 'https://qpublic.schneidercorp.com/Application.aspx'
    + `?AppID=${qpAppId}&PageTypeID=4&KeyValue=${encodeURIComponent(p.parcel_id)}`;
  items.push(['qPublic record', qpUrl]);

  // GA SOS direct link — only if matched entity has a sos_business_id
  if (p.sos_business_id) {
    items.push(['GA SOS filing', `https://ecorp.sos.ga.gov/BusinessSearch/BusinessInformation?businessId=${encodeURIComponent(p.sos_business_id)}`]);
  }

  // Google Maps — property address (labeled "Street View" per plan)
  if (p.site_address) {
    items.push(['Street View', `https://maps.google.com/?q=${encodeURIComponent(p.site_address)}`]);
  }

  // Google Maps — owner mailing address
  const ownerMailStr = [p.owner_mail_addr1, p.owner_mail_addr2].filter(Boolean).join(', ');
  if (ownerMailStr) {
    items.push(['Owner address map', `https://maps.google.com/?q=${encodeURIComponent(ownerMailStr)}`]);
  }

  // OpenCorporates — only for corporate owners
  if (p.is_corporate && p.owner_name) {
    items.push(['OpenCorporates search', `https://opencorporates.com/companies?utf8=%E2%9C%93&q=${encodeURIComponent(p.owner_name)}&jurisdiction_code=us_ga`]);
  }

  parcelLinks.innerHTML = items.length
    ? `<p class="meta-section-label">External records</p>`
      + items.map(([label, url]) =>
          `<a class="ext-link" href="${escHtml(url)}" target="_blank" rel="noopener noreferrer">${escHtml(label)} ↗</a>`
        ).join('')
    : '';
}

function showPanel()  { detailPanel.hidden = false; }
function closePanel() { detailPanel.hidden = true; highlightParcel(null); highlightCluster(null); }

// ---------------------------------------------------------------------------
// Area filter — neighborhood / NPU / council district
// ---------------------------------------------------------------------------

const filterToggle   = document.getElementById('filter-toggle');
const filterPanel    = document.getElementById('filter-panel');
const filterNbInput  = document.getElementById('filter-neighborhood');
const filterNbList   = document.getElementById('filter-neighborhood-results');
const filterNpuSel   = document.getElementById('filter-npu');
const filterCouncil  = document.getElementById('filter-council');
const filterActive   = document.getElementById('filter-active');
const filterLabel    = document.getElementById('filter-active-label');
const filterClear    = document.getElementById('filter-clear');

const geoCache = {};  // keyed by 'neighborhoods' | 'npu' | 'council'

async function loadGeoData() {
  if (geoCache.neighborhoods) return;
  const [nb, npu, council] = await Promise.all([
    fetch('/geojson/neighborhoods.json').then(r => r.json()),
    fetch('/geojson/npu.json').then(r => r.json()),
    fetch('/geojson/council_districts.json').then(r => r.json()),
  ]);
  geoCache.neighborhoods = nb.features;
  geoCache.npu           = npu.features;
  geoCache.council       = council.features;

  npu.features
    .map(f => f.properties.NAME).sort()
    .forEach(n => filterNpuSel.append(Object.assign(document.createElement('option'), { value: n, textContent: `NPU ${n}` })));

  council.features
    .map(f => f.properties.NAME).sort((a, b) => +a - +b)
    .forEach(n => filterCouncil.append(Object.assign(document.createElement('option'), { value: n, textContent: `District ${n}` })));
}

// Build a world rectangle with the selected geometry cut out as a hole.
// Rendering this as a fill layer dims everything outside the selection.
function makeOutsideMask(geometry) {
  const worldRing = [[-180,-90],[180,-90],[180,90],[-180,90],[-180,-90]];
  const holeRings = geometry.type === 'Polygon'
    ? geometry.coordinates
    : geometry.coordinates.flat(); // MultiPolygon → flatten polygon rings
  return {
    type: 'Feature',
    geometry: { type: 'Polygon', coordinates: [worldRing, ...holeRings] },
  };
}

function geomBounds(geometry) {
  const coords = geometry.type === 'Polygon'
    ? geometry.coordinates.flat()
    : geometry.coordinates.flat(2);
  const lons = coords.map(c => c[0]);
  const lats = coords.map(c => c[1]);
  return [[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]];
}

function setAreaFilter(label, geometry) {
  activeAreaFilter = { label, geometry };
  if (map.getSource('area-overlay'))
    map.getSource('area-overlay').setData({ type: 'FeatureCollection', features: [makeOutsideMask(geometry)] });
  filterLabel.textContent = label;
  filterActive.hidden = false;
  filterToggle.classList.add('active');
  filterPanel.hidden = true;
  map.fitBounds(geomBounds(geometry), { padding: 40, maxZoom: 15 });
}

function clearAreaFilter() {
  activeAreaFilter = null;
  if (map.getSource('area-overlay'))
    map.getSource('area-overlay').setData({ type: 'FeatureCollection', features: [] });
  filterActive.hidden = true;
  filterToggle.classList.remove('active');
  filterNbInput.value  = '';
  filterNpuSel.value   = '';
  filterCouncil.value  = '';
  filterNbList.hidden  = true;
}

// Filter toggle open/close
filterToggle.addEventListener('click', async () => {
  const opening = filterPanel.hidden;
  filterPanel.hidden = !opening;
  if (opening) await loadGeoData();
});

// Close filter panel when clicking outside
document.addEventListener('click', (e) => {
  if (!e.target.closest('.filter-wrapper')) filterPanel.hidden = true;
});

// Neighborhood text search
let nbTimeout = null;
filterNbInput.addEventListener('input', () => {
  clearTimeout(nbTimeout);
  const q = filterNbInput.value.trim().toLowerCase();
  if (!q || !geoCache.neighborhoods) { filterNbList.hidden = true; return; }
  nbTimeout = setTimeout(() => {
    const matches = geoCache.neighborhoods
      .filter(f => f.properties.NAME.toLowerCase().includes(q))
      .slice(0, 10);
    if (!matches.length) { filterNbList.hidden = true; return; }
    filterNbList.innerHTML = matches.map(f =>
      `<li data-name="${escHtml(f.properties.NAME)}">${escHtml(f.properties.NAME)}</li>`
    ).join('');
    filterNbList.hidden = false;
  }, 150);
});

filterNbList.addEventListener('click', (e) => {
  const li = e.target.closest('li');
  if (!li) return;
  const name = li.dataset.name;
  const feat = geoCache.neighborhoods.find(f => f.properties.NAME === name);
  if (!feat) return;
  filterNbInput.value = name;
  filterNbList.hidden = true;
  filterNpuSel.value  = '';
  filterCouncil.value = '';
  setAreaFilter(`Neighborhood: ${name}`, feat.geometry);
});

filterNbInput.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') filterNbList.hidden = true;
});

// NPU select
filterNpuSel.addEventListener('change', () => {
  const val = filterNpuSel.value;
  if (!val) { clearAreaFilter(); return; }
  const feat = geoCache.npu.find(f => f.properties.NAME === val);
  if (!feat) return;
  filterNbInput.value  = '';
  filterCouncil.value  = '';
  setAreaFilter(`NPU ${val}`, feat.geometry);
});

// Council select
filterCouncil.addEventListener('change', () => {
  const val = filterCouncil.value;
  if (!val) { clearAreaFilter(); return; }
  const feat = geoCache.council.find(f => f.properties.NAME === val);
  if (!feat) return;
  filterNbInput.value = '';
  filterNpuSel.value  = '';
  setAreaFilter(`Council District ${val}`, feat.geometry);
});

// Clear button
filterClear.addEventListener('click', clearAreaFilter);

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function escHtml(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function fmtDate(iso) {
  return new Date(iso).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
}
