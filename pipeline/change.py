"""Deterministic change detection between two passes of the same sensor family.

Per 500 m cell: mean index deltas over pixels clear in BOTH passes, then robust z-scores against
the city-wide distribution of cell deltas (removes seasonal / radiometric shifts that affect the
whole scene — e.g. November senescence or a light snowfall). Only outlier cells are flagged.
"""
from __future__ import annotations

import datetime as dt
import warnings

import numpy as np
from rasterio.transform import Affine
from rasterio.warp import transform as warp_xy

from .grid import CONFIG, ROOT, read_local
from .products import dq8, DB_RANGE
from .sources import CLEAR, WATER

T = CONFIG["thresholds"]

HINTS = {
    ("ndvi", -1): "vegetation loss (possible clearing, grading, construction, harvest, or senescence)",
    ("ndvi", 1): "vegetation gain (landscaping, regrowth, irrigation)",
    ("ndbi", 1): "more built-up / bare surface (new structures, pavement, exposed soil)",
    ("ndbi", -1): "less built-up / bare signal (vegetation cover, wet surface, snow residue)",
    ("ndwi", 1): "more surface water or wetness (ponding, flooding, new stormwater pond)",
    ("ndwi", -1): "less surface water (drainage, drying, ice cover)",
    ("vv_db", 1): "radar backscatter increase (new hard structures, rough surfaces, metal, construction equipment)",
    ("vv_db", -1): "radar backscatter decrease (demolition, smoothing, standing water)",
    ("vh_db", 1): "cross-pol increase (vegetation volume or complex structures)",
    ("vh_db", -1): "cross-pol decrease (vegetation removal)",
}


def _load(record):
    arr, names = read_local(ROOT / record["path"] / "analysis.tif")
    layers = {}
    for a, n in zip(arr, names):
        if n == "mask":
            layers[n] = a
        else:
            lo, hi = DB_RANGE if n.endswith("_db") else (-1.0, 1.0)
            layers[n] = dq8(a, lo, hi)
    return layers


def _cells(a, c):
    h, w = (a.shape[0] // c) * c, (a.shape[1] // c) * c
    return a[:h, :w].reshape(h // c, c, w // c, c).swapaxes(1, 2).reshape(h // c, w // c, c * c)


def detect(cur: dict, prev: dict) -> dict:
    A, B = _load(prev), _load(cur)
    g = cur["analysis_grid"]
    cell = max(4, round(T["change_cell_m"] / g["ground_res_m"]))
    valid = np.isin(A["mask"], [CLEAR, WATER]) & np.isin(B["mask"], [CLEAR, WATER])
    vfrac = _cells(valid, cell).mean(-1)
    metrics = [k for k in B if k != "mask" and k in A]
    out_metrics, z = {}, {}
    eligible = vfrac >= T["change_min_valid"]
    for k in metrics:
        d = np.where(valid, B[k] - A[k], np.nan)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            m = np.nanmean(_cells(d, cell), -1)
        m[~eligible] = np.nan
        med = np.nanmedian(m) if np.isfinite(m).any() else 0.0
        mad = np.nanmedian(np.abs(m - med)) * 1.4826 if np.isfinite(m).any() else 0.0
        out_metrics[k] = (m, float(med))
        zk = (m - med) / max(mad, 1e-3)
        floor = T["change_min_abs_delta"]["db" if k.endswith("_db") else "index"]
        zk[np.abs(m - med) < floor] = 0.0  # must be a material change, not just a statistical outlier
        z[k] = zk

    tf = Affine(*g["transform"])
    gap = (dt.datetime.fromisoformat(cur["datetime"].rstrip("Z")) -
           dt.datetime.fromisoformat(prev["datetime"].rstrip("Z"))).days
    flagged = []
    if z:
        zstack = np.stack([np.nan_to_num(np.abs(z[k])) for k in metrics])
        score = zstack.max(0)
        rows, cols = np.where(score >= T["change_robust_z"])
        for r, c in zip(rows, cols):
            x0, y0 = tf * (c * cell, r * cell)
            x1, y1 = tf * ((c + 1) * cell, (r + 1) * cell)
            lon, lat = warp_xy("EPSG:3857", "EPSG:4326", [x0, x1, (x0 + x1) / 2], [y1, y0, (y0 + y1) / 2])
            dom = metrics[int(zstack[:, r, c].argmax())]
            sign = 1 if z[dom][r, c] > 0 else -1
            flagged.append({
                "cell": [int(r), int(c)],
                "bbox_lnglat": [round(lon[0], 6), round(lat[0], 6), round(lon[1], 6), round(lat[1], 6)],
                "center_lnglat": [round(lon[2], 6), round(lat[2], 6)],
                "score": round(float(score[r, c]), 2),
                "valid_fraction": round(float(vfrac[r, c]), 3),
                "deltas": {k: round(float(out_metrics[k][0][r, c]), 4) for k in metrics},
                "z": {k: round(float(z[k][r, c]), 2) for k in metrics},
                "dominant": dom,
                "deterministic_hint": HINTS.get((dom, sign), ""),
            })
    flagged.sort(key=lambda f: -f["score"])
    return {
        "current": cur["pass_id"], "baseline": prev["pass_id"], "gap_days": gap,
        "method": f"{T['change_cell_m']} m cells; robust z >= {T['change_robust_z']} vs city-wide "
                  f"cell distribution; both-clear fraction >= {T['change_min_valid']}",
        "eligible_cells": int(eligible.sum()), "total_cells": int(eligible.size),
        "scene_median_delta": {k: round(v[1], 4) for k, v in out_metrics.items()},
        "flagged_count": len(flagged), "flagged": flagged[:60],
    }
