"""
pipeline.py — Bridge Data Miner -> seamless map tiles -> Excel.

This is the PROVEN pipeline from the command-line prototype (map_export.py),
refactored into reusable functions that take parameters instead of module
globals so the FastAPI backend can drive them. The grid math, the print-service
fetch, and the openpyxl AbsoluteAnchor placement are preserved exactly — they
are the hard-won, seam-perfect parts.
"""

import io
import json
import math
import time

import requests
from PIL import Image as PILImage
from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.drawing.spreadsheet_drawing import AbsoluteAnchor
from openpyxl.drawing.xdr import XDRPoint2D, XDRPositiveSize2D
from openpyxl.utils.units import pixels_to_EMU

# ---------------------------------------------------------------- constants ---
# The Bridge Data Miner web map (holds the basemap + bridge layers).
WEBMAP_ID = "30d855df73574e5196fa49081f08d069"

# Esri's public print/export service (the same engine the website's Print uses).
EXPORT_URL = (
    "https://utility.arcgisonline.com/arcgis/rest/services/"
    "Utilities/PrintingTools/GPServer/Export%20Web%20Map%20Task/execute"
)

WEBMAP_DATA_URL = (
    f"https://www.arcgis.com/sharing/rest/content/items/{WEBMAP_ID}/data"
)

# Proven defaults (match the site's print dialog).
DEFAULT_SCALE = 6037.076507919011
DEFAULT_DPI = 96
DEFAULT_TILE_PX = 4000
DEFAULT_EXCEL_DISPLAY_SCALE = 1.0

R = 6378137.0  # Web Mercator sphere radius (meters)


# --------------------------------------------------------------- grid math ---
def lonlat_to_3857(lon, lat):
    """lon/lat (degrees) -> Web Mercator meters (EPSG:3857)."""
    x = math.radians(lon) * R
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * R
    return x, y


def mpp_from_scale(scale, dpi):
    """Meters per pixel at a given map scale and DPI."""
    return scale * 0.0254 / dpi


def build_grid(xmin, ymin, xmax, ymax, tile_px, mpp):
    """Build the tile grid in 3857.

    Row 0 is the TOP (north = max Y). Each tile's box starts exactly where the
    previous one ended, so tiles abut perfectly by construction.
    """
    tile_m = tile_px * mpp
    ncols = math.ceil((xmax - xmin) / tile_m)
    nrows = math.ceil((ymax - ymin) / tile_m)
    tiles = []
    for r in range(nrows):
        t_ymax = ymax - r * tile_m
        t_ymin = t_ymax - tile_m
        for c in range(ncols):
            t_xmin = xmin + c * tile_m
            tiles.append(
                dict(
                    row=r,
                    col=c,
                    xmin=t_xmin,
                    ymin=t_ymin,
                    xmax=t_xmin + tile_m,
                    ymax=t_ymax,
                )
            )
    return tiles, nrows, ncols, tile_m


def resolve_box_from_corners(nw_lat, nw_lon, se_lat, se_lon):
    """NW/SE lat-lon corners -> (lon_min, lat_min, lon_max, lat_max)."""
    lon_min, lon_max = sorted((nw_lon, se_lon))
    lat_min, lat_max = sorted((nw_lat, se_lat))
    return lon_min, lat_min, lon_max, lat_max


def plan_grid(nw_lat, nw_lon, se_lat, se_lon, scale, tile_px, dpi):
    """Compute the full grid plan for a lat-lon box. No network calls."""
    mpp = mpp_from_scale(scale, dpi)
    lon_min, lat_min, lon_max, lat_max = resolve_box_from_corners(
        nw_lat, nw_lon, se_lat, se_lon
    )
    xmin, ymin = lonlat_to_3857(lon_min, lat_min)
    xmax, ymax = lonlat_to_3857(lon_max, lat_max)
    tiles, nrows, ncols, tile_m = build_grid(xmin, ymin, xmax, ymax, tile_px, mpp)
    return {
        "tiles": tiles,
        "nrows": nrows,
        "ncols": ncols,
        "tile_m": tile_m,
        "mpp": mpp,
        "total_tiles": len(tiles),
    }


# ------------------------------------------------------------- print fetch ---
def fetch_webmap(session=None, timeout=60):
    """Fetch the web map definition (basemap + bridge layers)."""
    session = session or requests
    r = session.get(WEBMAP_DATA_URL, params={"f": "json"}, timeout=timeout)
    r.raise_for_status()
    wm = r.json()
    if "operationalLayers" not in wm or "baseMap" not in wm:
        raise RuntimeError("Web map JSON missing layers; check WEBMAP_ID.")
    return wm["operationalLayers"], wm["baseMap"]


def export_tile(tile, operational, basemap, tile_px, dpi, session=None,
                timeout=240):
    """Fetch ONE tile from the print service and return an RGB PIL image."""
    session = session or requests
    webmap_json = {
        "mapOptions": {
            "extent": {
                "xmin": tile["xmin"],
                "ymin": tile["ymin"],
                "xmax": tile["xmax"],
                "ymax": tile["ymax"],
                "spatialReference": {"wkid": 102100},
            },
            "spatialReference": {"wkid": 102100},
        },
        "operationalLayers": operational,
        "baseMap": basemap,
        "exportOptions": {"outputSize": [tile_px, tile_px], "dpi": dpi},
    }
    data = {
        "f": "json",
        "Format": "PNG32",
        "Layout_Template": "MAP_ONLY",
        "Web_Map_as_JSON": json.dumps(webmap_json),
    }
    resp = session.post(EXPORT_URL, data=data, timeout=timeout).json()
    if "results" not in resp:
        raise RuntimeError(f"Print service error: {json.dumps(resp)[:400]}")
    img_url = resp["results"][0]["value"]["url"]
    img_bytes = session.get(img_url, timeout=timeout).content
    return PILImage.open(io.BytesIO(img_bytes)).convert("RGB")


def export_tile_with_retries(tile, operational, basemap, tile_px, dpi,
                             retries=3, pause_s=0.4, session=None):
    """export_tile with retries and a polite backoff. Returns RGB PIL image."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return export_tile(tile, operational, basemap, tile_px, dpi, session)
        except Exception as e:  # noqa: BLE001 — surface after retries exhausted
            last_err = e
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"tile r{tile['row']} c{tile['col']} failed: {last_err}")


# ----------------------------------------------------------- excel writing ---
def build_workbook(tile_paths, tile_px, excel_display_scale, out_path):
    """Place each tile with an AbsoluteAnchor at exact EMU pixel positions.

    THIS IS THE CRITICAL, SEAM-PERFECT PART — kept byte-for-byte from the
    prototype. Seams are exact and independent of column widths.

    ``tile_paths`` maps (row, col) -> filesystem path of an RGB PNG tile.
    """
    disp = int(tile_px * excel_display_scale)  # display size per tile
    wb = Workbook()
    ws = wb.active
    ws.title = "Map"
    for (row, col), src in sorted(tile_paths.items()):
        img = XLImage(src)
        img.anchor = AbsoluteAnchor(
            pos=XDRPoint2D(pixels_to_EMU(col * disp), pixels_to_EMU(row * disp)),
            ext=XDRPositiveSize2D(pixels_to_EMU(disp), pixels_to_EMU(disp)),
        )
        ws.add_image(img)
    wb.save(out_path)
    return out_path
