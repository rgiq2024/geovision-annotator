"""Headless batch run (no browser):

  python backend/batch.py sample_aois/uae_airports_ports.geojson --mode local --classes aircraft ship
  python backend/batch.py my_airports.geojson --mode selected --releases 23001 31144 --zoom 18
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import pipeline  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("aois", help="GeoJSON FeatureCollection of AOI polygons (or points)")
    ap.add_argument("--mode", choices=["latest", "local", "selected"], default="latest")
    ap.add_argument("--releases", type=int, nargs="*", default=[])
    ap.add_argument("--zoom", type=int, default=18)
    ap.add_argument("--classes", nargs="*", default=["aircraft", "ship", "helicopter"])
    ap.add_argument("--conf", type=float, default=0.3)
    ap.add_argument("--model", default=None)
    ap.add_argument("--no-detect", action="store_true", help="only download and clip imagery")
    a = ap.parse_args()

    fc = json.loads(Path(a.aois).read_text())
    jid = pipeline.start_job({"aois": fc, "release_mode": a.mode, "releases": a.releases, "zoom": a.zoom,
                              "classes": a.classes, "conf": a.conf, "model": a.model,
                              "detect": not a.no_detect})
    seen = 0
    while True:
        j = pipeline.JOBS[jid]
        for line in j["log"][seen:]:
            print(line)
        seen = len(j["log"])
        if j["status"] in ("done", "error"):
            break
        time.sleep(1)
    print(f"\nOutputs: {pipeline.OUT_DIR / jid}\nZip:     {pipeline.OUT_DIR / (jid + '.zip')}")


if __name__ == "__main__":
    main()
