"""Esri World Imagery Wayback access: releases, local-change lookup, tile mosaics
clipped to an AOI polygon, and pixel <-> geographic conversion.

All rasters are produced in Web Mercator (EPSG:3857), the native tiling scheme of
Wayback, so no resampling is introduced before detection.
"""
from __future__ import annotations

import io
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests
from PIL import Image
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform

CONFIG_URL = "https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json"
TILE_URL = ("https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/"
            "default028mm/MapServer/tile/{release}/{z}/{y}/{x}")
TILEMAP_URL = ("https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/MapServer/"
               "tilemap/{release}/{z}/{y}/{x}")

ORIGIN = 20037508.342789244          # half the Web Mercator world width (m)
TILE = 256
MAX_TILES = 3000                     # safety cap per AOI mosaic (~190 MP)

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "tiles"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "GeoVision-Annotator/1.0"})

WKT_3857 = ('PROJCS["WGS 84 / Pseudo-Mercator",GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",'
            '6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],'
            'PROJECTION["Mercator_1SP"],PARAMETER["central_meridian",0],PARAMETER["scale_factor",1],'
            'PARAMETER["false_easting",0],PARAMETER["false_northing",0],UNIT["metre",1],'
            'AUTHORITY["EPSG","3857"]]')


# --------------------------------------------------------------------------- releases
_releases_cache: dict = {"t": 0, "data": None}


def get_releases(force: bool = False) -> list[dict]:
    """All Wayback releases, newest first: [{release, date, title, layer}]."""
    if not force and _releases_cache["data"] and time.time() - _releases_cache["t"] < 3600:
        return _releases_cache["data"]
    cfg = SESSION.get(CONFIG_URL, timeout=30).json()
    out = []
    for num, item in cfg.items():
        title = item.get("itemTitle", "")
        m = re.search(r"(\d{4}-\d{2}-\d{2})", title)
        out.append({
            "release": int(num),
            "date": m.group(1) if m else "",
            "title": title,
            "layer": item.get("layerIdentifier", ""),
            "tileUrl": item.get("itemURL", "").replace("{level}", "{z}").replace("{row}", "{y}").replace("{col}", "{x}")
                       or TILE_URL.replace("{release}", num),
        })
    out.sort(key=lambda r: r["date"], reverse=True)
    _releases_cache.update(t=time.time(), data=out)
    return out


def release_info(release: int) -> dict:
    for r in get_releases():
        if r["release"] == int(release):
            return r
    return {"release": int(release), "date": str(release), "title": f"Release {release}"}


def local_change_releases(lon: float, lat: float, z: int = 17) -> list[dict]:
    """Releases that actually changed the imagery at a point (same idea as the
    Wayback app's 'only updates with local changes' filter).

    Walk newest -> oldest; the tilemap 'select' field tells which older release
    really supplies the tile, so we jump straight to it.
    """
    rels = get_releases()
    by_num = {r["release"]: i for i, r in enumerate(rels)}
    x, y = lonlat_to_tile(lon, lat, z)
    found, i = [], 0
    while i < len(rels):
        r = rels[i]["release"]
        try:
            js = SESSION.get(TILEMAP_URL.format(release=r, z=z, y=y, x=x), timeout=20).json()
        except Exception:
            i += 1
            continue
        if not js.get("data") or js["data"][0] != 1:
            break                      # no imagery this old
        eff = (js.get("select") or [r])[0]
        if eff in by_num and (not found or found[-1]["release"] != eff):
            found.append(rels[by_num[eff]])
        i = max(i + 1, by_num.get(eff, i) + 1)
    return found


# --------------------------------------------------------------------------- geo math
def lonlat_to_merc(lon, lat):
    lat = max(min(lat, 85.05112878), -85.05112878)
    x = lon * ORIGIN / 180.0
    y = math.log(math.tan((90 + lat) * math.pi / 360.0)) * ORIGIN / math.pi
    return x, y


