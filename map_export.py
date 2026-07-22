"""
Bridge Data Miner -> seamless map tiles -> Excel  (COMMAND-LINE PROTOTYPE)

Fetches the SAME map the website's Print button produces (basemap + bridge
layers), but for exact coordinate boxes, so tiles line up pixel-perfect with
zero eyeballing. Stitches them and places them into an .xlsx you can annotate.

This is the proven reference prototype. The web app (app.py / pipeline.py)
reuses this exact logic; keep this file as the source of truth.

FIRST RUN: leave TEST_MODE = True. It grabs a 2x2 block near the county center
(~4 tiles, ~1 minute). Open the outputs, confirm the look and that the seams are
invisible. THEN set TEST_MODE = False for the whole county.

Install once (PowerShell):  pip install requests openpyxl pillow
"""

import os, io, json, math, time
import requests
from PIL import Image as PILImage
from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.drawing.spreadsheet_drawing import AbsoluteAnchor
from openpyxl.drawing.xdr import XDRPoint2D, XDRPositiveSize2D
from openpyxl.utils.units import pixels_to_EMU

# ============================ CONFIG ============================
# The Bridge Data Miner web map (this holds the basemap + bridge layers).
WEBMAP_ID = "30d855df73574e5196fa49081f08d069"

# Esri's public print/export service (same engine the website's Print uses).
EXPORT_URL = ("https://utility.arcgisonline.com/arcgis/rest/services/"
              "Utilities/PrintingTools/GPServer/Export%20Web%20Map%20Task/execute")

# The scale you typed into the print dialog, and the tile size.
SCALE = 6037.076507919011
DPI = 96            # print dialog default; leave as-is unless yours differed
TILE_PX = 4000      # pixels per tile side (detail is set by SCALE, not this)

# ---- WHERE TO CAPTURE ------------------------------------------
# Pick ONE method by setting SPEC_MODE, then fill in that method's numbers.
SPEC_MODE = "corners"        # "corners" or "center_miles"

# Method 1 - two opposite corners, read straight off Google Maps.
# Right-click a spot in Google Maps, it shows "lat, lon"; click to copy.
# NW = top-left corner of your box, SE = bottom-right corner.
NW_LAT, NW_LON = 38.17, -85.17   # top-left
SE_LAT, SE_LON = 37.86, -84.79   # bottom-right

# Method 2 - a center point plus how big a box, in miles.
CENTER_LAT, CENTER_LON = 38.015, -84.98
WIDTH_MI, HEIGHT_MI = 21.0, 21.0

BUFFER_M = 0   # extra meters padded on every side, on top of the box above

# Run control
TEST_MODE = False              # False = whole box; True = quick 2x2 center check
OUT_DIR = "anderson_out"
MAKE_STITCHED_PNG = True       # one big seamless PNG (auto-skipped if huge)
STITCH_MAX_PX = 16000          # downscale the big PNG past this (memory guard)
EXCEL_DISPLAY_SCALE = 1.0      # lower to ~0.5 if the .xlsx gets too heavy

RETRIES = 3
PAUSE_S = 0.4                  # be polite between requests
# ================================================================

R = 6378137.0


def lonlat_to_3857(lon, lat):
    x = math.radians(lon) * R
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * R
    return x, y


def mpp_from_scale(scale, dpi):
    return scale * 0.0254 / dpi


def resolve_lonlat_box():
    """Turn whichever spec method into (lon_min, lat_min, lon_max, lat_max)."""
    if SPEC_MODE == "corners":
        lon_min, lon_max = sorted((NW_LON, SE_LON))
        lat_min, lat_max = sorted((NW_LAT, SE_LAT))
    elif SPEC_MODE == "center_miles":
        dlat = (HEIGHT_MI / 69.055) / 2.0
        dlon = (WIDTH_MI / (69.055 * math.cos(math.radians(CENTER_LAT)))) / 2.0
        lon_min, lon_max = CENTER_LON - dlon, CENTER_LON + dlon
        lat_min, lat_max = CENTER_LAT - dlat, CENTER_LAT + dlat
    else:
        raise ValueError("SPEC_MODE must be 'corners' or 'center_miles'")
    return lon_min, lat_min, lon_max, lat_max


def build_grid(xmin, ymin, xmax, ymax, tile_px, mpp):
    tile_m = tile_px * mpp
    ncols = math.ceil((xmax - xmin) / tile_m)
    nrows = math.ceil((ymax - ymin) / tile_m)
    tiles = []
    for r in range(nrows):
        t_ymax = ymax - r * tile_m
        t_ymin = t_ymax - tile_m
        for c in range(ncols):
            t_xmin = xmin + c * tile_m
            tiles.append(dict(row=r, col=c, xmin=t_xmin, ymin=t_ymin,
                              xmax=t_xmin + tile_m, ymax=t_ymax))
    return tiles, nrows, ncols, tile_m


def fetch_webmap():
    print("Fetching web map definition (basemap + bridge layers)...")
    r = requests.get(
        f"https://www.arcgis.com/sharing/rest/content/items/{WEBMAP_ID}/data",
        params={"f": "json"}, timeout=60)
    r.raise_for_status()
    wm = r.json()
    if "operationalLayers" not in wm or "baseMap" not in wm:
        raise RuntimeError("Web map JSON missing layers; check WEBMAP_ID.")
    print(f"  {len(wm['operationalLayers'])} operational layer(s) loaded.")
    return wm["operationalLayers"], wm["baseMap"]


