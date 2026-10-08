# GeoVision Annotator

Web portal for detecting and annotating aircraft, ships and other objects on
**Esri World Imagery Wayback** across many AOIs (airports, ports, bases) and many
imagery dates, with export of GeoJSON and AOI-clipped images.

```
AOIs (GeoJSON/KML/SHP/CSV)  ─┐
Wayback releases (latest /   ├─► mosaic tiles ─► clip to AOI ─► YOLO-OBB detection ─► GeoJSON + GeoTIFF/PNG ─► zip
  every local change / pick) ┘                                       │
                                     review in portal (accept / reject / reclass / redraw) ─► training set ─► fine-tune
```

## Run it in the browser (GitHub Pages, nothing to install)

**https://rgiq2024.github.io/geovision-annotator/**

Everything happens on the visitor's computer: Wayback tiles are fetched by the browser, clipped
to each AOI on a canvas, run through YOLO11-OBB with ONNX Runtime Web (WebGPU on Chrome/Edge,
WebAssembly elsewhere), and packed into the export zip (GeoTIFF + PNG/world file + GeoJSON + previews).

One-time setup: **Settings → Pages → Source: GitHub Actions**. Every push to `main` then runs
`.github/workflows/pages.yml`, which exports the ONNX models (`scripts/export_onnx.py`) and publishes `frontend/`.

Speed guide: WebGPU is roughly 0.2–0.5 s per 1024 px chip; WebAssembly about 3 s. An airport at z18 is 30–70 chips.
Results live in the tab's memory, so export the zip before closing it. Reviewed annotations stay in the browser.

Training without a server: **Train → Download training dataset** (YOLO-OBB chips, labels, `data.yaml`),
train on any PC, Colab or Kaggle with the commands in its `TRAIN.md`, then **Detect → Load ONNX model**.

Run the static portal locally: `python scripts/export_onnx.py && cd frontend && python -m http.server 8080`.

## Optional Python server (large jobs, GPU training)

```bash
./run.sh            # Linux / macOS   (Windows: run.bat)
# open http://localhost:8000, or connect the Pages portal to it in Export → Processing
```

Python 3.10+. The first detection run downloads the YOLO11 OBB weights automatically.

## Workflow in the portal

| Tab | What you do |
|---|---|
| **AOIs** | Drop GeoJSON, KML, zipped Shapefile or CSV (`name,lat,lon`). Points become square AOIs (buffer in km). Or draw. *Load UAE sample* adds DXB, SHJ, AUH, DWC, Jebel Ali Port, Port Rashid. |
| **Wayback** | Browse every release on the map. Choose what to process: **Latest**, **All changes** (per AOI, every release where the imagery actually changed — a time series), or **Selected** releases. *Changes at AOI* marks the dates that exist for the active AOI. |
| **Detect** | Model, classes (aircraft, ship, helicopter, storage tank, vehicles, harbor, bridge…), confidence, zoom (z18 ≈ 0.54 m/px in the UAE). Runs every AOI × every release. |
| **Results** | Per AOI and date: object counts, a bar chart of objects per imagery date, clipped image overlay with detections. |
| **Annotate** | Click objects: Accept `A`, Reject `R`, Next `N`, Edit shape `E`, change class, delete; draw missed objects (box/polygon); add your own classes. *Save to training set*. |
| **Train** | Downloads a YOLO-OBB dataset built from saved annotations (1024 px chips from the same Wayback release). With the server connected, fine-tunes directly. |
| **Export** | Job zip, reviewed detections GeoJSON, AOIs, CSV table. |

## Outputs

```
<job_id>/                        (browser zip; outputs/<job_id>/ on the server)
  all_detections.geojson         every object, every AOI, every date (EPSG:4326)
  summary.json                   counts per AOI × date
  aois.geojson
  <AOI>/<YYYY-MM-DD>/
    image.tif                    GeoTIFF EPSG:3857, RGB + alpha, clipped to the AOI polygon
    image.png  image.pgw  image.prj   same clip as PNG + world file (opens in QGIS/ArcGIS)
    detections.geojson           oriented boxes with class, confidence, length_m, width_m, heading_deg
    preview_detections.jpg       clip with boxes drawn
    aoi.geojson
outputs/<job_id>.zip
```

Feature properties: `aoi_id, aoi_name, release, imagery_date, zoom, gsd_m, class, confidence,
length_m, width_m, heading_deg, centroid, status` (`predicted | verified | corrected | manual | rejected`).

## Headless batch (no browser)

```bash
python backend/batch.py my_airports.geojson --mode local --classes aircraft ship helicopter --zoom 18
python backend/batch.py ports.kml.geojson --mode selected --releases 31144 23001
python backend/batch.py aois.geojson --no-detect          # imagery clips only
```

## Notes

- **Model**: default `yolo11s-obb.pt` (DOTA v1, aerial). Use `yolo11m/l-obb` for accuracy, or your fine-tuned model.
  DOTA labels are mapped to friendly names (`plane → aircraft`, `storage tank → storage_tank`, …).
- **Large AOIs**: zoom steps down automatically to stay under 3,000 tiles per clip (`MAX_TILES` in `backend/wayback.py`).
- **Tile cache**: `cache/tiles/<release>/<z>/<x>/<y>.jpg`, reused across jobs and training.
- **Licensing**: Wayback imagery is subject to Esri's terms of use. Check that your use (especially
  bulk download and model training) is covered by your Esri/ArcGIS licence, and keep the attribution
  *Esri, Maxar, Earthstar Geographics, and the GIS User Community* on derived products.
