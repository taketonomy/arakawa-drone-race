"""Picking the river's water area out of OSM pieces (synthetic shapes, not real geography)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from geo import LocalProj, water_from_overpass  # noqa: E402

P = LocalProj(35.76, 139.80)


def way(ring, **tags):
    return {"type": "way", "tags": {"natural": "water", "water": "river", **tags},
            "geometry": [{"lon": P.to_ll(x, y)[0], "lat": P.to_ll(x, y)[1]} for x, y in ring]}


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def area_m2(g):
    return P.geom_xy(g).area


def test_unnamed_pieces_of_the_river_are_kept_and_others_dropped():
    osm = {"elements": [
        way(rect(0, -100, 1000, 100), name="荒川"),          # the one named piece
        way(rect(1000, -100, 3000, 100)),                     # unnamed continuation: keep
        way(rect(3000, -100, 4000, 100), name="隅田川"),      # another river: drop
        way(rect(1500, 100, 1525, 600)),                      # 25 m side channel off the bank: drop
        way(rect(0, 2000, 500, 2300)),                        # unconnected pond: drop
    ]}
    w = water_from_overpass(osm)
    assert abs(area_m2(w) - 3000 * 200) < 0.02 * 3000 * 200
