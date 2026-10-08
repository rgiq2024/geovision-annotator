"""Export YOLO OBB models to ONNX for the in-browser detector and write
frontend/models/models.json. Used by the GitHub Pages workflow; also run it
locally before serving frontend/ with any static server.

  python scripts/export_onnx.py                      # yolo11n-obb + yolo11s-obb
  python scripts/export_onnx.py --models my_best.pt  # your own fine-tuned weights
"""
import argparse
import json
import shutil
from pathlib import Path

from ultralytics import YOLO

LABELS = {"yolo11n-obb.pt": "YOLO11 nano OBB · fast", "yolo11s-obb.pt": "YOLO11 small OBB · more accurate",
          "yolo11m-obb.pt": "YOLO11 medium OBB · best, slow in browser"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=["yolo11n-obb.pt", "yolo11s-obb.pt"])
    ap.add_argument("--imgsz", type=int, default=1024)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "frontend" / "models"))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    entries = []
    for w in a.models:
        m = YOLO(w)
        onnx = Path(m.export(format="onnx", imgsz=a.imgsz, opset=17, simplify=True, dynamic=False))
        dst = out / onnx.name
        shutil.move(str(onnx), dst)
        entries.append({"file": dst.name, "label": LABELS.get(Path(w).name, Path(w).stem), "imgsz": a.imgsz,
                        "names": [m.names[i] for i in sorted(m.names)], "task": m.task,
                        "size_mb": round(dst.stat().st_size / 1e6, 1)})
        print("exported", dst, entries[-1]["size_mb"], "MB")
    (out / "models.json").write_text(json.dumps(entries, indent=1))


if __name__ == "__main__":
    main()
