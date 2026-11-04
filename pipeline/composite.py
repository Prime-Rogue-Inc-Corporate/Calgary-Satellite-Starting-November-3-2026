"""Latest-clear-pixel composite for the front-page hero map.

Walks archived passes newest-first and fills each pixel from the most recent pass that saw it
clearly. Writes to latest/ (gitignored); the workflow publishes it as a GitHub Release asset so the
repo history doesn't grow with a daily full-resolution image.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from .grid import ROOT

Image.MAX_IMAGE_PIXELS = None


def build(sensor="sentinel-2", layer="rgb", max_passes=30, out=ROOT / "latest"):
    index = json.loads((ROOT / "index.json").read_text())
    passes = [p for p in index["passes"] if p["sensor"] == sensor and p.get("status") == "archived"]
    passes.sort(key=lambda p: p["datetime"], reverse=True)
    if not passes:
        print("[composite] no passes")
        return None
    comp = filled = age = None
    used = []
    for i, p in enumerate(passes[:max_passes]):
        d = ROOT / p["path"]
        if not (d / f"{layer}.webp").exists() or not (d / "usable.png").exists():
            continue
        img = np.asarray(Image.open(d / f"{layer}.webp").convert("RGB"))
        ok = np.asarray(Image.open(d / "usable.png")).astype(bool)
        if comp is None:
            comp = np.zeros_like(img)
            filled = np.zeros(ok.shape, bool)
            age = np.full(ok.shape, 255, "uint8")
        take = ok & ~filled
        if take.any():
            comp[take] = img[take]
            age[take] = len(used)
            used.append({"pass_id": p["pass_id"], "datetime": p["datetime"],
                         "fraction": round(float(take.mean()), 4)})
            filled |= take
        if filled.mean() > 0.995:
            break
    if comp is None:
        return None
    out.mkdir(parents=True, exist_ok=True)
    alpha = np.where(filled, 255, 0).astype("uint8")
    im = Image.fromarray(np.dstack([comp, alpha]), "RGBA")
    im.save(out / f"composite_{sensor}_{layer}.webp", "WEBP", quality=82, method=6)
    im.resize((1600, round(im.height * 1600 / im.width)), Image.LANCZOS).save(
        out / f"composite_{sensor}_{layer}_1600.webp", "WEBP", quality=80, method=6)
    Image.fromarray(age).save(out / f"composite_{sensor}_age.png", optimize=True)
    ref = json.loads((ROOT / passes[0]["path"] / "meta.json").read_text())
    meta = {"sensor": sensor, "layer": layer, "corners_lnglat": ref["grid"]["corners_lnglat"],
            "bounds_lnglat": ref["grid"]["bounds_lnglat"], "filled_fraction": round(float(filled.mean()), 4),
            "newest": used[0]["datetime"] if used else None, "oldest": used[-1]["datetime"] if used else None,
            "contributing_passes": used,
            "age_png": "pixel value = index into contributing_passes (255 = no clear data)",
            "attribution": ref["attribution"]}
    (out / f"composite_{sensor}_{layer}.json").write_text(json.dumps(meta, indent=2))
    print(f"[composite] {sensor}/{layer}: {meta['filled_fraction']:.1%} filled from {len(used)} passes")
    return meta


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sensor", default="sentinel-2")
    ap.add_argument("--layer", default="rgb")
    a = ap.parse_args()
    build(a.sensor, a.layer)