def export_tile(tile, operational, basemap):
    webmap_json = {
        "mapOptions": {
            "extent": {"xmin": tile["xmin"], "ymin": tile["ymin"],
                       "xmax": tile["xmax"], "ymax": tile["ymax"],
                       "spatialReference": {"wkid": 102100}},
            "spatialReference": {"wkid": 102100},
        },
        "operationalLayers": operational,
        "baseMap": basemap,
        "exportOptions": {"outputSize": [TILE_PX, TILE_PX], "dpi": DPI},
    }
    data = {"f": "json", "Format": "PNG32", "Layout_Template": "MAP_ONLY",
            "Web_Map_as_JSON": json.dumps(webmap_json)}
    resp = requests.post(EXPORT_URL, data=data, timeout=240).json()
    if "results" not in resp:
        raise RuntimeError(f"Print service error: {json.dumps(resp)[:400]}")
    img_url = resp["results"][0]["value"]["url"]
    img_bytes = requests.get(img_url, timeout=240).content
    return PILImage.open(io.BytesIO(img_bytes)).convert("RGB")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    tiles_dir = os.path.join(OUT_DIR, "tiles")
    os.makedirs(tiles_dir, exist_ok=True)

    mpp = mpp_from_scale(SCALE, DPI)
    lon_min, lat_min, lon_max, lat_max = resolve_lonlat_box()
    print(f"Area [{SPEC_MODE}]: NW ({lat_max:.4f}, {lon_min:.4f})  "
          f"SE ({lat_min:.4f}, {lon_max:.4f})")
    xmin, ymin = lonlat_to_3857(lon_min, lat_min)
    xmax, ymax = lonlat_to_3857(lon_max, lat_max)
    xmin -= BUFFER_M; ymin -= BUFFER_M; xmax += BUFFER_M; ymax += BUFFER_M

    tiles, nrows, ncols, tile_m = build_grid(xmin, ymin, xmax, ymax, TILE_PX, mpp)
    print(f"Scale {SCALE:.1f} -> {mpp:.3f} m/px | tile = {tile_m:.0f} m "
          f"({TILE_PX}px) | full grid {nrows} rows x {ncols} cols = {len(tiles)} tiles")

    if TEST_MODE:
        rc, cc = nrows // 2, ncols // 2
        wanted = {(rc, cc), (rc, cc + 1), (rc + 1, cc), (rc + 1, cc + 1)}
        tiles = [t for t in tiles if (t["row"], t["col"]) in wanted]
        rows = sorted({t["row"] for t in tiles}); cols = sorted({t["col"] for t in tiles})
        remap = {(t["row"], t["col"]): (rows.index(t["row"]), cols.index(t["col"])) for t in tiles}
        nrows, ncols = len(rows), len(cols)
        print(f"TEST_MODE: fetching {len(tiles)} tiles near center only.")
    else:
        remap = {(t["row"], t["col"]): (t["row"], t["col"]) for t in tiles}
        print("FULL RUN. This downloads every tile; may take several minutes.")

    operational, basemap = fetch_webmap()

    images = {}
    for i, t in enumerate(tiles, 1):
        rr, cc = remap[(t["row"], t["col"])]
        path = os.path.join(tiles_dir, f"tile_r{rr}_c{cc}.png")
        if os.path.exists(path):                      # resume: skip done tiles
            images[(rr, cc)] = PILImage.open(path).convert("RGB")
            print(f"  [{i}/{len(tiles)}] r{rr} c{cc} cached")
            continue
        for attempt in range(1, RETRIES + 1):
            try:
                im = export_tile(t, operational, basemap)
                im.save(path)
                images[(rr, cc)] = im
                print(f"  [{i}/{len(tiles)}] r{rr} c{cc} ok")
                break
            except Exception as e:
                print(f"  [{i}/{len(tiles)}] r{rr} c{cc} attempt {attempt} failed: {e}")
                time.sleep(1.5 * attempt)
        else:
            print(f"  !! giving up on r{rr} c{cc}; rerun to retry just this one.")
        time.sleep(PAUSE_S)

    # ---- optional single seamless PNG ----
    if MAKE_STITCHED_PNG and images:
        big_w, big_h = ncols * TILE_PX, nrows * TILE_PX
        if max(big_w, big_h) > STITCH_MAX_PX:
            print(f"Stitched PNG would be {big_w}x{big_h}px; skipping the giant "
                  f"single image (tiles are all saved individually).")
        else:
            canvas = PILImage.new("RGB", (big_w, big_h), (255, 255, 255))
            for (rr, cc), im in images.items():
                canvas.paste(im, (cc * TILE_PX, rr * TILE_PX))
            p = os.path.join(OUT_DIR, "stitched.png")
            canvas.save(p)
            print(f"Saved {p} ({big_w}x{big_h}px)")

    # ---- Excel with exact pixel-anchored tiles ----
    disp = int(TILE_PX * EXCEL_DISPLAY_SCALE)
    wb = Workbook(); ws = wb.active; ws.title = "Map"
    for (rr, cc), _ in images.items():
        src = os.path.join(tiles_dir, f"tile_r{rr}_c{cc}.png")
        img = XLImage(src)
        img.anchor = AbsoluteAnchor(
            pos=XDRPoint2D(pixels_to_EMU(cc * disp), pixels_to_EMU(rr * disp)),
            ext=XDRPositiveSize2D(pixels_to_EMU(disp), pixels_to_EMU(disp)))
        ws.add_image(img)
    xlsx = os.path.join(OUT_DIR, "map.xlsx")
    wb.save(xlsx)
    print(f"Saved {xlsx}  ->  open it, the tiles are placed edge to edge.")


if __name__ == "__main__":
    main()
