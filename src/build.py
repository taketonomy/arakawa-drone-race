"""Build the demo: river + flight plans -> Z3 scenarios -> docs/index.html (self-contained).

Usage:
  python src/fetch_osm.py            # once: download the river and bridges from OpenStreetMap
  python src/build.py                # solve and write docs/index.html
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from shapely.geometry import Point, mapping

sys.path.insert(0, str(Path(__file__).parent))
from geo import LocalProj, bridges_from_overpass, build_reach, flight_plan_shapes, water_from_overpass  # noqa: E402
from solver import LABELS, PLAN_BUFFER, Params, solve, verify_path  # noqa: E402
from fetch_osm import BBOX  # noqa: E402
from shapely.geometry import box  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BANKS = [30, 40, 50, 60, 70, 80, 100]   # C2 distances offered on the slider (m)


def weekend_slots(start: datetime, end: datetime, hour: int = 10, hours: int = 2):
    out = []
    day = start
    while day <= end:
        if day.weekday() >= 5:
            a = day.replace(hour=hour, minute=0, second=0)
            out.append((a, a + timedelta(hours=hours)))
        day += timedelta(days=1)
    return out


def _dt(s):
    try:
        return datetime.fromisoformat(str(s)[:19])
    except (TypeError, ValueError):
        return None


def blocked_by_plans(reach, plans, slots):
    """For each slot, the station indices that fall within PLAN_BUFFER of a plan flying then."""
    over_reach = []
    for props, poly in plans:
        if poly.distance(reach.water_xy) > PLAN_BUFFER:
            continue
        a, b = _dt(props.get("飛行予定日時_開始")), _dt(props.get("飛行予定日時_終了"))
        if not a or not b:
            continue
        hit = {j for j, s in enumerate(reach.stations) if poly.distance(Point(s.x, s.y)) < PLAN_BUFFER + s.halfwidth}
        over_reach.append((props, poly, a, b, hit))
    blocked, per_slot = [], []
    for sa, sb in slots:
        bset = set()
        for props, poly, a, b, hit in over_reach:
            if a < sb and sa < b and hit:
                bset |= hit
        blocked.append(bset)
        per_slot.append(sum(1 for _, _, a, b, hit in over_reach if a < sb and sa < b and hit))
    return blocked, per_slot, over_reach


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--osm", default=str(ROOT / "data" / "osm_arakawa.json"))
    ap.add_argument("--plans", default=str(ROOT / "data" / "adachi_flight_plans_202407-202506.geojson"))
    ap.add_argument("--out", default=str(ROOT / "docs"))
    ap.add_argument("--pick", nargs=2, metavar=("BRIDGE_A", "BRIDGE_B"), help="names of the two end bridges")
    args = ap.parse_args(argv)

    osm = json.loads(Path(args.osm).read_text(encoding="utf-8"))
    south, west, north, east = BBOX
    water = water_from_overpass(osm).intersection(box(west, south, east, north))
    bridges = bridges_from_overpass(osm)
    c = water.centroid
    proj = LocalProj(lat0=c.y, lon0=c.x)
    reach = build_reach(water, bridges, proj, pick=tuple(args.pick) if args.pick else None)

    plans = flight_plan_shapes(json.loads(Path(args.plans).read_text(encoding="utf-8")), proj)
    slots = weekend_slots(datetime(2025, 1, 1), datetime(2025, 6, 30))
    blocked, per_slot, over_reach = blocked_by_plans(reach, plans, slots)

    scenarios = []
    for bank in BANKS:
        p = Params(bank=bank)
        r = solve(reach, p, blocked)
        sc = {"bank": bank, "status": r.status, "core": [LABELS.get(c, c) for c in r.core], "gates": r.gates,
              "path": r.path, "back": r.back}
        if r.slot is not None:
            sa, sb = slots[r.slot]
            active = [poly for _, poly, a, b, _ in over_reach if a < sb and sa < b]
            bad = verify_path(reach, r.gates, p, plans=active)
            if bad:
                sys.exit(f"bank {bank} m: the solved path fails the geometric check: {bad}")
            sc["verified"] = True
            sc["slot"] = {"start": sa.isoformat(timespec="minutes"), "end": sb.isoformat(timespec="minutes"),
                          "skipped": sum(1 for n in per_slot[: r.slot] if n)}
        scenarios.append(sc)
        print(f"bank {bank:>3} m: {r.status}" + (f"  core={r.core}" if r.core else f"  slot={sc['slot']['start']}"))

    hw = [s.halfwidth for s in reach.stations]
    data = {
        "reach": {
            "ends": list(reach.end_names),
            "length_m": round(reach.stations[-1].s),
            "halfwidth_min": round(min(h for h in hw if h > 0)),
            "halfwidth_max": round(max(hw)),
            "centerline": [list(proj.to_ll(s.x, s.y))[::-1] for s in reach.stations],
            "water": mapping(proj.geom_ll(reach.water_xy)),
            "bridges": [{"name": n, "line": [[lat, lon] for lon, lat in proj.geom_ll(l).coords]} for n, l in reach.bridges],
        },
        "plans": [{"start": a.isoformat(timespec="minutes"), "end": b.isoformat(timespec="minutes"),
                   "geom": mapping(proj.geom_ll(poly))} for _, poly, a, b, hit in over_reach],
        "slots": {"count": len(slots), "busy": sum(1 for n in per_slot if n)},
        "scenarios": scenarios,
        "labels": LABELS,
        "built": datetime.now().isoformat(timespec="minutes"),
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    html = (ROOT / "src" / "template.html").read_text(encoding="utf-8")
    html = html.replace("/*LEAFLET_CSS*/", (ROOT / "vendor" / "leaflet.css").read_text(encoding="utf-8"))
    html = html.replace("/*LEAFLET_JS*/", (ROOT / "vendor" / "leaflet.js").read_text(encoding="utf-8"))
    html = html.replace("/*DATA*/null", json.dumps(data, ensure_ascii=False))
    (out / "index.html").write_text(html, encoding="utf-8")
    print(f"wrote {out / 'index.html'}")


if __name__ == "__main__":
    main()
