"""Z3 model of the race course.

Every condition is added with a name (assert_and_track), so when no course exists
Z3 can report which conditions clash (the unsat core).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import z3

from geo import Reach

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


@dataclass
class Result:
    status: str                       # "sat" | "unsat"
    core: list[str] = field(default_factory=list)
    gates: list[dict] = field(default_factory=list)
    slot: int | None = None


def solve(reach: Reach, params: Params, blocked: list[set[int]] | None = None) -> Result:
    """blocked[j] = station indices that clash with an existing flight plan in time slot j."""
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
    c1, c2 = [], []
    for i in range(N):
        for j, s in enumerate(st):
            hw = math.floor(s.halfwidth)  # round inward so whole-metre gates stay over the water
            c1.append(z3.Implies(k[i] == j, z3.And(d[i] <= hw, d[i] >= -hw)))
            lim = math.floor(s.halfwidth - params.bank)
            c2.append(z3.Implies(k[i] == j, z3.And(d[i] <= lim, d[i] >= -lim)))
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

    # 1) Is there any course at all? If not, ask for a minimal set of clashing conditions.
    chk = z3.Solver()
    chk.set("core.minimize", True)
    chk.add(base)
    for name, cond in tracked:
        chk.assert_and_track(cond, z3.Bool(name))
    if chk.check() != z3.sat:
        return Result(status="unsat", core=sorted(str(c) for c in chk.unsat_core()))

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
    return Result(status="sat", gates=gates, slot=m.eval(slot).as_long())


def _pt(x, y):
    from shapely.geometry import Point
    return Point(x, y)

