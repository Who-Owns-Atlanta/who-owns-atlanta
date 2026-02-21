// ---------------------------------------------------------------------------
// Who Owns Atlanta? — map page
// ---------------------------------------------------------------------------

// Tile URL: relative path in dev (served by local nginx), CloudFront in prod.
// Set PROD_TILES_URL once the CloudFront distribution is live.
const PROD_TILES_URL = null; // e.g. "https://tiles.who-owns-atlanta.org/tiles/{z}/{x}/{y}.pbf"
const DEV_TILES_URL  = `${window.location.origin}/tiles/{z}/{x}/{y}.pbf`;

const PARCEL_TILES_URL = (window.location.hostname === "who-owns-atlanta.local")
  ? DEV_TILES_URL
  : PROD_TILES_URL;

// ---------------------------------------------------------------------------
// Map init
// ---------------------------------------------------------------------------

const map = new maplibregl.Map({
  container: 'map',
  style: 'https://tiles.openfreemap.org/styles/liberty',
  center: [-84.388, 33.749],  // Atlanta
  zoom: 12,
});

map.addControl(new maplibregl.NavigationControl(), 'top-left');

let selectedMarker = null;

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
      'fill-color': [
        'case',
        ['get', 'is_corporate'],    'rgba(220, 38, 38, 0.6)',
        ['get', 'is_institutional'], 'rgba(217, 119, 6, 0.6)',
        'rgba(148, 163, 184, 0.4)',
      ],
      'fill-outline-color': 'rgba(0,0,0,0.1)',
    },
  });

  // Zoom 13+: color by cluster_id (deterministic hue)
  map.addLayer({
    id: 'parcels-detail',
    type: 'fill',
    source: 'parcels',
    'source-layer': 'parcels',
    minzoom: 13,
    paint: {
      'fill-color': clusterColor(),
      'fill-opacity': 0.65,
      'fill-outline-color': 'rgba(0,0,0,0.15)',
    },
  });

  // Selected parcel highlight layer
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
});

// Deterministic cluster → HSL color expression for MapLibre
function clusterColor() {
  // Produces a stable hue from cluster_id using modulo over a golden-ratio spread.
  // Uses to-color + concat to build a CSS hsl() string, avoiding MapLibre 4's
  // strict type checking on the ['hsl', expr, ...] form.
  return [
    'to-color',
    ['concat',
      'hsl(',
      ['to-string', ['%', ['*', ['coalesce', ['get', 'cluster_id'], 0], 137], 360]],
      ',65%,55%)',
    ],
    'hsl(0,0%,70%)',  // fallback for null cluster_id
  ];
}

// Highlight selected parcel in tile layer
function highlightParcel(parcelId) {
  if (!PARCEL_TILES_URL) return;
  if (map.getLayer('parcels-selected')) {
    map.setFilter('parcels-selected', ['==', 'parcel_id', parcelId || '']);
  }
}

// Highlight all parcels in a cluster
function highlightCluster(clusterId) {
  if (!PARCEL_TILES_URL) return;
  if (map.getLayer('parcels-selected')) {
    if (clusterId) {
      map.setFilter('parcels-selected', ['==', ['get', 'cluster_id'], clusterId]);
    } else {
      map.setFilter('parcels-selected', ['==', 'parcel_id', '']);
    }
  }
}

// ---------------------------------------------------------------------------
// ?cluster=ID deep link — highlight a cluster on page load
// ---------------------------------------------------------------------------

map.on('load', () => {
  const clusterId = parseInt(new URLSearchParams(window.location.search).get('cluster'));
  if (!clusterId) return;

  fetch(`/api/owner/${clusterId}`)
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      if (!data || !data.parcels.length) return;

      // Compute centroid of all parcel coordinates
      const lats = data.parcels.map(p => p.lat).filter(Boolean);
      const lons = data.parcels.map(p => p.lon).filter(Boolean);
      const centerLat = lats.reduce((a, b) => a + b, 0) / lats.length;
      const centerLon = lons.reduce((a, b) => a + b, 0) / lons.length;

      map.flyTo({ center: [centerLon, centerLat], zoom: 14, duration: 800 });
      highlightCluster(clusterId);

      // Load detail panel for first parcel so user sees context
      const first = data.parcels[0];
      loadParcel(first.county, first.parcel_id);
    })
    .catch(() => {});
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
