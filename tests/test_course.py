"""Tests on a synthetic straight river (a fixture, not real geography)."""
import math
import sys
from pathlib import Path

import pytest
from shapely.geometry import Point

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from geo import LocalProj, bridges_from_overpass, build_reach, water_from_overpass  # noqa: E402
from solver import Params, solve  # noqa: E402

LAT0, LON0 = 35.76, 139.80
ANG = math.radians(25)        # river direction
LENGTH, WIDTH = 2400.0, 200.0


def _ll(proj, x, y):
    # rotate local (along, across) into east/north
    e = x * math.cos(ANG) - y * math.sin(ANG)
    n = x * math.sin(ANG) + y * math.cos(ANG)
    lon, lat = proj.to_ll(e, n)
    return {"lat": lat, "lon": lon}


def synthetic_osm():
    p = LocalProj(LAT0, LON0)
    h = WIDTH / 2
    corners = [(-200, -h), (LENGTH + 200, -h), (LENGTH + 200, h), (-200, h)]
    # The water is a relation whose outer ring is split into two member ways (as in OSM).
    w1 = [_ll(p, *c) for c in corners[:3]]
    w2 = [_ll(p, *c) for c in corners[2:] + corners[:1]]
    rel = {"type": "relation", "tags": {"natural": "water", "name": "荒川"},
           "members": [{"type": "way", "role": "outer", "geometry": w1},
                       {"type": "way", "role": "outer", "geometry": w2}]}

    def bridge(name, x):
        return {"type": "way", "tags": {"bridge": "yes", "highway": "primary", "name": name},
                "geometry": [_ll(p, x, -h - 50), _ll(p, x, h + 50)]}

    return {"elements": [rel, bridge("A橋", 0), bridge("B橋", 300), bridge("C橋", LENGTH)]}


@pytest.fixture(scope="module")
def reach():
    osm = synthetic_osm()
    water = water_from_overpass(osm)
    c = water.centroid
    return build_reach(water, bridges_from_overpass(osm), LocalProj(c.y, c.x))


def test_reach_picks_longest_bridge_gap(reach):
    assert set(reach.end_names) == {"B橋", "C橋"}
    assert reach.stations[-1].s == pytest.approx(LENGTH - 300, abs=40)
    mid = reach.stations[len(reach.stations) // 2]
    assert mid.halfwidth == pytest.approx(WIDTH / 2, abs=2)


def test_sat_course_respects_every_rule(reach):
    p = Params()
    r = solve(reach, p)
    assert r.status == "sat"
    assert len(r.gates) == p.gates
    for a, b in zip(r.gates, r.gates[1:]):
        assert p.gap_min <= (b["k"] - a["k"]) * reach.step <= p.gap_max
    for g in r.gates:
        hw = reach.stations[g["k"]].halfwidth
        assert abs(g["d"]) <= hw - p.bank + 1e-6
        assert abs(g["d"]) >= p.swing - 1e-6
        assert p.alt_min <= g["alt"] <= p.alt_max
        x, y = reach.point(g["k"], g["d"])
        assert reach.water_xy.buffer(-p.bank + 1).contains(Point(x, y))
        for _, line in reach.bridges:
            st = reach.stations[g["k"]]
            assert line.distance(Point(st.x, st.y)) >= p.bridge
    signs = [g["d"] > 0 for g in r.gates]
    assert all(s != t for s, t in zip(signs, signs[1:]))


def test_unsat_when_banks_leave_no_room_to_swing(reach):
    # Half width 100 m - bank 90 m leaves 10 m, less than the 15 m swing.
    r = solve(reach, Params(bank=90))
    assert r.status == "unsat"
    assert {"C2", "C6"} <= set(r.core)


def test_existing_flight_plan_moves_the_date(reach):
    everything = set(range(len(reach.stations)))
    r = solve(reach, Params(), blocked=[everything, everything, set()])
    assert r.status == "sat" and r.slot == 2


def test_unsat_when_every_slot_is_taken(reach):
    everything = set(range(len(reach.stations)))
    r = solve(reach, Params(), blocked=[everything, everything])
    assert r.status == "unsat"
    assert "C7" in r.core
