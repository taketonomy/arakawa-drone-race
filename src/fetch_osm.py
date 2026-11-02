"""Download the Arakawa water area and the bridges over it (Adachi-ku section) from OpenStreetMap.

Data © OpenStreetMap contributors, ODbL 1.0. Writes data/osm_arakawa.json (raw Overpass response).
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BBOX = (35.735, 139.70, 35.800, 139.870)   # south, west, north, east — Arakawa along Adachi-ku
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

QUERY = """
[out:json][timeout:180];
(
  relation["natural"="water"]["name"="荒川"]({b});
  way["natural"="water"]["name"="荒川"]({b});
  way["waterway"="riverbank"]["name"="荒川"]({b});
);
out geom;
(
  way["bridge"]["highway"]({b});
  way["bridge"]["railway"]({b});
);
out geom;
"""


def main() -> int:
    b = ",".join(str(v) for v in BBOX)
    body = urllib.parse.urlencode({"data": QUERY.format(b=b)}).encode()
    last = None
    for url in ENDPOINTS:
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": "arakawa-drone-race/0.1"})
            with urllib.request.urlopen(req, timeout=200) as r:
                osm = json.load(r)
            break
        except Exception as e:  # try the next mirror
            last = e
            print(f"{url}: {e}", file=sys.stderr)
    else:
        print(f"Overpass download failed: {last}", file=sys.stderr)
        return 1
    n_water = sum(1 for e in osm["elements"] if e.get("tags", {}).get("name") == "荒川")
    if not n_water:
        print("no water element named 荒川 in the response", file=sys.stderr)
        return 1
    out = ROOT / "data" / "osm_arakawa.json"
    out.write_text(json.dumps(osm, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out} ({len(osm['elements'])} elements, {n_water} water)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
