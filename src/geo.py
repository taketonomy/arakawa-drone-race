"""Geometry helpers: local metric projection, OSM parsing, river reach extraction.

The river is described in "river coordinates":
  s = distance along the reach (m), d = signed offset from the center line (m).
That turns "the gate is over the water" into a linear inequality |d| <= halfwidth(s).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from shapely.geometry import LineString, MultiPolygon, Point, Polygon, shape
from shapely.ops import linemerge, polygonize, unary_union
import shapely

R_EARTH = 6_371_008.8


@dataclass
class LocalProj:
    """Equirectangular projection around a reference point. Good to ~0.1% over a few km."""

    lat0: float
    lon0: float

    def to_xy(self, lon: float, lat: float) -> tuple[float, float]:
        x = math.radians(lon - self.lon0) * R_EARTH * math.cos(math.radians(self.lat0))
        y = math.radians(lat - self.lat0) * R_EARTH
        return x, y

    def to_ll(self, x: float, y: float) -> tuple[float, float]:
        lat = self.lat0 + math.degrees(y / R_EARTH)
        lon = self.lon0 + math.degrees(x / (R_EARTH * math.cos(math.radians(self.lat0))))
        return lon, lat

    def geom_xy(self, g):
        return shapely.transform(g, lambda c: _arr(self.to_xy, c))

    def geom_ll(self, g):
        return shapely.transform(g, lambda c: _arr(self.to_ll, c))


def _arr(fn, coords):
    import numpy as np
    return np.array([fn(x, y) for x, y in coords], dtype=float).reshape(-1, 2)


# ---------------------------------------------------------------- OSM parsing

def _way_line(el) -> LineString | None:
    g = el.get("geometry")
    if not g or len(g) < 2:
        return None
    return LineString([(p["lon"], p["lat"]) for p in g])


OTHER_RIVERS = {"隅田川", "新河岸川", "中川", "綾瀬川", "旧中川", "石神井川", "芝川", "新芝川"}
JOIN = 3e-5   # ~3 m in degrees: closes the hairline gaps between adjacent OSM river pieces
MAIN_CHANNEL = 30.0   # m: channels narrower than twice this are side channels, not the river


def water_from_overpass(osm: dict, river: str = "荒川"):
    """Build the water area (lon/lat) from an Overpass `out geom` response.

    Handles closed ways and multipolygon relations (outer/inner member ways are stitched).
    Most of the Arakawa's surface in OSM is `water=river` with no name; only one piece is named.
    Unnamed pieces are kept only where they connect to a piece named `river`; pieces named after
    another river are dropped, and so is anything not connected (ponds, canals, other rivers).
    """
    polys, named = [], []
    for el in osm.get("elements", []):
        tags = el.get("tags", {})
        is_water = tags.get("natural") == "water" or tags.get("waterway") == "riverbank"
        if not is_water or tags.get("name") in OTHER_RIVERS:
            continue
        n_before = len(polys)
        if el["type"] == "way":
            line = _way_line(el)
            if line is not None and line.is_closed and len(line.coords) >= 4:
                polys.append(Polygon(line.coords))
        elif el["type"] == "relation":
            outers, inners = [], []
            for m in el.get("members", []):
                if m.get("type") != "way":
                    continue
                line = _way_line(m)
                if line is None:
                    continue
                (inners if m.get("role") == "inner" else outers).append(line)
            outer = unary_union(list(polygonize(linemerge(outers)))) if outers else None
            if outer is None or outer.is_empty:
                continue
            if inners:
                hole = unary_union(list(polygonize(linemerge(inners))))
                outer = outer.difference(hole)
            polys.append(outer)
        if tags.get("name") == river:
            named += polys[n_before:]
    if not polys:
        raise ValueError("no water polygons in the Overpass response")
    if not named:
        raise ValueError(f"no water element named {river}")
    merged = unary_union([p.buffer(0) for p in polys]).buffer(JOIN).buffer(-JOIN)
    seeds = unary_union(named)
    # Side channels, canals and inlets touch the river too (綾瀬川 along the levee, 芝川, golf-course
    # ponds). Peel MAIN_CHANNEL metres off every edge in a metric projection: anything narrower than
    # twice that disappears, the main channel (>150 m wide) survives. Keep what is still connected
    # to the named piece, grow it back, and cut it to the original shoreline.
    c = seeds.centroid
    proj = LocalProj(c.y, c.x)
    m_xy, s_xy = proj.geom_xy(merged), proj.geom_xy(seeds)
    core = m_xy.buffer(-MAIN_CHANNEL)
    kept = [g for g in getattr(core, "geoms", [core]) if g.intersects(s_xy)]
    if not kept:
        raise ValueError(f"{river} is narrower than {2 * MAIN_CHANNEL:.0f} m everywhere")
    river_xy = unary_union(kept).buffer(MAIN_CHANNEL + 5).intersection(m_xy)
    parts = [g for g in getattr(river_xy, "geoms", [river_xy]) if g.area > 1000]
    return proj.geom_ll(unary_union(parts)).buffer(0)


def bridges_from_overpass(osm: dict) -> list[tuple[str, LineString]]:
    out = []
    for el in osm.get("elements", []):
        tags = el.get("tags", {})
        if el["type"] != "way" or tags.get("bridge") in (None, "no"):
            continue
        if "highway" not in tags and "railway" not in tags:
            continue
        line = _way_line(el)
        if line is None:
            continue
        name = tags.get("bridge:name") or tags.get("name") or tags.get("railway") and "鉄道橋" or "橋"
        out.append((name, line))
    return out


# ---------------------------------------------------------------- reach model

@dataclass
class Station:
    s: float          # distance along the reach (m)
    x: float          # center line point (local m)
    y: float
    halfwidth: float  # half the water width at this station (m)


@dataclass
class Reach:
    proj: LocalProj
    stations: list[Station]
    step: float
    ux: float  # unit vector along the reach (A -> B)
    uy: float
    water_xy: object                      # shapely geometry, clipped to the reach
    bridges: list[tuple[str, LineString]] = field(default_factory=list)  # local xy
    end_names: tuple[str, str] = ("", "")
    banks_xy: object = None   # water clipped 500 m beyond the reach: its edges near the reach are real banks

    @property
    def nx(self) -> float:  # unit normal (left of A -> B)
        return -self.uy

    @property
    def ny(self) -> float:
        return self.ux

    def point(self, k: int, d: float) -> tuple[float, float]:
        st = self.stations[k]
        return st.x + d * self.nx, st.y + d * self.ny

    def point_ll(self, k: int, d: float) -> tuple[float, float]:
        return self.proj.to_ll(*self.point(k, d))


def _crossings(water_xy, bridges_xy):
    """Midpoint of each bridge's span over the water."""
    out = []
    for name, line in bridges_xy:
        inter = line.intersection(water_xy)
        if inter.is_empty or inter.length < 20:
            continue
        mid = inter.interpolate(0.5, normalized=True) if inter.geom_type == "LineString" else inter.centroid
        out.append((name, mid, line))
    return out


