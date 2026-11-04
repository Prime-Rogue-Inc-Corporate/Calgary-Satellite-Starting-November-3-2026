"""Daily archive run. Idempotent: passes already in index.json are skipped.

python -m pipeline.run [--days-back 10] [--sensors sentinel-2,landsat,sentinel-1] [--no-annotate]
"""
from __future__ import annotations

import argparse
import datetime as dt
import gc
import json
import subprocess
import traceback

from . import annotate, change, communities, composite, products, sources, viirs
from .grid import CONFIG, ROOT

INDEX = ROOT / "index.json"
EVENTS = ROOT / "events" / "changes.json"
T = CONFIG["thresholds"]
ARCHIVE_EPOCH = dt.date(2026, 11, 3)


def load_index():
    if INDEX.exists():
        return json.loads(INDEX.read_text())
    return {"aoi": CONFIG["name"], "bbox_lnglat": CONFIG["bbox_lnglat"], "passes": []}


def baseline_for(index, rec):
    cands = [p for p in index["passes"] if p["family"] == rec["family"] and p["status"] == "archived"
             and p["datetime"] < rec["datetime"]
             and (rec["sensor"] != "sentinel-1" or p.get("relative_orbit") == rec.get("relative_orbit"))]
    return max(cands, key=lambda p: p["datetime"]) if cands else None


def to_events(rec, ann):
    """Shared OSINT YYC event schema (geo_scope = point)."""
    out = []
    for a in ann:
        an = a.get("annotation") or {}
        out.append({
            "id": f"sat:{rec['pass_id']}:{a['cell'][0]}-{a['cell'][1]}",
            "type": "satellite_change", "geo_scope": "point",
            "geometry": {"type": "Point", "coordinates": a["center_lnglat"]},
            "bbox_lnglat": a["bbox_lnglat"], "community": a["community"],
            "datetime": rec["datetime"], "source": rec["sensor"], "pass_id": rec["pass_id"],
            "title": an.get("what_changed") or a["deterministic_hint"],
            "category": an.get("category", "unclear"), "confidence": an.get("confidence", "low"),
            "machine_generated": True, "review_status": a["review_status"],
            "panel": f"{rec['path']}/{a['panel']}", "annotation": an or None, "score": a["score"],
        })
    return out


def run(days_back=10, sensors=("sentinel-2", "landsat", "sentinel-1"), do_annotate=True, start=None):
    index = load_index()
    known = {p["pass_id"] for p in index["passes"]}
    feats = communities.load(refresh=not (ROOT / "boundaries" / "communities.geojson").exists())
    loc = communities.Locator(feats)
    today = dt.datetime.now(dt.timezone.utc).date()
    start = start or max(ARCHIVE_EPOCH, today - dt.timedelta(days=days_back))
    events = json.loads(EVENTS.read_text()) if EVENTS.exists() else []

    todo = []
    for s in sensors:
        try:
            todo += [p for p in sources.discover(s, start, today) if p["pass_id"] not in known]
        except Exception as e:  # noqa: BLE001
            print(f"[discover] {s} failed: {e}")
    todo.sort(key=lambda p: p["datetime"])
    print(f"[run] {len(todo)} new passes since {start}")

    for p in todo:
        print(f"[run] {p['pass_id']} ({len(p['items'])} items)")
        try:
            b = sources.process(p)
            stats = products.coverage_stats(b["mask"])
            base = {k: p[k] for k in ("pass_id", "sensor", "family", "platform", "datetime")}
            base["relative_orbit"] = p["orbit"]
            if stats["aoi_coverage"] < T["min_aoi_coverage"] or stats.get("usable_fraction", 0) < T["min_clear_fraction"]:
                index["passes"].append(base | {"status": "skipped", "reason": "coverage/cloud", "stats": stats,
                                               "source_items": [i.id for i in p["items"]]})
                print(f"   skipped {stats}")
                save(index, events)
                continue
            rec = products.write_pass(b)
            entry = base | {"status": "archived", "stats": rec["stats"], "path": rec["path"],
                            "files": rec["files"], "corners_lnglat": rec["grid"]["corners_lnglat"]}
            entry["communities"] = communities.sample_pass(b, rec, feats) if feats else {}
            del b
            gc.collect()
            prev = baseline_for(index, entry)
            if prev:
                prev_meta = json.loads((ROOT / prev["path"] / "meta.json").read_text())
                ch = change.detect(rec, prev_meta)
                ann = annotate.annotate_pass(ch, prev_meta, rec, loc.name_at) if do_annotate else []
                ch["annotations"] = ann
                (ROOT / rec["path"] / "changes.json").write_text(json.dumps(ch, indent=1))
                entry["change"] = {"baseline": prev["pass_id"], "gap_days": ch["gap_days"],
                                   "flagged": ch["flagged_count"], "annotated": len(ann)}
                events = [e for e in events if e["pass_id"] != rec["pass_id"]] + to_events(rec, ann)
            index["passes"].append(entry)
            save(index, events)
            print(f"   archived {rec['stats']} change={entry.get('change')}")
        except Exception:  # noqa: BLE001 — leave out of index so it retries tomorrow
            traceback.print_exc()

    for d in (today - dt.timedelta(days=1), today):
        viirs.fetch_day(d)

    index["archive_size_mb"] = _size_mb()
    save(index, events)
    composite.build("sentinel-2", "rgb")
    composite.build("sentinel-2", "falsecolor")
    print(f"[run] archive size {index['archive_size_mb']} MB")


def save(index, events):
    index["passes"].sort(key=lambda p: p["datetime"])
    index["updated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    INDEX.write_text(json.dumps(index, indent=1))
    EVENTS.parent.mkdir(exist_ok=True)
    events.sort(key=lambda e: e["datetime"], reverse=True)
    EVENTS.write_text(json.dumps(events[:1000], indent=1))


def _size_mb():
    try:
        out = subprocess.run(["du", "-sm", str(ROOT / "archive")], capture_output=True, text=True).stdout
        return int(out.split()[0])
    except Exception:  # noqa: BLE001
        return None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-back", type=int, default=10)
    ap.add_argument("--sensors", default="sentinel-2,landsat,sentinel-1")
    ap.add_argument("--no-annotate", action="store_true")
    ap.add_argument("--start", type=dt.date.fromisoformat, help="backfill from this UTC date")
    a = ap.parse_args()
    run(a.days_back, tuple(a.sensors.split(",")), not a.no_annotate, a.start)
