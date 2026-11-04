# Calgary Satellite Archive — starting November 3, 2026

A public, daily-growing archive of free satellite imagery over Calgary, Alberta, with deterministic
change detection and machine-generated (LLM) interpretation of flagged changes. Feeds
[OSINT YYC](https://github.com/Prime-Rogue-Inc-Corporate/osintyyc). Operated by Prime Rogue Inc.

> **Temporary home.** This repo is the pre-alpha archive. At alpha it moves to object storage on our VPS;
> this repo's history stays as the public record.

## What's collected

| Sensor | Source | Ground res. | Revisit | Products |
|---|---|---|---|---|
| Sentinel-2 L2A (A/B/C) | Element84 Earth Search | 10 m | ~2–5 days | true colour, false colour (NIR), NDVI/NDBI/NDWI, cloud mask |
| Landsat 8/9 C2 L2 | Microsoft Planetary Computer | 30 m | ~8 days combined | true colour, false colour, **surface temperature**, indices |
| Sentinel-1 RTC (SAR) | Microsoft Planetary Computer | 10 m | ~6–12 days | VV/VH backscatter (sees through cloud and at night) |
| VIIRS (NOAA-20/21, SNPP), MODIS (Terra/Aqua) | NASA GIBS | 250–375 m | daily | southern-Alberta context snapshots (smoke, snow line) |

AOI: `[-114.32, 50.84, -113.86, 51.21]` (lng/lat), all products on one EPSG:3857 grid so they overlay web maps exactly.

## Layout

```
archive/<sensor>/YYYY/MM/DD/<pass_id>/
  meta.json            sources, grid, coverage & cloud stats, attribution
  rgb.webp (+_1600)    true colour, alpha = no data
  falsecolor.webp      NIR-R-G (vegetation bright red)
  thermal.webp         Landsat surface temperature, −25…+45 °C ramp
  analysis.tif         20 m COG: quantized indices (or SAR dB) + mask band
  usable.png           1-bit full-res clear-sky mask
  changes.json         change detection vs previous pass of the same family (+ annotations)
  changes/cell_*.webp  before/after panels sent to the LLM
  communities/*.webp   sample-community crops
archive/daily-context/YYYY/MM/DD/*.jpg   VIIRS/MODIS daily snapshots
baseline/<sensor>/YYYY/MM/DD/<pass_id>/   pre-archive reference passes (same layout), see below
communities/<slug>/timeline.json         per-community time series (+ boundary.geojson)
events/changes.json                      change events in the OSINT YYC event schema
index.json                               every pass seen, archived or skipped (and why)
```

Passes are archived when ≥30 % of the AOI is covered and ≥15 % is clear; otherwise they're recorded in
`index.json` as `skipped` with stats, so gaps are documented rather than silent.

The **latest clear-pixel composite** (front-page hero map) is rebuilt every run and published as assets
on the [`latest` release](../../releases/tag/latest), not committed, to keep history small.

## Baseline (before 3 November 2026)

Calgary sits where two Sentinel-2 orbit tracks overlap. **Relative orbit 70** (~18:50 UTC, 11:50 MST)
sees the whole city. **Relative orbit 27** (~18:40 UTC) ends over it: its western swath edge runs
14.5° west of due south, about 2.3 km east of the YYC terminal and 3.5 km east of the Calgary Tower,
and has drifted no more than 1.5 km across 16 passes (Sep–Nov 2026). Change detection compares each
pass with the previous pass on the same orbit. To start with a complete, cloud-free view, two pre-archive
passes are kept in `baseline/` and flagged `"baseline": true` in `index.json`:

- **12 Oct 2026, Sentinel-2B (18:50 track)**: whole city, west tile cloud-free
- **14 Oct 2026, Sentinel-2C (18:40 track)**: east side, cloud-free

They feed the latest-clear composite and serve as the first change-detection baseline. The dated
archive itself still begins 3 November 2026.

```bash
python -m pipeline.run --baseline --start 2026-10-12 --end 2026-10-14 --sensors sentinel-2
```

## Change detection → LLM annotation

1. Per 500 m cell, mean index change over pixels clear in **both** passes
2. Robust z-score against the city-wide distribution (removes seasonal / radiometric shifts) **and** a
   minimum absolute change — both must pass
3. Only flagged cells go to the LLM, as before/after panels plus the numbers; it returns structured JSON
   (`what_changed`, `likely_cause`, `alternative_explanations`, `category`, `confidence`, `verify_with`)
4. Everything is labelled `machine_generated: true`, `review_status: "unreviewed"` until an editor reviews it

At 10–30 m resolution, people, vehicles and individual houses are not identifiable.

## Sample communities (pre-alpha)

Killarney/Glengarry (fixed), plus a seeded random draw (seed `20261103`): Tuscany (NW), Saddle Ridge (NE),
Auburn Bay (SE), Beltline — full footprint, merged City districts (centre). Boundaries: City of Calgary
Community District Boundaries (`surr-xmvs`).

## Running

```bash
pip install -r requirements.txt
python -m pipeline.run                       # last 10 days, all sensors
python -m pipeline.run --start 2026-11-03    # backfill
python -m pipeline.run --no-annotate         # skip LLM
```

GitHub Actions runs it twice daily (`.github/workflows/daily-archive.yml`). Secrets: `ANTHROPIC_API_KEY`
(optional, enables annotation), `OSINTYYC_DISPATCH_TOKEN` (optional, triggers a site rebuild).

## Licence

Version 0.0.1 (pre-alpha), see [CHANGELOG.md](CHANGELOG.md). Powered by [Prime Rogue Inc.](https://primerogueinc.com)

Code: MIT (see `LICENSE`). **Imagery and derived data are not MIT** — see [`DATA-LICENSE.md`](DATA-LICENSE.md).
