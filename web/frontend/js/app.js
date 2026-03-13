// ---------------------------------------------------------------------------
// Who Owns Atlanta? — map page
// ---------------------------------------------------------------------------

// Tile URL: relative path in dev (served by local nginx), CloudFront in prod.
// Set PROD_TILES_URL once the CloudFront distribution is live.
const PROD_TILES_URL = "https://tiles.who-owns-atlanta.org/tiles/{z}/{x}/{y}.pbf?v=202603B";
const DEV_TILES_URL  = `${window.location.origin}/tiles/{z}/{x}/{y}.pbf`;

const DEV_HOSTNAMES = ["who-owns-atlanta.local", "who-owns-atlanta.lan", "localhost"];
const PARCEL_TILES_URL = DEV_HOSTNAMES.includes(window.location.hostname)
  ? DEV_TILES_URL
  : PROD_TILES_URL;

// ---------------------------------------------------------------------------
// Map init
// ---------------------------------------------------------------------------

// Detect ?cluster=ID before map init so we can suppress the parcel flash.
const _initParams     = new URLSearchParams(window.location.search);
let pendingClusterId  = parseInt(_initParams.get('cluster')) || null;
const pendingGeoType  = _initParams.get('geo')  || null;  // 'neighborhood' | 'npu' | 'council'
const pendingGeoArea  = _initParams.get('area') || null;  // raw GeoJSON NAME value
const pendingParcel   = _initParams.get('parcel') || null; // 'county/parcel_id'
const pendingHomeType = _initParams.get('hometype') || null;

const map = new maplibregl.Map({
  container: 'map',
  style: 'https://tiles.openfreemap.org/styles/liberty',
  center: [-84.388, 33.749],  // Fallback center
  zoom: 10,                   // Initial zoom while loading
  minZoom: 10,                // Keeps fitBounds from landing below our tile coverage
});

const ATLANTA_BOUNDS = [[-84.551, 33.637], [-84.289, 33.887]];

map.addControl(new maplibregl.NavigationControl(), 'top-left');

// ---------------------------------------------------------------------------
// Locate Control (GPS)
// ---------------------------------------------------------------------------

class LocateControl {
  onAdd(map) {
    this._map = map;
    this._container = document.createElement('div');
    this._container.className = 'maplibregl-ctrl maplibregl-ctrl-group';

    this._button = document.createElement('button');
    this._button.className = 'maplibregl-ctrl-locate';
    this._button.type = 'button';
    this._button.title = 'Zoom to your location';
    this._button.innerHTML = `
      <svg width="18" height="18" viewBox="0 0 24 24">
        <path d="M12 8c-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4-1.79-4-4-4zm8.94 3c-.46-4.17-3.77-7.48-7.94-7.94V1h-2v2.06C6.83 3.52 3.52 6.83 3.06 11H1v2h2.06c.46 4.17 3.77 7.48 7.94 7.94V23h2v-2.06c4.17-.46 7.48-3.77 7.94-7.94H23v-2h-2.06zM12 19c-3.87 0-7-3.13-7-7s3.13-7 7-7 7 3.13 7 7-3.13 7-7 7z"/>
      </svg>
    `;

    this._button.onclick = () => this.locate();

    this._container.appendChild(this._button);
    return this._container;
  }

  onRemove() {
    this._container.parentNode.removeChild(this._container);
    this._map = undefined;
  }

  locate() {
    if (this._button.classList.contains('loading')) return;

    this._button.classList.add('loading');

    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        const { longitude, latitude } = pos.coords;
        const point = [longitude, latitude];

        const inCity = await this.checkInCity(point);

        if (inCity) {
          this._map.flyTo({ center: point, zoom: 16 });
          this._button.classList.add('active');
        } else {
          alert("Your location is outside the Atlanta city limits.");
          this._button.classList.remove('active');
        }
        this._button.classList.remove('loading');
      },
      (err) => {
        console.warn("Geolocation error:", err);
        // Error codes: 1: Permission denied, 2: Position unavailable, 3: Timeout
        if (err.code === 1) alert("Location access was denied.");
        else alert("Unable to retrieve your location.");
        this._button.classList.remove('loading');
      },
      { enableHighAccuracy: true, timeout: 8000 }
    );
  }

  async checkInCity(point) {
    if (!this._cityLimits) {
      try {
        const res = await fetch('/geojson/atlanta_city_limits.json');
        const data = await res.json();
        this._cityLimits = data.features[0].geometry;
      } catch (e) {
        console.error("Failed to load city limits for check", e);
        return false;
      }
    }
    return isPointInGeometry(point, this._cityLimits);
  }
}

map.addControl(new LocateControl(), 'top-left');

let selectedMarker  = null;
let searchMarker    = null;
let activeClusterId = null;   // cluster currently in "focus" mode
let clusterMarkers  = [];     // teardrop DOM pins (small clusters only, < CLUSTER_PIN_THRESHOLD)
let clusterParcels  = [];     // parcel list for the active cluster
let _clusterSourceHandlers = [];  // event handlers registered for the GeoJSON cluster source

const CLUSTER_PIN_THRESHOLD = 50; // use GeoJSON cluster source above this count
let activeAreaFilter = null;  // { label, geometry } when an area filter is active
let mapMode = 'ownership';    // 'ownership' | 'income' | 'poverty' | 'renter'
const hoverPopup    = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 12 });

