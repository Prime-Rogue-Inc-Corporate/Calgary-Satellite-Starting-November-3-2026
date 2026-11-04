# Changelog — Calgary satellite archive

Versions track the [OSINT YYC](https://github.com/Prime-Rogue-Inc-Corporate/osintyyc) release they ship with.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), [SemVer](https://semver.org/).

## [Unreleased]

### Added
- `baseline/`: 12 and 14 Oct 2026 Sentinel-2 passes (one per orbit track) as the composite's starting view and first change baseline, flagged `baseline: true`
- `--end` and `--baseline` options (CLI and workflow inputs)
- Sentinel-2 relative orbit recorded per pass (27 = east-only track, 70 = whole city); change detection compares passes on the same orbit

## [0.0.1] — 2026-11-04 · Pre-alpha

### Added
- Twice-daily archive of Sentinel-2 L2A, Landsat 8/9 C2 L2 and Sentinel-1 RTC over Calgary from
  3 November 2026, on one EPSG:3857 grid; VIIRS/MODIS daily context snapshots
- Rolling latest-clear composite published as the `latest` release asset
- Deterministic change detection (500 m cells, robust z plus absolute floor) and LLM annotation of
  flagged cells, labelled machine-generated and unreviewed
- Sample communities with per-pass crops and timelines
- Data licence covering Copernicus, USGS, NASA and City of Calgary terms

### Fixed (before release)
- Sentinel-2 BOA offset was applied twice on Earth Search items, producing negative reflectance and
  false change on dark surfaces

[0.0.1]: https://github.com/Prime-Rogue-Inc-Corporate/Calgary-Satellite-Starting-November-3-2026/releases/tag/v0.0.1
