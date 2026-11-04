"""Daily coarse context snapshots (VIIRS / MODIS corrected reflectance) from NASA GIBS.

Covers a wider southern-Alberta box (smoke, snow line, plumes) — not for city-scale change.
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.parse
import urllib.request

from rasterio.warp import transform_bounds

from .grid import CONFIG, ROOT

WMS = "https://gibs.earthdata.nasa.gov/wms/epsg3857/best/wms.cgi"
LAYERS = {
    "viirs-noaa20": "VIIRS_NOAA20_CorrectedReflectance_TrueColor",
    "viirs-noaa21": "VIIRS_NOAA21_CorrectedReflectance_TrueColor",
    "viirs-snpp": "VIIRS_SNPP_CorrectedReflectance_TrueColor",
    "modis-terra": "MODIS_Terra_CorrectedReflectance_TrueColor",
    "modis-aqua": "MODIS_Aqua_CorrectedReflectance_TrueColor",
}
WIDTH = 1400


def fetch_day(day: dt.date) -> list[dict]:
    bb = CONFIG["viirs_context_bbox_lnglat"]
    w, s, e, n = transform_bounds("EPSG:4326", "EPSG:3857", *bb)
    h = round(WIDTH * (n - s) / (e - w))
    out_dir = ROOT / "archive" / "daily-context" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
    recs = []
    for key, layer in LAYERS.items():
        path = out_dir / f"{key}.jpg"
        if path.exists():
            continue
        q = {"SERVICE": "WMS", "REQUEST": "GetMap", "VERSION": "1.3.0", "LAYERS": layer, "STYLES": "",
             "CRS": "EPSG:3857", "BBOX": f"{w},{s},{e},{n}", "WIDTH": WIDTH, "HEIGHT": h,
             "FORMAT": "image/jpeg", "TIME": day.isoformat()}
        try:
            data = urllib.request.urlopen(WMS + "?" + urllib.parse.urlencode(q), timeout=90).read()
            if len(data) < 20_000:  # empty/blank tile
                continue
            out_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            recs.append({"date": day.isoformat(), "layer": key, "gibs_layer": layer,
                         "path": str(path.relative_to(ROOT)), "bounds_lnglat": bb,
                         "attribution": "NASA EOSDIS GIBS / Worldview"})
        except Exception as err:  # noqa: BLE001
            print(f"[viirs] {key} {day}: {err}")
    if recs:
        idx = out_dir / "index.json"
        prev = json.loads(idx.read_text()) if idx.exists() else []
        idx.write_text(json.dumps(prev + recs, indent=1))
    return recs
