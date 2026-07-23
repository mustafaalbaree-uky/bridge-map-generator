# KYTC Bridge Map → Excel

Draw a rectangle over Kentucky, click a button, watch a progress bar, and
download an Excel file. The workbook contains screenshots of the
[KYTC Bridge Data Miner](https://www.arcgis.com/) map for that rectangle,
**tiled edge to edge with perfect seams**, ready to annotate.

This replaces the manual process of screenshotting the map one pane at a time
and eyeballing them into alignment in Excel.

---

## How it works

The Bridge Data Miner is an ArcGIS web map whose imagery comes from Esri's
public print service. For an exact coordinate box we:

1. Fetch the web map definition once (basemap + bridge layers).
2. Split the box into a grid of tiles in Web Mercator (EPSG:3857). Because every
   tile is the same pixel size at the same scale and each tile's box starts
   exactly where the previous one ended, **tiles abut perfectly by
   construction** — no eyeballing.
3. POST each tile to Esri's print service (`Export Web Map Task`) to get a PNG.
4. Place each tile into the `.xlsx` with an `AbsoluteAnchor` at exact EMU pixel
   positions (`1 px = 9525 EMU`), so seams stay perfect regardless of Excel
   column widths.

The tile fetch and the openpyxl anchoring are the hard-won, proven parts — they
live in **`pipeline.py`**, lifted from the command-line prototype (`map_export.py`)
and left intact. The web layer never re-implements them in JavaScript.

### Tile caching / resume

Tiles are written to `jobs/<job_id>/tiles/` **on disk** as they arrive. They are
**cached and kept**, not thrown away:

- **Each tile is saved the moment it downloads.** Close the app (or lose power)
  mid-run and everything fetched so far is safe on disk.
- **The job id is derived from the box + scale + tile size + dpi**, not random.
  So drawing the *same area* again reuses the *same* cache folder and **resumes**:
  already-downloaded tiles are skipped, and only the missing ones are fetched.
  (Changing just the Excel display scale reuses the tiles too — it only affects
  placement, not the imagery.)
- Re-submitting a box that's still fetching **attaches to the running job**
  instead of starting a duplicate.

Two separate ways to reclaim space, so the history sticks around:

- **Clear cache** (`POST /clear/<job_id>`) deletes the heavy files — the cached
  tile images and the built `.xlsx` — but **keeps the metadata**, so the area
  stays listed under "Saved areas" (showing its box and the Excel's old name)
  until you delete it. This is what the post-download button does.
- **Delete** (`DELETE /jobs/<job_id>`) removes the job **entirely** — images,
  Excel, and the saved entry.

Smart cleanup of *old* tiles:

- A background sweep removes any job folder untouched for **24h** (`CLEANUP_TTL_S`
  in `app.py`) — a safety net for abandoned jobs.
- The sweep **never touches a job that's actively fetching or building**, and
  **never touches a metadata-only (already-cleared) job** — those stay until you
  delete them by hand.
- Because the folder's timestamp updates every time a tile lands, a job you're
  actively resuming keeps resetting its own 24h clock.

---

## Run it — double-click launcher (easiest)

No commands needed. After getting the folder (clone, or **Code → Download ZIP**
on GitHub and unzip):

- **Windows:** double-click **`start.bat`**
- **macOS:** double-click **`start.command`** (first time: right-click → Open to
  clear the "unidentified developer" warning)
- **Linux:** run **`./start.sh`**

The first launch creates a Python environment and installs dependencies (a
minute or two); later launches start in seconds. Your browser opens at
<http://localhost:8000> automatically. **Leave the terminal window open while you
use the app — closing it stops the app.** You need Python 3.9+ installed; the
launcher tells you where to get it if it's missing.

## Run it — manually

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --reload
```

Open <http://localhost:8000>. Draw a rectangle, adjust settings if you like,
and click **Generate Excel**.

> **Network note:** the app calls Esri's public print service
> (`utility.arcgisonline.com`) and ArcGIS Online (`www.arcgis.com`). Run it
> somewhere with open outbound HTTPS to those hosts. (Some locked-down/CI
> sandboxes block them at the egress proxy.)

The first tile fetch logs the web map's operational layer URLs, e.g.:

```
[webmap] 3 operational layer(s):
  [webmap]  'Bridges' -> https://.../FeatureServer/0
```

Paste the bridge layer URL into `BRIDGE_LAYER_URL` at the top of
`static/app.js` to overlay the bridge flags on the draw map.

---

## Automatic updates

The app updates itself from GitHub — users never re-download anything after the
one-time setup.

- On launch (and on page load) the app compares its local `VERSION` to the
  `VERSION` file on GitHub's `main` branch. If GitHub's is newer, a green
  **"A new version is available"** banner appears with an **Update now** button.
- Clicking it downloads the latest code straight from GitHub, overwrites the
  app's files, and the launcher restarts the app — reinstalling Python
  dependencies automatically if the update added any. The page then reloads
  itself into the new version. **Cached tiles, saved areas, and the Python
  environment are left untouched.**
- If GitHub can't be reached, the check silently does nothing — the app keeps
  working offline.

The launchers (`start.bat` / `start.command` / `start.sh`) are the one thing the
auto-updater deliberately does **not** overwrite (a running launcher can't
safely replace itself). They rarely change; if one ever must, re-send the folder
that once.

### Shipping an update (maintainer)

1. Make your changes and **bump the number in the `VERSION` file** (it's a plain
   integer: `1` → `2` → `3` …).
2. Commit and push to `main`.

That's it. Next time each user opens the app — or reloads the page — they'll see
the update banner and can pull it in with one click.

## Settings

| Control | Default | Meaning |
|---|---|---|
| Map scale | `6037.076507919011` | Detail level (matches the site's print dialog). Lower = more zoomed in. |
| Tile size (px) | `4000` | Pixels per tile side. |
| Excel display scale | `1.0` | Shrinks how large each tile is drawn in Excel (e.g. `0.5` for a lighter file). Seams stay exact. |

The DPI (96) matches the print dialog and can be passed to `/export` if needed.

### Guardrails

- Jobs over **200 tiles** are rejected with a clear message (draw smaller or
  raise the scale number).
- At most **2 concurrent** fetching jobs.
- Each tile fetch has 3 retries and a per-request timeout.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/export` | `{nw_lat, nw_lon, se_lat, se_lon, scale, tile_px, dpi, excel_scale}` → starts a job, returns `{job_id, ncols, nrows, total_tiles}`. |
| `GET` | `/plan` | Grid estimate (tiles/rows/cols) for the frontend. No network. |
| `GET` | `/progress/{job_id}` | Server-Sent Events stream of `{done, total, status}`. |
| `GET` | `/status/{job_id}` | Polling fallback for progress. |
| `GET` | `/download/{job_id}` | The finished `.xlsx` as an attachment. |
| `POST` | `/clear/{job_id}` | Delete the cached images + Excel, keep the saved entry. (`/confirm` is a back-compat alias.) |
| `DELETE` | `/jobs/{job_id}` | Delete the job entirely, including its saved entry. |
| `GET` | `/jobs` | List remembered areas (box, cache status, Excel name). |
| `GET` | `/version` | The app's current build number. |
| `GET` | `/check-update` | Compares local vs GitHub `VERSION`; reports if an update exists. |
| `POST` | `/update` | Downloads + applies the latest code, then restarts. |

---

## Deploy on Render.com

Create a new **Web Service** from this repo:

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `uvicorn app:app --host 0.0.0.0 --port $PORT`

Render provides `$PORT`. No other configuration is required. Note that on
Render's ephemeral disk the `jobs/` cache does not survive a redeploy — download
your Excel before redeploying.

---

## File tree

```
app.py              FastAPI service: endpoints, background jobs, caching, SSE
pipeline.py         Proven pipeline — grid math, tile fetch, Excel anchoring
static/index.html   Draw-a-box map UI
static/app.js       Map + draw + progress-bar glue
static/style.css    Styling
requirements.txt
README.md
map_export.py       Original command-line prototype (reference / source of truth)
```
