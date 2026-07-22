/* KYTC Bridge Map -> Excel — frontend glue.
 * Leaflet map + rectangle draw -> POST /export -> SSE progress -> download.
 * The heavy lifting (tile fetch, Excel anchoring) is on the Python backend. */

// Optional: overlay the KYTC bridge feature layer so users draw around the
// flags they actually see. The backend logs the operational layer URLs from
// the web map on first fetch ("[webmap] ... -> <url>"); paste that URL here to
// turn the overlay on.
const BRIDGE_LAYER_URL = "";

const map = L.map("map").setView([37.75, -85.0], 7); // Kentucky

// Esri basemap via esri-leaflet (matches the print output's world imagery/
// topo feel). Falls back gracefully if esri-leaflet failed to load.
try {
  L.esri.basemapLayer("Topographic").addTo(map);
} catch (e) {
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "© OpenStreetMap",
  }).addTo(map);
}

if (BRIDGE_LAYER_URL) {
  try {
    L.esri.featureLayer({ url: BRIDGE_LAYER_URL }).addTo(map);
  } catch (e) {
    console.warn("Could not add bridge layer:", e);
  }
}

// ---- rectangle draw ----
const drawn = new L.FeatureGroup().addTo(map);
const drawControl = new L.Control.Draw({
  draw: {
    rectangle: { shapeOptions: { color: "#d6482b", weight: 2 } },
    polygon: false, polyline: false, circle: false,
    marker: false, circlemarker: false,
  },
  edit: { featureGroup: drawn, edit: false, remove: true },
});
map.addControl(drawControl);

let box = null; // {nw_lat, nw_lon, se_lat, se_lon}

function boundsToBox(b) {
  return {
    nw_lat: b.getNorth(), nw_lon: b.getWest(),
    se_lat: b.getSouth(), se_lon: b.getEast(),
  };
}

map.on(L.Draw.Event.CREATED, (e) => {
  drawn.clearLayers();
  drawn.addLayer(e.layer);
  box = boundsToBox(e.layer.getBounds());
  showBounds();
  updateEstimate();
});
map.on(L.Draw.Event.DELETED, () => {
  box = null;
  showBounds();
  updateEstimate();
});

function showBounds() {
  const el = document.getElementById("bounds");
  if (!box) { el.textContent = "No area drawn yet."; el.classList.add("muted"); return; }
  el.classList.remove("muted");
  el.innerHTML =
    `NW <b>${box.nw_lat.toFixed(5)}, ${box.nw_lon.toFixed(5)}</b> &nbsp;·&nbsp; ` +
    `SE <b>${box.se_lat.toFixed(5)}, ${box.se_lon.toFixed(5)}</b>`;
}

// ---- controls ----
const $ = (id) => document.getElementById(id);
const getSettings = () => ({
  scale: parseFloat($("scale").value),
  tile_px: parseInt($("tile_px").value, 10),
  excel_scale: parseFloat($("excel_scale").value),
});

["scale", "tile_px", "excel_scale"].forEach((id) =>
  $(id).addEventListener("input", updateEstimate));

let estTimer = null;
function updateEstimate() {
  clearTimeout(estTimer);
  estTimer = setTimeout(doEstimate, 250);
}

async function doEstimate() {
  const estEl = $("estimate");
  const genBtn = $("generate");
  if (!box) {
    estEl.className = "estimate muted";
    estEl.textContent = "Draw a box to estimate tiles.";
    genBtn.disabled = true;
    return;
  }
  const s = getSettings();
  const q = new URLSearchParams({ ...box, scale: s.scale, tile_px: s.tile_px }).toString();
  try {
    const r = await fetch(`/plan?${q}`);
    const d = await r.json();
    const over = d.total_tiles > d.max_tiles;
    estEl.className = "estimate" + (over ? " over" : d.total_tiles > 50 ? " warn" : "");
    estEl.innerHTML =
      `<b>${d.total_tiles}</b> tiles (${d.nrows} rows × ${d.ncols} cols) · ` +
      `${d.mpp.toFixed(2)} m/px` +
      (over ? `<br>⚠ Over the ${d.max_tiles}-tile cap — draw smaller or raise the scale.`
            : d.total_tiles > 50 ? `<br>Heads up: large job, this may take a while.` : "");
    genBtn.disabled = over;
  } catch (e) {
    estEl.className = "estimate over";
    estEl.textContent = "Could not estimate.";
    genBtn.disabled = true;
  }
}

// ---- generate + progress ----
let currentJob = null;

$("generate").addEventListener("click", async () => {
  if (!box) return;
  resetOutputs();
  const s = getSettings();
  $("generate").disabled = true;

  let resp;
  try {
    resp = await fetch("/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...box, ...s }),
    });
  } catch (e) {
    return showError("Network error starting the export.");
  }
  const data = await resp.json();
  if (!resp.ok) {
    $("generate").disabled = false;
    return showError(data.detail || "Export was rejected.");
  }

  currentJob = data.job_id;
  $("progress-wrap").classList.remove("hidden");
  setProgress(0, data.total_tiles, "starting…");
  streamProgress(data.job_id, data.total_tiles);
});

function streamProgress(jobId, total) {
  const es = new EventSource(`/progress/${jobId}`);
  es.onmessage = (ev) => {
    const d = JSON.parse(ev.data);
    setProgress(d.done, d.total || total, d.status);
    if (d.status === "done") { es.close(); onDone(jobId); }
    else if (d.status === "error") { es.close(); showError(d.error || "Export failed."); }
  };
  es.onerror = () => {
    // SSE dropped — fall back to polling
    es.close();
    pollProgress(jobId, total);
  };
}

async function pollProgress(jobId, total) {
  try {
    const r = await fetch(`/status/${jobId}`);
    const d = await r.json();
    setProgress(d.done, d.total || total, d.status);
    if (d.status === "done") return onDone(jobId);
    if (d.status === "error") return showError(d.error || "Export failed.");
    setTimeout(() => pollProgress(jobId, total), 1000);
  } catch (e) {
    showError("Lost contact with the server.");
  }
}

function setProgress(done, total, status) {
  const pct = total ? Math.round((done / total) * 100) : 0;
  $("bar-fill").style.width = pct + "%";
  const label = status === "building" ? "building Excel…"
    : status === "done" ? "done" : status;
  $("progress-text").textContent = `${done} / ${total} tiles — ${label}`;
}

function onDone(jobId) {
  setProgress(1, 1, "done");
  $("download").href = `/download/${jobId}`;
  $("result").classList.remove("hidden");
}

$("confirm").addEventListener("click", async () => {
  if (!currentJob) return;
  $("confirm").disabled = true;
  try {
    const r = await fetch(`/confirm/${currentJob}`, { method: "POST" });
    if (r.ok) {
      $("confirm-msg").textContent = "Cached tiles cleared. ✓";
      $("download").classList.add("hidden");
    } else {
      $("confirm-msg").textContent = "Could not clean up (already gone?).";
    }
  } catch (e) {
    $("confirm-msg").textContent = "Cleanup request failed.";
  }
});

// ---- helpers ----
function resetOutputs() {
  $("error").classList.add("hidden");
  $("result").classList.add("hidden");
  $("progress-wrap").classList.add("hidden");
  $("confirm").disabled = false;
  $("confirm-msg").textContent = "";
  $("download").classList.remove("hidden");
}
function showError(msg) {
  const el = $("error");
  el.textContent = msg;
  el.classList.remove("hidden");
  $("generate").disabled = false;
}
