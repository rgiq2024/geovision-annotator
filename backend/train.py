"""Turn portal annotations (verified / corrected / hand-drawn objects on Wayback
imagery) into a YOLO-OBB dataset and fine-tune a model.

Annotation feature properties used: class, release, zoom, status
(status == "rejected" is excluded and acts as a hard negative in its chip).
"""
from __future__ import annotations

import json
import random
import shutil
import threading
import time
from pathlib import Path

from PIL import Image
from shapely.geometry import box, shape

import wayback

ROOT = Path(__file__).resolve().parent.parent
ANN_FILE = ROOT / "data" / "annotations.geojson"
DATA_DIR = ROOT / "data" / "datasets"
MODELS_DIR = ROOT / "models"
TRAIN_JOBS: dict[str, dict] = {}


def load_annotations() -> dict:
    if ANN_FILE.exists():
        return json.loads(ANN_FILE.read_text())
    return {"type": "FeatureCollection", "features": []}


def save_annotations(fc: dict, replace_ids: bool = True) -> dict:
    """Upsert features by id."""
    cur = load_annotations()
    by_id = {f.get("id"): f for f in cur["features"] if f.get("id")}
    extra = [f for f in cur["features"] if not f.get("id")]
    for f in fc.get("features", []):
        if f.get("id") and replace_ids:
            by_id[f["id"]] = f
        else:
            extra.append(f)
    out = {"type": "FeatureCollection", "features": list(by_id.values()) + extra}
    ANN_FILE.parent.mkdir(parents=True, exist_ok=True)
    ANN_FILE.write_text(json.dumps(out))
    return out


def build_dataset(chip: int = 1024, zoom: int = 18, val_frac: float = 0.15, log=print) -> Path:
    fc = load_annotations()
    feats = [f for f in fc["features"]
             if (f.get("properties") or {}).get("status") != "rejected"
             and (f.get("properties") or {}).get("class")]
    if not feats:
        raise ValueError("No annotations yet. Verify, correct or draw some objects first.")
    classes = sorted({f["properties"]["class"] for f in feats})
    cid = {c: i for i, c in enumerate(classes)}

    name = time.strftime("ds_%Y%m%d_%H%M%S")
    ds = DATA_DIR / name
    for s in ("train", "val"):
        (ds / "images" / s).mkdir(parents=True, exist_ok=True)
        (ds / "labels" / s).mkdir(parents=True, exist_ok=True)

    groups: dict = {}
    for f in feats:
        p = f["properties"]
        groups.setdefault((int(p.get("release")), int(p.get("zoom", zoom))), []).append(f)

    n_chips = 0
    for (rel, z), gfeats in groups.items():
        geoms = [shape(f["geometry"]) for f in gfeats]
        # cluster annotations into ~2 km cells so we don't download whole regions
        cells: dict = {}
        for f, g in zip(gfeats, geoms):
            c = g.centroid
            cells.setdefault((round(c.x / 0.02), round(c.y / 0.02)), []).append((f, g))
        for items in cells.values():
            minx = min(g.bounds[0] for _, g in items); miny = min(g.bounds[1] for _, g in items)
            maxx = max(g.bounds[2] for _, g in items); maxy = max(g.bounds[3] for _, g in items)
            area = box(minx, miny, maxx, maxy).buffer(0.0015)
            log(f"release {rel} z{z}: {len(items)} objects")
            ras = wayback.build_raster(area, rel, z)
            h, w = ras.image.shape[:2]
            px_polys = []
            for f, g in items:
                g = g.minimum_rotated_rectangle
                gm = wayback.to_merc_geom(g)
                pts = [((x - ras.x0) / ras.res, (ras.y0 - y) / ras.res) for x, y in list(gm.exterior.coords)[:4]]
                px_polys.append((cid[f["properties"]["class"]], pts))
            from detector import _windows
            for x, y in _windows(h, w, chip, 200):
                    lines = []
                    for k, pts in px_polys:
                        cx = sum(p[0] for p in pts) / 4; cy = sum(p[1] for p in pts) / 4
                        if x <= cx < x + chip and y <= cy < y + chip:
                            norm = [min(max(v, 0), 1) for p in pts
                                    for v in ((p[0] - x) / chip, (p[1] - y) / chip)]
                            lines.append(f"{k} " + " ".join(f"{v:.6f}" for v in norm))
                    split = "val" if random.random() < val_frac else "train"
                    stem = f"r{rel}_z{z}_{n_chips:05d}"
                    tile = Image.new("RGB", (chip, chip))
                    tile.paste(Image.fromarray(ras.image[y:y + chip, x:x + chip]), (0, 0))
                    tile.save(ds / "images" / split / f"{stem}.jpg", quality=92)
                    (ds / "labels" / split / f"{stem}.txt").write_text("\n".join(lines))
                    n_chips += 1
    # make sure val is not empty
    if not any((ds / "images" / "val").iterdir()):
        first = next((ds / "images" / "train").iterdir())
        shutil.copy(first, ds / "images" / "val" / first.name)
        shutil.copy(ds / "labels" / "train" / (first.stem + ".txt"), ds / "labels" / "val" / (first.stem + ".txt"))
    (ds / "data.yaml").write_text(
        f"path: {ds.as_posix()}\ntrain: images/train\nval: images/val\nnames:\n"
        + "".join(f"  {i}: {c}\n" for i, c in enumerate(classes)))
    log(f"Dataset {name}: {n_chips} chips, classes {classes}")
    return ds


def start_training(params: dict) -> str:
    tid = time.strftime("train_%Y%m%d_%H%M%S")
    TRAIN_JOBS[tid] = {"id": tid, "status": "running", "log": [], "params": params}
    threading.Thread(target=_train, args=(tid,), daemon=True).start()
    return tid


def _train(tid):
    job = TRAIN_JOBS[tid]
    log = lambda m: job["log"].append(f'{time.strftime("%H:%M:%S")}  {m}')
    p = job["params"]
    try:
        ds = build_dataset(zoom=int(p.get("zoom", 18)), log=log)
        from ultralytics import YOLO
        base = p.get("base_model") or "yolo11s-obb.pt"
        log(f"Fine-tuning {base} for {p.get('epochs', 50)} epochs…")
        model = YOLO(base)
        model.train(data=str(ds / "data.yaml"), epochs=int(p.get("epochs", 50)), imgsz=1024,
                    batch=int(p.get("batch", 4)), project=str(MODELS_DIR / "runs"), name=tid,
                    exist_ok=True, verbose=False)
        best = MODELS_DIR / "runs" / tid / "weights" / "best.pt"
        if best.exists():
            dst = MODELS_DIR / f"{p.get('name') or tid}.pt"
            shutil.copy(best, dst)
            log(f"Saved model {dst.name} — select it in Detect.")
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        log(f"ERROR: {e}")
