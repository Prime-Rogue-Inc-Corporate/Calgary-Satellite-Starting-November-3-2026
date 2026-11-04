# Data licence and attribution

The MIT licence in `LICENSE` covers the **code** in this repository only. Imagery and data products in
`archive/`, `communities/`, `boundaries/`, `events/` and the release assets are derived from third-party
sources and carry their terms:

| Source | Terms | Required attribution |
|---|---|---|
| Copernicus Sentinel-1 and Sentinel-2 (ESA / EU) | Free, full and open access under the Copernicus data policy; redistribution and modification permitted | "Contains modified Copernicus Sentinel data [year]" |
| Landsat 8/9 (USGS / NASA) | U.S. public domain | "Landsat imagery courtesy of the U.S. Geological Survey" (requested) |
| VIIRS / MODIS via NASA GIBS | NASA open data policy | "NASA EOSDIS GIBS / Worldview" (requested) |
| City of Calgary community boundaries (`surr-xmvs`) | Open Government Licence – City of Calgary | "Contains information licensed under the Open Government Licence – City of Calgary" |

Each pass's `meta.json` carries its own `attribution` string.

**LLM annotations** in `changes.json` / `events/changes.json` are machine-generated interpretations,
not verified findings, and are published by Prime Rogue Inc. for transparency about the method. Treat
any annotation with `review_status` other than `reviewed` as unverified.
