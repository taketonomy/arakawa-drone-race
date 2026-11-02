"""Tests for the flight-path conditions P1-P3 (synthetic rivers, not real geography)."""
import copy
import math
import sys
from pathlib import Path

import pytest
from shapely.geometry import LineString

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from geo import LocalProj, bridges_from_overpass, build_reach, water_from_overpass  # noqa: E402
from solver import Params, bridge_stations, course_path, solve, verify_path  # noqa: E402
from test_course import LENGTH, _ll, synthetic_osm  # noqa: E402

NECK = (1100.0, 1200.0)   # a narrow stretch, along-river metres
NECK_HALF = 40.0          # half width there: 40 - bank 30 = 10 m, less than the 15 m swing


def necked_osm():
    """Straight river, 200 m wide, narrowing to 80 m for 100 m in the middle of the B-C reach."""
    p = LocalProj(35.76, 139.80)
    h, a, b, n = 100.0, NECK[0], NECK[1], NECK_HALF
    ring = [(-200, -h), (a, -h), (a, -n), (b, -n), (b, -h), (LENGTH + 200, -h),
            (LENGTH + 200, h), (b, h), (b, n), (a, n), (a, h), (-200, h), (-200, -h)]
    water = {"type": "way", "tags": {"natural": "water", "name": "荒川"}, "geometry": [_ll(p, *c) for c in ring]}
    bridges = [e for e in synthetic_osm()["elements"] if e["type"] == "way"]
    return {"elements": [water] + bridges}


def _reach(osm):
    water = water_from_overpass(osm)
    c = water.centroid
    return build_reach(water, bridges_from_overpass(osm), LocalProj(c.y, c.x))


@pytest.fixture(scope="module")
def straight():
    return _reach(synthetic_osm())


@pytest.fixture(scope="module")
def necked():
    return _reach(necked_osm())


def _k_at(reach, s):
    return min(range(len(reach.stations)), key=lambda j: abs(reach.stations[j].s - s))


def test_path_visits_every_station_and_passes_the_geometric_check(straight):
    p = Params()
    r = solve(straight, p)
    assert r.status == "sat"
    fwd, back = course_path(straight, r.gates)
    ks = [k for *_, k, _ in fwd]
    assert ks == list(range(r.gates[0]["k"], r.gates[-1]["k"] + 1))
    # P1 as stated: every path point is within the bank limit at its own station.
    for *_, k, d in fwd:
        assert abs(d) <= straight.stations[k].halfwidth - p.bank + 1e-6
    assert len(back) == len(fwd)
    assert len(r.path) == len(fwd) and len(r.back) == len(back)
    assert verify_path(straight, r.gates, p) == []


def test_safe_halfwidth_never_exceeds_distance_to_bank(necked):
    # Compare with the whole water area (the reach's clipped area has artificial edges at its ends).
    edge = necked.proj.geom_xy(water_from_overpass(necked_osm())).boundary
    for s in necked.stations:
        assert s.halfwidth <= edge.distance(_pt(s)) + 1e-6
    near_neck = [s for s in necked.stations if abs(s.s - (NECK[0] - 300)) < 5]
    assert near_neck and near_neck[0].halfwidth < 100 - 1   # the neck's corner already limits the room


def _pt(s):
    from shapely.geometry import Point
    return Point(s.x, s.y)


def test_gates_only_model_crosses_the_neck_but_path_model_proves_no_course(necked):
    # The longer side of the neck is about 1150 m. Ask for a 1300 m course: it has to cross the neck.
    gates_only = solve(necked, Params(span_min=1300, path=False))
    assert gates_only.status == "sat"            # every gate is fine...
    assert verify_path(necked, gates_only.gates, Params(span_min=1300))  # ...but the path is not
    r = solve(necked, Params(span_min=1300))
    assert r.status == "unsat"
    assert {"P1", "C6", "C8"} <= set(r.core)


def test_course_stays_on_one_side_of_the_neck_when_it_fits(necked):
    p = Params()
    r = solve(necked, p)
    assert r.status == "sat"
    k0, kn = r.gates[0]["k"], r.gates[-1]["k"]
    neck = [j for j, s in enumerate(necked.stations) if s.halfwidth - p.bank < p.swing]
    inner = [j for j in neck if 0 < necked.stations[j].s < necked.stations[-1].s]
    assert inner and not any(k0 <= j <= kn for j in inner if necked.stations[j].halfwidth > 0)
    assert verify_path(necked, r.gates, p) == []


def test_path_never_runs_under_a_bridge(straight):
    reach = copy.copy(straight)
    mid = straight.stations[len(straight.stations) // 2]
    nx, ny = straight.nx, straight.ny
    footbridge = LineString([(mid.x - nx * 200, mid.y - ny * 200), (mid.x + nx * 200, mid.y + ny * 200)])
    reach.bridges = straight.bridges + [("中央歩道橋", footbridge)]
    ban = set(bridge_stations(reach, Params().bridge))

    r = solve(reach, Params(span_min=900))
    assert r.status == "sat"
    k0, kn = r.gates[0]["k"], r.gates[-1]["k"]
    assert not any(k0 <= j <= kn for j in ban)
    assert verify_path(reach, r.gates, Params(span_min=900)) == []

    # A course longer than either side cannot exist, and Z3 says why.
    r = solve(reach, Params(span_min=1200))
    assert r.status == "unsat"
    assert {"P2", "C8"} <= set(r.core)


def test_flight_plan_between_gates_moves_the_date(straight):
    # Slot 0: a plan over a 30 m strip in the middle. Gates alone could hop over it.
    mid = len(straight.stations) // 2
    strip = {mid - 1, mid, mid + 1}
    p = Params(span_min=1100)
    assert solve(straight, Params(span_min=1100, path=False), blocked=[strip, set()]).slot == 0
    r = solve(straight, p, blocked=[strip, set()])
    assert r.status == "sat" and r.slot == 1


def test_verifier_flags_a_path_that_leaves_the_safe_band(straight):
    p = Params()
    r = solve(straight, p)
    bad = copy.deepcopy(r.gates)
    bad[1]["d"] = math.floor(straight.stations[bad[1]["k"]].halfwidth)   # right at the water edge
    assert any(v.startswith("P1") for v in verify_path(straight, bad, p))
