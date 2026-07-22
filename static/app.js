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
  const startMsg = data.resumed
    ? `resuming — ${data.cached || ""} tiles already cached…`
    : "starting…";
  setProgress(data.cached || 0, data.total_tiles, startMsg);
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
  loadJobs();
}

$("confirm").addEventListener("click", async () => {
  if (!currentJob) return;
  $("confirm").disabled = true;
  try {
    const r = await fetch(`/confirm/${currentJob}`, { method: "POST" });
    if (r.ok) {
      $("confirm-msg").textContent = "Cached tiles cleared. ✓";
      $("download").classList.add("hidden");
      loadJobs();
    } else {
      $("confirm-msg").textContent = "Could not clean up (already gone?).";
    }
  } catch (e) {
    $("confirm-msg").textContent = "Cleanup request failed.";
  }
});

// ---- saved areas menu ----
const savedLayer = new L.FeatureGroup().addTo(map); // highlight for a picked job

function fmtDate(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleString([], { month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit" });
}

function statusBadge(s) {
  const map = { done: "✓ done", running: "⏳ fetching", building: "⚙ building Excel",
    error: "⚠ error", interrupted: "⏸ interrupted", cancelled: "✕ cancelled" };
  return `<span class="badge b-${s}">${map[s] || s}</span>`;
}

async function loadJobs() {
  const ul = $("jobs-list");
  try {
    const r = await fetch("/jobs");
    const jobs = await r.json();
    if (!jobs.length) { ul.innerHTML = `<li class="muted empty">No saved areas yet.</li>`; return; }
    ul.innerHTML = "";
    for (const j of jobs) {
      const p = j.params || {};
      const tiles = `${j.cached}/${j.total} tiles cached`;
      const excel = j.has_xlsx ? `Excel: ${j.xlsx_name}` : "Excel: not built yet";
      const li = document.createElement("li");
      li.className = "job";
      li.innerHTML = `
        <div class="job-top">
          ${statusBadge(j.status)}
          <span class="job-date">${fmtDate(j.created)}</span>
        </div>
        <div class="job-box">${p.nw_lat != null
          ? `NW ${(+p.nw_lat).toFixed(4)}, ${(+p.nw_lon).toFixed(4)} · SE ${(+p.se_lat).toFixed(4)}, ${(+p.se_lon).toFixed(4)}`
          : "(box unknown)"}</div>
        <div class="job-meta">${tiles} · ${excel}</div>
        <div class="job-actions">
          <button class="linkbtn show">Show on map</button>
          ${j.has_xlsx ? `<a class="linkbtn" href="/download/${j.job_id}">Download</a>` : ""}
          <button class="linkbtn danger del">Delete</button>
        </div>`;
      li.querySelector(".show").addEventListener("click", () => showJob(p));
      li.querySelector(".del").addEventListener("click", () => deleteJob(j.job_id));
      ul.appendChild(li);
    }
  } catch (e) {
    ul.innerHTML = `<li class="muted empty">Could not load saved areas.</li>`;
  }
}

function showJob(p) {
  if (p.nw_lat == null) return;
  savedLayer.clearLayers();
  const b = L.latLngBounds([p.se_lat, p.nw_lon], [p.nw_lat, p.se_lon]);
  L.rectangle(b, { color: "#7a3ea6", weight: 2, dashArray: "5,4", fill: false })
    .addTo(savedLayer);
  map.fitBounds(b, { padding: [30, 30] });
}

async function deleteJob(jobId) {
  if (!confirm("Delete this area's cached tiles and Excel?")) return;
  try {
    await fetch(`/confirm/${jobId}`, { method: "POST" });
  } catch (e) { /* ignore */ }
  loadJobs();
}

$("refresh-jobs").addEventListener("click", loadJobs);

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

// populate the saved-areas menu on first load
loadJobs();