// Add parcel tile layer once map is ready (only if URL is configured)
map.on('load', () => {
  if (!pendingClusterId && !pendingGeoType) {
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

  // Neighborhood demographics choropleth (hidden by default; shown in demog modes)
  map.addSource('nbhd-demo', {
    type: 'geojson',
    data: '/geojson/neighborhood_demographics.json',
  });
  map.addLayer({
    id: 'nbhd-choropleth',
    type: 'fill',
    source: 'nbhd-demo',
    layout: { visibility: 'none' },
    paint: {
      'fill-color': CHORO_COLORS.income,
      'fill-opacity': 0.80,
      'fill-outline-color': 'rgba(255,255,255,0.4)',
    },
  }, 'parcels-overview'); // render below parcel layers

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

  // Pre-initialize cluster source and layers (empty, hidden).
  // Adding the cluster: true GeoJSON source here — during map load — ensures
  // the tile pipeline is warmed up before cluster mode is entered.  When
  // enterClusterMode runs, it just calls setData + toggles visibility.
  map.addSource('cluster-source', {
    type: 'geojson',
    data: { type: 'FeatureCollection', features: [] },
    cluster: true,
    clusterMaxZoom: 13,
    clusterRadius: 50,
  });
  map.addLayer({
    id: 'cluster-circles',
    type: 'circle',
    source: 'cluster-source',
    filter: ['has', 'point_count'],
    layout: { visibility: 'none' },
    paint: {
      'circle-color': '#16a34a',
      'circle-radius': ['step', ['coalesce', ['to-number', ['get', 'point_count']], 0], 18, 10, 26, 50, 34, 200, 44],
      'circle-opacity': 0.85,
      'circle-stroke-width': 2,
      'circle-stroke-color': '#fff',
    },
  });
  map.addLayer({
    id: 'cluster-count',
    type: 'symbol',
    source: 'cluster-source',
    filter: ['has', 'point_count'],
    layout: {
      visibility: 'none',
      'text-field': '{point_count_abbreviated}',
      'text-font': ['Noto Sans Bold'],
      'text-size': 13,
    },
    paint: { 'text-color': '#fff' },
  });
  map.addLayer({
    id: 'cluster-unclustered',
    type: 'circle',
    source: 'cluster-source',
    filter: ['!', ['has', 'point_count']],
    layout: { visibility: 'none' },
    paint: {
      'circle-color': '#16a34a',
      'circle-radius': 7,
      'circle-stroke-width': 2,
      'circle-stroke-color': '#fff',
      'circle-opacity': 0.9,
    },
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
    if (activeClusterId || pendingClusterId) return;
    const p = e.features[0].properties;
    if (!p.parcel_id) return;
    const addr = escHtml(p.site_address || p.parcel_id);
    const unitTag = p.is_condo ? ` <span class="unit-count">(${p.unit_count} units)</span>` : '';
    const ownerLine = p.is_condo ? '' : `<div class="hover-owner">${escHtml(p.owner_name || '')}</div>`;
    hoverPopup
      .setLngLat(e.lngLat)
      .setHTML(`<div class="hover-tip"><div class="hover-address">${addr}${unitTag}</div>${ownerLine}</div>`)
      .addTo(map);
  });

  // Neighborhood hover tooltip — only in demographic modes
  map.on('mousemove', 'nbhd-choropleth', (e) => {
    if (mapMode === 'ownership') return;
    if (map.getZoom() >= 13) return; // parcel detail takes over at z13+
    const p = e.features[0].properties;
    const value = mapMode === 'income'
      ? `Median income: $${p.income.toLocaleString()}`
      : mapMode === 'poverty'
      ? `${p.poverty_pct}% below poverty line`
      : `${p.renter_pct}% renter-occupied`;
    hoverPopup
      .setLngLat(e.lngLat)
      .setHTML(`<div class="hover-tip"><div class="hover-address">${escHtml(p.name)}</div><div class="hover-owner">${value}</div></div>`)
      .addTo(map);
    map.getCanvas().style.cursor = 'default';
  });
  map.on('mouseleave', 'nbhd-choropleth', () => {
    if (mapMode !== 'ownership') { hoverPopup.remove(); map.getCanvas().style.cursor = ''; }
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

function isCondo() {
  return [
    'any',
    ['to-boolean', ['get', 'is_condo']],
    ['>', ['to-number', ['coalesce', ['get', 'unit_count'], 0]], 1]
  ];
}

function clusterColor() {
  return [
    'case',
    isCondo(),                                          '#8b5cf6', // violet — condo building
    // cluster_size < 2 (single owner, or no cluster): gray
    ['<', ['to-number', ['coalesce', ['get', 'cluster_size'], 0]], 2], '#94a3b8',
    ['to-boolean', ['get', 'is_corporate']],            '#dc2626', // red    — corporate
    ['to-boolean', ['get', 'is_institutional']],        '#d97706', // amber  — institutional
                                                        '#3b82f6', // blue   — individual w/ portfolio
  ];
}

// Opacity for parcels-detail in normal mode: darker = larger portfolio.
function detailOpacity() {
  return [
    'case',
    isCondo(), 0.85,
    [
      'step', ['to-number', ['coalesce', ['get', 'cluster_size'], 0]],
      0.40,        // default: 0–1 parcels (single owner, also gray)
      2,   0.55,   //  2–9 parcels
      10,  0.70,   // 10–49 parcels
      50,  0.90,   // 50+ parcels
    ]
  ];
}

// Default fill-color expression for the overview layer (mirrored here so
// exitClusterMode() can restore it without re-reading paint state).
const OVERVIEW_COLOR = [
  'case',
  isCondo(),                                   'rgba(139, 92, 246, 0.8)', // violet — condo
  ['to-boolean', ['get', 'is_corporate']],     'rgba(220, 38, 38, 0.5)',  // red    — corporate
  ['to-boolean', ['get', 'is_institutional']], 'rgba(217, 119, 6, 0.5)',  // amber  — institutional
  ['has', 'cluster_id'],                       'rgba(59, 130, 246, 0.5)', // blue   — individual portfolio (approx)
  'rgba(148, 163, 184, 0.3)',                                             // gray   — default
];

// Choropleth color expressions for demographic modes.
// Each interpolates a property from the neighborhood_demographics.json features.
const CHORO_COLORS = {
  income:  ['interpolate', ['linear'], ['get', 'income'],
              20000, '#ffffcc', 45000, '#a1dab4', 68000, '#41b6c4', 120000, '#2c7fb8', 200001, '#253494'],
  poverty: ['interpolate', ['linear'], ['get', 'poverty_pct'],
              0, '#ffffb2', 5, '#fecc5c', 14, '#fd8d3c', 30, '#e31a1c', 50, '#800026'],
  renter:  ['interpolate', ['linear'], ['get', 'renter_pct'],
              5, '#f7f4f9', 26, '#d4b9da', 50, '#9e9ac8', 66, '#6a51a3', 90, '#3f007d'],
};

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
  if (shape === 'dot') {
    // Filled circle, matching the GeoJSON cluster unclustered points
    return `<div class="legend-item">` +
      `<svg width="13" height="13" viewBox="0 0 13 13" style="flex-shrink:0">` +
      `<circle cx="6.5" cy="6.5" r="5" fill="${color}" stroke="#fff" stroke-width="1.5"/>` +
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

const LEGEND_TABS = [
  { key: 'ownership', label: 'Ownership' },
  { key: 'income',    label: 'Income' },
  { key: 'poverty',   label: 'Poverty' },
  { key: 'renter',    label: 'Renter %' },
];

const CHORO_META = {
  income:  { title: 'Median household income', labels: ['$20k', '$68k', '$200k+'],
             stops: ['#ffffcc', '#41b6c4', '#253494'] },
  poverty: { title: 'Below poverty line',       labels: ['0%', '14%', '50%+'],
             stops: ['#ffffb2', '#fd8d3c', '#800026'] },
  renter:  { title: 'Renter-occupied housing',  labels: ['5%', '50%', '90%+'],
             stops: ['#f7f4f9', '#9e9ac8', '#3f007d'] },
};

function updateLegend() {
  const legend = document.getElementById('map-legend');
  legend.hidden = false;

  const tabsHtml = `<div class="legend-tabs">${
    LEGEND_TABS.map(t =>
      `<button class="legend-tab${mapMode === t.key ? ' active' : ''}" data-mode="${t.key}">${t.label}</button>`
    ).join('')
  }</div>`;

  let contentHtml = '';
  if (mapMode === 'ownership') {
    if (map.getZoom() >= 13) {
      contentHtml =
        swatch('#dc2626', 'Corporate') +
        swatch('#d97706', 'Institutional') +
        swatch('#3b82f6', 'Individual portfolio') +
        swatch('#8b5cf6', 'Condo building') +
        swatch('#94a3b8', 'Single owner');
    } else {
      contentHtml =
        swatch('rgba(220,38,38,0.8)',  'Corporate') +
        swatch('rgba(217,119,6,0.8)',  'Institutional') +
        swatch('rgba(139,92,246,0.8)', 'Condo building') +
        swatch('rgba(148,163,184,0.6)', 'Other');
    }
    if (activeClusterId) {
      const isLarge = clusterParcels.filter(p => p.lon && p.lat).length >= CLUSTER_PIN_THRESHOLD;
      contentHtml += isLarge
        ? swatch('#16a34a', 'In cluster', 'dot')
        : swatch('#16a34a', 'In cluster', 'pin');
    }
  } else {
    const m = CHORO_META[mapMode];
    const gradient = m.stops.map((c, i) => `${c} ${i * 50}%`).join(', ');
    contentHtml = `
      <div class="legend-ramp-title">${m.title}</div>
      <div class="legend-ramp-bar" style="background:linear-gradient(to right,${gradient})"></div>
      <div class="legend-ramp-labels">${m.labels.map(l => `<span>${l}</span>`).join('')}</div>`;
  }
  contentHtml += swatch(null, 'City limits', 'boundary');

  legend.innerHTML = tabsHtml + contentHtml;
  legend.querySelectorAll('.legend-tab').forEach(btn => {
    btn.addEventListener('click', () => setMapMode(btn.dataset.mode));
  });
}

// Choropleth opacity depends on both map mode and whether an area filter is active.
// With an area filter the choropleth stays visible at all zooms so the neighborhood
// context isn't lost when drilling in. Without a filter it fades out by z13 so
// parcel ownership colors take over cleanly.
function updateChoroOpacity() {
  if (mapMode === 'ownership') return;
  const opacityExpr = activeAreaFilter
    ? ['interpolate', ['linear'], ['zoom'], 10, 0.80, 13, 0.55, 15, 0.45]
    : ['interpolate', ['linear'], ['zoom'], 10, 0.80, 12, 0.65, 13, 0.0];
  map.setPaintProperty('nbhd-choropleth', 'fill-opacity', opacityExpr);
}

function setMapMode(mode) {
  mapMode = mode;
  const isDemog = mode !== 'ownership';

  // Overview layer (z10–12): hide ownership colors in demog mode so choropleth shows cleanly
  map.setPaintProperty('parcels-overview', 'fill-color',
    isDemog ? 'rgba(0,0,0,0)' : OVERVIEW_COLOR);

  // Detail layer (z13+): always show ownership colors — choropleth fades out by z13 anyway
  map.setPaintProperty('parcels-detail', 'fill-color', clusterColor());
  map.setPaintProperty('parcels-detail', 'fill-opacity', detailOpacity());

  if (isDemog) {
    map.setPaintProperty('nbhd-choropleth', 'fill-color', CHORO_COLORS[mode]);
    map.setLayoutProperty('nbhd-choropleth', 'visibility', 'visible');
    updateChoroOpacity();
  } else {
    map.setLayoutProperty('nbhd-choropleth', 'visibility', 'none');
  }
  updateLegend();
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
  hoverPopup.remove();

  if (map.getLayer('parcels-detail'))   map.setPaintProperty('parcels-detail',   'fill-opacity', detailOpacity());
  if (map.getLayer('parcels-overview')) map.setPaintProperty('parcels-overview', 'fill-opacity', 1.0);

  // Clear any previous cluster visualization.
  removeClusterSource();
  for (const m of clusterMarkers) m.remove();
  clusterMarkers = [];

  const withCoords = clusterParcels.filter(p => p.lon && p.lat);
  if (withCoords.length >= CLUSTER_PIN_THRESHOLD) {
    addClusterSource(withCoords);
  } else {
    placeClusterMarkers(clusterParcels);
  }

  updateLegend();
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
  removeClusterSource();
  if (selectedMarker) { selectedMarker.remove(); selectedMarker = null; }
  if (map.getLayer('parcels-detail'))   map.setPaintProperty('parcels-detail',   'fill-opacity', detailOpacity());
  if (map.getLayer('parcels-overview')) map.setPaintProperty('parcels-overview', 'fill-opacity', 1.0);
  updateLegend();
}

// ---------------------------------------------------------------------------
// GeoJSON cluster source — used for large clusters (≥ CLUSTER_PIN_THRESHOLD)
// ---------------------------------------------------------------------------

function addClusterSource(parcels) {
  const features = parcels.map(p => ({
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [p.lon, p.lat] },
    properties: { county: p.county, parcel_id: p.parcel_id },
  }));

  // Source and layers are pre-initialized (hidden, empty) during map load.
  // Set the data, make layers visible, then repaint once the source tiles
  // are ready to ensure the circles render on the first frame.
  map.getSource('cluster-source').setData({ type: 'FeatureCollection', features });
  map.setLayoutProperty('cluster-circles',     'visibility', 'visible');
  map.setLayoutProperty('cluster-count',       'visibility', 'visible');
  map.setLayoutProperty('cluster-unclustered', 'visibility', 'visible');
  map.once('sourcedata', (e) => {
    if (e.sourceId === 'cluster-source' && e.isSourceLoaded) map.triggerRepaint();
  });

  // Click cluster circle → zoom in to expand it.
  const onCircleClick = async (e) => {
    const feats = map.queryRenderedFeatures(e.point, { layers: ['cluster-circles'] });
    if (!feats.length) return;
    const srcClusterId = feats[0].properties.cluster_id;
    try {
      const zoom = await map.getSource('cluster-source').getClusterExpansionZoom(srcClusterId);
      map.easeTo({ center: feats[0].geometry.coordinates, zoom });
    } catch { /* source cleared */ }
  };

  // Click individual point → load parcel.
  const onPointClick = (e) => {
    const props = e.features[0].properties;
    loadParcel(props.county, props.parcel_id);
  };

  const onCircleEnter = () => { map.getCanvas().style.cursor = 'pointer'; };
  const onCircleLeave = () => { map.getCanvas().style.cursor = ''; };
  const onPointEnter  = () => { map.getCanvas().style.cursor = 'pointer'; };
  const onPointLeave  = () => { map.getCanvas().style.cursor = ''; };

  map.on('click',      'cluster-circles',     onCircleClick);
  map.on('click',      'cluster-unclustered', onPointClick);
  map.on('mouseenter', 'cluster-circles',     onCircleEnter);
  map.on('mouseleave', 'cluster-circles',     onCircleLeave);
  map.on('mouseenter', 'cluster-unclustered', onPointEnter);
  map.on('mouseleave', 'cluster-unclustered', onPointLeave);

  _clusterSourceHandlers = [
    { event: 'click',      layer: 'cluster-circles',     fn: onCircleClick },
    { event: 'click',      layer: 'cluster-unclustered', fn: onPointClick  },
    { event: 'mouseenter', layer: 'cluster-circles',     fn: onCircleEnter },
    { event: 'mouseleave', layer: 'cluster-circles',     fn: onCircleLeave },
    { event: 'mouseenter', layer: 'cluster-unclustered', fn: onPointEnter  },
    { event: 'mouseleave', layer: 'cluster-unclustered', fn: onPointLeave  },
  ];
}

function removeClusterSource() {
  for (const { event, layer, fn } of _clusterSourceHandlers) {
    map.off(event, layer, fn);
  }
  _clusterSourceHandlers = [];
  if (!map.getSource('cluster-source')) return;
  map.setLayoutProperty('cluster-circles',     'visibility', 'none');
  map.setLayoutProperty('cluster-count',       'visibility', 'none');
  map.setLayoutProperty('cluster-unclustered', 'visibility', 'none');
  map.getSource('cluster-source').setData({ type: 'FeatureCollection', features: [] });
}

// ---------------------------------------------------------------------------
// ?cluster=ID deep link — highlight a cluster on page load
// ---------------------------------------------------------------------------

map.on('load', () => {
  if (!pendingClusterId) return;

  const clusterLoading = document.getElementById('cluster-loading');
  clusterLoading.hidden = false;

  const clusterToLoad = pendingClusterId;

  fetch(`/api/owner/${clusterToLoad}`)
    .then(r => r.ok ? r.json() : null)
    .then(async data => {
      if (!data || !data.parcels.length) {
        clusterLoading.hidden = true;
        pendingClusterId = null;
        return;
      }

      const withCoords = data.parcels.filter(p => p.lon && p.lat);

      // Fit map to the cluster's bounding box — unless a geo area deep link is
      // also present, in which case the geo handler owns the viewport.
      if (withCoords.length && !pendingGeoType) {
        const bounds = withCoords.reduce(
          (b, p) => b.extend([p.lon, p.lat]),
          new maplibregl.LngLatBounds([withCoords[0].lon, withCoords[0].lat], [withCoords[0].lon, withCoords[0].lat])
        );
        map.fitBounds(bounds, { padding: 80, maxZoom: 15, duration: 0 });
      }

      const first = data.parcels[0];
      await loadParcel(first.county, first.parcel_id);
      highlightCluster(clusterToLoad, data.parcels);

      clusterLoading.hidden = true;
      pendingClusterId = null;
    })
    .catch(() => {
      clusterLoading.hidden = true;
      pendingClusterId = null;
    });
});

// ---------------------------------------------------------------------------
// ?geo=+?area= deep link — apply area filter on page load
// ---------------------------------------------------------------------------

map.on('load', async () => {
  if (!pendingGeoType || !pendingGeoArea) return;
  await loadGeoData();
  const cacheKey = pendingGeoType === 'neighborhood' ? 'neighborhoods'
                 : pendingGeoType === 'npu'          ? 'npu'
                 :                                     'council';
  const feat = geoCache[cacheKey].find(f => f.properties.NAME === pendingGeoArea);
  if (!feat) return;
  const label = pendingGeoType === 'neighborhood' ? `Neighborhood: ${pendingGeoArea}`
              : pendingGeoType === 'npu'          ? `NPU ${pendingGeoArea}`
              :                                     `Council District ${pendingGeoArea}`;
  // Always fly to the geo area — geo handler owns the viewport.
  // Cluster handler skips its own fitBounds when pendingGeoType is set.
  setAreaFilter(label, feat.geometry);
});

// ---------------------------------------------------------------------------
// ?parcel={county}/{parcel_id} deep link — open a specific parcel on page load
// ---------------------------------------------------------------------------

map.on('load', async () => {
  if (!pendingParcel) return;
  const slash = pendingParcel.indexOf('/');
  if (slash === -1) return;
  const county   = pendingParcel.slice(0, slash);
  const parcelId = pendingParcel.slice(slash + 1);

  // Fetch parcel to get lat/lon for fly-to, then render the panel.
  try {
    const res = await fetch(`/api/parcel/${county}/${encodeURIComponent(parcelId)}`);
    if (!res.ok) return;
    const data = await res.json();
    if (data.lat && data.lon) {
      map.flyTo({ center: [data.lon, data.lat], zoom: 16, duration: 800 });
    }
    renderParcelPanel(data);
    highlightParcel(parcelId);
    showPanel();
  } catch { /* silently ignore */ }
});

// ---------------------------------------------------------------------------
// ?hometype= deep link — apply home type filter on page load
// ---------------------------------------------------------------------------

map.on('load', () => {
  if (!pendingHomeType) return;
  filterHomeTypeSel.value = pendingHomeType;
  updateHomeTypeFilter();
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
  searchInput.setAttribute('aria-expanded', results.length > 0 ? 'true' : 'false');
  if (!results.length) { hideResults(); return; }

  results.forEach((r, idx) => {
    const li = document.createElement('li');
    li.id = `search-opt-${idx}`;
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
  if (i >= 0) {
    searchInput.setAttribute('aria-activedescendant', `search-opt-${i}`);
  } else {
    searchInput.removeAttribute('aria-activedescendant');
  }
}

function hideResults() {
  searchResults.hidden = true;
  searchResults.innerHTML = '';
  currentResults = [];
  selectedIndex = -1;
  searchInput.setAttribute('aria-expanded', 'false');
  searchInput.removeAttribute('aria-activedescendant');
}

function selectResult(result) {
  searchInput.value = result.fulladdr;
  hideResults();
  map.flyTo({ center: [result.lon, result.lat], zoom: 16, duration: 800 });
  placeSearchMarker(result.lon, result.lat);
  loadParcel(result.county, result.parcel_id);
}

// ---------------------------------------------------------------------------
// Markers
// ---------------------------------------------------------------------------

function placeSearchMarker(lon, lat) {
  if (searchMarker) searchMarker.remove();
  searchMarker = new maplibregl.Marker({ color: '#1d4ed8' }) // Dark blue for address searches
    .setLngLat([lon, lat])
    .addTo(map);
}

function placeMarker(lon, lat) {
  if (selectedMarker) selectedMarker.remove();
  selectedMarker = new maplibregl.Marker({ color: '#2563eb' }) // Brighter blue for specific parcel selection
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
const parcelMetaCity   = document.getElementById('parcel-meta-city');
const parcelUnits      = document.getElementById('parcel-units');
const parcelUnitsList  = document.getElementById('parcel-units-list');
const parcelUnitsSumm  = document.getElementById('parcel-units-summary');
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

// ---------------------------------------------------------------------------
// Land use codes — Fulton County Board of Assessors (Updated April 2024)
// Source: docs/Fulton-County-Land-Use-Codes-2024.pdf
// DeKalb codes inferred from pipeline context (no official DeKalb PDF available)
// ---------------------------------------------------------------------------
const LAND_USE_CODES = {
  fulton: {
    '100':'Residential vacant','101':'Residential 1 family','102':'Residential 2 family',
    '103':'Residential 3 family','104':'Residential 4 family',
    '105':'Commercial/Dwelling Conversion','106':'Single Family Residential Condominium',
    '107':'Single Family Residential Townhouse','108':'Single Family Residential Mobile Home',
    '109':'Auxiliary Improvement','110':'Single Family Residential Loft',
    '111':'Common Area - residential subdivision','112':'Agricultural-Improved',
    '113':'Preferential Agricultural-Vacant','166':'Common Area - condominiums',
    '188':'Homeowner Association Common Area','199':'Residential under construction',
    '200':'Apartment Vacant Land','201':'Apartment/Dwelling Conversion (>4 units)',
    '206':'Apt Lofts with Retail on First Floor','207':'Commercial Converted to Residential',
    '208':'Co Ops Single Family Fee Simple','209':'Apartment Loft Without Retail',
    '210':'Apartment - Vacant/Boarded Up','211':'Apartment - Garden (3 story & under)',
    '212':'Apartment - High Rise','213':'Mobile Home Park',
    '250':'Super Luxury Hotel','251':'Luxury Hotel','252':'First Class Hotel',
    '253':'High Rise Hotel','254':'Luxury Budget Motel','255':'Economy Motel',
    '257':'Motel Bed and Breakfast','281':'Low Income Tax Credit',
    '288':'Partially Exempt Apartment Complex','299':'Apartment Land Tie Back',
    '2A0':'Apt Mid Rise (4-10) Class A','2A1':'Apt Garden Class A','2A2':'Apt High Rise Class A',
    '2B0':'Apt Mid Rise (4-10) Class B','2B1':'Apt Garden Class B','2B2':'Apt High Rise Class B',
    '2C0':'Apt Mid Rise (4-10) Class C','2C1':'Apt Garden Class C','2D1':'Apt Garden Class D',
    '2H1':'Apt Low Rise Partial Tax Exempt','2H2':'Apt High Rise Partial Tax Exempt',
    '2X0':'Apt Mid Rise (4-10) Class X','2X1':'Apt Garden Class X','2X2':'Apt High Rise Class X',
    '300':'Vacant Commercial Land','301':'Commercial/Dwelling Conversion',
    '302':'Par 3 Golf Course','303':'Miniature Golf Course','304':'Fishing Pier',
    '305':'Marina','306':'Boat Dock','307':'Private Boat Launch',
    '308':'Water Amusements','309':'Miscellaneous Amusement',
    '310':'Unsound Commercial Structure','312':'Assisted Living Residence Community',
    '316':'Nursing Home','318':'Boarding-Rooming House','319':'Mixed Res/Comm (Built as Comm)',
    '320':'Commercial Auxiliary Improvements','321':'Restaurant','323':'Food Stands',
    '325':'Franchise Food','326':'Convenience/Fast Food Market','327':'Bar/Lounge',
    '328':'Night Club/Dinner Theater','331':'Auto Dealer, Full Service',
    '332':'Auto Service Garage','333':'Service Station with bays',
    '334':'Service Station (without bays)','335':'Truck Stop',
    '336':'Car Wash - Manual','337':'Car Wash - Automatic',
    '338':'Parking Garage/Deck','339':'Parking Lot (Paved)',
    '340':'Super Regional Shopping Mall','341':'Regional Shopping Mall',
    '342':'Community Shopping Center','343':'Neighborhood Shopping Center',
    '344':'Strip Shopping Center','345':'Discount Department Store',
    '346':'Department Store','347':'Supermarket','348':'Convenience Food Market',
    '349':'Medical Office Building','350':'Telecommunications Office Bldg',
    '351':'Bank','352':'Savings Institution',
    '353':'Office Building - Low Rise - 1-4 Story',
    '354':'Office Building - High Rise - 5 Story & Up',
    '355':'Office Condominium','356':'Retail Condominium',
    '361':'Funeral Home','362':'Veterinary Clinic','363':'Legitimate Theater',
    '364':'Motion Picture Theater','365':'Cinema/Theater',
    '366':'Radio, TV or Motion Picture Studio','367':'Social/Fraternal Hall',
    '368':'Hangar','369':'Day Care Center','370':'Greenhouse/Florist',
    '371':'Downtown Row Type','373':'Retail - Single Occupancy',
    '374':'Retail - Multiple Occupancy','375':'Retail - Drive Up',
    '381':'Bowling Alley','382':'Skating Rink','383':'Health Spa',
    '384':'Indoor Swimming Pool','385':'Indoor Tennis Club','386':'Indoor Racquet Club',
    '387':'Country Club','388':'Club House','389':'Country Club with Golf Course',
    '390':'Amusement Park','391':'Cold Storage Facility','392':'Lumber Storage',
    '393':'Auxiliary Improvement','394':'Warehouse (Distribution)','395':'Truck Terminal',
    '396':'Mini Warehouse','397':'Office Warehouse (flex)',
    '398':'Warehouse (bulk)','399':'Prefab Warehouse',
    '3A3':'Office Building (Low Rise >4) Class A','3A4':'Office Building (High Rise <5) Class A',
    '3B3':'Office Building (Low Rise >4) Class B','3B4':'Office Building (High Rise <5) Class B',
    '3C3':'Office Building (Low Rise >4) Class C','3C4':'Office Building (High Rise <5) Class C',
    '3D3':'Office Building (Low Rise >4) Class D',
    '3H3':'Office Bldg with Partial Exempt Status',
    '3T4':'Office Building (High Rise <5) Trophy',
    '3X4':'Office Building (High Rise <5) Class X',
    '400':'Vacant Industrial Land','401':'Manufacturing/Processing',
    '405':'Research and Development','411':'Aircraft Engine Mfg.',
    '412':'Aluminum & Foil Mfg.','413':'Asphalt Plant','414':'Automobile Parts Mfg.',
    '415':'Bakery','416':'Bottling Plant','417':'Broom Mfg','418':'Candy Mfg.',
    '419':'Cement Mfg.','420':'Concrete Mfg.','421':'Chemical Plant','422':'Clay Products',
    '423':'Clothing Mfg.','424':'Coal Processing',
    '425':'Compressor Station (not public utility)','426':'Dairy',
    '428':'Dental & Medical Lab','429':'Electronic Components',
    '430':'Electrical Equipment Mfg','431':'Feed & Flower Manufacturing',
    '432':'Foundry Products','433':'Food Processing','434':'Glass Manufacturing',
    '435':'Glass Manufacturing (specialized)','436':'Grain and Milling Products Mfg',
    '437':'Ice Plant','438':'Leather Products Mfg','439':'Liquefied Natural Gas Plant',
    '440':'Logging/Cutting of Timber','441':'Machinery/Equipment Mfg',
    '442':'Meat Packing/Slaughterhouse','443':'Metal Working',
    '444':'Mining/Deep','445':'Mining/Strip','446':'Natural Gas Extracting Facility',
    '447':'Nickel Manufacturing','448':'Newspaper Printing Plant',
    '449':'Oil and Gas Pipelining','450':'Optical Manufacturing',
    '451':'Paint Manufacturing','452':'Paper Finishing and Converting',
    '455':'Plastics Products Mfg','456':'Plastics Products - Specialized',
    '457':'Print Shop','458':'Pulp and Paper','459':'Quarries (Rock)',
    '460':'Railroad Car Manufacturing','461':'Rubber Mfg (Tire Recapping)',
    '462':'Shoe Manufacturing','463':'Steel Manufacturing',
    '464':'Steam Generating Plant','465':'Saw Mill (Permanent)','466':'Saw Mill (temporary)',
    '467':'Textile Manufacturing','468':'Tobacco Products Manufacturing',
    '469':'Wood Working Shop','470':'Wire Products Manufacturing',
    '471':'Jewelry, Toys, Sporting Goods, Other Mfg','472':'Furniture Mfg',
    '485':'Land Fill','499':'Industrial Tie Back Land',
    '512':'School Private (Taxable)','513':'College Dormitory (Taxable)',
    '520':'Taxable Church, Synagogue, Mosque','540':'Hospital/For Profit (Taxable)',
    '550':'Charitable Office/Svc Center (Taxable)','580':'Cultural Facilities',
    '591':'USPS (Taxable Private Ownership)',
    '600':'Vacant Exempt Land','601':'Cemetery','610':'Recreation/Health',
    '611':'Library','612':'School','613':'College',
    '614':'Single Family Residential: Institutional',
    '620':'Religious: Churches, Synagogue, Mosque','621':'Church Parking (Paved)',
    '622':'Single Family Residential: Parsonage',
    '625':'Religious Mission (Salvation Army, GW)',
    '630':'Auditorium','640':'Hospital','641':'Urgent Care Facility',
    '650':'Charitable Office (Service Center)','660':'Police or Fire Station',
    '670':'Correctional (Local, State, Federal)','680':'Cultural Facilities',
    '684':'Housing for the Disabled','685':'Housing for the Homeless',
    '686':'Housing for the Aged','690':'Rail/Bus/Air Terminal',
    '691':'US Postal Services (Private)','692':'US Postal Services (Exempt)',
    '699':'Improved Government Owned Exempt NEC',
    '700':'Utility Vacant Land','701':'Railroad','702':'Electric Utility',
    '703':'Gas Utility','704':'Water Utility','705':'Sewer Utility',
    '706':'Multiple Service Utility','710':'Telephone Equipment Building',
    '711':'Telephone Utility NEC','715':'Telephone Service Garage',
    '720':'Radio/TV Transmitter Building','799':'Other Utility NEC',
    '800':'Unique Restricted Vacant Land',
    '888':'Economic Development/Public Housing','889':'Brownfield',
    '88H':'Economic Development - Hotel','88O':'Economic Development - Office',
    '88R':'Economic Development - Retail','88X':'Economic Development - Apts Class X',
    '890':'Economic Development/Brownfield','999':'Commercial Land Tie Back',
  },
  dekalb: {
    'SUB':'Residential Subdivision','TN':'Townhome / Townhouse',
    'CRC':'Condo Residential Community','TC':'Town Center',
    'NC':'Neighborhood Commercial','RC':'Regional Commercial',
    'LIND':'Light Industrial','COS':'Common Open Space',
    'INS':'Institutional','IND':'Industrial','OP':'Open Space',
  },
};

// Atlanta city zoning codes
// Source: Atlanta Code of Ordinances, Chapter 150 (Zoning)
const ATL_ZONING_CODES = {
  'R-1':'Single-Family Residential (9,000 sq ft min)',
  'R-2':'Single-Family Residential (7,500 sq ft min)',
  'R-2A':'Single-Family Residential',
  'R-3':'Single-Family Residential (5,000 sq ft min)',
  'R-3A':'Single-Family Residential',
  'R-4':'Single-Family Residential (4,500 sq ft min)',
  'R-4A':'Single-Family Residential',
  'R-4B':'Single-Family Residential',
  'R-5':'Two-Family Residential',
  'RG-1':'Residential General, Low-Density',
  'RG-2':'Residential General',
  'RG-3':'Residential General, Medium-Density',
  'RG-4':'Residential General, High-Density',
  'RG-5':'Residential General, Very High-Density',
  'MR-1':'Multi-Family Residential',
  'MR-2':'Multi-Family Residential',
  'MR-3':'Multi-Family Residential',
  'MR-4':'Multi-Family Residential, High-Density',
  'MR-4A':'Multi-Family Residential',
  'MRC-1':'Mixed Residential-Commercial',
  'MRC-2':'Mixed Residential-Commercial',
  'MRC-3':'Mixed Residential-Commercial',
  'C-1':'Commercial, Neighborhood Services',
  'C-2':'Commercial, Medium-Intensity',
  'C-3':'Commercial, Highway Services',
  'C-4':'Commercial, High-Intensity',
  'I-1':'Industrial, Light',
  'I-2':'Industrial, Heavy',
  'O-A':'Office-Apartment',
  'O-I':'Office-Institutional',
  'PD-H':'Planned Development - Housing',
  'PD-MU':'Planned Development - Mixed Use',
  'FCR-1':'Former Campbellton Road District 1',
  'FCR-2':'Former Campbellton Road District 2',
  'FCR-3':'Former Campbellton Road District 3',
};

function lookupAtlZoning(code) {
  if (!code) return null;
  if (ATL_ZONING_CODES[code]) return ATL_ZONING_CODES[code];
  const base = code.replace(/-C$/, '');
  const baseDesc = ATL_ZONING_CODES[base];
  if (baseDesc) return baseDesc + ' — Conditional';
  const spiMatch = code.match(/^SPI-(\d+)/);
  if (spiMatch) return `Special Public Interest District ${spiMatch[1]}`;
  return null;
}

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

  // ── County tax parcel fields (first dl) ──────────────────────
  const countyMeta = [];
  countyMeta.push(['__divider__', 'County tax parcel']);
  countyMeta.push(['County', p.county === 'fulton' ? 'Fulton County' : 'DeKalb County']);
  countyMeta.push(['Parcel ID', p.parcel_id]);

  // Property class — same GA state code in both Fulton (classcode) and DeKalb (classdscrp)
  if (p.property_class) {
    countyMeta.push(['Property class', GA_PROPERTY_CLASS[p.property_class] || p.property_class]);
  }

  // Co-owner (DeKalb ownernme2)
  if (p.owner_name2) countyMeta.push(['Co-owner', p.owner_name2]);

  // Physical details
  if (p.land_acres != null) countyMeta.push(['Land', `${Number(p.land_acres).toFixed(2)} acres`]);
  if (p.living_units)       countyMeta.push(['Units', p.living_units]);
  if (p.land_use) {
    const luDesc = LAND_USE_CODES[p.county]?.[p.land_use] ?? null;
    countyMeta.push(['__raw__', 'Land use', codeTipHtml(p.land_use, luDesc)]);
  }

  // Homestead exemption (Fulton only) — excode non-empty = homestead exempt
  if (p.county === 'fulton') {
    countyMeta.push(['Exemption', p.exemption_code ? 'Homestead exempt' : 'Not homestead exempt']);
  }

  // Appraised value (DeKalb only)
  if (p.appraised_value != null) {
    countyMeta.push(['Assessed value', '$' + Number(p.appraised_value).toLocaleString() + ' (DeKalb)']);
  }

  // Zoning / historic / overlay (skip if blank — API returns null when blank)
  if (p.zoning)            countyMeta.push(['Zoning', p.zoning]);
  if (p.historic_district) countyMeta.push(['Historic district', p.historic_district]);
  if (p.overlay_district)  countyMeta.push(['Overlay district', p.overlay_district]);

  const renderRow    = ([k, v])     => `<dt>${escHtml(k)}</dt><dd>${escHtml(String(v))}</dd>`;
  const renderRowRaw = ([k, vHtml]) => `<dt>${escHtml(k)}</dt><dd>${vHtml}</dd>`;
  const renderDivider = (label) =>
    `<dt class="meta-source-divider">${escHtml(label)}<sup><a href="/faq/#data-sources" title="Data source information">*</a></sup></dt>`;

  parcelMeta.innerHTML = countyMeta.map(([k, v, vHtml]) =>
    k === '__divider__' ? renderDivider(v)
    : k === '__raw__'   ? renderRowRaw([v, vHtml])
    : renderRow([k, v])
  ).join('');

  // Owner mailing address — county tax parcel record, rendered as block between the two dls
  const mailAddr = [p.owner_mail_addr1, p.owner_mail_addr2].filter(Boolean);
  const mailBlock = document.getElementById('owner-mail-addr');
  if (mailAddr.length) {
    mailBlock.innerHTML = `<p class="meta-section-label">Owner mailing address</p>`
      + `<p class="owner-mail">${mailAddr.map(escHtml).join('<br>')}</p>`;
    mailBlock.hidden = false;
  } else {
    mailBlock.hidden = true;
  }

  // ── City of Atlanta GIS fields (second dl) ───────────────────
  const cityFields = [];
  if (p.neighborhood)     cityFields.push(['Neighborhood', p.neighborhood]);
  if (p.npu)              cityFields.push(['NPU', p.npu]);
  if (p.council_district) cityFields.push(['Council', `District ${p.council_district}`]);
  if (p.home_type)        cityFields.push(['Home type', p.home_type]);
  if (p.city_zoning) {
    const czDesc = lookupAtlZoning(p.city_zoning);
    cityFields.push(['__raw__', 'Zoning', codeTipHtml(p.city_zoning, czDesc)]);
  }

  if (cityFields.length) {
    parcelMetaCity.innerHTML = renderDivider('City of Atlanta GIS')
      + cityFields.map(([k, v, vHtml]) =>
          k === '__raw__' ? renderRowRaw([v, vHtml]) : renderRow([k, v])
        ).join('');
    parcelMetaCity.hidden = false;
  } else {
    parcelMetaCity.innerHTML = '';
    parcelMetaCity.hidden = true;
  }

  // Related units (condo building)
  renderRelatedUnits(p);

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

  // Marker & Highlight
  placeMarker(p.lon, p.lat);
  highlightParcel(p.parcel_id);
  showPanel();
}

function renderRelatedUnits(p) {
  const units = p.related_units || [];
  if (units.length === 0) {
    parcelUnits.hidden = true;
    return;
  }

  // Helper to extract unit string (e.g., "# 101", "UNIT 202", "303")
  function getUnitLabel(addr, baseAddr, parcelId) {
    if (!addr) return parcelId.slice(-4);
    const cleanBase = (baseAddr || '').replace(/\s+/g, ' ').trim().toUpperCase();
    const cleanAddr = addr.replace(/\s+/g, ' ').trim().toUpperCase();
    
    // If exact match
    if (cleanAddr === cleanBase) return 'Main';
    if (cleanAddr.startsWith(cleanBase)) {
      let unit = cleanAddr.slice(cleanBase.length).trim();
      unit = unit.replace(/^(#|UNIT)\s*/, '');
      return unit || 'Main';
    }
    
    // If addresses match but they're supposed to be units, or if extraction fails, use last 4 of PID
    return parcelId.slice(-4);
  }

  const baseAddr = p.site_address || '';
  parcelUnitsSumm.textContent = `${units.length + 1} units in this building`;

  const renderRow = (u, isCurrent) => {
    const cls = ['unit-row'];
    let badges = [];
    if (isCurrent) cls.push('unit-current');
    if (u.is_corporate) {
      cls.push('unit-corporate');
      badges.push('<span class="u-badge corp">Corp</span>');
    }
    if (u.is_institutional) {
      cls.push('unit-institutional');
      badges.push('<span class="u-badge inst">Inst</span>');
    }
    
    const unitLabel = getUnitLabel(u.site_address, baseAddr, u.parcel_id);
    
    return `
      <tr class="${cls.join(' ')}" data-county="${p.county}" data-pid="${u.parcel_id}">
        <td class="unit-num">${escHtml(unitLabel)}</td>
        <td class="unit-owner">
          <div class="u-name">${escHtml(u.owner_name || '')}</div>
          <div class="u-badges">${badges.join('')}</div>
        </td>
      </tr>
    `;
  };

  parcelUnitsList.innerHTML = [
    renderRow(p, true),
    ...units.map(u => renderRow(u, false))
  ].join('');

  // Scroll current unit into view and add click handlers
  parcelUnitsList.querySelectorAll('.unit-row').forEach(row => {
    const select = () => loadParcel(row.dataset.county, row.dataset.pid);
    row.setAttribute('tabindex', '0');
    row.addEventListener('click', select);
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        select();
      }
    });
  });

  parcelUnits.hidden = false;
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

function showPanel()  { 
  detailPanel.hidden = false;
  panelClose.focus(); // Accessibility: Move focus to the panel
}
function closePanel() {
  detailPanel.hidden = true;
  if (selectedMarker) { selectedMarker.remove(); selectedMarker = null; }
  highlightParcel(null);
  highlightCluster(null);
}

// ---------------------------------------------------------------------------
// Area filter — neighborhood / NPU / council district
// ---------------------------------------------------------------------------

const filterToggle   = document.getElementById('filter-toggle');
const filterPanel    = document.getElementById('filter-panel');
const filterNbInput  = document.getElementById('filter-neighborhood');
const filterNbList   = document.getElementById('filter-neighborhood-results');
const filterNpuSel   = document.getElementById('filter-npu');
const filterCouncil  = document.getElementById('filter-council');
const filterHomeTypeSel      = document.getElementById('filter-home-type');
const filterActive           = document.getElementById('filter-active');
const filterLabel            = document.getElementById('filter-active-label');
const filterClear            = document.getElementById('filter-clear');
const filterActiveHometype   = document.getElementById('filter-active-hometype');
const filterHometypeLabel    = document.getElementById('filter-hometype-label');
const filterClearHometype    = document.getElementById('filter-clear-hometype');

const HOME_TYPE_ABBR = {
  'Single-Family':        'SFH',
  'Multi-Family / Condo': 'MFC',
  'Multi-Family / Other': 'MFO',
  'Other':                'Other',
};

function syncHasActive() {
  const anyActive = !!(activeAreaFilter || filterHomeTypeSel.value);
  document.getElementById('filter-details').classList.toggle('has-active', anyActive);
  document.querySelector('header').classList.toggle('filter-is-active', anyActive);
}

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
  syncHasActive();
  document.getElementById('filter-details').open = false; // Native close
  map.fitBounds(geomBounds(geometry), { padding: 40, maxZoom: 15 });
  updateChoroOpacity();
}

function clearAreaFilter() {
  activeAreaFilter = null;
  if (map.getSource('area-overlay'))
    map.getSource('area-overlay').setData({ type: 'FeatureCollection', features: [] });
  updateChoroOpacity();
  filterActive.hidden = true;
  filterNbInput.value = '';
  filterNpuSel.value  = '';
  filterCouncil.value = '';
  filterNbList.hidden = true;
  // NOTE: home type is NOT cleared here — independent filter
  syncHasActive();
}

// Filter toggle open/close (now using native <details> toggle event)
document.getElementById('filter-details').addEventListener('toggle', async (e) => {
  if (e.target.open) await loadGeoData();
});

// Close filter panel when clicking outside
document.addEventListener('click', (e) => {
  const details = document.getElementById('filter-details');
  if (!e.target.closest('.filter-wrapper')) details.open = false;
});

// Neighborhood search (accessibility enhanced)
let nbCurrentResults = [];
let nbSelectedIndex = -1;

function renderNbResults(matches) {
  nbCurrentResults = matches;
  nbSelectedIndex = -1;
  filterNbList.innerHTML = matches.map((f, idx) =>
    `<li id="nb-opt-${idx}" role="option" aria-selected="false" data-name="${escHtml(f.properties.NAME)}">${escHtml(f.properties.NAME)}</li>`
  ).join('');
  filterNbList.hidden = matches.length === 0;
  filterNbInput.setAttribute('aria-expanded', matches.length > 0 ? 'true' : 'false');
}

function setNbSelectedIndex(i) {
  const items = filterNbList.querySelectorAll('li');
  items.forEach((el, idx) => {
    el.setAttribute('aria-selected', idx === i ? 'true' : 'false');
  });
  nbSelectedIndex = i;
  if (i >= 0) {
    filterNbInput.setAttribute('aria-activedescendant', `nb-opt-${i}`);
  } else {
    filterNbInput.removeAttribute('aria-activedescendant');
  }
}

function hideNbResults() {
  filterNbList.hidden = true;
  filterNbInput.setAttribute('aria-expanded', 'false');
  filterNbInput.removeAttribute('aria-activedescendant');
  nbSelectedIndex = -1;
}

function selectNbResult(name) {
  const feat = geoCache.neighborhoods.find(f => f.properties.NAME === name);
  if (!feat) return;
  filterNbInput.value = name;
  hideNbResults();
  filterNpuSel.value  = '';
  filterCouncil.value = '';
  setAreaFilter(`Neighborhood: ${name}`, feat.geometry);
}

// Neighborhood text search
let nbTimeout = null;
filterNbInput.addEventListener('input', () => {
  clearTimeout(nbTimeout);
  const q = filterNbInput.value.trim().toLowerCase();
  if (!q || !geoCache.neighborhoods) { hideNbResults(); return; }
  nbTimeout = setTimeout(() => {
    const matches = geoCache.neighborhoods
      .filter(f => f.properties.NAME.toLowerCase().includes(q))
      .slice(0, 10);
    renderNbResults(matches);
  }, 150);
});

filterNbList.addEventListener('click', (e) => {
  const li = e.target.closest('li');
  if (!li) return;
  selectNbResult(li.dataset.name);
});

filterNbInput.addEventListener('keydown', (e) => {
  if (filterNbList.hidden) return;
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    setNbSelectedIndex(Math.min(nbSelectedIndex + 1, nbCurrentResults.length - 1));
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    setNbSelectedIndex(Math.max(nbSelectedIndex - 1, 0));
  } else if (e.key === 'Enter' && nbSelectedIndex >= 0) {
    e.preventDefault();
    selectNbResult(nbCurrentResults[nbSelectedIndex].properties.NAME);
  } else if (e.key === 'Escape' || e.key === 'Tab') {
    hideNbResults();
  }
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

// Home Type select
filterHomeTypeSel.addEventListener('change', () => {
  updateHomeTypeFilter();
});

function updateHomeTypeFilter() {
  const val = filterHomeTypeSel.value;
  const filter = val ? ['==', ['get', 'home_type'], val] : null;

  if (map.getLayer('parcels-overview')) map.setFilter('parcels-overview', filter);
  if (map.getLayer('parcels-detail'))   map.setFilter('parcels-detail', filter);

  if (val) {
    filterHometypeLabel.textContent = HOME_TYPE_ABBR[val] || val;
    filterClearHometype.setAttribute('aria-label', `Clear home type filter: ${val}`);
    filterActiveHometype.hidden = false;
  } else {
    filterActiveHometype.hidden = true;
  }
  syncHasActive();
}

// Area clear button
filterClear.addEventListener('click', (e) => {
  e.stopPropagation();
  clearAreaFilter();
  filterToggle.focus();
});

// Home type clear button
filterClearHometype.addEventListener('click', (e) => {
  e.stopPropagation();
  filterHomeTypeSel.value = '';
  updateHomeTypeFilter();
  filterToggle.focus();
});

// ---------------------------------------------------------------------------
// Code-lookup tooltip handlers (land use, zoning)
// ---------------------------------------------------------------------------

// Toggle tooltip on tap/click (mobile — hover handles desktop)
document.getElementById('detail-panel').addEventListener('click', (e) => {
  const btn = e.target.closest('.code-lookup');
  if (!btn) return;
  const wasOpen = btn.classList.contains('tip-open');
  document.querySelectorAll('.code-lookup.tip-open')
    .forEach(b => b.classList.remove('tip-open'));
  if (!wasOpen) btn.classList.add('tip-open');
  e.stopPropagation();
});

// Dismiss open tooltips when clicking elsewhere
document.addEventListener('click', () => {
  document.querySelectorAll('.code-lookup.tip-open')
    .forEach(b => b.classList.remove('tip-open'));
});

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function escHtml(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Renders a code value with an accessible tooltip if a description is available.
function codeTipHtml(code, desc) {
  if (!desc) return escHtml(code);
  return `<button class="code-lookup" data-tip="${escHtml(desc)}" aria-label="${escHtml(desc)}">${escHtml(code)}</button>`;
}

function fmtDate(iso) {
  return new Date(iso).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
}

// ---------------------------------------------------------------------------
// Point-in-polygon checks for LocateControl
// ---------------------------------------------------------------------------

function isPointInPolygon(point, vs) {
  const x = point[0], y = point[1];
  let inside = false;
  for (let i = 0, j = vs.length - 1; i < vs.length; j = i++) {
    const xi = vs[i][0], yi = vs[i][1];
    const xj = vs[j][0], yj = vs[j][1];
    const intersect = ((yi > y) != (yj > y)) &&
                      (x < (xj - xi) * (y - yi) / (yj - yi) + xi);
    if (intersect) inside = !inside;
  }
  return inside;
}

function isPointInGeometry(point, geom) {
  if (geom.type === 'Polygon') {
    // Exterior ring must contain point, interior rings (holes) must NOT.
    if (!isPointInPolygon(point, geom.coordinates[0])) return false;
    for (let i = 1; i < geom.coordinates.length; i++) {
      if (isPointInPolygon(point, geom.coordinates[i])) return false;
    }
    return true;
  }
  if (geom.type === 'MultiPolygon') {
    return geom.coordinates.some(polyCoords => {
      // Each polyCoords is an array of rings (exterior, interior...)
      if (!isPointInPolygon(point, polyCoords[0])) return false;
      for (let i = 1; i < polyCoords.length; i++) {
        if (isPointInPolygon(point, polyCoords[i])) return false;
      }
      return true;
    });
  }
  return false;
}
