"""GeoVision Annotator — FastAPI server.

Run:  uvicorn app:app --app-dir backend --port 8000
Then open http://localhost:8000
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import pipeline
import train
import wayback

ROOT = Path(__file__).resolve().parent.parent
FRONT = ROOT / "frontend"
pipeline.OUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="GeoVision Annotator")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/api/health")
def health():
    try:
        import ultralytics  # noqa
        det = True
    except ImportError:
        det = False
    return {"ok": True, "detector": det}


@app.get("/api/releases")
def releases():
    return wayback.get_releases()


@app.post("/api/releases/local")
def releases_local(body: dict = Body(...)):
    """{lon, lat, zoom} -> releases with real imagery change at that spot."""
    return wayback.local_change_releases(float(body["lon"]), float(body["lat"]), int(body.get("zoom", 17)))


@app.get("/api/models")
def models():
    from detector import DEFAULT_MODEL, MODELS_DIR
    MODELS_DIR.mkdir(exist_ok=True)
    local = sorted(p.name for p in MODELS_DIR.glob("*.pt"))
    official = ["yolo11n-obb.pt", "yolo11s-obb.pt", "yolo11m-obb.pt", "yolo11l-obb.pt"]
    return {"default": DEFAULT_MODEL, "models": local + [m for m in official if m not in local]}


@app.get("/api/models/{name}/classes")
def classes(name: str):
    import detector
    return detector.model_classes(name)


# ---------------------------------------------------------------- detection jobs
@app.post("/api/jobs")
def create_job(body: dict = Body(...)):
    """body: {aois: FeatureCollection, release_mode: latest|local|selected, releases: [int],
              zoom, classes: [str], conf, model, detect: bool}"""
    if not body.get("aois", {}).get("features"):
        raise HTTPException(400, "Add at least one AOI.")
    return {"job_id": pipeline.start_job(body)}


@app.get("/api/jobs")
def list_jobs():
    live = [{k: j[k] for k in ("id", "status", "progress")} for j in pipeline.JOBS.values()]
    ids = {j["id"] for j in live}
    for d in sorted(pipeline.OUT_DIR.glob("*/summary.json"), reverse=True):
        if d.parent.name not in ids:
            live.append({"id": d.parent.name, "status": "done", "progress": 1})
    return live


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    if job_id in pipeline.JOBS:
        j = pipeline.JOBS[job_id]
        return {k: j[k] for k in ("id", "status", "progress", "log", "results")}
    s = pipeline.OUT_DIR / job_id / "summary.json"
    if s.exists():
        d = json.loads(s.read_text())
        return {"id": job_id, "status": "done", "progress": 1, "log": [], "results": d["results"]}
    raise HTTPException(404, "Job not found")


@app.get("/api/jobs/{job_id}/export")
def export(job_id: str):
    z = pipeline.OUT_DIR / f"{job_id}.zip"
    if not z.exists():
        if not (pipeline.OUT_DIR / job_id).exists():
            raise HTTPException(404, "Job not found")
        pipeline.make_zip(job_id)
    return FileResponse(z, filename=f"geovision_{job_id}.zip", media_type="application/zip")


# ---------------------------------------------------------------- annotations & training
@app.get("/api/annotations")
def get_annotations():
    return train.load_annotations()


@app.post("/api/annotations")
def post_annotations(fc: dict = Body(...)):
    out = train.save_annotations(fc)
    return {"count": len(out["features"])}


@app.post("/api/train")
def start_train(body: dict = Body(...)):
    return {"train_id": train.start_training(body)}


@app.get("/api/train/{tid}")
def train_status(tid: str):
    if tid not in train.TRAIN_JOBS:
        raise HTTPException(404, "Training job not found")
    return train.TRAIN_JOBS[tid]


# ---------------------------------------------------------------- static
app.mount("/outputs", StaticFiles(directory=pipeline.OUT_DIR), name="outputs")


@app.get("/")
def index():
    return FileResponse(FRONT / "index.html")


app.mount("/", StaticFiles(directory=FRONT), name="front")