def merc_to_lonlat(x, y):
    lon = x / ORIGIN * 180.0
    lat = math.degrees(2 * math.atan(math.exp(y / ORIGIN * math.pi)) - math.pi / 2)
    return lon, lat


def resolution(z: int) -> float:
    return 2 * ORIGIN / (TILE * 2 ** z)


def lonlat_to_pixel(lon, lat, z):
    """Global pixel coordinates at zoom z."""
    x, y = lonlat_to_merc(lon, lat)
    res = resolution(z)
    return (x + ORIGIN) / res, (ORIGIN - y) / res


def lonlat_to_tile(lon, lat, z):
    px, py = lonlat_to_pixel(lon, lat, z)
    return int(px // TILE), int(py // TILE)


def to_merc_geom(geom):
    return shp_transform(lambda x, y, z=None: _vec(lonlat_to_merc, x, y), geom)


def to_lonlat_geom(geom):
    return shp_transform(lambda x, y, z=None: _vec(merc_to_lonlat, x, y), geom)


def _vec(fn, xs, ys):
    if np.isscalar(xs):
        return fn(xs, ys)
    pts = [fn(a, b) for a, b in zip(xs, ys)]
    return tuple(np.array(v) for v in zip(*pts))


@dataclass
class Raster:
    image: np.ndarray        # H x W x 3 uint8, outside-AOI pixels = 0
    mask: np.ndarray         # H x W bool, True inside AOI
    x0: float                # mercator x of left edge
    y0: float                # mercator y of top edge
    res: float               # metres per pixel (mercator)
    zoom: int
    release: int

    def pixel_to_lonlat(self, px, py):
        return merc_to_lonlat(self.x0 + px * self.res, self.y0 - py * self.res)

    def ground_res(self, lat) -> float:
        return self.res * math.cos(math.radians(lat))

    @property
    def transform(self):
        return (self.res, 0.0, self.x0, 0.0, -self.res, self.y0)


# --------------------------------------------------------------------------- tiles
def _fetch_tile(release: int, z: int, x: int, y: int) -> Image.Image | None:
    path = CACHE_DIR / str(release) / str(z) / str(x) / f"{y}.jpg"
    if path.exists():
        try:
            return Image.open(path).convert("RGB")
        except Exception:
            path.unlink(missing_ok=True)
    url = TILE_URL.format(release=release, z=z, y=y, x=x)
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=30)
            if r.status_code == 200 and r.content:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(r.content)
                return Image.open(io.BytesIO(r.content)).convert("RGB")
            if r.status_code in (404, 400):
                return None
        except requests.RequestException:
            time.sleep(1 + attempt)
    return None


def pick_zoom(geom_ll, zoom: int) -> int:
    """Drop zoom until the AOI mosaic is under MAX_TILES."""
    minx, miny, maxx, maxy = geom_ll.bounds
    z = zoom
    while z > 10:
        tx0, ty0 = lonlat_to_tile(minx, maxy, z)
        tx1, ty1 = lonlat_to_tile(maxx, miny, z)
        if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) <= MAX_TILES:
            return z
        z -= 1
    return z


