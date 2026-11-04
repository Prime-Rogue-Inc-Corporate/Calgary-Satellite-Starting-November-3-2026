"""City of Calgary community district boundaries (open data surr-xmvs) and per-community samples."""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image
from rasterio.features import geometry_mask
from shapely.geometry import Point, mapping, shape
from shapely.strtree import STRtree
from rasterio.warp import transform_geom

from .grid import CONFIG, ROOT
from .sources import CLEAR, NODATA, WATER

CACHE = ROOT / "boundaries" / "communities.geojson"
TIMELINES = ROOT / "communities"


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def load(refresh: bool = False) -> list[dict]:
    if refresh or not CACHE.exists():
        try:
            req = urllib.request.Request(CONFIG["communities_source"], headers={"User-Agent": "osintyyc-archive"})
            raw = json.load(urllib.request.urlopen(req, timeout=60))
            feats = []
            for f in raw["features"]:
                p = f["properties"]
                geom = shape(f["geometry"]).simplify(0.00005, preserve_topology=True)
                feats.append({"type": "Feature", "geometry": mapping(geom),
                              "properties": {"name": (p.get("name") or "").upper(),
                                             "comm_code": p.get("comm_code"),
                                             "class": p.get("class"), "sector": p.get("sector"),
                                             "structure": p.get("comm_structure")}})
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps({"type": "FeatureCollection", "features": feats,
                                         "source": CONFIG["communities_source"],
                                         "licence": "Open Government Licence – City of Calgary"}))
        except Exception as e:  # noqa: BLE001
            print(f"[communities] fetch failed ({e}); using cache if present")
    if not CACHE.exists():
        return []
    return json.loads(CACHE.read_text())["features"]


class Locator:
    def __init__(self, feats):
        self.feats = feats
        self.geoms = [shape(f["geometry"]) for f in feats]
        self.tree = STRtree(self.geoms) if self.geoms else None

    def name_at(self, lon, lat):
        if not self.tree:
            return None
        pt = Point(lon, lat)
        for i in self.tree.query(pt):
            if self.geoms[i].contains(pt):
                return self.feats[i]["properties"]["name"]
        return None


def samples(feats) -> list[dict]:
    """Sample communities; entries are a district name or {"label", "union": [names]} merged polygons."""
    from shapely.ops import unary_union
    by = {f["properties"]["name"]: f for f in feats}
    out = []
    for n in CONFIG["sample_communities"]["names"]:
        if isinstance(n, str):
            if n in by:
                out.append(by[n])
            else:
                print(f"[communities] WARNING sample '{n}' not found in boundaries")
            continue
        parts = [by[m] for m in n["union"] if m in by]
        missing = [m for m in n["union"] if m not in by]
        if missing:
            print(f"[communities] {n['label']}: not in boundaries (skipped): {missing}")
        if parts:
            geom = unary_union([shape(f["geometry"]) for f in parts])
            out.append({"type": "Feature", "geometry": mapping(geom), "properties": {
                "name": n["label"], "comm_code": "+".join(str(f["properties"].get("comm_code")) for f in parts),
                "sector": parts[0]["properties"].get("sector"),
                "members": [f["properties"]["name"] for f in parts]}})
    return out


def _window(geom_merc, grid, pad_px=12):
    inv = ~grid.transform
    minx, miny, maxx, maxy = geom_merc.bounds
    c0, r0 = inv * (minx, maxy)
    c1, r1 = inv * (maxx, miny)
    c0, r0 = max(0, int(c0) - pad_px), max(0, int(r0) - pad_px)
    c1, r1 = min(grid.width, int(c1) + pad_px), min(grid.height, int(r1) + pad_px)
    return r0, r1, c0, c1


def sample_pass(bundle: dict, record: dict, feats: list[dict]) -> dict:
    """Crop each sample community from this pass and append to its timeline."""
    grid, mask = bundle["grid"], bundle["mask"]
    pdir = ROOT / record["path"] / "communities"
    results = {}
    for f in samples(feats):
        name = f["properties"]["name"]
        gm = shape(transform_geom("EPSG:4326", "EPSG:3857", f["geometry"]))
        r0, r1, c0, c1 = _window(gm, grid)
        if r1 <= r0 or c1 <= c0:
            continue
        sub_tf = grid.transform * grid.transform.translation(c0, r0)
        inside = ~geometry_mask([mapping(gm)], out_shape=(r1 - r0, c1 - c0), transform=sub_tf)
        m = mask[r0:r1, c0:c1]
        have = inside & (m != NODATA)
        cov = float(have.sum()) / max(1, inside.sum())
        stat = {"pass_id": record["pass_id"], "datetime": record["datetime"], "sensor": record["sensor"],
                "coverage": round(cov, 3)}
        if have.any():
            usable = inside & np.isin(m, [CLEAR, WATER])
            stat["usable_fraction"] = round(float(usable.sum()) / have.sum(), 3)
            for k, arr in bundle["layers"].items():
                v = arr[r0:r1, c0:c1][usable]
                v = v[np.isfinite(v)]
                if v.size:
                    stat[f"{k}_mean"] = round(float(v.mean()), 4)
            for k, arr in bundle["stats_extra"].items():
                v = arr[r0:r1, c0:c1][usable]
                v = v[np.isfinite(v)]
                if v.size:
                    stat[f"{k}_median"] = round(float(np.median(v)), 2)
            files = {}
            layers = {"rgb": bundle["rgb"], "falsecolor": bundle["falsecolor"], **bundle["extras"]}
            for lk, img in layers.items():
                if img is None:
                    continue
                crop = img[r0:r1, c0:c1].astype("float32")
                crop[~inside] *= 0.35  # dim outside the community boundary
                alpha = np.where(m == NODATA, 0, 255).astype("uint8")
                pdir.mkdir(parents=True, exist_ok=True)
                fn = f"{slug(name)}_{lk}.webp"
                Image.fromarray(np.dstack([crop.astype("uint8"), alpha]), "RGBA").save(
                    pdir / fn, "WEBP", quality=85, method=6)
                files[lk] = f"communities/{fn}"
            stat["files"] = files
        results[name] = stat
        _append_timeline(name, f, stat)
    return results


def _append_timeline(name, feat, stat):
    p = TIMELINES / slug(name) / "timeline.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tl = json.loads(p.read_text()) if p.exists() else {"name": name, "comm_code": feat["properties"].get("comm_code"),
                                                       "sector": feat["properties"].get("sector"), "passes": []}
    tl["passes"] = [x for x in tl["passes"] if x["pass_id"] != stat["pass_id"]] + [stat]
    tl["passes"].sort(key=lambda x: x["datetime"])
    (p.parent / "boundary.geojson").write_text(json.dumps(feat))
    p.write_text(json.dumps(tl, indent=1))
