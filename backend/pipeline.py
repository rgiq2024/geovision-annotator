"""Batch job: for every AOI x every selected Wayback release ->
clip imagery to AOI, detect objects, write GeoJSON + images, build a zip export."""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path

from shapely.geometry import mapping, shape

import wayback

OUT_DIR = Path(__file__).resolve().parent.parent / "outputs"
JOBS: dict[str, dict] = {}


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(s)).strip("_")[:60] or "aoi"


def aoi_name(f: dict, i: int) -> str:
    p = f.get("properties") or {}
    for k in ("name", "Name", "NAME", "iata", "IATA", "icao", "ICAO", "id", "title"):
        if p.get(k):
            return str(p[k])
    return f"AOI_{i + 1}"


def resolve_releases(geom, mode: str, chosen: list[int], zoom: int) -> list[dict]:
    if mode == "latest":
        return wayback.get_releases()[:1]
    if mode == "local":
        c = geom.representative_point()
        return wayback.local_change_releases(c.x, c.y, min(zoom, 17))
    return [wayback.release_info(r) for r in chosen]


def start_job(params: dict) -> str:
    job_id = time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    JOBS[job_id] = {"id": job_id, "status": "queued", "progress": 0.0, "log": [], "params": params,
                    "results": [], "started": time.time()}
    threading.Thread(target=_run, args=(job_id,), daemon=True).start()
    return job_id


def _log(job, msg):
    job["log"].append(f'{time.strftime("%H:%M:%S")}  {msg}')


def _run(job_id: str):
    job = JOBS[job_id]
    p = job["params"]
    root = OUT_DIR / job_id
    root.mkdir(parents=True, exist_ok=True)
    job["status"] = "running"
    all_feats, summary = [], []
    try:
        aois = p["aois"]["features"]
        zoom = int(p.get("zoom", 18))
        detect_on = p.get("detect", True)
        if detect_on:
            import detector  # heavy import only when needed
        (root / "aois.geojson").write_text(json.dumps(p["aois"]))

        plan = []
        for i, f in enumerate(aois):
            geom = shape(f["geometry"])
            if geom.geom_type not in ("Polygon", "MultiPolygon"):
                geom = geom.buffer(0.01)                    # point/line AOI -> ~1 km buffer
            name = aoi_name(f, i)
            rels = resolve_releases(geom, p.get("release_mode", "latest"), p.get("releases", []), zoom)
            _log(job, f"{name}: {len(rels)} release(s) — " + ", ".join(r["date"] for r in rels[:8])
                 + (" …" if len(rels) > 8 else ""))
            for r in rels:
                plan.append((i, name, geom, r))

        for n, (i, name, geom, rel) in enumerate(plan):
            base = n / len(plan)
            step = 1 / len(plan)
            sub = root / slug(name) / (rel["date"] or str(rel["release"]))
            _log(job, f"[{n + 1}/{len(plan)}] {name} · {rel['date']} — fetching imagery")
            ras = wayback.build_raster(geom, rel["release"], zoom,
                                       progress=lambda f: job.__setitem__("progress", base + step * 0.5 * f))
            files = wayback.save_raster(ras, sub)
            bounds = wayback.raster_bounds_ll(ras)
            props = {"aoi_id": slug(name), "aoi_name": name, "release": rel["release"],
                     "imagery_date": rel["date"], "zoom": ras.zoom,
                     "gsd_m": round(ras.ground_res(geom.centroid.y), 3), "source": "Esri Wayback"}
            feats, counts = [], {}
            if detect_on and ras.mask.any():
                _log(job, f"    detecting ({ras.image.shape[1]}×{ras.image.shape[0]} px, z{ras.zoom})")
                dets = detector.detect(ras, p.get("model"), p.get("classes") or None,
                                       float(p.get("conf", 0.3)),
                                       progress=lambda f: job.__setitem__("progress", base + step * (0.5 + 0.5 * f)))
                feats = detector.to_features(dets, ras, props)
                detector.draw_preview(ras, dets, sub / "preview_detections.jpg")
                files["preview"] = "preview_detections.jpg"
                for ft in feats:
                    counts[ft["properties"]["class"]] = counts.get(ft["properties"]["class"], 0) + 1
            fc = {"type": "FeatureCollection", "features": feats,
                  "properties": {**props, "image_bounds": bounds}}
            (sub / "detections.geojson").write_text(json.dumps(fc))
            (sub / "aoi.geojson").write_text(json.dumps(
                {"type": "Feature", "properties": {"name": name}, "geometry": mapping(geom)}))
            all_feats.extend(feats)
            rel_path = sub.relative_to(root).as_posix()
            entry = {"aoi": name, "aoi_id": slug(name), "release": rel["release"], "date": rel["date"],
                     "zoom": ras.zoom, "bounds": bounds, "path": rel_path, "files": files,
                     "counts": counts, "total": len(feats)}
            summary.append(entry)
            job["results"] = summary
            _log(job, f"    {len(feats)} objects " + (str(counts) if counts else ""))
            job["progress"] = base + step

        (root / "all_detections.geojson").write_text(json.dumps(
            {"type": "FeatureCollection", "features": all_feats}))
        (root / "summary.json").write_text(json.dumps(
            {"job": job_id, "params": {k: v for k, v in p.items() if k != "aois"}, "results": summary}, indent=2))
        make_zip(job_id)
        job["status"] = "done"
        job["progress"] = 1.0
        _log(job, f"Finished: {len(all_feats)} objects across {len(summary)} AOI/date clips")
    except Exception as e:
        job["status"] = "error"
        _log(job, f"ERROR: {e}")
        _log(job, traceback.format_exc().splitlines()[-1])


def make_zip(job_id: str) -> Path:
    root = OUT_DIR / job_id
    zpath = OUT_DIR / f"{job_id}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in root.rglob("*"):
            if f.is_file():
                z.write(f, f"{job_id}/{f.relative_to(root).as_posix()}")
    return zpath