def build_raster(geom_ll, release: int, zoom: int = 18, fetch=None, workers: int = 16,
                 progress=None) -> Raster:
    """Mosaic Wayback tiles covering the AOI and mask pixels outside the polygon."""
    fetch = fetch or _fetch_tile
    z = pick_zoom(geom_ll, zoom)
    minx, miny, maxx, maxy = geom_ll.bounds
    gx0, gy0 = lonlat_to_pixel(minx, maxy, z)
    gx1, gy1 = lonlat_to_pixel(maxx, miny, z)
    gx0, gy0, gx1, gy1 = int(math.floor(gx0)), int(math.floor(gy0)), int(math.ceil(gx1)), int(math.ceil(gy1))
    tx0, ty0, tx1, ty1 = gx0 // TILE, gy0 // TILE, (gx1 - 1) // TILE, (gy1 - 1) // TILE

    canvas = np.zeros(((ty1 - ty0 + 1) * TILE, (tx1 - tx0 + 1) * TILE, 3), np.uint8)
    jobs = [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]
    done = 0

    def task(t):
        return t, fetch(release, z, t[0], t[1])

    with ThreadPoolExecutor(workers) as ex:
        for (tx, ty), img in ex.map(task, jobs):
            if img is not None:
                ox, oy = (tx - tx0) * TILE, (ty - ty0) * TILE
                canvas[oy:oy + TILE, ox:ox + TILE] = np.asarray(img.resize((TILE, TILE)))
            done += 1
            if progress and done % 25 == 0:
                progress(done / len(jobs))

    cx0, cy0 = gx0 - tx0 * TILE, gy0 - ty0 * TILE
    img = canvas[cy0:cy0 + (gy1 - gy0), cx0:cx0 + (gx1 - gx0)].copy()
    res = resolution(z)
    x0, y0 = -ORIGIN + gx0 * res, ORIGIN - gy0 * res

    mask = polygon_mask(to_merc_geom(geom_ll), img.shape[1], img.shape[0], x0, y0, res)
    img[~mask] = 0
    return Raster(img, mask, x0, y0, res, z, int(release))


def polygon_mask(geom_m, w, h, x0, y0, res) -> np.ndarray:
    from PIL import ImageDraw
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    polys = [geom_m] if geom_m.geom_type == "Polygon" else list(getattr(geom_m, "geoms", []))
    for p in polys:
        if p.geom_type != "Polygon":
            continue
        ext = [((x - x0) / res, (y0 - y) / res) for x, y in p.exterior.coords]
        d.polygon(ext, fill=255)
        for ring in p.interiors:
            d.polygon([((x - x0) / res, (y0 - y) / res) for x, y in ring.coords], fill=0)
    return np.asarray(m) > 0


# --------------------------------------------------------------------------- writers
def save_raster(r: Raster, out_dir: Path, stem: str = "image") -> dict:
    """Write the AOI clip as GeoTIFF (if rasterio present), PNG + world file, and JPG preview."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    rgba = np.dstack([r.image, (r.mask * 255).astype(np.uint8)])
    png = out_dir / f"{stem}.png"
    Image.fromarray(rgba, "RGBA").save(png, optimize=True)
    files["png"] = png.name
    # world file: pixel centre of upper-left pixel
    (out_dir / f"{stem}.pgw").write_text(
        f"{r.res}\n0.0\n0.0\n{-r.res}\n{r.x0 + r.res / 2}\n{r.y0 - r.res / 2}\n")
    (out_dir / f"{stem}.prj").write_text(WKT_3857)
    try:
        import rasterio
        from rasterio.transform import Affine
        tif = out_dir / f"{stem}.tif"
        with rasterio.open(tif, "w", driver="GTiff", width=r.image.shape[1], height=r.image.shape[0],
                           count=4, dtype="uint8", crs="EPSG:3857",
                           transform=Affine(r.res, 0, r.x0, 0, -r.res, r.y0),
                           compress="deflate", tiled=True, photometric="RGB") as dst:
            for b in range(3):
                dst.write(r.image[:, :, b], b + 1)
            dst.write((r.mask * 255).astype(np.uint8), 4)
            dst.colorinterp = [rasterio.enums.ColorInterp.red, rasterio.enums.ColorInterp.green,
                               rasterio.enums.ColorInterp.blue, rasterio.enums.ColorInterp.alpha]
            dst.update_tags(SOURCE="Esri World Imagery Wayback", RELEASE=str(r.release), ZOOM=str(r.zoom))
        files["geotiff"] = tif.name
    except ImportError:
        pass
    return files


def raster_bounds_ll(r: Raster):
    h, w = r.image.shape[:2]
    lon0, lat0 = r.pixel_to_lonlat(0, h)
    lon1, lat1 = r.pixel_to_lonlat(w, 0)
    return [lon0, lat0, lon1, lat1]
