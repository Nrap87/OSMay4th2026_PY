"""
Evaluate a fixed planet sequence with the same local scoring model as tsp_solver.py:
  - Euclidean x route multiplier; all hyperlane edges in the map.
  - Forbidden planet IDs must not appear in the route sequence (same as solver / OutSystems).
  - Optional: OutSystems CalculateCoaxium.

Output and -v logging mirror tsp_solver style (fixed route; no LB/B&B solver phase).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

DEFAULT_BASE_URL = (
    "https://wecode.outsystems.com/StarDelivery_Ngin/rest/StarDeliveryServices"
)


def pick(obj, *keys):
    for key in keys:
        if key in obj:
            return obj[key]
    return None


def pick_bool(obj, *keys):
    for key in keys:
        if key in obj and isinstance(obj[key], bool):
            return obj[key]
    return False


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

    out = {
        "startPlanetId": start,
        "mandatoryPlanetIds": [to_int(v) for v in (mandatory_ids or [])],
        "forbiddenPlanetIds": [to_int(v) for v in (forbidden_ids or [])],
        "bonusStops": bonus_stops,
    }
    cname = pick(raw, "challengeName", "ChallengeName", "name", "Name")
    if cname is not None and str(cname).strip():
        out["challengeName"] = str(cname).strip()
    cid_raw = pick(raw, "challengeId", "ChallengeId")
    if cid_raw is not None and str(cid_raw).strip() != "":
        try:
            out["challengeId"] = int(cid_raw)
        except (TypeError, ValueError):
            pass
    out["isFinished"] = pick_bool(raw, "IsFinished", "isFinished")
    return out


def build_route_mult(planets, routes_data):
    """Same as tsp_solver: all edges whose endpoints exist in planets."""
    route_mult = {}
    for r in routes_data:
        a, b = r["from_planet"], r["to_planet_id"]
        if a not in planets or b not in planets:
            continue
        m = 0.5 if r["route_type"] == "Main Route" else 2.0 / 3.0
        k = (min(a, b), max(a, b))
        if k not in route_mult or m < route_mult[k]:
            route_mult[k] = m
    return route_mult


def euclid(planets, a, b):
    pa, pb = planets[a], planets[b]
    return math.hypot(pa["x"] - pb["x"], pa["y"] - pb["y"])


def edge_cost(planets, route_mult, a, b):
    if a == b:
        return 0.0
    return euclid(planets, a, b) * route_mult.get((min(a, b), max(a, b)), 1.0)


def challenge_context(challenge, planet_ids):
    """Align with tsp_solver planet sets (no solver)."""
    if challenge is None:
        return None
    start = challenge["startPlanetId"]
    mandatory = set(challenge.get("mandatoryPlanetIds") or [])
    forbidden = set(challenge.get("forbiddenPlanetIds") or [])
    bonuses = {b["planetId"]: b["value"] for b in challenge.get("bonusStops") or []}
    allowed = {pid for pid in planet_ids if pid not in forbidden}
    bonus_list = sorted(
        b for b in bonuses if b in allowed and b != start and b not in mandatory
    )
    return {
        "START": start,
        "MANDATORY": mandatory,
        "FORBIDDEN": forbidden,
        "BONUSES": bonuses,
        "allowed": allowed,
        "mandatory_list": sorted(m for m in mandatory if m != start),
        "bonus_list": bonus_list,
    }


def local_route_metrics(planets, routes_data, route_ids, challenge=None):
    bonuses = {}
    if challenge:
        bonuses = {b["planetId"]: b["value"] for b in challenge.get("bonusStops") or []}

    route_mult = build_route_mult(planets, routes_data)

    missing = [pid for pid in route_ids if pid not in planets]
    if missing:
        raise ValueError(f"Unknown planet ids (not in data): {missing}")

    gross = 0.0
    legs = []
    for i in range(len(route_ids) - 1):
        u, v = route_ids[i], route_ids[i + 1]
        c = edge_cost(planets, route_mult, u, v)
        gross += c
        legs.append((u, v, c))

    bonus_applied = 0.0
    seen_bonus_planets = set()
    for pid in route_ids:
        if pid in bonuses and pid not in seen_bonus_planets:
            bonus_applied += bonuses[pid]
            seen_bonus_planets.add(pid)

    net_solver_style = gross - bonus_applied
    return {
        "gross_fuel": gross,
        "bonus_subtotal": bonus_applied,
        "net_fuel_solver_style": net_solver_style,
        "legs": legs,
        "bonus_taken_ids": tuple(sorted(seen_bonus_planets)),
    }


def name_fn(planets):
    return lambda pid: planets[pid]["name"]


def validate_route(route_ids, ctx, planets):
    """Same validation spirit as tsp_solver printed section."""
    if ctx is None:
        return {
            "forbidden_transit": [],
            "mandatory_missing": [],
            "revisits": {},
            "start_visits": route_ids.count(route_ids[0]) if route_ids else 0,
        }
    start = ctx["START"]
    mandatory = ctx["MANDATORY"]
    forbidden = ctx["FORBIDDEN"]
    vc = {}
    for p in route_ids:
        vc[p] = vc.get(p, 0) + 1
    revisits = {
        p: c
        for p, c in vc.items()
        if c > 1 and p != start and p not in forbidden
    }
    forbidden_transit = sorted({p for p in route_ids if p in forbidden})
    mandatory_missing = [m for m in mandatory if m not in vc]
    return {
        "forbidden_transit": forbidden_transit,
        "mandatory_missing": mandatory_missing,
        "revisits": revisits,
        "start_visits": vc.get(start, 0),
    }


def print_tsp_style_report(
    label,
    planets,
    route_ids,
    metrics,
    challenge,
    ctx,
    verbose,
    log_interval,
    check_t0,
    scoring_ms,
    route_file_challenge_id=None,
):
    """Mirror tsp_solver stdout layout for a fixed route."""
    name = name_fn(planets)
    key_set = set()
    start = None
    mandatory_list = []
    bonuses = {}
    if ctx:
        start = ctx["START"]
        mandatory_list = ctx["mandatory_list"]
        bonuses = ctx["BONUSES"]
        key_set = {start, *mandatory_list, *ctx["bonus_list"]}

    bonus_taken = set(metrics["bonus_taken_ids"])

    print("=" * 70)
    print("ROUTE CHECK - FIXED PATH (tsp_solver local model)")
    if label:
        print(f"  {label}")
    print("=" * 70)

    _cid = challenge.get("challengeId") if challenge else None
    if _cid is None and route_file_challenge_id is not None:
        _cid = route_file_challenge_id
    if _cid is not None:
        print(f"ChallengeId: {_cid}")
    elif challenge is None:
        print(
            "ChallengeId: (not set — use --challenge-file or route JSON challengeId / --challenge-id)"
        )
    else:
        print("ChallengeId: (not set in challenge JSON)")
    if challenge and challenge.get("challengeName"):
        print(f"ChallengeName: {challenge['challengeName']}")

    if challenge is None:
        print("\n(no challenge file: path tags and validation partial)")
        print(f"\nFull path ({len(route_ids)} hops, {len(set(route_ids))} unique):")
        for i, p in enumerate(route_ids):
            print(f"  {i:3d}   {p:4d}  {name(p)}")
    else:
        print(f"\nStart/End:  {name(start)} ({start})")
        print(f"Mandatory:  {[(p, name(p)) for p in mandatory_list] or 'none'}")
        print(
            f"Forbidden:  {[(p, name(p)) for p in sorted(ctx['FORBIDDEN'])] or 'none'}"
        )
        print(f"\nBonus planets:")
        for bid in sorted(bonuses):
            mark = "TAKEN" if bid in bonus_taken else "skip "
            print(f"  [{mark}] {name(bid):20s} ({bid:3d})  value={bonuses[bid]}")

        print(f"\nFull path ({len(route_ids)} hops, {len(set(route_ids))} unique):")
        for i, p in enumerate(route_ids):
            tag = ""
            if p == start and (i == 0 or i == len(route_ids) - 1):
                tag = " (START/END)"
            elif p in ctx["MANDATORY"]:
                tag = " (mandatory)"
            elif p in bonuses:
                tag = f" (bonus +{bonuses[p]})"
            print(f"  {i:3d} {'*' if p in key_set else ' '} {p:4d}  {name(p)}{tag}")

    print()
    print(
        f"Full path planet ids ({len(route_ids)} stops): "
        f"{json.dumps(route_ids, separators=(',', ':'))}"
    )
    print()
    print(f"Gross fuel:    {metrics['gross_fuel']:12.2f}")
    print(f"Bonus value:   {metrics['bonus_subtotal']:12.2f}")
    print(f"NET fuel:      {metrics['net_fuel_solver_style']:12.2f}")
    print()
    print("VALIDATION:")
    v = validate_route(route_ids, ctx, planets)
    fb = v["forbidden_transit"]
    if fb:
        print(
            f"  ERROR forbidden in path: {fb} "
            "(must not appear in submitted PlanetId sequence)"
        )
    else:
        print("  Forbidden in path:    none (required)")
    print(
        f"  Mandatory missing:    {v['mandatory_missing'] if v['mandatory_missing'] else 'none'}"
    )
    print(
        f"  Revisits (non-start): {v['revisits'] if v['revisits'] else 'none'}"
    )
    print(f"  Start visits:         {v['start_visits']} (expected 2)")
    print()
    print(f"Local scoring: {scoring_ms:.1f}ms (no solver search / LB / B&B)")
    if verbose:
        elapsed = time.monotonic() - check_t0
        print(
            f"Check elapsed: {elapsed * 1000:.1f}ms",
            file=sys.stderr,
            flush=True,
        )


def solver_log(verbose, check_t0, msg):
    if verbose:
        elapsed = time.monotonic() - check_t0
        print(f"[t={elapsed:9.2f}s] {msg}", file=sys.stderr, flush=True)


def run_scoring_with_verbose(planets, routes_list, route_ids, challenge, verbose, log_interval, check_t0):
    t0 = time.time()
    metrics = local_route_metrics(planets, routes_list, route_ids, challenge)
    scoring_ms = (time.time() - t0) * 1000

    ctx = challenge_context(challenge, set(planets.keys()))
    if verbose and ctx:
        routing_nodes = len(planets)
        allowed_wp = len(ctx["allowed"])
        key_nodes_n = 1 + len(ctx["mandatory_list"]) + len(ctx["bonus_list"])
        _v_cid = challenge.get("challengeId") if challenge else None
        _cid_note = (
            f" ChallengeId={_v_cid}" if _v_cid is not None else " ChallengeId=(n/a)"
        )
        solver_log(
            verbose,
            check_t0,
            f"Problem size: routing_nodes={routing_nodes} allowed_waypoints={allowed_wp} "
            f"key_nodes={key_nodes_n} (mandatory={len(ctx['mandatory_list'])}, "
            f"bonus={len(ctx['bonus_list'])}){_cid_note}",
        )
    elif verbose and ctx is None:
        solver_log(
            verbose,
            check_t0,
            f"Problem size: routing_nodes={len(planets)} (no challenge file)",
        )

    if verbose:
        solver_log(verbose, check_t0, f"Scoring fixed route: {len(metrics['legs'])} legs in {scoring_ms:.1f}ms")
        if log_interval and metrics["legs"]:
            for i, (u, v, c) in enumerate(metrics["legs"]):
                if log_interval and (i + 1) % log_interval == 0:
                    solver_log(
                        verbose,
                        check_t0,
                        f"Leg progress: {i + 1}/{len(metrics['legs'])} "
                        f"({u}->{v} cost={c:.4f})",
                    )
        solver_log(
            verbose,
            check_t0,
            f"Done: gross={metrics['gross_fuel']:.2f} net={metrics['net_fuel_solver_style']:.2f}",
        )

    return metrics, scoring_ms, ctx


def api_connection(base_url_cli, player_guid_cli, player_email_cli):
    base_url = (
        base_url_cli or os.getenv("STAR_DELIVERY_BASE_URL") or DEFAULT_BASE_URL
    ).rstrip("/")
    player_guid = player_guid_cli or os.getenv("PLAYER_GUID")
    player_email = player_email_cli or os.getenv("PLAYER_EMAIL")
    if not player_guid or not player_email:
        raise ValueError(
            "API calls need PlayerGuid and PlayerEmail "
            "(CLI flags or PLAYER_GUID / PLAYER_EMAIL)."
        )
    headers = {
        "Accept": "application/json",
        "PlayerGuid": player_guid,
        "PlayerEmail": player_email,
    }
    return base_url, headers


def normalize_coaxium_response(raw):
    o = raw if isinstance(raw, dict) else {}
    coax_raw = pick(o, "Coaxium", "coaxium")
    try:
        coaxium = int(float(coax_raw)) if coax_raw is not None else 0
    except (TypeError, ValueError):
        coaxium = 0
    return {
        "is_success": pick_bool(o, "IsSuccess", "isSuccess"),
        "feedback_message": str(pick(o, "FeedbackMessage", "feedbackMessage") or ""),
        "coaxium": coaxium,
    }


def post_calculate_coaxium(base_url, challenge_id, submission, headers):
    q = urlencode({"ChallengeId": str(challenge_id)})
    url = f"{base_url}/CalculateCoaxium?{q}"
    data = json.dumps(submission).encode("utf-8")
    h = dict(headers)
    h["Content-Type"] = "application/json; charset=utf-8"
    req = request.Request(url, data=data, headers=h, method="POST")
    try:
        with request.urlopen(req, timeout=120) as resp:
            text = resp.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"CalculateCoaxium HTTP {exc.code}: {detail[:500]}") from exc
    except URLError as exc:
        raise RuntimeError(f"CalculateCoaxium request failed: {exc}") from exc

    if not text.strip():
        return {
            "is_success": False,
            "feedback_message": "Empty response body.",
            "coaxium": 0,
        }
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("CalculateCoaxium response is not JSON") from exc
    return normalize_coaxium_response(parsed)


def load_route_payload(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("Route file must be a JSON object.")
    route = pick(raw, "route", "planetIds", "path", "Route", "PlanetIds")
    if route is None:
        raise ValueError(
            'Route file needs a "route" array (aliases: planetIds, path).'
        )
    if not isinstance(route, list):
        raise ValueError("route must be a JSON array of planet ids.")
    ids = [to_int(x) for x in route]
    cid = pick(raw, "challengeId", "ChallengeId")
    challenge_id = None
    if cid is not None and str(cid).strip() != "":
        try:
            challenge_id = int(cid)
        except (TypeError, ValueError):
            challenge_id = None
    cids_raw = pick(raw, "challengeIds", "ChallengeIds")
    challenge_ids_list = None
    if isinstance(cids_raw, list):
        challenge_ids_list = []
        for x in cids_raw:
            if x is None or (isinstance(x, str) and not x.strip()):
                challenge_ids_list.append(None)
                continue
            try:
                challenge_ids_list.append(int(x))
            except (TypeError, ValueError):
                challenge_ids_list.append(None)
    return ids, challenge_id, challenge_ids_list


def load_challenge_entries(paths):
    result = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        entries = raw if isinstance(raw, list) else [raw]
        base = os.path.basename(path)
        for i, obj in enumerate(entries):
            if not isinstance(obj, dict):
                raise ValueError(f"{path}: challenge entry {i} must be an object")
            ch = normalize_challenge(obj)
            name = ch.get("challengeName") or ""
            cid = ch.get("challengeId")
            if name and cid is not None:
                label = f"{name} (ChallengeId={cid})"
            elif name:
                label = name
            elif cid is not None:
                label = f"ChallengeId={cid}"
            else:
                label = f"{base}[{i}]"
            result.append(
                {
                    "label": label,
                    "source_path": path,
                    "index_in_file": i,
                    "challenge": ch,
                }
            )
    if len(result) > 1:
        for j, row in enumerate(result):
            row["label"] = f"{row['label']} [#{j + 1}]"
    return result


def resolve_api_challenge_id(idx, entry, challenge_count, route_legacy_cid, route_id_list, cli_cid):
    if challenge_count == 1 and cli_cid is not None:
        return cli_cid
    ch = entry["challenge"]
    cid = ch.get("challengeId")
    if cid is not None:
        return cid
    if route_id_list is not None and idx < len(route_id_list):
        alt = route_id_list[idx]
        if alt is not None:
            return alt
    if challenge_count == 1 and route_legacy_cid is not None:
        return route_legacy_cid
    return None


def parse_args():
    p = argparse.ArgumentParser(
        description="Score a fixed route (tsp_solver local model) and/or CalculateCoaxium."
    )
    p.add_argument(
        "data_file",
        nargs="?",
        default="data.json",
        help="Planet map JSON (same shape as tsp_solver data.json)",
    )
    p.add_argument(
        "route_file",
        nargs="?",
        default="route_check.json",
        help='JSON with {"route": [...]} (see docstring)',
    )
    p.add_argument(
        "--challenge-file",
        action="append",
        default=None,
        metavar="PATH",
        help="Challenge JSON (one object or array). Repeat for multiple.",
    )
    p.add_argument(
        "--challenge-id",
        type=int,
        default=None,
        help="ChallengeId for API when one challenge row; else use JSON / challengeIds.",
    )
    p.add_argument(
        "--show-legs",
        action="store_true",
        help="Print per-leg edge costs in stdout (default: on for 1 challenge).",
    )
    p.add_argument(
        "--compact",
        action="store_true",
        help="With multiple challenges: table only, no full tsp-style block per row.",
    )
    p.add_argument(
        "--api",
        action="store_true",
        help="Call OutSystems CalculateCoaxium.",
    )
    p.add_argument("--base-url", default="", help="Star Delivery REST base URL")
    p.add_argument("--player-guid", default="", help="PlayerGuid header")
    p.add_argument("--player-email", default="", help="PlayerEmail header")
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Progress log to stderr (tsp_solver-style). Or TSP_CHECK_VERBOSE=1.",
    )
    p.add_argument(
        "--log-interval",
        type=int,
        default=0,
        metavar="N",
        help="With -v: log every N legs (0 = no per-leg progress).",
    )
    return p.parse_args()


def main():
    args = parse_args()
    verbose = args.verbose or (
        os.getenv("TSP_CHECK_VERBOSE", "").strip().lower()
        in ("1", "true", "yes", "on")
    )
    log_interval = max(0, args.log_interval)
    check_t0 = time.monotonic()

    with open(args.data_file, encoding="utf-8") as f:
        data_raw = json.load(f)
    planets_list = [normalize_planet(x) for x in data_raw.get("planets", [])]
    routes_list = [normalize_route(x) for x in data_raw.get("routes", [])]
    planets = {p["id"]: p for p in planets_list}

    challenge_paths = args.challenge_file or []
    entries = load_challenge_entries(challenge_paths) if challenge_paths else []

    route_ids, route_file_cid, route_challenge_ids = load_route_payload(args.route_file)

    planet_keys = set(planets.keys())
    forbidden_violations = []
    for entry in entries:
        ctx = challenge_context(entry["challenge"], planet_keys)
        ft = validate_route(route_ids, ctx, planets)["forbidden_transit"]
        if ft:
            forbidden_violations.append(
                (entry["label"], entry["challenge"].get("challengeId"), ft)
            )

    if args.api and len(entries) > 1 and args.challenge_id is not None:
        print(
            "Note: with multiple challenges, --challenge-id is ignored; "
            'use each JSON ChallengeId or route "challengeIds".',
            file=sys.stderr,
        )

    show_legs_default = len(entries) <= 1
    show_legs = args.show_legs or show_legs_default

    if not entries:
        metrics, scoring_ms, ctx = run_scoring_with_verbose(
            planets, routes_list, route_ids, None, verbose, log_interval, check_t0
        )
        print_tsp_style_report(
            "",
            planets,
            route_ids,
            metrics,
            None,
            ctx,
            verbose,
            log_interval,
            check_t0,
            scoring_ms,
            route_file_challenge_id=route_file_cid,
        )
        if show_legs:
            print("\nPer-leg edge_cost:")
            for u, v, c in metrics["legs"]:
                print(f"    {u:4d} -> {v:4d}   {c:12.4f}")
    elif args.compact and len(entries) > 1:
        print("=" * 70)
        print("ROUTE CHECK (compact table)")
        print("=" * 70)
        print(f"Data: {args.data_file}  Route: {args.route_file}\n")
        print(
            f"{'Challenge':<34} {'ChallengeId':>12} {'Gross':>10} {'Bonus':>10} {'Net':>10}"
        )
        print("-" * 80)
        compact_rows = []
        for entry in entries:
            metrics, scoring_ms, _ = run_scoring_with_verbose(
                planets,
                routes_list,
                route_ids,
                entry["challenge"],
                verbose,
                log_interval,
                check_t0,
            )
            compact_rows.append((entry, metrics))
            _row_cid = entry["challenge"].get("challengeId")
            _cid_cell = str(_row_cid) if _row_cid is not None else "-"
            print(
                f"{entry['label']:<34} {_cid_cell:>12} "
                f"{metrics['gross_fuel']:>10.2f} "
                f"{metrics['bonus_subtotal']:>10.2f} "
                f"{metrics['net_fuel_solver_style']:>10.2f}"
            )
            solver_log(verbose, check_t0, f"{entry['label']}: scoring {scoring_ms:.1f}ms")
        if show_legs:
            for entry, metrics in compact_rows:
                _leg_cid = entry["challenge"].get("challengeId")
                _leg_cid_s = (
                    f"ChallengeId={_leg_cid}" if _leg_cid is not None else "ChallengeId=-"
                )
                print(f"\nPer-leg edge_cost - {entry['label']} ({_leg_cid_s})")
                for u, v, c in metrics["legs"]:
                    print(f"    {u:4d} -> {v:4d}   {c:12.4f}")
    else:
        first = True
        for entry in entries:
            metrics, scoring_ms, ctx = run_scoring_with_verbose(
                planets,
                routes_list,
                route_ids,
                entry["challenge"],
                verbose,
                log_interval,
                check_t0,
            )
            if not first:
                print()
            first = False
            print_tsp_style_report(
                entry["label"],
                planets,
                route_ids,
                metrics,
                entry["challenge"],
                ctx,
                verbose,
                log_interval,
                check_t0,
                scoring_ms,
                route_file_challenge_id=route_file_cid,
            )
            if show_legs:
                print("\nPer-leg edge_cost:")
                for u, v, c in metrics["legs"]:
                    print(f"    {u:4d} -> {v:4d}   {c:12.4f}")

    if forbidden_violations:
        print()
        print("=" * 70)
        print(
            "ROUTE INVALID: forbidden planet id(s) in sequence "
            "(must not appear in submitted PlanetId list)."
        )
        for label, fcid, ft in forbidden_violations:
            id_suffix = f"  ChallengeId={fcid}" if fcid is not None else ""
            print(f"  {label}{id_suffix}: {ft}")
        sys.exit(2)

    if args.api:
        print()
        print("=" * 70)
        print("OUTSYSTEMS: CalculateCoaxium")
        if not entries and route_file_cid is not None:
            print(f"  ChallengeId: {route_file_cid} (from route file or --challenge-id)")
        elif entries:
            print("  ChallengeId: (per challenge row below)")
        print("=" * 70)
        try:
            base_url, headers = api_connection(
                args.base_url, args.player_guid, args.player_email
            )
        except ValueError as exc:
            print(exc, file=sys.stderr)
            sys.exit(1)
        submission = [
            {"PlanetId": pid, "Name": planets[pid]["name"]} for pid in route_ids
        ]
        any_ok = False
        if not entries:
            cid = args.challenge_id if args.challenge_id is not None else route_file_cid
            if cid is None:
                print(
                    "Missing ChallengeId: use route JSON \"challengeId\" or --challenge-id.",
                    file=sys.stderr,
                )
                sys.exit(1)
            try:
                api_res = post_calculate_coaxium(base_url, cid, submission, headers)
            except RuntimeError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)
            print(f"  ChallengeId: {cid}")
            print(f"  Success:   {api_res['is_success']}")
            print(f"  Coaxium:   {api_res['coaxium']}")
            print(f"  Feedback:  {api_res['feedback_message']}")
            any_ok = True
        else:
            for idx, entry in enumerate(entries):
                cid = resolve_api_challenge_id(
                    idx,
                    entry,
                    len(entries),
                    route_file_cid,
                    route_challenge_ids,
                    args.challenge_id,
                )
                print(f"  [{entry['label']}]")
                if cid is None:
                    print(
                        "    Skipped: no ChallengeId (set in challenge JSON, "
                        'or route "challengeIds" aligned by index).',
                    )
                    continue
                try:
                    api_res = post_calculate_coaxium(
                        base_url, cid, submission, headers
                    )
                except RuntimeError as exc:
                    print(f"    Error: {exc}", file=sys.stderr)
                    continue
                print(f"    ChallengeId: {cid}")
                print(f"    Success:     {api_res['is_success']}")
                print(f"    Coaxium:     {api_res['coaxium']}")
                print(f"    Feedback:    {api_res['feedback_message']}")
                any_ok = True
            if not any_ok:
                sys.exit(1)
        print()
        print(
            "Note: OutSystems coaxium may differ from local net/gross "
            "(different rounding, scoring rules, or payload validation)."
        )


if __name__ == "__main__":
    main()
