"""Common analysis grid (EPSG:3857 so web-map overlays are exact) and raster IO helpers."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config" / "aoi.json").read_text())

GDAL_ENV = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.TIF,.tiff",
    GDAL_HTTP_MULTIRANGE="YES",
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
    GDAL_HTTP_MAX_RETRY="4",
    GDAL_HTTP_RETRY_DELAY="2",
    VSI_CACHE="TRUE",
)


@dataclass(frozen=True)
class Grid:
    crs: str
    transform: rasterio.Affine
    width: int
    height: int
    bounds_merc: tuple[float, float, float, float]
    bounds_lnglat: tuple[float, float, float, float]
    pixel_m: float  # nominal ground metres per pixel

    @property
    def corners_lnglat(self):
        """MapLibre image-source order: TL, TR, BR, BL."""
        w, s, e, n = self.bounds_lnglat
        return [[w, n], [e, n], [e, s], [w, s]]


def make_grid(ground_res_m: float) -> Grid:
    bb = CONFIG["bbox_lnglat"]
    lat_c = (bb[1] + bb[3]) / 2
    res = ground_res_m / math.cos(math.radians(lat_c))  # mercator units per pixel
    w, s, e, n = transform_bounds("EPSG:4326", "EPSG:3857", *bb)
    width = int(math.ceil((e - w) / res))
    height = int(math.ceil((n - s) / res))
    e2, s2 = w + width * res, n - height * res
    lnglat = transform_bounds("EPSG:3857", "EPSG:4326", w, s2, e2, n)
    return Grid("EPSG:3857", from_origin(w, n, res, res), width, height, (w, s2, e2, n), lnglat, ground_res_m)


def read_to_grid(href: str, grid: Grid, band: int = 1, resampling=Resampling.bilinear,
                 scale: float | None = None, offset: float | None = None, dtype="float32",
                 nodata_value=None) -> np.ndarray:
    """Warp one band of a (remote) COG onto the grid. Returns float array with NaN as nodata
    (or raw integer array when dtype is an int type)."""
    with rasterio.Env(**GDAL_ENV):
        with rasterio.open(href) as src:
            nd = src.nodata if nodata_value is None else nodata_value
            with WarpedVRT(src, crs=grid.crs, transform=grid.transform, width=grid.width,
                           height=grid.height, resampling=resampling,
                           src_nodata=nd, nodata=nd) as vrt:
                if src.count >= 3 and band == 0:  # read all bands (e.g. TCI)
                    return vrt.read([1, 2, 3])
                arr = vrt.read(band)
    if np.issubdtype(np.dtype(dtype), np.integer):
        return arr
    out = arr.astype("float32")
    if nd is not None and not (isinstance(nd, float) and math.isnan(nd)):
        out[arr == nd] = np.nan
    if scale is not None:
        out = out * scale + (offset or 0.0)
    return out


def mosaic(arrays: list[np.ndarray]) -> np.ndarray:
    """First-valid-wins mosaic. Inputs ordered best-first."""
    out = arrays[0].copy()
    for a in arrays[1:]:
        if np.issubdtype(out.dtype, np.floating):
            m = np.isnan(out) & ~np.isnan(a)
        else:
            m = (out == 0) & (a != 0)
        out[m] = a[m]
    return out


def write_cog(path: Path, bands: list[np.ndarray], grid: Grid, descriptions: list[str], nodata=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(driver="COG", width=grid.width, height=grid.height, count=len(bands),
                   dtype=bands[0].dtype, crs=grid.crs, transform=grid.transform, nodata=nodata,
                   compress="DEFLATE", predictor=2, blocksize=512, overview_resampling="average")
    with rasterio.open(path, "w", **profile) as dst:
        for i, (b, d) in enumerate(zip(bands, descriptions), start=1):
            dst.write(b, i)
            dst.set_band_description(i, d)


def read_local(path: Path) -> tuple[np.ndarray, list[str]]:
    with rasterio.open(path) as src:
        return src.read(), list(src.descriptions)
