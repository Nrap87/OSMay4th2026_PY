#!/usr/bin/env python3
"""
Compare two full planet sequences under the same model as tsp_solver.py:

  * Sum of edge_cost along the given hop sequence ("walked gross").
  * Extract the subsequence of visits to key planets (START + mandatory + bonuses).
  * Replay segment-by-segment costs using dijkstra_avoiding with the same exclusion
    rules as the B&B search (so you can see why key order changes intermediates).

Usage:
  python compare_two_routes.py data.json challenge.json \\
    --route1 '[25,175,...]' --route2 '[25,16,...]'

Defaults use the two example routes from the discussion if --route1/--route2 omitted.
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
from collections import defaultdict


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def pick(obj, *keys):
    for key in keys:
        if key in obj:
            return obj[key]
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


def normalize_planet(raw):
    return {
        "id": to_int(pick(raw, "id", "Id")),
        "name": str(pick(raw, "name", "Name") or ""),
        "x": to_float(pick(raw, "x", "X", "coordinateX", "CoordinateX", "Coordinate_X")),
        "y": to_float(pick(raw, "y", "Y", "coordinateY", "CoordinateY", "Coordinate_Y")),
    }


def normalize_route(raw):
    return {
        "from_planet": to_int(pick(raw, "from_planet", "fromPlanet", "FromPlanet", "From_Planet")),
        "to_planet_id": to_int(pick(raw, "to_planet_id", "toPlanetId", "ToPlanetId", "To_PlanetId")),
        "route_type": str(pick(raw, "route_type", "routeType", "RouteType") or ""),
    }


def normalize_challenge(raw):
    start = to_int(pick(raw, "startPlanetId", "StartPlanetId"))
    mandatory_ids = pick(raw, "mandatoryPlanetIds", "MandatoryPlanetIds")
    forbidden_ids = pick(raw, "forbiddenPlanetIds", "ForbiddenPlanetIds")
    bonus_stops = pick(raw, "bonusStops", "BonusStops")
    if mandatory_ids is None:
        mandatory_planets = pick(raw, "mandatoryPlanets", "MandatoryPlanets") or []
        mandatory_ids = [to_int(pick(p, "planetId", "PlanetId")) for p in mandatory_planets]
    if forbidden_ids is None:
        forbidden_planets = pick(raw, "forbiddenPlanets", "ForbiddenPlanets") or []
        forbidden_ids = [to_int(pick(p, "planetId", "PlanetId")) for p in forbidden_planets]
    if bonus_stops is None:
        bonus_planets = pick(raw, "bonusPlanets", "BonusPlanets") or []
        bonus_stops = [
            {
                "planetId": to_int(pick(b, "planetId", "PlanetId")),
                "value": to_float(pick(b, "value", "Value", "bonus", "Bonus")),
            }
            for b in bonus_planets
        ]
    else:
        bonus_stops = [
            {
                "planetId": to_int(pick(b, "planetId", "PlanetId")),
                "value": to_float(pick(b, "value", "Value")),
            }
            for b in bonus_stops
        ]
    return {
        "startPlanetId": start,
        "mandatoryPlanetIds": [to_int(v) for v in (mandatory_ids or [])],
        "forbiddenPlanetIds": [to_int(v) for v in (forbidden_ids or [])],
        "bonusStops": bonus_stops,
    }


def walked_gross(route, planets, route_mult, forbidden):
    """Sum edge_cost(route[i], route[i+1]) for consecutive hops."""

    def euclid(a, b):
        pa, pb = planets[a], planets[b]
        return math.hypot(pa["x"] - pb["x"], pa["y"] - pb["y"])

    def edge_cost(a, b):
        if a == b:
            return 0.0
        return euclid(a, b) * route_mult.get((min(a, b), max(a, b)), 1.0)

    total = 0.0
    for i in range(len(route) - 1):
        a, b = route[i], route[i + 1]
        if a in forbidden or b in forbidden:
            return None, f"Hop {i}->{i+1} touches forbidden: {a}->{b}"
        total += edge_cost(a, b)
    return total, None


def build_graph(planets, routes):
    route_mult = {}
    for r in routes:
        a, b = r["from_planet"], r["to_planet_id"]
        if a not in planets or b not in planets:
            continue
        m = 0.5 if r["route_type"] == "Main Route" else 2.0 / 3.0
        k = (min(a, b), max(a, b))
        if k not in route_mult or m < route_mult[k]:
            route_mult[k] = m

    def euclid(a, b):
        pa, pb = planets[a], planets[b]
        return math.hypot(pa["x"] - pb["x"], pa["y"] - pb["y"])

    def edge_cost(a, b):
        if a == b:
            return 0.0
        return euclid(a, b) * route_mult.get((min(a, b), max(a, b)), 1.0)

    adj = defaultdict(list)
    pids = list(planets.keys())
    for a in pids:
        for b in pids:
            if a == b:
                continue
            adj[a].append((b, edge_cost(a, b)))
    return adj, edge_cost


def dijkstra_avoiding(adj, forbidden, src, dst, excluded):
    INF = float("inf")
    distv = {}
    prev = {}
    distv[src] = 0.0
    pq = [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > distv.get(u, INF):
            continue
        if u == dst:
            break
        for v, w in adj[u]:
            if v in forbidden:
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


def extract_key_visit_order(route, key_set, start):
    """Order of visits to key planets (dedupe consecutive duplicates)."""
    seq = []
    for p in route:
        if p not in key_set:
            continue
        if not seq or seq[-1] != p:
            seq.append(p)
    if seq and seq[0] != start:
        return None, f"Route does not start at START={start} (first key is {seq[0]})"
    if len(seq) < 2 or seq[-1] != start:
        return None, f"Route does not end at START={start} (last key is {seq[-1] if seq else 'empty'})"
    return seq, None


def replay_segments(
    label,
    key_seq,
    adj,
    forbidden,
    mandatory_set,
    bonus_set,
    bonuses,
    start,
    verbose=True,
):
    """
    Replay tsp_solver search() segment costs for the given key visit order
    (including final return to start).
    """
    visited_m = set()
    visited_b = set()
    used = set()
    current = key_seq[0]
    total_gross = 0.0
    bonus_value = 0.0
    rows = []

    for i in range(len(key_seq) - 1):
        nxt = key_seq[i + 1]
        remaining_m = mandatory_set - visited_m
        remaining_b = bonus_set - visited_b

        if not remaining_m and nxt == start:
            excluded = set(used)
            for kn in remaining_b:
                excluded.add(kn)
            excluded.discard(current)
            excluded.discard(start)
            kind = "close_to_start"
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
            kind = "to_next_key"

        cost, seg = dijkstra_avoiding(adj, forbidden, current, nxt, excluded)
        if seg is None:
            return None, f"{label}: infeasible segment {current} -> {nxt} with kind={kind}"

        ex_sorted = sorted(excluded)
        rows.append(
            {
                "from": current,
                "to": nxt,
                "kind": kind,
                "cost": cost,
                "len": len(seg),
                "path": seg,
                "excluded_count": len(excluded),
                "excluded_sample": ex_sorted[:24],
                "excluded_truncated": len(ex_sorted) > 24,
            }
        )
        total_gross += cost
        for p in seg[1:-1]:
            used.add(p)
        used.add(current)
        current = nxt
        if nxt in mandatory_set:
            visited_m.add(nxt)
        elif nxt in bonus_set:
            visited_b.add(nxt)
            bonus_value += bonuses[nxt]

    missing_m = mandatory_set - visited_m
    if missing_m:
        return None, f"{label}: after replay, mandatory not all visited: {sorted(missing_m)}"

    net = total_gross - bonus_value
    if verbose:
        print(f"\n{'=' * 70}\n{label}  --  replay solver segment model (key order from this route)\n{'=' * 70}")
        for r in rows:
            print(
                f"  {r['from']:4d} -> {r['to']:4d}  {r['kind']:16s}  cost={r['cost']:10.2f}  "
                f"hops={r['len']:3d}  excluded={r['excluded_count']}  "
                f"sample={r['excluded_sample']}{'…' if r['excluded_truncated'] else ''}"
            )
            print(f"         path: {r['path']}")
        print(f"  ---\n  replay gross: {total_gross:.2f}  bonus: {bonus_value:.2f}  net: {net:.2f}")

    return {"rows": rows, "gross": total_gross, "bonus": bonus_value, "net": net}, None


DEFAULT_ROUTE1 = [25, 16, 183, 10, 3, 178, 31, 30, 29, 44, 189, 33, 54, 53, 62, 76, 56, 88, 144, 99, 49, 175, 25]
DEFAULT_ROUTE2 = [25, 175, 49, 99, 144, 88, 56, 76, 62, 53, 54, 33, 189, 44, 29, 30, 31, 10, 3, 25]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data_path", nargs="?", default="data.json")
    ap.add_argument("challenge_path", nargs="?", default="challenge.json")
    ap.add_argument("--route1", default="", help="JSON array of planet ids (route A)")
    ap.add_argument("--route2", default="", help="JSON array of planet ids (route B)")
    ap.add_argument("--names", action="store_true", help="Append planet names in one-line summaries")
    args = ap.parse_args()

    route1 = json.loads(args.route1) if args.route1.strip() else DEFAULT_ROUTE1
    route2 = json.loads(args.route2) if args.route2.strip() else DEFAULT_ROUTE2

    data_raw = load_json(args.data_path)
    planets = {normalize_planet(p)["id"]: normalize_planet(p) for p in (pick(data_raw, "Planets", "planets") or [])}
    routes = [normalize_route(r) for r in (pick(data_raw, "Routes", "routes") or [])]
    ch = normalize_challenge(load_json(args.challenge_path))

    start = ch["startPlanetId"]
    mandatory = set(ch["mandatoryPlanetIds"])
    forbidden = set(ch["forbiddenPlanetIds"])
    bonuses = {b["planetId"]: b["value"] for b in ch.get("bonusStops", [])}
    allowed = {pid for pid in planets if pid not in forbidden}
    mandatory_list = sorted(m for m in mandatory if m != start)
    bonus_list = sorted(b for b in bonuses if b in allowed and b != start and b not in mandatory)
    key_nodes = [start] + mandatory_list + bonus_list
    key_set = set(key_nodes)
    mandatory_set = set(mandatory_list)
    bonus_set = set(bonus_list)

    route_mult = {}
    for r in routes:
        a, b = r["from_planet"], r["to_planet_id"]
        if a not in planets or b not in planets:
            continue
        m = 0.5 if r["route_type"] == "Main Route" else 2.0 / 3.0
        k = (min(a, b), max(a, b))
        if k not in route_mult or m < route_mult[k]:
            route_mult[k] = m

    def pname(pid):
        return planets.get(pid, {}).get("name", "?")

    adj, _edge_cost_fn = build_graph(planets, routes)

    print("Challenge summary")
    print(f"  START={start} ({pname(start)})")
    print(f"  mandatory={sorted(mandatory)}")
    print(f"  forbidden={sorted(forbidden)}")
    print(f"  bonuses={ {b: bonuses[b] for b in sorted(bonuses)} }")
    print(f"  key_nodes ({len(key_nodes)}): {key_nodes}")

    for lab, route in [("Route A", route1), ("Route B", route2)]:
        wg, err = walked_gross(route, planets, route_mult, forbidden)
        if err:
            print(f"\n{lab} walked gross: ERROR {err}")
            continue
        taken = sorted({p for p in route if p in bonus_set})
        bonus_sum = sum(bonuses[b] for b in taken)
        print(f"\n{lab}: {len(route)} hops, walked gross={wg:.2f}, bonuses taken={taken} value={bonus_sum:.2f}, net={wg - bonus_sum:.2f}")
        if args.names:
            print("  ", " -> ".join(f"{p}:{pname(p)}" for p in route))

        ks, kerr = extract_key_visit_order(route, key_set, start)
        if kerr:
            print(f"  key extraction: {kerr}")
            continue
        print(f"  key visit order ({len(ks)} keys): {ks}")

        rep, rerr = replay_segments(
            lab, ks, adj, forbidden, mandatory_set, bonus_set, bonuses, start, verbose=True
        )
        if rerr:
            print(f"  replay: {rerr}")

    print("\nDone.")
    print("  * Lower replay 'net' = better for the solver's segment model (shortest legal hop between each")
    print("    consecutive key in that route's key order).")
    print("  * 'Walked gross' sums your exact hop list; it can exceed replay gross if a leg is not the")
    print("    shortest admissible path for that key order (detours / non-shortest stitching).")


if __name__ == "__main__":
    main()
