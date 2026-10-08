"""Object detection on large AOI rasters with Ultralytics YOLO.

Default model: YOLO11 OBB pretrained on DOTA v1 (aerial imagery). It returns
oriented boxes for aircraft ("plane"), ships, storage tanks, helicopters,
large/small vehicles, harbours, bridges and more. Any custom model trained in
this portal (OBB or axis-aligned) is also accepted.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon, mapping

from wayback import Raster

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
DEFAULT_MODEL = "yolo11s-obb.pt"

# Friendly names for DOTA v1 classes
LABELS = {
    "plane": "aircraft", "ship": "ship", "storage tank": "storage_tank",
    "baseball diamond": "baseball_diamond", "tennis court": "tennis_court",
    "basketball court": "basketball_court", "ground track field": "track_field",
    "harbor": "harbor", "bridge": "bridge", "large vehicle": "large_vehicle",
    "small vehicle": "small_vehicle", "helicopter": "helicopter",
    "roundabout": "roundabout", "soccer ball field": "soccer_field",
    "swimming pool": "swimming_pool",
}

_models: dict = {}


def resolve_model(name: str | None) -> str:
    name = name or DEFAULT_MODEL
    p = MODELS_DIR / name
    if p.exists():
        return str(p)
    for cand in MODELS_DIR.rglob(name):
        return str(cand)
    return name            # ultralytics downloads official weights by name


def load_model(name: str | None = None):
    from ultralytics import YOLO
    path = resolve_model(name)
    if path not in _models:
        _models[path] = YOLO(path)
    return _models[path]


def model_classes(name: str | None = None) -> list[str]:
    m = load_model(name)
    return [LABELS.get(v, v) for _, v in sorted(m.names.items())]


def _windows(h, w, size, overlap):
    step = max(size - overlap, 1)
    ys = list(range(0, max(h - size, 0) + 1, step)) or [0]
    xs = list(range(0, max(w - size, 0) + 1, step)) or [0]
    if ys[-1] + size < h:
        ys.append(h - size)
    if xs[-1] + size < w:
        xs.append(w - size)
    for y in ys:
        for x in xs:
            yield max(x, 0), max(y, 0)


def _nms(dets, iou_thr=0.45):
    """Per-class greedy NMS on polygons (handles tile-overlap duplicates)."""
    keep = []
    for cls in {d["cls"] for d in dets}:
        group = sorted([d for d in dets if d["cls"] == cls], key=lambda d: -d["conf"])
        kept: list = []
        for d in group:
            p = d["poly"]
            ok = True
            for k in kept:
                q = k["poly"]
                if not p.intersects(q):
                    continue
                inter = p.intersection(q).area
                if inter / max(min(p.area, q.area), 1e-9) > iou_thr:   # IoMin is robust at tile edges
                    ok = False
                    break
            if ok:
                kept.append(d)
        keep.extend(kept)
    return keep


def detect(raster: Raster, model_name: str | None = None, classes: list[str] | None = None,
           conf: float = 0.3, chip: int = 1024, overlap: int = 256, imgsz: int = 1024,
           progress=None, device=None) -> list[dict]:
    """Run tiled inference. Returns detections in pixel space with polygons."""
    model = load_model(model_name)
    names = {i: LABELS.get(n, n) for i, n in model.names.items()}
    wanted = set(classes) if classes else None
    img = raster.image
    h, w = img.shape[:2]
    wins = list(_windows(h, w, chip, overlap))
    dets = []
    for i, (x, y) in enumerate(wins):
        sub_mask = raster.mask[y:y + chip, x:x + chip]
        if sub_mask.mean() < 0.02:
            continue
        sub = img[y:y + chip, x:x + chip]
        if sub.shape[0] < chip or sub.shape[1] < chip:       # pad edge chips
            pad = np.zeros((chip, chip, 3), np.uint8)
            pad[:sub.shape[0], :sub.shape[1]] = sub
            sub = pad
        res = model.predict(sub[:, :, ::-1], imgsz=imgsz, conf=conf, verbose=False, device=device)[0]
        if getattr(res, "obb", None) is not None and len(res.obb):
            polys = res.obb.xyxyxyxy.cpu().numpy()
            confs = res.obb.conf.cpu().numpy()
            clss = res.obb.cls.cpu().numpy().astype(int)
        elif res.boxes is not None and len(res.boxes):
            b = res.boxes.xyxy.cpu().numpy()
            polys = np.stack([b[:, [0, 1]], b[:, [2, 1]], b[:, [2, 3]], b[:, [0, 3]]], 1)
            confs = res.boxes.conf.cpu().numpy()
            clss = res.boxes.cls.cpu().numpy().astype(int)
        else:
            polys, confs, clss = [], [], []
        for poly, c, k in zip(polys, confs, clss):
            name = names.get(int(k), str(k))
            if wanted and name not in wanted:
                continue
            pts = [(float(px) + x, float(py) + y) for px, py in poly]
            cx = sum(p[0] for p in pts) / 4
            cy = sum(p[1] for p in pts) / 4
            if not (0 <= int(cy) < h and 0 <= int(cx) < w) or not raster.mask[int(cy), int(cx)]:
                continue                                           # centre outside AOI
            P = Polygon(pts)
            if not P.is_valid or P.area < 4:
                continue
            dets.append({"cls": name, "conf": float(c), "poly": P})
        if progress:
            progress((i + 1) / len(wins))
    return _nms(dets)


def to_features(dets: list[dict], raster: Raster, props: dict) -> list[dict]:
    """Pixel polygons -> GeoJSON features (EPSG:4326) with size and heading."""
    feats = []
    for i, d in enumerate(sorted(dets, key=lambda d: -d["conf"])):
        coords = list(d["poly"].exterior.coords)[:4]
        ll = [raster.pixel_to_lonlat(px, py) for px, py in coords]
        ll.append(ll[0])
        c = d["poly"].centroid
        clon, clat = raster.pixel_to_lonlat(c.x, c.y)
        gr = raster.ground_res(clat)
        e1 = math.dist(coords[0], coords[1]) * gr
        e2 = math.dist(coords[1], coords[2]) * gr
        a, b = (coords[0], coords[1]) if e1 >= e2 else (coords[1], coords[2])
        heading = (math.degrees(math.atan2(b[0] - a[0], -(b[1] - a[1]))) + 360) % 180
        feats.append({
            "type": "Feature",
            "id": f'{props.get("aoi_id", "aoi")}_{props.get("release", "")}_{i:04d}',
            "geometry": {"type": "Polygon", "coordinates": [[[round(x, 7), round(y, 7)] for x, y in ll]]},
            "properties": {
                **props,
                "class": d["cls"],
                "confidence": round(d["conf"], 4),
                "length_m": round(max(e1, e2), 1),
                "width_m": round(min(e1, e2), 1),
                "heading_deg": round(heading, 1),
                "centroid": [round(clon, 7), round(clat, 7)],
                "status": "predicted",
            },
        })
    return feats


def draw_preview(raster: Raster, dets: list[dict], path: Path, max_side: int = 4000):
    from PIL import Image, ImageDraw
    palette = {"aircraft": (255, 176, 0), "ship": (0, 200, 255), "helicopter": (255, 90, 160),
               "storage_tank": (160, 255, 120), "large_vehicle": (255, 120, 60),
               "small_vehicle": (200, 200, 255), "harbor": (120, 220, 220), "bridge": (230, 230, 120)}
    im = Image.fromarray(raster.image)
    d = ImageDraw.Draw(im)
    for det in dets:
        col = palette.get(det["cls"], (255, 255, 255))
        pts = list(det["poly"].exterior.coords)
        d.line(pts, fill=col, width=2)
    s = max(im.size) / max_side
    if s > 1:
        im = im.resize((int(im.width / s), int(im.height / s)))
    im.save(path, quality=88)
