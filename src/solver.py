"""Z3 model of the race course.

Every condition is added with a name (assert_and_track), so when no course exists
Z3 can report which conditions clash (the unsat core).

C1-C8 constrain the gates. P1-P3 constrain the flight path between them: the drone flies
from gate to gate along a straight line in river coordinates, d(k) linear in the station
index k, passing every station (every `reach.step` metres) and then returns along the
center line (d = 0). Z3 proves P1-P3 at every station; `verify_path` then re-checks the
actual polyline with exact geometry (shapely), independently of the Z3 model.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import z3

from geo import Reach

PLAN_BUFFER = 50.0   # a course must stay this far from an existing plan's area (m)

# Human-readable names for the tracked conditions (used in the unsat core).
LABELS = {
    "C1": "C1 コースは川の上（水面）からはみ出さない",
    "C2": "C2 岸（観客エリア）から一定の距離をとる",
    "C3": "C3 橋に近づきすぎない",
    "C4": "C4 高度は 150 m 未満（レースは 5〜30 m）",
    "C5": "C5 ゲートの間隔",
    "C6": "C6 左右に振る（スラローム）",
    "C7": "C7 既存の飛行計画と時間・場所が重ならない",
    "C8": "C8 コース全体の長さ",
    "P1": "P1 ゲートの間の経路も水面の上で、岸から C2 の距離を保つ",
    "P2": "P2 経路が橋に近づかない（橋の下をくぐらない）",
    "P3": "P3 経路が既存の飛行計画と時間・場所で重ならない",
}


@dataclass
class Params:
    gates: int = 8
    bank: float = 30.0          # C2: min distance from the water edge (m)
    bridge: float = 30.0        # C3: min distance from any bridge (m)
    swing: int = 15         # C6: min offset from the center line, alternating sides (m)
    gap_min: float = 80.0       # C5
    gap_max: float = 250.0
    span_min: float = 600.0     # C8: first-to-last gate distance along the river
    span_max: float = 1800.0
    alt_min: int = 5            # C4
    alt_max: int = 30
    path: bool = True           # add P1-P3 (False = gates only, for comparison)


@dataclass
class Result:
    status: str                       # "sat" | "unsat"
    core: list[str] = field(default_factory=list)
    gates: list[dict] = field(default_factory=list)
    slot: int | None = None
    path: list[list[float]] = field(default_factory=list)   # [lat, lon] gate 1 -> gate N, every station
    back: list[list[float]] = field(default_factory=list)   # [lat, lon] return leg on the center line


def solve(reach: Reach, params: Params, blocked: list[set[int]] | None = None, optimize: bool = True) -> Result:
    """blocked[j] = station indices that clash with an existing flight plan in time slot j.

    optimize=False only decides sat/unsat (no course is returned for sat): much faster.
    """
    st = reach.stations
    M = len(st)
    N = params.gates
    base, tracked = [], []
    k = [z3.Int(f"k{i}") for i in range(N)]
    d = [z3.Int(f"d{i}") for i in range(N)]   # whole metres
    h = [z3.Int(f"h{i}") for i in range(N)]
    slot = z3.Int("slot")

    def track(name: str, cond):
        tracked.append((name, cond))

    # Station index range (structural, not a named rule).
    for i in range(N):
        base += [k[i] >= 0, k[i] < M]

    # C1 / C2: over water, and away from the banks.
    lims = [math.floor(s.halfwidth - params.bank) for s in st]   # round inward (whole metres)
    c1, c2 = [], []
    for i in range(N):
        for j, s in enumerate(st):
            hw = math.floor(s.halfwidth)  # round inward so whole-metre gates stay over the water
            c1.append(z3.Implies(k[i] == j, z3.And(d[i] <= hw, d[i] >= -hw)))
            c2.append(z3.Implies(k[i] == j, z3.And(d[i] <= lims[j], d[i] >= -lims[j])))
    track("C1", z3.And(c1))
    track("C2", z3.And(c2))

    # C3: keep away from bridges (distance from the station's center point).
    near = [j for j, s in enumerate(st)
            if any(line.distance(_pt(s.x, s.y)) < params.bridge for _, line in reach.bridges)]
    track("C3", z3.And([k[i] != j for i in range(N) for j in near]) if near else z3.BoolVal(True))

    # C4: altitude.
    track("C4", z3.And([z3.And(h[i] >= params.alt_min, h[i] <= params.alt_max, h[i] < 150) for i in range(N)]))

    # C5: spacing between consecutive gates, in order along the river.
    step = reach.step
    track("C5", z3.And([z3.And((k[i + 1] - k[i]) * step >= params.gap_min,
                                (k[i + 1] - k[i]) * step <= params.gap_max) for i in range(N - 1)]))

    # C6: slalom, alternate sides.
    track("C6", z3.And([d[i] >= params.swing if i % 2 == 0 else d[i] <= -params.swing for i in range(N)]))

    # C7: pick a time slot in which no gate sits in an existing flight plan's area.
    if blocked:
        rules = [z3.And(slot >= 0, slot < len(blocked))]
        for j, bset in enumerate(blocked):
            if bset:
                rules.append(z3.Implies(slot == j, z3.And([k[i] != b for i in range(N) for b in sorted(bset)])))
        track("C7", z3.And(rules))
    else:
        base.append(slot == 0)

    # C8: total span.
    span = (k[N - 1] - k[0]) * step
    track("C8", z3.And(span >= params.span_min, span <= params.span_max))

    if params.path:
        # The path covers every station from k[0] to k[N-1] (gate legs and the return leg).
        def on_path(j):
            return z3.And(k[0] <= j, j <= k[N - 1])

        # P1: on the leg from gate i to gate i+1 the path is d(j) = (d[i] (k[i+1] - j) + d[i+1] (j - k[i])) / n
        # with n = k[i+1] - k[i] > 0 (C5). Multiplying by n gives an exact condition at every
        # station j on the leg: |num| <= lims[j] * n. It is nonlinear (gate offsets times gate
        # positions); Z3 decides it over these bounded integers. The return leg (d = 0) and the
        # sideways moves at the two ends need 0 <= lims[j] and |d| <= lims[k], which this implies
        # at the gates themselves (j = k[i], k[i+1]).
        p1 = []
        for i in range(N - 1):
            n = k[i + 1] - k[i]
            for j in range(M):
                num = d[i] * (k[i + 1] - j) + d[i + 1] * (j - k[i])   # = d(j) * n
                fit = z3.And(num <= lims[j] * n, num >= -lims[j] * n)
                p1.append(z3.Implies(z3.And(k[i] <= j, j <= k[i + 1]), fit))
        track("P1", z3.And(p1))

        # P2: no station whose whole cross-section (|d| <= halfwidth) comes within
        # params.bridge of a bridge may lie on the path.
        ban = bridge_stations(reach, params.bridge)
        track("P2", z3.And([z3.Not(on_path(j)) for j in ban]) if ban else z3.BoolVal(True))

        # P3: in the chosen slot, no blocked station on the path. A station is blocked when
        # any point of its cross-section is near a plan, so this covers every path point.
        if blocked:
            p3 = [z3.Implies(slot == jj, z3.And([z3.Not(on_path(b)) for b in sorted(bset)]))
                  for jj, bset in enumerate(blocked) if bset]
            track("P3", z3.And(p3) if p3 else z3.BoolVal(True))

    # 1) Is there any course at all? If not, ask for a minimal set of clashing conditions.
    chk = z3.Solver()
    chk.set("core.minimize", True)
    chk.add(base)
    for name, cond in tracked:
        chk.assert_and_track(cond, z3.Bool(name))
    if chk.check() != z3.sat:
        return Result(status="unsat", core=sorted(str(c) for c in chk.unsat_core()))
    if not optimize:
        return Result(status="sat")

    # 2) Pick the best one: earliest date, then the longest course, then the widest swings.
    opt = z3.Optimize()
    opt.add(base + [cond for _, cond in tracked])
    opt.minimize(slot)
    opt.maximize(span)
    opt.maximize(z3.Sum([z3.If(d[i] >= 0, d[i], -d[i]) for i in range(N)]))
    assert opt.check() == z3.sat

    m = opt.model()
    gates = []
    for i in range(N):
        ki = m.eval(k[i]).as_long()
        di = m.eval(d[i]).as_long()
        lon, lat = reach.point_ll(ki, di)
        gates.append({"i": i + 1, "k": ki, "s": round(st[ki].s, 1), "d": di,
                      "alt": m.eval(h[i]).as_long(), "lat": lat, "lon": lon})
    fwd, back = course_path(reach, gates)

    def ll(pts):
        return [[round(lat, 7), round(lon, 7)] for lon, lat in (reach.proj.to_ll(x, y) for x, y, *_ in pts)]

    return Result(status="sat", gates=gates, slot=m.eval(slot).as_long(), path=ll(fwd), back=ll(back))


def bridge_stations(reach: Reach, min_dist: float) -> list[int]:
    """Stations whose cross-section (|d| <= halfwidth) comes within min_dist of a bridge."""
    from shapely.geometry import LineString
    out = []
    for j, s in enumerate(reach.stations):
        cut = LineString([reach.point(j, -s.halfwidth), reach.point(j, s.halfwidth)]) if s.halfwidth > 0 \
            else _pt(s.x, s.y)
        if any(line.distance(cut) < min_dist for _, line in reach.bridges):
            out.append(j)
    return out


def course_path(reach: Reach, gates: list[dict]):
    """The flight path in local metres.

    Forward: (x, y, alt, k, d) at every station from gate 1 to gate N, d and alt linear in k
    between gates. Back: (x, y) on the center line from gate N's station down to gate 1's.
    The loop closes with sideways moves within a single cross-section at each end.
    """
    fwd = []
    for a, b in zip(gates, gates[1:]):
        n = b["k"] - a["k"]
        for j in range(a["k"], b["k"] + (b is gates[-1])):
            f = (j - a["k"]) / n
            dj = a["d"] + (b["d"] - a["d"]) * f
            x, y = reach.point(j, dj)
            fwd.append((x, y, a["alt"] + (b["alt"] - a["alt"]) * f, j, dj))
    back = [reach.point(j, 0.0) for j in range(gates[-1]["k"], gates[0]["k"] - 1, -1)]
    return fwd, back


def verify_path(reach: Reach, gates: list[dict], params: Params, plans=(), tol: float = 1.0) -> list[str]:
    """Independent check of the whole loop with exact geometry. Returns violations (empty = OK).

    plans: shapely polygons (local metres) of the existing flight plans active in the chosen slot.
    `tol` absorbs rounding (whole-metre offsets) and the 10 m station spacing on bends.
    """
    from shapely.geometry import LineString
    fwd, back = course_path(reach, gates)
    loop = [(x, y) for x, y, *_ in fwd] + list(back) + [fwd[0][:2]]
    line = LineString(loop)
    bad = []
    # Distance to the banks: use the water area extended past the end bridges, because the
    # reach's own clipped area has artificial edges at its ends that are not banks.
    water = reach.banks_xy if reach.banks_xy is not None else reach.water_xy
    if not water.buffer(-(params.bank - tol)).contains(line):
        inner = water.buffer(-params.bank)
        worst = max(inner.distance(_pt(x, y)) for x, y in loop)
        bad.append(f"P1: path comes {worst:.1f} m closer to the bank than {params.bank} m")
    for name, br in reach.bridges:
        dist = br.distance(line)
        if dist < params.bridge - tol:
            bad.append(f"P2: path passes {dist:.1f} m from {name}")
    for x, y, alt, *_ in fwd:
        if not (params.alt_min <= alt <= params.alt_max and alt < 150):
            bad.append(f"C4: altitude {alt} on the path")
            break
    for poly in plans:
        dist = poly.distance(line)
        if dist < PLAN_BUFFER - tol:
            bad.append(f"P3: path passes {dist:.1f} m from an existing flight plan")
    return bad


def _pt(x, y):
    from shapely.geometry import Point
    return Point(x, y)

