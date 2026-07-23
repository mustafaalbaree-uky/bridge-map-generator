"""
app.py - FastAPI web service around the proven Bridge-Map -> Excel pipeline.

Flow:
  * User draws a box on the map (frontend) and POSTs /export.
  * We plan the tile grid, guard against oversized jobs, and start a background
    thread that fetches each tile from Esri's print service and writes it to
    jobs/<job_id>/tiles/ ON DISK. Tiles are CACHED there so an interrupted run
    resumes from what it already has, and they are NOT deleted automatically -
    they stay until the user confirms the Excel is good (POST /confirm) or the
    24h safety sweep removes stale jobs.
  * Progress streams over SSE at /progress/<job_id>.
  * The finished .xlsx downloads at /download/<job_id>.

The tile fetch and the openpyxl AbsoluteAnchor placement live in pipeline.py and
are preserved exactly from the command-line prototype.
"""

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import pipeline

# ------------------------------------------------------------------ config ---
BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "jobs"          # persistent tile cache lives here
STATIC_DIR = BASE_DIR / "static"
JOBS_DIR.mkdir(exist_ok=True)

# ---- self-update (pulls new code from GitHub on the user's click) ----
REPO_SLUG = "mustafaalbaree-uky/bridge-map-generator"
UPDATE_BRANCH = "main"
VERSION_FILE = BASE_DIR / "VERSION"
RAW_VERSION_URL = f"https://raw.githubusercontent.com/{REPO_SLUG}/{UPDATE_BRANCH}/VERSION"
ZIP_URL = f"https://codeload.github.com/{REPO_SLUG}/zip/refs/heads/{UPDATE_BRANCH}"
UPDATE_SENTINEL = BASE_DIR / ".do_update"   # launcher watches this to restart
# Never overwrite/replace these during an update (runtime data, env, launchers).
UPDATE_SKIP = {".venv", "jobs", ".git", ".do_update", "__pycache__",
               "start.bat", "start.command", "start.sh"}

MAX_TILES = 200                        # reject jobs larger than this
MAX_CONCURRENT_JOBS = 2                # cap simultaneous fetching jobs
TILE_TIMEOUT_S = 240                   # per-tile fetch timeout
CLEANUP_TTL_S = 24 * 3600              # safety sweep for abandoned jobs
RETRIES = 3
PAUSE_S = 0.4

# In-memory progress registry. Each job dir also has meta.json so state survives
# a server restart (tiles on disk are the real cache; this mirrors it).
_JOBS = {}
_LOCK = threading.Lock()

# Cache the web map definition once per process (basemap + bridge layers).
_WEBMAP_CACHE = {"operational": None, "basemap": None}
_WEBMAP_LOCK = threading.Lock()

app = FastAPI(title="KYTC Bridge Map -> Excel")


@app.middleware("http")
async def _no_cache_static(request, call_next):
    """Never let the browser cache the app's own HTML/JS/CSS - otherwise after
    a self-update it would keep running the old page and mismatch the backend."""
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


# ------------------------------------------------------------- job helpers ---
def _job_dir(job_id):
    return JOBS_DIR / job_id


def _meta_path(job_id):
    return _job_dir(job_id) / "meta.json"


def _tiles_dir(job_id):
    return _job_dir(job_id) / "tiles"


def _xlsx_path(job_id):
    return _job_dir(job_id) / "map.xlsx"


def _write_meta(job_id):
    """Persist the in-memory record to disk (best effort)."""
    rec = _JOBS.get(job_id)
    if not rec:
        return
    try:
        _meta_path(job_id).write_text(json.dumps(rec))
    except OSError:
        pass


def _set(job_id, **changes):
    with _LOCK:
        rec = _JOBS.setdefault(job_id, {})
        rec.update(changes)
    _write_meta(job_id)


def _get(job_id):
    with _LOCK:
        rec = _JOBS.get(job_id)
        return dict(rec) if rec else None


