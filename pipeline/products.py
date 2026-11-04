"""Write archive products for one pass."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

from .grid import ROOT, write_cog
from .sources import CLEAR, CLOUD, NODATA, SNOW, WATER

ARCHIVE = ROOT / "archive"
ATTRIBUTION = {
    "sentinel-2": "Contains modified Copernicus Sentinel data {year}",
    "sentinel-1": "Contains modified Copernicus Sentinel data {year}",
    "landsat": "Landsat imagery courtesy of the U.S. Geological Survey",
}
Image.MAX_IMAGE_PIXELS = None


BASELINE = ROOT / "baseline"


def pass_dir(meta) -> Path:
    """Dated archive (from 3 Nov 2026) or, for pre-archive reference passes, baseline/."""
    d = meta["datetime"]
    root = BASELINE if meta.get("baseline") else ARCHIVE
    return root / meta["sensor"] / d[0:4] / d[5:7] / d[8:10] / meta["pass_id"]


def q8(x, lo=-1.0, hi=1.0):
    """Quantize to uint8 1..255, 0 = nodata."""
    y = np.round((np.clip(x, lo, hi) - lo) / (hi - lo) * 254 + 1)
    y[~np.isfinite(x)] = 0
    return y.astype("uint8")


def dq8(b, lo=-1.0, hi=1.0):
    out = (b.astype("float32") - 1) / 254 * (hi - lo) + lo
    out[b == 0] = np.nan
    return out


DB_RANGE = (-35.0, 10.0)
ANALYSIS_RES_M = 20


def analysis_factor(grid) -> int:
    return max(1, round(ANALYSIS_RES_M / grid.pixel_m))


def coarsen_grid(grid, f):
    from dataclasses import replace
    if f == 1:
        return grid
    t = grid.transform
    return replace(grid, transform=t * t.scale(f, f), width=grid.width // f,
                   height=grid.height // f, pixel_m=grid.pixel_m * f)


def _blocks(a, f):
    h, w = (a.shape[0] // f) * f, (a.shape[1] // f) * f
    return a[:h, :w].reshape(h // f, f, w // f, f).swapaxes(1, 2).reshape(h // f, w // f, f * f)


def block_mean(a, f):
    if f == 1:
        return a
    with np.errstate(invalid="ignore"), __import__("warnings").catch_warnings():
        __import__("warnings").simplefilter("ignore", RuntimeWarning)
        return np.nanmean(_blocks(a, f), axis=-1)


def block_mask(m, f):
    """Conservative: any cloud -> cloud; any snow -> snow; majority water -> water; any clear -> clear."""
    if f == 1:
        return m
    b = _blocks(m, f)
    out = np.full(b.shape[:2], NODATA, "uint8")
    out[(b == CLEAR).any(-1) | (b == WATER).any(-1)] = CLEAR
    out[(b == WATER).sum(-1) * 2 > b.shape[-1]] = WATER
    out[(b == SNOW).any(-1)] = SNOW
    out[(b == CLOUD).any(-1)] = CLOUD
    out[(b == NODATA).all(-1)] = NODATA
    return out


def save_webp(path: Path, rgb: np.ndarray, alpha: np.ndarray, sizes=(None, 1600)):
    img = Image.fromarray(np.dstack([rgb, alpha]).astype("uint8"), "RGBA")
    out = {}
    for s in sizes:
        im = img
        name = path.name
        if s and img.width > s:
            im = img.resize((s, round(img.height * s / img.width)), Image.LANCZOS)
            name = path.stem + f"_{s}.webp"
        elif s:
            continue
        im.save(path.parent / name, "WEBP", quality=82, method=6)
        out[str(s or "full")] = name
    return out


def coverage_stats(mask: np.ndarray) -> dict:
    total = mask.size
    have = int((mask != NODATA).sum())
    s = {"aoi_coverage": round(have / total, 4)}
    if have:
        for k, v in (("clear", CLEAR), ("water", WATER), ("cloud", CLOUD), ("snow", SNOW)):
            s[f"{k}_fraction"] = round(float((mask == v).sum()) / have, 4)
        s["usable_fraction"] = round(s["clear_fraction"] + s["water_fraction"], 4)
    return s


def write_pass(b: dict) -> dict:
    meta, grid, mask = b["meta"], b["grid"], b["mask"]
    d = pass_dir(meta)
    d.mkdir(parents=True, exist_ok=True)
    alpha = np.where(mask == NODATA, 0, 255).astype("uint8")
    files = {"rgb": save_webp(d / "rgb.webp", b["rgb"], alpha)}
    if b["falsecolor"] is not None:
        files["falsecolor"] = save_webp(d / "falsecolor.webp", b["falsecolor"], alpha)
    for k, arr in b["extras"].items():
        files[k] = save_webp(d / f"{k}.webp", arr, alpha)
    # 1-bit full-resolution usable mask (clear or water) — drives the latest-clear composite
    Image.fromarray(np.isin(mask, [CLEAR, WATER])).save(d / "usable.png", optimize=True)
    files["usable_mask"] = "usable.png"

    f = analysis_factor(grid)
    agrid = coarsen_grid(grid, f)
    names, bands = [], []
    for k, arr in b["layers"].items():
        lo, hi = DB_RANGE if k.endswith("_db") else (-1.0, 1.0)
        names.append(k)
        bands.append(q8(block_mean(arr, f), lo, hi))
    names.append("mask")
    bands.append(block_mask(mask, f))
    write_cog(d / "analysis.tif", bands, agrid, names)
    files["analysis"] = "analysis.tif"

    year = meta["datetime"][:4]
    record = {
        "pass_id": meta["pass_id"], "baseline": bool(meta.get("baseline")),
        "sensor": meta["sensor"], "family": meta["family"],
        "platform": meta["platform"], "datetime": meta["datetime"], "relative_orbit": meta["orbit"],
        "source_items": [{"id": i.id, "collection": i.collection_id,
                          "self": i.get_self_href() and i.get_self_href().split("?")[0],
                          "cloud_cover_scene": i.properties.get("eo:cloud_cover")}
                         for i in meta["items"]],
        "grid": {"crs": grid.crs, "width": grid.width, "height": grid.height,
                 "ground_res_m": grid.pixel_m, "transform": list(grid.transform)[:6],
                 "bounds_lnglat": [round(v, 6) for v in grid.bounds_lnglat],
                 "corners_lnglat": [[round(x, 6), round(y, 6)] for x, y in grid.corners_lnglat]},
        "analysis_grid": {"width": agrid.width, "height": agrid.height, "ground_res_m": agrid.pixel_m,
                          "transform": list(agrid.transform)[:6]},
        "analysis_bands": {n: ("dB quantized uint8 1..255 over [-35,10], 0=nodata" if n.endswith("_db")
                               else "quantized uint8 1..255 over [-1,1], 0=nodata") for n in names[:-1]}
                          | {"mask": "0 nodata, 1 clear, 2 cloud/shadow, 3 snow, 4 water"},
        "stats": coverage_stats(mask),
        "files": files,
        "path": str(d.relative_to(ROOT)),
        "attribution": ATTRIBUTION[meta["sensor"]].format(year=year),
    }
    for k, arr in b["stats_extra"].items():
        v = arr[np.isfinite(arr) & (mask == CLEAR)]
        if v.size:
            record["stats"][k] = {"p10": round(float(np.percentile(v, 10)), 2),
                                  "median": round(float(np.median(v)), 2),
                                  "p90": round(float(np.percentile(v, 90)), 2)}
    (d / "meta.json").write_text(json.dumps(record, indent=2))
    return record
