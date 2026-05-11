#!/usr/bin/env python3
"""
Diagnose why tsp_solver.py prunes the branch that leads to the known-better
route. We replay the same segment model as tsp_solver.search() along a chosen
key-visit order, and at each prefix we recompute the solver's lower_bound()
and compare it against an `--incumbent` (e.g. the net the solver actually
returned). If at any step `lower_bound >= incumbent`, that branch would have
been pruned in the real B&B even though its true completion net is lower.

Usage:
  python diagnose_prune.py data.json challenge.json \
      --order '[25,175,49,88,62,33,189,44,3,25]' --incumbent 2472.70
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
from collections import defaultdict


# ---------- small loaders copied from tsp_solver.py ----------
def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def pick(obj, *keys):
    for k in keys:
        if k in obj:
            return obj[k]
    return None


def to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def norm_planet(r):
    return {
        "id": to_int(pick(r, "id", "Id")),
        "name": str(pick(r, "name", "Name") or ""),
        "x": to_float(pick(r, "x", "X")),
        "y": to_float(pick(r, "y", "Y")),
    }


def norm_route(r):
    return {
        "from_planet": to_int(pick(r, "from_planet", "fromPlanet", "FromPlanet")),
        "to_planet_id": to_int(pick(r, "to_planet_id", "toPlanetId", "ToPlanetId")),
        "route_type": str(pick(r, "route_type", "routeType", "RouteType") or ""),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data_path", default="data.json", nargs="?")
    ap.add_argument("challenge_path", default="challenge.json", nargs="?")
    ap.add_argument(
        "--order",
        required=True,
        help="JSON list of key planets to visit (e.g. [25,175,49,88,62,33,189,44,3,25])",
    )
    ap.add_argument("--incumbent", type=float, required=True, help="best_net used as prune cutoff")
    args = ap.parse_args()

    key_order = json.loads(args.order)

    data = load_json(args.data_path)
    ch = load_json(args.challenge_path)
    planets = {norm_planet(p)["id"]: norm_planet(p) for p in (data.get("planets") or data.get("Planets") or [])}
    routes = [norm_route(r) for r in (data.get("routes") or data.get("Routes") or [])]
    START = to_int(pick(ch, "startPlanetId", "StartPlanetId"))
    MANDATORY = set(int(x) for x in (pick(ch, "mandatoryPlanetIds", "MandatoryPlanetIds") or []))
    FORBIDDEN = set(int(x) for x in (pick(ch, "forbiddenPlanetIds", "ForbiddenPlanetIds") or []))
    BONUSES = {
        to_int(b.get("planetId") or b.get("PlanetId")): to_float(b.get("value") or b.get("Value"))
        for b in (pick(ch, "bonusStops", "BonusStops") or [])
    }

    allowed = {pid for pid in planets if pid not in FORBIDDEN}
    mandatory_list = sorted(m for m in MANDATORY if m != START)
    bonus_list = sorted(b for b in BONUSES if b in allowed and b != START and b not in MANDATORY)
    key_nodes = [START] + mandatory_list + bonus_list
    mandatory_set = set(mandatory_list)
    bonus_set = set(bonus_list)

    # ---------- build graph and Dijkstra ----------
    route_mult = {}
    for r in routes:
        a, b = r["from_planet"], r["to_planet_id"]
        if a not in planets or b not in planets:
            continue
        m = 0.5 if r["route_type"] == "Main Route" else 2.0 / 3.0
        k = (min(a, b), max(a, b))
        if k not in route_mult or m < route_mult[k]:
            route_mult[k] = m

    def edge_cost(a, b):
        if a == b:
            return 0.0
        pa, pb = planets[a], planets[b]
        return math.hypot(pa["x"] - pb["x"], pa["y"] - pb["y"]) * route_mult.get((min(a, b), max(a, b)), 1.0)

    adj = defaultdict(list)
    pids = list(planets.keys())
    for a in pids:
        for b in pids:
            if a != b:
                adj[a].append((b, edge_cost(a, b)))

    def dijkstra_all(src):
        INF = float("inf")
        distv = {src: 0.0}
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > distv.get(u, INF):
                continue
            for v, w in adj[u]:
                if v in FORBIDDEN:
                    continue
                nd = d + w
                if nd < distv.get(v, INF):
                    distv[v] = nd
                    heapq.heappush(pq, (nd, v))
        return distv

    def dijkstra_avoiding(src, dst, excluded):
        INF = float("inf")
        distv = {src: 0.0}
        prev = {}
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > distv.get(u, INF):
                continue
            if u == dst:
                break
            for v, w in adj[u]:
                if v in FORBIDDEN:
                    continue
                if v in excluded and v != dst:
                    continue
                nd = d + w
                if nd < distv.get(v, INF):
                    distv[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        if distv.get(dst, INF) == INF:
            return None, None
        path, cur = [dst], dst
        while cur != src:
            cur = prev.get(cur)
            if cur is None:
                return None, None
            path.append(cur)
        path.reverse()
        return distv[dst], path

    LB = {}
    for s in key_nodes:
        d = dijkstra_all(s)
        LB[s] = {dst: d.get(dst, float("inf")) for dst in key_nodes}
    min_in = {k: min(LB[other][k] for other in key_nodes if other != k) for k in key_nodes}

    _hk_cache = {}

    def hk_lb(current, remaining):
        if not remaining:
            return LB[current][START]
        key = (current, remaining)
        if key in _hk_cache:
            return _hk_cache[key]
        best = float("inf")
        for nxt in remaining:
            c = LB[current][nxt] + hk_lb(nxt, remaining - {nxt})
            if c < best:
                best = c
        _hk_cache[key] = best
        return best

    def lower_bound(current, visited_m, visited_b, current_cost, bonus_value):
        remaining_m = mandatory_set - visited_m
        traversal_lb = hk_lb(current, frozenset(remaining_m))
        extra_bonus_savings = sum(
            max(0.0, BONUSES[b] - 2.0 * min_in.get(b, 0.0))
            for b in bonus_set
            if b not in visited_b
        )
        return current_cost + traversal_lb - bonus_value - extra_bonus_savings

    # ---------- replay the chosen key order, with bound at each step ----------
    visited_m = set()
    visited_b = set()
    used = set()
    current = key_order[0]
    current_cost = 0.0
    bonus_value = 0.0

    print(f"START={START}  incumbent(best_net cutoff)={args.incumbent:.4f}")
    print("-" * 110)
    header = f"{'step':>4} {'from':>4} -> {'to':>4}  {'segcost':>10}  {'cum':>10}  {'bonus':>8}  {'hk_lb':>10}  {'extra_sav':>10}  {'bound':>10}  {'pruned?':>8}"
    print(header)
    print("-" * 110)

    pruned_anywhere = False
    for i in range(len(key_order) - 1):
        nxt = key_order[i + 1]

        # Compute bound BEFORE moving to nxt (same as search() does on entry to current).
        b_before = lower_bound(current, frozenset(visited_m), frozenset(visited_b), current_cost, bonus_value)
        pruned_before = b_before >= args.incumbent

        remaining_m = mandatory_set - visited_m
        remaining_b = bonus_set - visited_b
        if not remaining_m and nxt == START:
            excluded = set(used)
            for kn in remaining_b:
                excluded.add(kn)
            excluded.discard(current)
            excluded.discard(START)
        else:
            excluded = set(used)
            for kn in remaining_m:
                if kn != nxt:
                    excluded.add(kn)
            for kn in remaining_b:
                if kn != nxt:
                    excluded.add(kn)
            excluded.discard(current)
            excluded.discard(nxt)

        cost, seg = dijkstra_avoiding(current, nxt, excluded)
        if seg is None:
            print(f"{i:>4} {current:>4} -> {nxt:>4}  INFEASIBLE under exclusions")
            return

        new_cost = current_cost + cost
        # extra info: traversal_lb / savings at this prefix
        traversal_lb = hk_lb(current, frozenset(mandatory_set - visited_m))
        extra_sav = sum(
            max(0.0, BONUSES[b] - 2.0 * min_in.get(b, 0.0))
            for b in bonus_set
            if b not in visited_b
        )
        marker = "PRUNED" if pruned_before else ""
        if pruned_before:
            pruned_anywhere = True
        print(
            f"{i:>4} {current:>4} -> {nxt:>4}  {cost:>10.4f}  {new_cost:>10.4f}  "
            f"{bonus_value:>8.2f}  {traversal_lb:>10.4f}  {extra_sav:>10.4f}  {b_before:>10.4f}  {marker:>8}"
        )

        for p in seg[1:-1]:
            used.add(p)
        used.add(current)
        current = nxt
        current_cost = new_cost
        if nxt in mandatory_set:
            visited_m.add(nxt)
        elif nxt in bonus_set:
            visited_b.add(nxt)
            bonus_value += BONUSES[nxt]

    # Final summary
    final_net = current_cost - bonus_value
    print("-" * 110)
    print(f"final gross={current_cost:.4f}  bonus={bonus_value:.2f}  net={final_net:.4f}")
    if pruned_anywhere:
        print(
            "\nAt least one prefix has bound >= incumbent. With incumbent="
            f"{args.incumbent:.4f}, the real solver would have *cut this branch off*,"
            "\neven though this order completes to net=" + f"{final_net:.4f}."
        )
        print("=> lower_bound() is INADMISSIBLE for this instance (too tight).")
    else:
        print(
            "\nNo prefix has bound >= incumbent under this incumbent."
            " So pruning would *not* drop this order — the solver should have found it."
        )


if __name__ == "__main__":
    main()
