"""Discovery and per-sensor processing for the free constellation.

Sentinel-2 L2A  -> Element84 Earth Search (public AWS COGs)
Landsat 8/9 L2  -> Microsoft Planetary Computer (signed URLs)
Sentinel-1 RTC  -> Microsoft Planetary Computer (signed URLs)
VIIRS / MODIS   -> NASA GIBS daily corrected-reflectance snapshots (see viirs.py)
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import planetary_computer as pc
import pystac_client
from rasterio.enums import Resampling

from .grid import CONFIG, Grid, make_grid, mosaic, read_to_grid

EARTH_SEARCH = "https://earth-search.aws.element84.com/v1"
PLANETARY = "https://planetarycomputer.microsoft.com/api/stac/v1"

# mask classes stored in analysis.tif band "mask"
NODATA, CLEAR, CLOUD, SNOW, WATER = 0, 1, 2, 3, 4

SENSORS = {
    "sentinel-2": dict(api=EARTH_SEARCH, collection="sentinel-2-l2a", family="sentinel-2"),
    "landsat": dict(api=PLANETARY, collection="landsat-c2-l2", family="landsat",
                    query={"platform": {"in": ["landsat-8", "landsat-9"]}}),
    "sentinel-1": dict(api=PLANETARY, collection="sentinel-1-rtc", family="sentinel-1"),
}


def _client(api):
    return pystac_client.Client.open(api, modifier=pc.sign_inplace if api == PLANETARY else None)


def discover(sensor: str, start: dt.date, end: dt.date) -> list[dict]:
    """Return passes: items grouped by platform + UTC date (+ relative orbit for SAR)."""
    cfg = SENSORS[sensor]
    search = _client(cfg["api"]).search(
        collections=[cfg["collection"]], bbox=CONFIG["bbox_lnglat"],
        datetime=f"{start.isoformat()}/{end.isoformat()}T23:59:59Z",
        query=cfg.get("query"), max_items=500)
    groups = defaultdict(list)
    for it in search.items():
        p = it.properties
        day = it.datetime.strftime("%Y%m%d")
        orbit = p.get("sat:relative_orbit", "")
        if orbit == "" and sensor == "sentinel-2":  # Earth Search keeps it only in the product URI (…_R027_…)
            m = re.search(r"_R(\d{3})_", p.get("s2:product_uri", ""))
            orbit = int(m.group(1)) if m else ""
        groups[(p.get("platform", sensor), day, orbit)].append(it)
    passes = []
    for (platform, day, orbit), items in groups.items():
        t0 = min(i.datetime for i in items)
        plat = platform.replace("sentinel-", "S").replace("landsat-", "L").upper()
        # SAR keeps the orbit in its id (several per day); optical ids stay platform + time
        pid = f"{plat}_{t0.strftime('%Y%m%dT%H%M')}" + (f"_R{orbit:03d}" if orbit != "" and sensor == "sentinel-1" else "")
        passes.append(dict(pass_id=pid, sensor=sensor, family=cfg["family"], platform=platform,
                           datetime=t0.isoformat().replace("+00:00", "Z"), orbit=orbit,
                           items=items))
    return sorted(passes, key=lambda p: p["datetime"])


def _scale(asset):
    rb = (asset.extra_fields.get("raster:bands") or [{}])[0]
    return rb.get("scale"), rb.get("offset")


def _read_band(items, key, grid, resampling=Resampling.bilinear, raw=False):
    def one(it):
        a = it.assets[key]
        if raw:
            return read_to_grid(a.href, grid, resampling=Resampling.nearest, dtype="uint16")
        s, o = _scale(a)
        if it.properties.get("earthsearch:boa_offset_applied"):
            o = 0.0  # Earth Search already removed the L2A BOA offset; applying it again goes negative
        return read_to_grid(a.href, grid, resampling=resampling, scale=s, offset=o)
    with ThreadPoolExecutor(4) as ex:
        arrs = list(ex.map(one, items))
    return mosaic(arrs)


def _nd(a, b):
    """Normalized difference; NaN where the surface is too dark for the ratio to be meaningful."""
    with np.errstate(divide="ignore", invalid="ignore"):
        den = a + b
        out = (a - b) / den
    out[~(den >= CONFIG["thresholds"]["min_ratio_denominator"])] = np.nan
    return out


def _stretch(x, lo, hi, gamma=1.0):
    y = np.clip((x - lo) / (hi - lo), 0, 1) ** (1 / gamma)
    return np.nan_to_num(y * 255).astype("uint8")


def process(p: dict) -> dict:
    """Read a pass onto the common grid and return standard products."""
    return {"sentinel-2": _s2, "landsat": _landsat, "sentinel-1": _s1}[p["sensor"]](p)


def _order_by_cloud(items):
    return sorted(items, key=lambda i: i.properties.get("eo:cloud_cover", 100))


def _s2(p):
    grid = make_grid(CONFIG["ground_res_m"]["sentinel-2"])
    items = _order_by_cloud(p["items"])
    g, r, n, sw = (_read_band(items, k, grid) for k in ("green", "red", "nir", "swir16"))
    scl = _read_band(items, "scl", grid, raw=True)
    tci = mosaic([read_to_grid(i.assets["visual"].href, grid, band=0, dtype="uint8") for i in items])
    mask = np.full(scl.shape, NODATA, "uint8")
    mask[np.isin(scl, [2, 4, 5, 7])] = CLEAR
    mask[scl == 6] = WATER
    mask[np.isin(scl, [1, 3, 8, 9, 10])] = CLOUD
    mask[scl == 11] = SNOW
    rgb = np.moveaxis(tci, 0, -1)
    fc = np.dstack([_stretch(n, 0, 0.45), _stretch(r, 0, 0.25), _stretch(g, 0, 0.25)])
    return _bundle(p, grid, mask, rgb, fc, ndvi=_nd(n, r), ndbi=_nd(sw, n), ndwi=_nd(g, n),
                   extras={})


def _landsat(p):
    grid = make_grid(CONFIG["ground_res_m"]["landsat"])
    items = _order_by_cloud(p["items"])
    b, g, r, n, sw = (_read_band(items, k, grid) for k in ("blue", "green", "red", "nir08", "swir16"))
    qa = _read_band(items, "qa_pixel", grid, raw=True).astype("uint32")
    st = _read_band(items, "lwir11", grid)  # Kelvin
    bit = lambda k: (qa >> k) & 1 == 1
    mask = np.full(qa.shape, NODATA, "uint8")
    mask[(qa != 0) & ~bit(0)] = CLEAR
    mask[bit(7)] = WATER
    mask[bit(1) | bit(3) | bit(4)] = CLOUD
    mask[bit(5)] = SNOW
    rgb = np.dstack([_stretch(x, 0, 0.3, 1.6) for x in (r, g, b)])
    fc = np.dstack([_stretch(n, 0, 0.45), _stretch(r, 0, 0.25), _stretch(g, 0, 0.25)])
    tc = st - 273.15
    return _bundle(p, grid, mask, rgb, fc, ndvi=_nd(n, r), ndbi=_nd(sw, n), ndwi=_nd(g, n),
                   extras={"thermal": _thermal_ramp(tc)}, stats_extra={"surface_temp_c": tc})


def _s1(p):
    grid = make_grid(CONFIG["ground_res_m"]["sentinel-1"])
    items = p["items"]
    vv = _read_band(items, "vv", grid)
    vh = _read_band(items, "vh", grid)
    with np.errstate(divide="ignore", invalid="ignore"):
        vv_db, vh_db = 10 * np.log10(vv), 10 * np.log10(vh)
    valid = np.isfinite(vv_db) & np.isfinite(vh_db)
    mask = np.where(valid, CLEAR, NODATA).astype("uint8")  # radar sees through cloud
    rgb = np.dstack([_stretch(vv_db, -20, 0), _stretch(vh_db, -27, -7), _stretch(vv_db - vh_db, 2, 14)])
    return _bundle(p, grid, mask, rgb, None, vv_db=vv_db, vh_db=vh_db, extras={})


def _thermal_ramp(tc):
    """-25..+45 °C -> perceptual-ish ramp (dark blue → yellow → red)."""
    t = np.clip((np.nan_to_num(tc, nan=-99) + 25) / 70, 0, 1)
    stops = np.array([[13, 8, 135], [126, 3, 168], [204, 71, 120], [248, 149, 64], [240, 249, 33]], float)
    x = t * (len(stops) - 1)
    i = np.clip(x.astype(int), 0, len(stops) - 2)
    f = (x - i)[..., None]
    out = (stops[i] * (1 - f) + stops[i + 1] * f).astype("uint8")
    out[np.isnan(tc)] = 0
    return out


def _bundle(p, grid: Grid, mask, rgb, fc, extras, stats_extra=None, **layers):
    return dict(meta=p, grid=grid, mask=mask, rgb=rgb, falsecolor=fc, extras=extras,
                layers=layers, stats_extra=stats_extra or {})