def _axis(water_xy):
    """Principal axis of the water area (unit vector)."""
    pts = []
    geoms = getattr(water_xy, "geoms", [water_xy])
    for g in geoms:
        pts.extend(g.exterior.coords)
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    syy = sum((p[1] - my) ** 2 for p in pts)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    ang = 0.5 * math.atan2(2 * sxy, sxx - syy)
    return math.cos(ang), math.sin(ang)


def build_reach(water_ll, bridges_ll, proj: LocalProj, step: float = 10.0,
                pick: tuple[str, str] | None = None, min_len: float = 600.0) -> Reach:
    """Pick a bridge-to-bridge reach and sample it into stations.

    pick: names of the two end bridges. Default: the longest gap between consecutive bridges.
    """
    water_xy = proj.geom_xy(water_ll)
    bridges_xy = [(n, proj.geom_xy(l)) for n, l in bridges_ll]
    cross = _crossings(water_xy, bridges_xy)
    if len(cross) < 2:
        raise ValueError("need at least two bridges crossing the water")
    ax, ay = _axis(water_xy)
    cross.sort(key=lambda c: c[1].x * ax + c[1].y * ay)
    # Merge crossings closer than 40 m (dual carriageways, parallel rail tracks).
    merged = []
    for c in cross:
        if merged and merged[-1][1].distance(c[1]) < 40:
            continue
        merged.append(c)
    pairs = list(zip(merged, merged[1:]))
    if pick:
        pairs = [p for p in pairs if {p[0][0], p[1][0]} == set(pick)] or pairs
    a, b = max(pairs, key=lambda p: p[0][1].distance(p[1][1]))
    A, B = a[1], b[1]
    L = A.distance(B)
    if L < min_len:
        raise ValueError(f"longest bridge-free reach is only {L:.0f} m")
    ux, uy = (B.x - A.x) / L, (B.y - A.y) / L
    nx, ny = -uy, ux

    stations: list[Station] = []
    cx = []
    t = 0.0
    while t <= L + 1e-6:
        px, py = A.x + ux * t, A.y + uy * t
        cut = LineString([(px - nx * 2000, py - ny * 2000), (px + nx * 2000, py + ny * 2000)])
        inter = cut.intersection(water_xy)
        parts = [g for g in getattr(inter, "geoms", [inter]) if g.geom_type == "LineString" and g.length > 0]
        if parts:
            P = Point(px, py)
            seg = min(parts, key=lambda g: g.distance(P))
            mid = seg.interpolate(0.5, normalized=True)
            cx.append((t, mid.x, mid.y, seg.length / 2))
        t += step
    if len(cx) < 10:
        raise ValueError("could not sample the reach")
    # Light smoothing of the center line (moving average over 5 stations).
    s_acc = 0.0
    prev = None
    for i, (t, x, y, hw) in enumerate(cx):
        win = cx[max(0, i - 2): i + 3]
        sx = sum(w[1] for w in win) / len(win)
        sy = sum(w[2] for w in win) / len(win)
        if prev is not None:
            s_acc += math.hypot(sx - prev[0], sy - prev[1])
        prev = (sx, sy)
        # Recompute halfwidth around the smoothed point so |d| <= hw stays over water.
        cut = LineString([(sx - nx * 2000, sy - ny * 2000), (sx + nx * 2000, sy + ny * 2000)])
        inter = cut.intersection(water_xy)
        parts = [g for g in getattr(inter, "geoms", [inter]) if g.geom_type == "LineString" and g.length > 0]
        P = Point(sx, sy)
        seg = min(parts, key=lambda g: g.distance(P)) if parts else None
        if seg is None or seg.distance(P) > 1e-6:
            hw_safe = 0.0
        else:
            c0, c1 = seg.coords[0], seg.coords[-1]
            hw_safe = min(math.hypot(c0[0] - sx, c0[1] - sy), math.hypot(c1[0] - sx, c1[1] - sy))
            # On a bend the cross-section is oblique, so its half length overstates the room.
            # The distance to the nearest edge is a safe radius: by the triangle inequality a
            # point at offset |d| is at least (hw - |d|) from the edge.
            hw_safe = min(hw_safe, water_xy.boundary.distance(P))
        stations.append(Station(s=s_acc, x=sx, y=sy, halfwidth=hw_safe))

    box = Polygon([
        (A.x - nx * 2000, A.y - ny * 2000), (A.x + nx * 2000, A.y + ny * 2000),
        (B.x + nx * 2000, B.y + ny * 2000), (B.x - nx * 2000, B.y - ny * 2000),
    ])
    near = [(n, l) for n, l in bridges_xy if l.intersects(box.buffer(300))]
    return Reach(proj=proj, stations=stations, step=step, ux=ux, uy=uy,
                 water_xy=water_xy.intersection(box), bridges=near, end_names=(a[0], b[0]),
                 banks_xy=water_xy.intersection(box.buffer(500)))


def flight_plan_shapes(geojson: dict, proj: LocalProj):
    """(properties, polygon in local xy) for each flight plan."""
    return [(f["properties"], proj.geom_xy(shape(f["geometry"]))) for f in geojson["features"]]


def as_multipolygon(g):
    if isinstance(g, Polygon):
        return MultiPolygon([g])
    return g
