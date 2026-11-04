"""LLM interpretation of deterministically flagged change cells.

The model sees only flagged cells (before/after crops + the numbers that flagged them) and must
return structured JSON. Every annotation is labelled machine-generated and starts unreviewed.
Requires ANTHROPIC_API_KEY; skipped cleanly without it.
"""
from __future__ import annotations

import base64
import io
import json
import os
import urllib.request

import numpy as np
from PIL import Image
from rasterio.transform import Affine
from rasterio.warp import transform as warp_xy

from .grid import CONFIG, ROOT

MODEL = os.environ.get("ANNOTATE_MODEL", "claude-sonnet-5-5")
API = "https://api.anthropic.com/v1/messages"
Image.MAX_IMAGE_PIXELS = None

SYSTEM = """You are an imagery analyst for OSINT YYC, a public-interest open-source intelligence site about Calgary, Alberta.
You interpret medium-resolution satellite change (10 m optical / 30 m Landsat / 10 m radar). Rules:
- Describe only what the pixels and numbers support. At this resolution individual people, vehicles and houses are NOT identifiable; never speculate about specific people or households.
- Consider mundane explanations first: season (senescence, snow, frost, low sun/shadow), crop harvest, irrigation, ice on ponds, sensor/sun-angle differences, residual cloud or haze.
- Give a calibrated confidence: "low", "medium" or "high". Default to low when the change could be seasonal or atmospheric.
- Suggest concrete public sources to verify (e.g. City of Calgary development/building permits, road construction notices, 311, Alberta Wildfire, news).
Respond with ONLY a JSON object, no prose."""

SCHEMA_HINT = {
    "what_changed": "one or two plain-language sentences",
    "likely_cause": "most plausible explanation",
    "alternative_explanations": ["..."],
    "category": "construction|clearing|vegetation|water|snow_ice|agriculture|fire_burn|infrastructure|atmospheric_artifact|seasonal|unclear",
    "confidence": "low|medium|high",
    "newsworthy": "true/false — would a local reporter plausibly want to check this?",
    "verify_with": ["..."],
}


def _crop(path, grid_meta, bbox_lnglat, pad_cells=1.0):
    img = Image.open(path).convert("RGB")
    tf = Affine(*grid_meta["transform"])
    w, s, e, n = bbox_lnglat
    xs, ys = warp_xy("EPSG:4326", "EPSG:3857", [w, e], [n, s])
    inv = ~tf
    c0, r0 = inv * (xs[0], ys[0])
    c1, r1 = inv * (xs[1], ys[1])
    pw, ph = (c1 - c0) * pad_cells, (r1 - r0) * pad_cells
    box = (int(max(0, c0 - pw)), int(max(0, r0 - ph)), int(min(img.width, c1 + pw)), int(min(img.height, r1 + ph)))
    crop = img.crop(box)
    # outline the flagged cell
    arr = np.asarray(crop).copy()
    x0, y0, x1, y1 = int(c0 - box[0]), int(r0 - box[1]), int(c1 - box[0]), int(r1 - box[1])
    for t in range(2):
        arr[max(0, y0 - t), x0:x1] = arr[min(arr.shape[0] - 1, y1 + t), x0:x1] = (255, 230, 0)
        arr[y0:y1, max(0, x0 - t)] = arr[y0:y1, min(arr.shape[1] - 1, x1 + t)] = (255, 230, 0)
    return Image.fromarray(arr)


def panel(prev_meta, cur_meta, flag):
    rows = []
    for layer in ("rgb", "falsecolor", "thermal"):
        if layer in cur_meta["files"] and layer in prev_meta["files"]:
            a = _crop(ROOT / prev_meta["path"] / f"{layer}.webp", prev_meta["grid"], flag["bbox_lnglat"])
            b = _crop(ROOT / cur_meta["path"] / f"{layer}.webp", cur_meta["grid"], flag["bbox_lnglat"])
            size = (max(a.width, b.width), max(a.height, b.height))
            scale = max(1, 320 // max(size))
            size = (size[0] * scale, size[1] * scale)
            row = Image.new("RGB", (size[0] * 2 + 8, size[1]), "white")
            row.paste(a.resize(size, Image.NEAREST), (0, 0))
            row.paste(b.resize(size, Image.NEAREST), (size[0] + 8, 0))
            rows.append((layer, row))
    H = sum(r.height for _, r in rows) + 8 * (len(rows) - 1)
    out = Image.new("RGB", (max(r.width for _, r in rows), H), "white")
    y = 0
    for _, r in rows:
        out.paste(r, (0, y))
        y += r.height + 8
    return out, [l for l, _ in rows]


def annotate_pass(change: dict, prev_meta: dict, cur_meta: dict, locate=None) -> list[dict]:
    key = os.environ.get("ANTHROPIC_API_KEY")
    k = CONFIG["thresholds"]["max_annotations_per_pass"]
    out = []
    adir = ROOT / cur_meta["path"] / "changes"
    for i, f in enumerate(change["flagged"][:k]):
        img, layers = panel(prev_meta, cur_meta, f)
        adir.mkdir(parents=True, exist_ok=True)
        fn = f"cell_{f['cell'][0]}_{f['cell'][1]}.webp"
        img.save(adir / fn, "WEBP", quality=85)
        community = locate(*f["center_lnglat"]) if locate else None
        rec = {"cell": f["cell"], "center_lnglat": f["center_lnglat"], "bbox_lnglat": f["bbox_lnglat"],
               "community": community, "panel": f"changes/{fn}", "score": f["score"],
               "deterministic_hint": f["deterministic_hint"], "machine_generated": True,
               "review_status": "unreviewed", "model": None, "annotation": None}
        if key:
            try:
                rec["annotation"] = _ask(key, img, layers, f, change, cur_meta, prev_meta, community)
                rec["model"] = MODEL
            except Exception as e:  # noqa: BLE001
                rec["error"] = str(e)[:300]
        out.append(rec)
    return out


def _ask(key, img, layers, f, change, cur, prev, community):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    ctx = {
        "location": {"city": "Calgary, Alberta", "community": community, "cell_center_lnglat": f["center_lnglat"],
                     "cell_size_m": CONFIG["thresholds"]["change_cell_m"]},
        "baseline": {"pass": prev["pass_id"], "datetime": prev["datetime"]},
        "current": {"pass": cur["pass_id"], "datetime": cur["datetime"]},
        "gap_days": change["gap_days"], "sensor": cur["sensor"],
        "panel_layout": f"rows = {layers}; left = baseline, right = current; yellow box = flagged cell",
        "cell_index_deltas": f["deltas"], "robust_z": f["z"], "scene_median_delta": change["scene_median_delta"],
        "deterministic_hint": f["deterministic_hint"],
    }
    body = {"model": MODEL, "max_tokens": 700, "system": SYSTEM, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.b64encode(buf.getvalue()).decode()}},
        {"type": "text", "text": "Context:\n" + json.dumps(ctx, indent=1) +
         "\n\nReturn JSON with exactly these keys:\n" + json.dumps(SCHEMA_HINT, indent=1)}]}]}
    req = urllib.request.Request(API, data=json.dumps(body).encode(), headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    resp = json.load(urllib.request.urlopen(req, timeout=120))
    text = "".join(c.get("text", "") for c in resp["content"] if c["type"] == "text").strip()
    text = text[text.find("{"): text.rfind("}") + 1]
    return json.loads(text)