def _load_jobs_from_disk():
    """Rehydrate job records from disk so /progress and /download survive a
    restart. Jobs that were mid-fetch are marked interrupted (their cached
    tiles remain and can be resumed by re-running the same box)."""
    for d in JOBS_DIR.glob("*"):
        if not d.is_dir():
            continue
        meta = d / "meta.json"
        if not meta.exists():
            continue
        try:
            rec = json.loads(meta.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("status") == "running":
            rec["status"] = "interrupted"
            rec["error"] = "Server restarted while fetching; tiles are cached."
        _JOBS[d.name] = rec


def _running_count():
    with _LOCK:
        return sum(1 for r in _JOBS.values() if r.get("status") == "running")


def _get_webmap(session):
    with _WEBMAP_LOCK:
        if _WEBMAP_CACHE["operational"] is None:
            op, bm = pipeline.fetch_webmap(session=session)
            # Log operational layer URLs so the bridge layer can be hardcoded
            # into the frontend overlay (spec step: "log it so we can hardcode").
            print(f"[webmap] {len(op)} operational layer(s):")
            for L in op:
                print(f"  [webmap]  {L.get('title')!r} -> {L.get('url')}")
            _WEBMAP_CACHE["operational"] = op
            _WEBMAP_CACHE["basemap"] = bm
        return _WEBMAP_CACHE["operational"], _WEBMAP_CACHE["basemap"]


# ------------------------------------------------------------ background job --
def _run_job(job_id, params, plan):
    tiles_dir = _tiles_dir(job_id)
    tiles_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()

    try:
        op, bm = _get_webmap(session)
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", error=f"Could not load web map: {e}")
        return

    tile_px = params["tile_px"]
    dpi = params["dpi"]
    tile_paths = {}
    done = 0

    for t in plan["tiles"]:
        # allow cancellation via status flag
        cur = _get(job_id)
        if cur and cur.get("status") == "cancelled":
            return
        r, c = t["row"], t["col"]
        path = tiles_dir / f"tile_r{r}_c{c}.png"
        if path.exists():  # resume: reuse cached tile
            tile_paths[(r, c)] = str(path)
            done += 1
            _set(job_id, done=done)
            continue
        try:
            im = pipeline.export_tile_with_retries(
                t, op, bm, tile_px, dpi,
                retries=RETRIES, pause_s=PAUSE_S, session=session,
            )
            im.save(path)
            tile_paths[(r, c)] = str(path)
            done += 1
            _set(job_id, done=done)
        except Exception as e:  # noqa: BLE001 - keep cached tiles, stop here
            _set(job_id, status="error",
                 error=f"Tile r{r} c{c} failed after {RETRIES} tries: {e}. "
                       f"Cached tiles are kept - re-run to resume.")
            return
        time.sleep(PAUSE_S)

    # ---- build the Excel from the cached tiles ----
    _set(job_id, status="building")
    try:
        pipeline.build_workbook(
            tile_paths, tile_px, params["excel_scale"], str(_xlsx_path(job_id))
        )
    except Exception as e:  # noqa: BLE001
        _set(job_id, status="error", error=f"Excel build failed: {e}")
        return

    _set(job_id, status="done", done=done)


# --------------------------------------------------------------- API models ---
class ExportRequest(BaseModel):
    nw_lat: float
    nw_lon: float
    se_lat: float
    se_lon: float
    scale: float = Field(default=pipeline.DEFAULT_SCALE, gt=0)
    tile_px: int = Field(default=pipeline.DEFAULT_TILE_PX, gt=0, le=8000)
    dpi: int = Field(default=pipeline.DEFAULT_DPI, gt=0, le=600)
    excel_scale: float = Field(default=pipeline.DEFAULT_EXCEL_DISPLAY_SCALE,
                               gt=0, le=1.0)


# ----------------------------------------------------------------- endpoints --
@app.post("/export")
def export(req: ExportRequest):
    # basic geometry sanity
    if not (-90 <= req.nw_lat <= 90 and -90 <= req.se_lat <= 90):
        raise HTTPException(400, "Latitude must be between -90 and 90.")
    if not (-180 <= req.nw_lon <= 180 and -180 <= req.se_lon <= 180):
        raise HTTPException(400, "Longitude must be between -180 and 180.")
    if req.nw_lat == req.se_lat or req.nw_lon == req.se_lon:
        raise HTTPException(400, "The box has zero width or height.")

    plan = pipeline.plan_grid(
        req.nw_lat, req.nw_lon, req.se_lat, req.se_lon,
        req.scale, req.tile_px, req.dpi,
    )
    if plan["total_tiles"] > MAX_TILES:
        raise HTTPException(
            400,
            f"That box needs {plan['total_tiles']} tiles "
            f"({plan['nrows']}x{plan['ncols']}), over the {MAX_TILES} cap. "
            f"Draw a smaller box or raise the scale number.",
        )
    if _running_count() >= MAX_CONCURRENT_JOBS:
        raise HTTPException(
            429, "The server is busy with other exports. Try again shortly.")

    params = {
        "nw_lat": req.nw_lat, "nw_lon": req.nw_lon,
        "se_lat": req.se_lat, "se_lon": req.se_lon,
        "scale": req.scale, "tile_px": req.tile_px,
        "dpi": req.dpi, "excel_scale": req.excel_scale,
    }
    # Deterministic job id from the box + tile-defining settings (NOT excel_scale,
    # which only affects placement). Same area => same cache folder => an
    # interrupted run RESUMES from the tiles already on disk instead of
    # re-downloading them.
    key = "|".join(str(params[k]) for k in
                   ("nw_lat", "nw_lon", "se_lat", "se_lon", "scale", "tile_px", "dpi"))
    job_id = hashlib.sha1(key.encode()).hexdigest()[:12]

    existing = _get(job_id)
    if existing and existing.get("status") in ("running", "building"):
        # already in flight - attach to it rather than starting a second thread
        return {
            "job_id": job_id, "ncols": existing.get("ncols", plan["ncols"]),
            "nrows": existing.get("nrows", plan["nrows"]),
            "total_tiles": existing.get("total", plan["total_tiles"]),
            "resumed": True,
        }

    _job_dir(job_id).mkdir(parents=True, exist_ok=True)
    _set(
        job_id,
        status="running", done=0, total=plan["total_tiles"],
        nrows=plan["nrows"], ncols=plan["ncols"],
        params=params, created=time.time(), error=None,
    )
    cached = len(list(_tiles_dir(job_id).glob("*.png"))) \
        if _tiles_dir(job_id).exists() else 0
    # strip the heavy tile list before threading - the job recomputes as needed
    threading.Thread(
        target=_run_job, args=(job_id, params, plan), daemon=True
    ).start()
    return {
        "job_id": job_id,
        "ncols": plan["ncols"],
        "nrows": plan["nrows"],
        "total_tiles": plan["total_tiles"],
        "cached": cached,
        "resumed": cached > 0,
    }


@app.get("/plan")
def plan_estimate(nw_lat: float, nw_lon: float, se_lat: float, se_lon: float,
                  scale: float = pipeline.DEFAULT_SCALE,
                  tile_px: int = pipeline.DEFAULT_TILE_PX,
                  dpi: int = pipeline.DEFAULT_DPI):
    """Lightweight grid estimate for the frontend (no network, no job)."""
    plan = pipeline.plan_grid(nw_lat, nw_lon, se_lat, se_lon, scale, tile_px, dpi)
    return {
        "nrows": plan["nrows"], "ncols": plan["ncols"],
        "total_tiles": plan["total_tiles"], "max_tiles": MAX_TILES,
        "mpp": plan["mpp"], "tile_m": plan["tile_m"],
    }


@app.get("/progress/{job_id}")
async def progress(job_id: str):
    if _get(job_id) is None:
        raise HTTPException(404, "Unknown job.")

    async def event_gen():
        import asyncio
        while True:
            rec = _get(job_id)
            if rec is None:
                yield {"event": "error", "data": json.dumps({"error": "gone"})}
                return
            payload = {
                "done": rec.get("done", 0),
                "total": rec.get("total", 0),
                "status": rec.get("status", "unknown"),
                "error": rec.get("error"),
            }
            yield {"data": json.dumps(payload)}
            if rec.get("status") in ("done", "error", "cancelled"):
                return
            await asyncio.sleep(0.5)

    return EventSourceResponse(event_gen())


@app.get("/status/{job_id}")
def status(job_id: str):
    """Polling fallback for environments where SSE is awkward."""
    rec = _get(job_id)
    if rec is None:
        raise HTTPException(404, "Unknown job.")
    return {
        "done": rec.get("done", 0),
        "total": rec.get("total", 0),
        "status": rec.get("status"),
        "error": rec.get("error"),
    }


@app.get("/download/{job_id}")
def download(job_id: str):
    rec = _get(job_id)
    if rec is None:
        raise HTTPException(404, "Unknown job.")
    xlsx = _xlsx_path(job_id)
    if not xlsx.exists():
        raise HTTPException(409, "The Excel file is not ready yet.")
    return FileResponse(
        str(xlsx),
        media_type="application/vnd.openxmlformats-officedocument."
                   "spreadsheetml.sheet",
        filename=f"bridge_map_{job_id}.xlsx",
    )


@app.post("/clear/{job_id}")
def clear_cache(job_id: str):
    """Delete the HEAVY files (cached tile images + the built .xlsx) to free
    disk, but KEEP the metadata so the area still shows under "Saved areas"
    until the user explicitly deletes it."""
    d = _job_dir(job_id)
    if not d.exists() and _get(job_id) is None:
        raise HTTPException(404, "Unknown job.")
    tdir = _tiles_dir(job_id)
    if tdir.exists():
        shutil.rmtree(tdir, ignore_errors=True)
    xlsx = _xlsx_path(job_id)
    if xlsx.exists():
        try:
            xlsx.unlink()
        except OSError:
            pass
    _set(job_id, cache_cleared=True, done=0)  # keeps/rewrites meta.json
    return {"ok": True, "cleared": job_id}


# Back-compat alias: /confirm now means "clear cache, keep the entry".
@app.post("/confirm/{job_id}")
def confirm(job_id: str):
    return clear_cache(job_id)


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    """Remove the job entirely - cached tiles, Excel, AND the metadata."""
    d = _job_dir(job_id)
    if not d.exists() and _get(job_id) is None:
        raise HTTPException(404, "Unknown job.")
    shutil.rmtree(d, ignore_errors=True)
    with _LOCK:
        _JOBS.pop(job_id, None)
    return {"ok": True, "deleted": job_id}


@app.get("/jobs")
def list_jobs():
    """List remembered jobs: the drawn box, whether tiles are still cached,
    and what the Excel was saved as. Powers the "Saved areas" menu."""
    out = []
    with _LOCK:
        recs = list(_JOBS.items())
    for jid, rec in recs:
        tdir = _tiles_dir(jid)
        cached = len(list(tdir.glob("*.png"))) if tdir.exists() else 0
        out.append({
            "job_id": jid,
            "status": rec.get("status"),
            "done": rec.get("done", 0),
            "total": rec.get("total", 0),
            "cached": cached,
            "created": rec.get("created"),
            "params": rec.get("params"),          # the drawn box + settings
            "has_xlsx": _xlsx_path(jid).exists(),
            "xlsx_name": f"bridge_map_{jid}.xlsx",
            "cache_cleared": rec.get("cache_cleared", False),
        })
    out.sort(key=lambda r: r.get("created") or 0, reverse=True)
    return out


# ------------------------------------------------------------- self-update ---
def _local_version():
    try:
        return int(VERSION_FILE.read_text().strip())
    except (OSError, ValueError):
        return 0


def _remote_version(timeout=15):
    r = requests.get(RAW_VERSION_URL, timeout=timeout)
    r.raise_for_status()
    return int(r.text.strip())


def _copy_tree(src, dst):
    for item in src.iterdir():
        if item.name in UPDATE_SKIP:
            continue
        target = dst / item.name
        if item.is_dir():
            target.mkdir(exist_ok=True)
            _copy_tree(item, target)
        else:
            shutil.copy2(item, target)


def _apply_update():
    """Download the repo ZIP from GitHub and overwrite the app's own files.
    Runtime data (jobs/, .venv/) and the launchers are left untouched."""
    import tempfile
    import zipfile
    r = requests.get(ZIP_URL, timeout=180)
    r.raise_for_status()
    tmp = Path(tempfile.mkdtemp(prefix="bmg_update_"))
    try:
        zpath = tmp / "src.zip"
        zpath.write_bytes(r.content)
        xdir = tmp / "x"
        with zipfile.ZipFile(zpath) as z:
            z.extractall(xdir)
        roots = [p for p in xdir.iterdir() if p.is_dir()]
        if not roots:
            raise RuntimeError("downloaded archive was empty")
        _copy_tree(roots[0], BASE_DIR)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _schedule_restart(delay=1.5):
    """Signal the launcher to restart us, then exit hard so it takes over."""
    UPDATE_SENTINEL.write_text("restart")

    def _bye():
        time.sleep(delay)
        os._exit(0)

    threading.Thread(target=_bye, daemon=True).start()


@app.get("/version")
def version():
    return {"version": _local_version()}


@app.get("/check-update")
def check_update():
    cur = _local_version()
    try:
        latest = _remote_version()
    except Exception as e:  # noqa: BLE001 - offline is fine, just no update
        return {"current": cur, "latest": None,
                "update_available": False, "error": str(e)}
    return {"current": cur, "latest": latest, "update_available": latest > cur}


@app.post("/update")
def update():
    cur = _local_version()
    try:
        latest = _remote_version()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Could not reach GitHub to check for updates: {e}")
    if latest <= cur:
        return {"ok": True, "updated": False, "version": cur,
                "message": "Already up to date."}
    try:
        _apply_update()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Update download/apply failed: {e}")
    _schedule_restart()
    return {"ok": True, "updated": True, "restarting": True,
            "from": cur, "to": latest}


# ----------------------------------------------------------------- cleanup ---
def _cleanup_loop():
    while True:
        now = time.time()
        for d in list(JOBS_DIR.glob("*")):
            if not d.is_dir():
                continue
            # Never sweep a job that's actively fetching or building.
            rec = _get(d.name)
            if rec and rec.get("status") in ("running", "building"):
                continue
            # Only sweep jobs that still hold HEAVY files (tiles). A job whose
            # cache was cleared is metadata-only - keep it until the user
            # deletes it from "Saved areas".
            tdir = d / "tiles"
            has_tiles = tdir.exists() and any(tdir.glob("*.png"))
            if not has_tiles:
                continue
            try:
                age = now - d.stat().st_mtime
            except OSError:
                continue
            if age > CLEANUP_TTL_S:
                shutil.rmtree(d, ignore_errors=True)
                with _LOCK:
                    _JOBS.pop(d.name, None)
        time.sleep(600)


@app.on_event("startup")
def _startup():
    _load_jobs_from_disk()
    threading.Thread(target=_cleanup_loop, daemon=True).start()


# static frontend LAST so it doesn't shadow the API routes
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
