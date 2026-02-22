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
  center: [-84.388, 33.749],  // Atlanta
  zoom: 12,
});

map.addControl(new maplibregl.NavigationControl(), 'top-left');

let selectedMarker  = null;
let activeClusterId = null;   // cluster currently in "focus" mode
let clusterMarkers  = [];     // teardrop pins placed for each cluster parcel (z13+ only)
let clusterParcels  = [];     // parcel list for the active cluster (for zoom-toggling markers)

// Add parcel tile layer once map is ready (only if URL is configured)
map.on('load', () => {
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
  map.on('mouseleave', 'parcels-overview', () => { map.getCanvas().style.cursor = ''; });
  map.on('mouseleave', 'parcels-detail',   () => { map.getCanvas().style.cursor = ''; });

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
const ownerProfileLink = document.getElementById('owner-profile-link');
const panelClose       = document.getElementById('panel-close');

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
  if (p.is_corporate)    parcelBadges.innerHTML += '<span class="badge-corporate">CORPORATE</span>';
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
  if (p.neighborhood)      meta.push(['Neighborhood', p.neighborhood]);
  if (p.npu)               meta.push(['NPU', p.npu]);
  if (p.council_district)  meta.push(['Council', `District ${p.council_district}`]);
  if (p.land_acres != null) meta.push(['Land', `${Number(p.land_acres).toFixed(2)} acres`]);
  if (p.living_units)      meta.push(['Units', p.living_units]);
  if (p.land_use)          meta.push(['Land use', p.land_use]);

  parcelMeta.innerHTML = meta.map(([k, v]) =>
    `<dt>${escHtml(k)}</dt><dd>${escHtml(String(v))}</dd>`
  ).join('');

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

  // Owner profile link
  if (p.cluster_id) {
    ownerProfileLink.href = `/owner/${p.cluster_id}/`;
    ownerProfileLink.hidden = false;
  } else {
    ownerProfileLink.hidden = true;
  }
}

function showPanel()  { detailPanel.hidden = false; }
function closePanel() { detailPanel.hidden = true; highlightParcel(null); highlightCluster(null); }

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function escHtml(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function fmtDate(iso) {
  return new Date(iso).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
}
